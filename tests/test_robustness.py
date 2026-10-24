"""Building blocks that keep long runs safe: durable state, race-free control and messages, process cleanup,
terminal-safe output, log retention, git safety and error classification."""

import datetime as dt
import gzip
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from bananavibe import gitops, housekeeping
from bananavibe.adapters import LIMIT, OK, TRANSIENT, ClaudeParser, CodexParser, make_adapter, parse_retry_at
from bananavibe.config import AgentConfig
from bananavibe.runner import run_session
from bananavibe.state import (
    Paths,
    State,
    add_message,
    consume_messages,
    pending_messages,
    read_control,
    take_control,
)
from bananavibe.util import Console, clean, fit, fit_styled, read_tail, strip_styles, text_width, write_atomic


def test_clean_removes_escape_sequences_and_control_characters():
    raw = "\x1b[2J\x1b[31mred\x1b[0m \x1b]0;title\x07ok\x07 progress 10%\rprogress 100%\tend\x00\u202e"
    out = clean(raw)
    assert "\x1b" not in out and "\x07" not in out and "\x00" not in out and "\u202e" not in out
    assert out == "red okprogress 100%   end" or "progress 100%" in out
    assert "progress 10%" not in out
    assert clean("a\r\nb") == "a\nb"


def test_fit_counts_wide_characters():
    assert text_width("🔧 ok") == 5 and text_width("日本") == 4
    assert text_width(fit("🔧 abcdefgh", 6)) <= 6
    styled = "\x1b[32m" + "x" * 50 + "\x1b[0m"
    cut = fit_styled(styled, 10)
    assert text_width(strip_styles(cut)) <= 10 and cut.endswith("\x1b[0m")


def test_dst_is_respected_when_parsing_reset_times(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Rome")
    time.tzset()
    try:
        ref = dt.datetime(2026, 10, 23, 10, 0).astimezone()  # summer time (+02:00)
        t = parse_retry_at("try again at Nov 4th, 2026 8:39 PM", ref)
        assert (t.month, t.day, t.hour, t.minute) == (11, 4, 20, 39)  # winter time there: still 8:39 PM
        assert t.utcoffset() == dt.timedelta(hours=1)
        t = parse_retry_at("resets 3am (Europe/Rome)", dt.datetime(2026, 10, 24, 22, 0).astimezone())
        assert t.astimezone().hour == 3 and t.utcoffset() == dt.timedelta(hours=1)  # across the DST change
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_project_output_in_the_event_stream_is_not_taken_for_a_limit():
    p = ClaudeParser()
    p.feed(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content":
           "test_rate_limit: 429 Too Many Requests, try again in 2 days"}]}}))
    p.feed("Error: something crashed")
    out = p.finish(1)
    assert out.kind == TRANSIENT and out.retry_at is None
    c = CodexParser()
    c.feed(json.dumps({"type": "item.completed", "item": {"type": "command_execution",
                                                            "aggregated_output": "401 Unauthorized"}}))
    assert c.finish(1).kind == TRANSIENT


def test_claude_api_error_messages_are_classified():
    p = ClaudeParser()
    p.feed(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Claude AI usage limit reached|1891400000"}]}}))
    out = p.finish(1)
    assert out.kind == LIMIT and out.retry_at is not None


def test_unknown_event_shapes_never_break_a_session():
    p = ClaudeParser()
    assert p.feed(json.dumps({"type": "assistant", "message": {"content": "not a list of blocks"}})) == []
    assert p.feed(json.dumps({"type": "assistant", "message": {"content": [None, 5, "x"]}})) == []
    assert p.finish(0).kind == OK


def test_state_writes_are_atomic_and_backed_up(tmp_path):
    paths = Paths(tmp_path)
    st = State(paths)
    st["iteration"] = 7
    st.save(backup=True)
    assert json.loads(paths.state_backup.read_text())["iteration"] == 7
    paths.state.write_text("")
    st = State(paths)
    assert st["iteration"] == 7 and "unreadable" in st.load_error
    assert not list(tmp_path.glob(".bananavibe/.*.tmp"))


def test_write_atomic_keeps_permissions(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("a")
    os.chmod(f, 0o640)
    write_atomic(f, "b")
    assert f.read_text() == "b" and (f.stat().st_mode & 0o777) == 0o640


def test_control_updates_from_many_processes_are_never_lost(tmp_path):
    paths = Paths(tmp_path)
    paths.root.mkdir()
    code = (f"import sys; sys.path.insert(0, {str(Path.cwd())!r})\n"
            "from pathlib import Path\nfrom bananavibe.state import Paths, set_control\n"
            "p = Paths(Path(sys.argv[1]))\n"
            "for i in range(25): set_control(p, **{f'k{sys.argv[2]}_{i}': True})\n")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(n)]) for n in range(4)]
    for p in procs:
        assert p.wait(timeout=60) == 0
    assert len(read_control(paths)) == 100
    assert take_control(paths, "k0_0", "missing") == {"k0_0": True}
    assert "k0_0" not in read_control(paths)


def test_messages_from_concurrent_senders_are_all_delivered_once(tmp_path):
    paths = Paths(tmp_path)
    paths.root.mkdir()
    threads = [threading.Thread(target=add_message, args=(paths, f"message {i}")) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    msgs = pending_messages(paths)
    assert sorted(m.text for m in msgs) == sorted(f"message {i}" for i in range(20))
    consume_messages(paths, [m.id for m in msgs[:5]])
    assert len(pending_messages(paths)) == 15
    assert paths.inbox_archive.read_text().count("### ") == 5
    assert add_message(paths, "now!", urgent=True).urgent and pending_messages(paths)[-1].urgent


def test_read_tail_of_large_and_compressed_files(tmp_path):
    big = tmp_path / "big.log"
    big.write_text("".join(f"line {i}\n" for i in range(200_000)))
    tail_text = read_tail(big, 1000)
    assert tail_text.endswith("line 199999\n") and len(tail_text) <= 1000 and tail_text.startswith("line ")
    gz = tmp_path / "old.log.gz"
    with gzip.open(gz, "wt") as f:
        f.write("".join(f"entry {i}\n" for i in range(50_000)))
    assert read_tail(tmp_path / "old.log", 500).endswith("entry 49999\n")


def test_runner_never_leaves_the_agent_running(tmp_path):
    adapter = make_adapter(AgentConfig("x", "custom", ["sh", "-c", "sleep 300 # {prompt_file}"]))
    pf = tmp_path / "p.md"
    pf.write_text("hi")
    seen = {}

    def boom(pid: int) -> None:
        seen["pid"] = pid
        raise RuntimeError("bookkeeping failed")

    with pytest.raises(RuntimeError):
        run_session(adapter, "hi", pf, tmp_path, tmp_path / "s.log", Console(quiet=True), 60, 60,
                    threading.Event(), on_start=boom)
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(seen["pid"], 0)


def test_runner_kills_stubborn_background_processes(tmp_path):
    marker = tmp_path / "pid"
    script = (f"(trap '' TERM; sleep 300) & echo $! > {marker}; echo started # {{prompt_file}}")
    adapter = make_adapter(AgentConfig("x", "custom", ["sh", "-c", script]))
    pf = tmp_path / "p.md"
    pf.write_text("hi")
    res = run_session(adapter, "hi", pf, tmp_path, tmp_path / "s.log", Console(quiet=True), 60, 60,
                      threading.Event())
    assert res.outcome.kind == OK
    pid = int(marker.read_text())
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.skipif(not Path("/proc/self/oom_score_adj").exists(), reason="Linux only")
def test_agents_are_marked_for_the_oom_killer_and_get_a_clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
    adapter = make_adapter(AgentConfig("x", "custom", ["sh", "-c", "sleep 0.5; cat /proc/self/oom_score_adj; "
                                                       "echo socket=${NOTIFY_SOCKET:-none} # {prompt_file}"]))
    pf = tmp_path / "p.md"
    pf.write_text("hi")
    res = run_session(adapter, "hi", pf, tmp_path, tmp_path / "s.log", Console(quiet=True), 60, 60,
                      threading.Event(), oom_score_adj=500)
    assert "500" in res.outcome.final_text and "socket=none" in res.outcome.final_text


def test_console_survives_a_vanished_terminal(tmp_path):
    class Gone:
        def write(self, _):
            raise OSError(5, "Input/output error")

        def flush(self):
            pass

        def isatty(self):
            return False

    c = Console(tmp_path / "s.log", stream=Gone())
    c.info("first")
    c.warn("second")
    assert c.quiet and "second" in (tmp_path / "s.log").read_text()


def test_log_retention(tmp_path):
    paths = Paths(tmp_path)
    paths.logs.mkdir(parents=True)
    old = time.time() - 3600
    for i in range(30):
        p = paths.logs / f"{i:04d}-work-fake-20261001-0000{i:02d}.log"
        p.write_text("x" * 20000)
        os.utime(p, (old + i, old + i))
    (paths.logs / "0001-checks").mkdir()
    done = housekeeping.prune_logs(paths, compress_after=20, max_mb=0)
    assert done and len(list(paths.logs.glob("*.log.gz"))) == 10 and len(list(paths.logs.glob("*.log"))) == 20
    done = housekeeping.prune_logs(paths, compress_after=20, max_mb=0.3)
    assert any("deleted" in d for d in done)
    total = sum(p.stat().st_size for p in paths.logs.rglob("*") if p.is_file())
    assert total <= 0.3e6
    assert (paths.logs / "0029-work-fake-20261001-000029.log").exists()  # the newest are kept


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)
    return repo


def test_snapshots_skip_huge_files_and_report_problems(tmp_path):
    repo = _repo(tmp_path)
    (repo / "big.bin").write_bytes(b"0" * 3_000_000)
    (repo / "code.py").write_text("print(1)\n")
    res = gitops.commit_all(repo, "snap", max_file_mb=1)
    assert res.commit and res.skipped == ["big.bin"] and not res.error
    tracked = gitops.git(repo, "ls-files").stdout
    assert "code.py" in tracked and "big.bin" not in tracked
    (repo / ".git" / "MERGE_HEAD").write_text(gitops.head(repo))
    (repo / "more.py").write_text("x\n")
    res = gitops.commit_all(repo, "snap 2")
    assert res.commit is None and "merge" in res.error


def test_fingerprint_sees_edits_to_already_modified_files(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("1")
    first = gitops.fingerprint(repo)
    time.sleep(0.01)
    (repo / "a.txt").write_text("22")
    assert gitops.fingerprint(repo) != first


def test_git_helper_never_raises_on_timeouts(tmp_path):
    r = gitops.git(tmp_path, "status", timeout=0.000001)
    assert r.returncode != 0


def test_journal_rotation_keeps_recent_handoffs(tmp_path):
    paths = Paths(tmp_path)
    paths.root.mkdir()
    paths.journal.write_text("".join(f"\n## Session {i} · fake\n\n{'x' * 1000}\n" for i in range(3000)))
    assert housekeeping.rotate_journal(paths, max_bytes=1_000_000, keep_bytes=100_000)
    text = paths.journal.read_text()
    assert "## Session 2999" in text and "## Session 10 " not in text and len(text) < 150_000
    assert "## Session 10 " in (paths.root / "JOURNAL.archive.md").read_text()


def test_huge_files_never_enter_the_object_store(tmp_path):
    repo = _repo(tmp_path)
    (repo / "data.bin").write_bytes(os.urandom(3_000_000))
    (repo / "code.py").write_text("x = 1\n")
    for i in range(3):
        (repo / "data.bin").write_bytes(os.urandom(3_000_000))
        (repo / "code.py").write_text(f"x = {i}\n")
        res = gitops.commit_all(repo, f"snap {i}", max_file_mb=1)
        assert "data.bin" in res.skipped
    objects = sum(p.stat().st_size for p in (repo / ".git" / "objects").rglob("*") if p.is_file())
    assert objects < 1_000_000


def test_excluded_paths_are_never_committed_whether_ignored_or_not(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    gitops.git(repo, "init", "-q", check=True)
    (repo / ".bananavibe").mkdir()
    (repo / ".bananavibe" / "state.json").write_text("{}")
    (repo / "app.py").write_text("x = 1\n")
    res = gitops.commit_all(repo, "snap", exclude=(".bananavibe",))  # not ignored (e.g. info/exclude missing)
    assert res.commit and not res.error
    gitops.exclude_path(repo, "/.bananavibe/")
    (repo / "app.py").write_text("x = 2\n")
    res = gitops.commit_all(repo, "snap 2", exclude=(".bananavibe",))  # ignored as well: must not fail
    assert res.commit and not res.error, res.error
    files = gitops.git(repo, "ls-files").stdout
    assert "app.py" in files and ".bananavibe" not in files
