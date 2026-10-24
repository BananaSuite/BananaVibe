"""Never stopping: a killed or hung supervisor comes back, nothing in flight is lost (sessions, messages,
feedback, done claims), the agents can't change the protected files, and resources are guarded."""

import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from conftest import configure, prompts, run_supervisor

from bananavibe import guard, supervisor
from bananavibe.adapters import LIMIT, Outcome
from bananavibe.config import load
from bananavibe.runner import SessionResult
from bananavibe.state import Paths, State, add_message, pending_messages, read_control, set_control, supervisor_pid
from bananavibe.supervisor import Supervisor
from bananavibe.util import Console, now, process_start

ROOT = os.path.dirname(os.path.dirname(__file__))


def cli(ws: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "bananavibe", "-w", str(ws), *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=120, **kw)


def run_bg(ws: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "bananavibe", "-w", str(ws), "run"], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for(cond, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.2)
    raise AssertionError("timed out waiting")


def git(ws, *args):
    return subprocess.run(["git", "-C", str(ws), *args], capture_output=True, text=True).stdout


def slow_idle(paths: Paths) -> None:
    paths.config.write_text(paths.config.read_text().replace("idle_timeout_minutes = 0.1", "idle_timeout_minutes = 5"))


def test_killed_supervisor_is_restarted_and_continues_the_session(workspace):
    paths = configure(workspace, ["plan", "killsup", "finish", "approve"])
    slow_idle(paths)
    proc = run_bg(workspace)
    try:
        assert proc.wait(timeout=120) == 0
    finally:
        proc.kill()
    st = State(paths)
    assert st["status"] == "done"
    assert any(h.get("outcome") == "lost (supervisor died)" for h in st["history"])
    assert any(e["kind"] == "restart" and "SIGKILL" in e["msg"] for e in st["events"])
    p = prompts(workspace)
    assert "Continuing an interrupted session" in p[2] and "supervisor stopped unexpectedly" in p[2]
    assert "working..." in p[2]  # the cut-off session's last activity
    assert "cut off: supervisor restart" in git(workspace, "log", "--format=%s")
    assert "wip.txt" in git(workspace, "ls-files")


def test_orphaned_agent_of_a_dead_supervisor_is_stopped(workspace):
    paths = configure(workspace, ["plan"])
    sleeper = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        st = State(paths)
        st["current"] = {"label": "work", "agent": "fake", "log": "", "iteration": 1, "started": "x",
                         "pid": sleeper.pid, "pstart": process_start(sleeper.pid), "boot": supervisor.boot_id()}
        st.save()
        sup = Supervisor(paths, load(paths.config, paths.workspace), Console(quiet=True))
        sup._recover_lost_session()
        assert sleeper.wait(timeout=20) is not None
        assert sup.state["interrupted"]["reason"] == "the supervisor stopped unexpectedly"
    finally:
        sleeper.kill()


def test_recovery_never_kills_a_reused_pid(workspace):
    paths = configure(workspace, ["plan"])
    other = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        for marker in ("linux:0", ""):  # a different start time, or none recorded
            st = State(paths)
            st["current"] = {"label": "work", "agent": "fake", "log": "", "iteration": 1, "started": "x",
                             "pid": other.pid, "pstart": marker, "boot": supervisor.boot_id()}
            st.save()
            Supervisor(paths, load(paths.config, paths.workspace), Console(quiet=True))._recover_lost_session()
            time.sleep(0.3)
            assert other.poll() is None
        # After a reboot nothing from before can be running: nothing is killed either.
        st = State(paths)
        st["current"] = {"label": "work", "agent": "fake", "log": "", "pid": other.pid,
                         "pstart": process_start(other.pid), "boot": "another-boot"}
        st.save()
        Supervisor(paths, load(paths.config, paths.workspace), Console(quiet=True))._recover_lost_session()
        time.sleep(0.3)
        assert other.poll() is None
    finally:
        other.kill()


def _guardian(tmp_path: Path, script: str, watchdog: float = 60) -> tuple[guard.Guardian, Paths]:
    ws = tmp_path / "gws"
    (ws / ".bananavibe").mkdir(parents=True)
    paths = Paths(ws)
    paths.config.write_text('[run]\nafter_done = "exit"\n')  # a run lives here
    child = tmp_path / "child.py"
    child.write_text(script)
    g = guard.Guardian(paths, [sys.executable, str(child), str(ws)], Console(quiet=True), watchdog)
    return g, paths


def test_guardian_restarts_a_crashed_supervisor(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "BACKOFF", (0,))
    g, paths = _guardian(tmp_path, """
import sys
from pathlib import Path
runs = Path(sys.argv[1]) / "runs"
n = int(runs.read_text()) if runs.exists() else 0
runs.write_text(str(n + 1))
if n >= 2:  # finished: a supervisor that ends on purpose leaves the run done (or stopped by the operator)
    (Path(sys.argv[1]) / ".bananavibe" / "state.json").write_text('{"status": "done"}')
sys.exit(1 if n < 2 else 0)
""")
    assert g.run() == 0
    assert (paths.workspace / "runs").read_text() == "3"
    assert not (paths.root / "guardian.json").exists()


def test_guardian_does_not_restart_after_a_stop_request(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "BACKOFF", (0,))
    g, paths = _guardian(tmp_path, """
import sys
from pathlib import Path
runs = Path(sys.argv[1]) / "runs"
runs.write_text(str(int(runs.read_text()) + 1 if runs.exists() else 1))
sys.exit(1)
""")
    set_control(paths, stop=True)
    assert g.run() == 0
    assert (paths.workspace / "runs").read_text() == "1"
    assert State(paths)["operator_stop"] is True  # so autostart won't start it again either


def test_guardian_restarts_a_hung_supervisor(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "BACKOFF", (0,))
    g, paths = _guardian(tmp_path, """
import json, os, sys, time, datetime as dt
from pathlib import Path
ws = Path(sys.argv[1])
runs = ws / "runs"
n = int(runs.read_text()) if runs.exists() else 0
runs.write_text(str(n + 1))
if n:
    (ws / ".bananavibe" / "state.json").write_text('{"status": "done"}')
    sys.exit(0)
beat = {"at": dt.datetime.now().astimezone().isoformat(), "pid": os.getpid(), "main_age": 3600}
(ws / ".bananavibe" / "heartbeat.json").write_text(json.dumps(beat))
time.sleep(120)
""", watchdog=1)
    started = time.monotonic()
    assert g.run() == 0
    assert (paths.workspace / "runs").read_text() == "2"
    assert time.monotonic() - started < 60


def test_messages_are_kept_until_a_session_completes(workspace):
    paths = configure(workspace, ["limit", "overload", "plan", "finish", "approve"])
    add_message(paths, "Use tabs, not spaces.")
    code, _ = run_supervisor(paths)
    assert code == 0
    p = prompts(workspace)
    assert all("Use tabs, not spaces." in x for x in p[:3])  # every failed attempt and the successful one
    assert "Use tabs" not in p[3]
    assert pending_messages(paths) == []
    assert "Use tabs, not spaces." in paths.inbox_archive.read_text()


def test_legacy_inbox_file_is_still_delivered(workspace):
    paths = configure(workspace, ["plan", "finish", "approve"])
    paths.inbox.write_text("\n### 2026-10-01T10:00:00+02:00\nOld style message.\n")
    code, _ = run_supervisor(paths)
    assert code == 0
    assert "Old style message." in prompts(workspace)[0]
    assert not paths.inbox.exists()


def test_say_now_interrupts_the_session_and_continues_it(workspace):
    paths = configure(workspace, ["hang", "plan", "finish", "approve"])
    slow_idle(paths)
    proc = run_bg(workspace)
    try:
        wait_for(lambda: (State(paths)["current"] or {}).get("label") == "work")
        r = cli(workspace, "say", "--now", "Write the README first.")
        assert r.returncode == 0 and "Delivering now" in r.stdout
        assert proc.wait(timeout=90) == 0
    finally:
        proc.kill()
    p = prompts(workspace)
    assert "Write the README first." in p[1] and "operator message" in p[1]
    assert "Write the README first." not in p[2]
    st = State(paths)
    assert st["status"] == "done" and any(h["outcome"] == "interrupted" for h in st["history"])


def test_stop_during_the_final_review_keeps_the_claim(workspace):
    paths = configure(workspace, ["plan", "finish", "hang", "approve"])
    slow_idle(paths)
    proc = run_bg(workspace)
    try:
        wait_for(lambda: (State(paths)["current"] or {}).get("label") == "review-final")
        set_control(paths, stop_now=True)
        assert proc.wait(timeout=60) == 0
    finally:
        proc.kill()
    st = State(paths)
    assert st["status"] == "stopped" and st["pending"]["step"] == "verify" and st["operator_stop"]
    set_control(paths, stop=None, stop_now=None)  # what an explicit `bananavibe start` does
    code, _ = run_supervisor(paths)
    assert code == 0 and State(paths)["status"] == "done"
    p = prompts(workspace)
    assert len(p) == 4  # no new work session: the interrupted review was simply done again
    assert "independent, skeptical reviewer" in p[3] and "Continuing an interrupted session" in p[3]


def test_feedback_survives_a_supervisor_crash(workspace):
    checks = '[[checks]]\nname = "bfile"\nrun = "test -f b.txt"\n'
    paths = configure(workspace, ["plan", "killsup", "work:b.txt", "finish", "approve"], checks=checks)
    slow_idle(paths)
    proc = run_bg(workspace)
    try:
        assert proc.wait(timeout=120) == 0
    finally:
        proc.kill()
    p = prompts(workspace)
    assert "Check `bfile` failed" in p[1]
    assert "Check `bfile` failed" in p[2]  # the session after the crash still gets it
    assert State(paths)["status"] == "done"


def test_cut_off_session_with_an_id_is_continued(workspace, monkeypatch):
    monkeypatch.setattr(supervisor, "CONTINUE_AFTER", 0)
    paths = configure(workspace, ["sid-limit", "plan", "finish", "approve"])
    code, _ = run_supervisor(paths)
    assert code == 0
    p = prompts(workspace)
    assert "Continuing an interrupted session" in p[1] and "cut off (usage limit)" in p[1]
    assert "Continuing an interrupted session" not in p[2]


def test_limit_waits_are_rechecked(workspace):
    paths = configure(workspace, ["plan"], extra="")
    cfg = load(paths.config, paths.workspace)
    cfg.limit_recheck_seconds = 600
    sup = Supervisor(paths, cfg, Console(quiet=True))
    far = now() + dt.timedelta(days=3)
    sup._register_failure("fake", SessionResult(Outcome(LIMIT, "resets in 3 days", far), 1, 1))
    until = dt.datetime.fromisoformat(sup.state.agent("fake")["cooldown_until"])
    assert until <= now() + dt.timedelta(seconds=601)
    sup._register_failure("fake", SessionResult(Outcome(LIMIT, "x", far, retry_exact=True), 1, 1))
    until = dt.datetime.fromisoformat(sup.state.agent("fake")["cooldown_until"])
    assert until > now() + dt.timedelta(days=2)


def test_agent_edits_to_the_config_are_reverted(workspace):
    paths = configure(workspace, ["editconfig", "finish", "approve"])
    original = paths.config.read_text()
    code, _ = run_supervisor(paths)
    assert code == 0
    assert paths.config.read_text() == original
    assert list(paths.reverted.glob("config.toml.*"))
    assert "config.toml` was modified during the previous session" in prompts(workspace)[1]


def test_operator_edits_and_reload_requests_are_accepted(workspace):
    paths = configure(workspace, ["plan"])
    sup = Supervisor(paths, load(paths.config, paths.workspace), Console(quiet=True))
    sup.goal_text = paths.goal.read_text()
    sup.accepted = {"goal": sup.goal_text, "config": paths.config.read_text()}
    sup._save()  # as a running supervisor has
    # Between sessions, no agent runs: an edit is the operator's.
    paths.goal.write_text("# Mission\n\nA new mission.\n")
    paths.config.write_text(paths.config.read_text().replace("stall_limit = 2", "stall_limit = 4"))
    sup._accept_operator_edits()
    assert sup.cfg.stall_limit == 4 and "A new mission" in sup.goal_text
    # An invalid config edit is not applied (the previous configuration stays in force).
    paths.config.write_text(paths.config.read_text() + "\n[run]\nnot valid = = toml\n")
    sup._accept_operator_edits()
    assert sup.cfg.stall_limit == 4
    paths.config.write_text(sup.accepted["config"])
    # During a session the operator approves a change with `bananavibe reload`; it is kept after the session.
    paths.goal.write_text("# Mission\n\nApproved during the session.\n")
    assert cli(workspace, "reload").returncode == 2  # no terminal to confirm on: --yes is required
    assert cli(workspace, "reload", "--yes").returncode == 0
    sup._restore_protected()
    assert "Approved during the session" in paths.goal.read_text()
    assert not sup.feedback


def test_after_done_it_waits_for_new_instructions(workspace):
    paths = configure(workspace, ["plan", "finish", "approve", "work:c.txt", "finish", "approve"])
    paths.config.write_text(paths.config.read_text().replace('after_done = "exit"', 'after_done = "wait"'))
    proc = run_bg(workspace)
    try:
        wait_for(lambda: "waiting for new instructions" in State(paths)["detail"])
        assert State(paths)["status"] == "done" and proc.poll() is None
        assert cli(workspace, "say", "Also create c.txt.").returncode == 0
        wait_for(lambda: len(prompts(workspace)) >= 6 and "waiting for new" in State(paths)["detail"], 90)
        assert "Also create c.txt." in prompts(workspace)[3]
        assert cli(workspace, "stop").returncode == 0
        assert proc.wait(timeout=60) == 0
    finally:
        proc.kill()
    assert State(paths)["status"] == "done"


def test_budget_stop_is_not_restarted_automatically(workspace):
    paths = configure(workspace, ["plan", "work:a.txt"], extra="max_iterations = 1")
    code, _ = run_supervisor(paths)
    assert code == 0 and State(paths)["operator_stop"] is True
    assert cli(workspace, "start", "--auto").returncode == 0
    assert not supervisor_pid(paths)


def test_start_auto_restarts_a_run_that_should_be_running(workspace):
    paths = configure(workspace, ["hang"])
    slow_idle(paths)
    st = State(paths)
    st["status"] = "crashed"
    st.save()
    r = cli(workspace, "start", "--auto")
    assert r.returncode == 0 and "started the run" in r.stdout
    try:
        wait_for(lambda: bool(supervisor_pid(paths)))
        assert cli(workspace, "start", "--auto").stdout == ""  # already running: nothing to do, silently
    finally:
        cli(workspace, "stop", "--now", "--wait")
    assert State(paths)["operator_stop"] is True
    assert cli(workspace, "start", "--auto").stdout == ""
    assert not supervisor_pid(paths)


def test_service_mode_respects_an_operator_stop(workspace):
    paths = configure(workspace, ["plan"])
    st = State(paths)
    st["operator_stop"] = True
    st["status"] = "stopped"
    st.save()
    r = cli(workspace, "run", "--service")
    assert r.returncode == 0 and not supervisor_pid(paths)
    assert "stopped on purpose" in paths.supervisor_log.read_text()


def test_branch_switches_by_agents_are_undone(workspace):
    paths = configure(workspace, ["branch", "finish", "approve"])
    code, _ = run_supervisor(paths)
    assert code == 0
    assert git(workspace, "branch", "--show-current").strip() == "bananavibe/work"
    assert "left `ws` on `side`" in prompts(workspace)[1]
    assert "loose.txt" in git(workspace, "ls-files")  # the uncommitted work came along


def test_large_files_are_not_committed(workspace):
    paths = configure(workspace, ["bigfile", "finish", "approve"])
    paths.config.write_text(paths.config.read_text() + "\n[git]\nmax_file_mb = 0.01\n")
    code, _ = run_supervisor(paths)
    assert code == 0
    tracked = git(workspace, "ls-files")
    assert "small.txt" in tracked and "huge.bin" not in tracked
    assert "huge.bin" in prompts(workspace)[1] and ".gitignore" in prompts(workspace)[1]


def test_low_disk_space_waits_instead_of_failing(workspace):
    paths = configure(workspace, ["plan", "finish", "approve"])
    paths.config.write_text(paths.config.read_text().replace("min_free_disk_mb = 0", "min_free_disk_mb = 1e12"))
    proc = run_bg(workspace)
    try:
        wait_for(lambda: State(paths)["status"] == "waiting" and "disk space" in State(paths)["detail"])
        assert not (workspace / ".fake" / "prompt-000.md").exists()
        assert any(e["kind"] == "attention" for e in State(paths)["events"])
        assert cli(workspace, "retry-now").returncode == 0  # the operator says go anyway
        wait_for(lambda: State(paths)["status"] == "done" and proc.poll() is not None, 90)
    finally:
        proc.kill()


def test_unreadable_state_falls_back_to_the_backup(workspace):
    paths = configure(workspace, ["plan", "finish", "approve"], extra="max_iterations = 1")
    code, _ = run_supervisor(paths)
    assert code == 0
    State(paths).save(backup=True)
    paths.state.write_text("{ this is not json")
    st = State(paths)
    assert st.load_error and st["iteration"] == 1
    sup = Supervisor(paths, load(paths.config, paths.workspace), Console(quiet=True))
    sup._startup()
    assert list(paths.root.glob("state.json.corrupt-*"))
    assert json.loads(paths.state.read_text())["iteration"] == 1


def test_pending_pause_and_stop_survive_a_supervisor_restart(workspace):
    paths = configure(workspace, ["plan", "finish", "approve"])
    set_control(paths, pause=True)  # e.g. `pause` (or pause_at_checkpoint), then a crash before it applied
    sup = Supervisor(paths, load(paths.config, paths.workspace), Console(quiet=True))
    sup._startup()
    assert read_control(paths).get("pause") is True
    set_control(paths, pause=None, stop=True)
    code, _ = run_supervisor(paths)  # a restarted supervisor honours the pending stop at once
    st = State(paths)
    assert code == 0 and st["status"] == "stopped" and st["operator_stop"] and st["iteration"] == 0
    assert read_control(paths).get("stop")  # still recorded: autostart keeps respecting it
    assert cli(workspace, "start", "--auto").stdout == "" and not supervisor_pid(paths)


def test_agent_edits_in_a_failed_session_are_reverted(workspace):
    paths = configure(workspace, ["goal-then-limit", "plan", "finish", "approve"])
    goal, config = paths.goal.read_text(), paths.config.read_text()
    code, _ = run_supervisor(paths)
    assert code == 0
    assert paths.goal.read_text() == goal and paths.config.read_text() == config
    assert not (workspace / "PWNED").exists()
    assert "Nothing to do." not in prompts(workspace)[1]
    assert not any("edited by the operator" in e["msg"] for e in State(paths)["events"])


def test_protected_files_changed_in_a_crashed_session_are_restored(workspace):
    paths = configure(workspace, ["plan"])
    goal, config = paths.goal.read_text(), paths.config.read_text()
    st = State(paths)
    st["accepted"] = {"goal": goal, "config": config}
    st["current"] = {"label": "work", "agent": "fake", "log": ""}
    st.save()
    paths.goal.write_text("# Mission\n\nChanged by an agent that then crashed.\n")
    messages, restored = supervisor.recover_protected_files(paths)
    assert restored == {"goal"} and paths.goal.read_text() == goal
    # A version the operator approved (`reload`/`edit`) that the crashed supervisor had not applied yet stays.
    approved = "# Mission\n\nApproved with reload during the session.\n"
    paths.goal.write_text(approved)
    set_control(paths, reload={"goal": approved})
    assert supervisor.recover_protected_files(paths) == ([], set()) and paths.goal.read_text() == approved
    # With no session in flight, edits are the operator's. A config that no longer parses is put back only
    # by automatic restarts (a person starting the run by hand sees the error instead).
    st["current"] = None
    st.save()
    paths.goal.write_text("# Mission\n\nThe operator's new mission.\n")
    paths.config.write_text("this is = = broken")
    assert supervisor.recover_protected_files(paths) == ([], set())
    r = cli(workspace, "start")
    assert r.returncode == 2 and "config error" in r.stderr and paths.config.read_text() == "this is = = broken"
    messages, restored = supervisor.recover_protected_files(paths, repair_broken=True)
    assert restored == {"config"} and paths.config.read_text() == config
    assert "operator's new mission" in paths.goal.read_text()


def test_a_stale_interrupt_does_not_cut_the_next_session(workspace):
    paths = configure(workspace, ["plan", "finish", "approve"])
    sup = Supervisor(paths, load(paths.config, paths.workspace), Console(quiet=True))
    sup.interrupt.set()  # e.g. `say --now` that arrived as the previous session was ending
    assert sup.run() == 0
    assert not any(h["outcome"] == "interrupted" for h in State(paths)["history"])


def test_ctrl_c_is_an_operator_stop_but_sigterm_is_not(workspace):
    import signal
    for sig, expect_code, operator in ((signal.SIGINT, 0, True), (signal.SIGTERM, 75, False)):
        paths = configure(workspace, ["hang"])
        slow_idle(paths)
        set_control(paths, stop=None, stop_now=None)
        st = State(paths)
        st["operator_stop"] = False
        st.save()
        proc = run_bg(workspace)
        try:
            wait_for(lambda p=paths: (State(p)["current"] or {}).get("label") == "work")
            proc.send_signal(sig)
            assert proc.wait(timeout=60) == expect_code
        finally:
            proc.kill()
        assert bool(State(paths)["operator_stop"]) is operator


def test_import_refuses_repository_paths_outside_the_project(workspace, tmp_path):
    import io
    import tarfile
    paths = configure(workspace, ["plan"], extra="max_iterations = 1")
    run_supervisor(paths)
    archive = tmp_path / "run.tar.gz"
    assert cli(workspace, "export", "-o", str(archive)).returncode == 0
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(archive) as src, tarfile.open(evil, "w:gz") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read() if m.isfile() else None
            if m.name == "manifest.json":
                manifest = json.loads(data)
                manifest["repos"][0]["path"] = ".git/hooks"
                data = json.dumps(manifest).encode()
                m.size = len(data)
            dst.addfile(m, io.BytesIO(data) if data is not None else None)
    r = cli(tmp_path / "victim", "import", str(evil))
    assert r.returncode == 1 and "suspicious repository entry" in r.stderr


def test_export_after_a_supervisor_crash_keeps_the_cut_off_session(workspace, tmp_path):
    paths = configure(workspace, ["plan"], extra="max_iterations = 1")
    run_supervisor(paths)
    st = State(paths)
    st["current"] = {"label": "work", "agent": "fake", "log": st["history"][0]["log"], "iteration": 2,
                     "started": "2026-10-23T10:00:00+02:00", "session_id": ""}
    st.save()
    archive = tmp_path / "run.tar.gz"
    assert cli(workspace, "export", "-o", str(archive)).returncode == 0
    other = tmp_path / "moved"
    assert cli(other, "import", str(archive)).returncode == 0
    st = State(Paths(other))
    assert st["current"] is None and st["interrupted"]["reason"] == "the supervisor stopped unexpectedly"


def test_a_crash_inside_a_session_is_continued_after_restart(workspace, monkeypatch):
    import pytest
    paths = configure(workspace, ["plan", "finish", "approve"])
    real = supervisor.run_session

    def broken(*args, **kwargs):
        raise RuntimeError("bug in the supervisor")

    monkeypatch.setattr(supervisor, "run_session", broken)
    with pytest.raises(RuntimeError):
        run_supervisor(paths)
    st = State(paths)
    assert st["status"] == "crashed" and st["current"]["label"] == "work"
    monkeypatch.setattr(supervisor, "run_session", real)
    code, _ = run_supervisor(paths)
    assert code == 0 and State(paths)["status"] == "done"
    assert "Continuing an interrupted session" in prompts(workspace)[0]


def test_new_instructions_during_the_final_review_come_first(workspace):
    paths = configure(workspace, ["plan", "finish", "approve", "work:c.txt", "finish", "approve"])
    real = supervisor.Supervisor.run_review

    def review_and_message(self, mode, iteration, worker, claim_file=None):
        if not getattr(self, "_sent", False):
            self._sent = True
            add_message(self.paths, "Also create c.txt.")
        return real(self, mode, iteration, worker, claim_file)

    supervisor.Supervisor.run_review = review_and_message
    try:
        code, _ = run_supervisor(paths)
    finally:
        supervisor.Supervisor.run_review = real
    assert code == 0 and State(paths)["status"] == "done"
    p = prompts(workspace)
    assert len(p) == 6 and "Also create c.txt." in p[3] and "operator sent new instructions" in p[3]
    assert pending_messages(paths) == []


def test_a_repository_stuck_mid_rebase_does_not_stop_the_run(workspace):
    git_ = ["git", "-C", str(workspace), "-c", "user.name=t", "-c", "user.email=t@t"]
    (workspace / "f.txt").write_text("base\n")
    subprocess.run([*git_, "add", "f.txt"], check=True)
    subprocess.run([*git_, "commit", "-qm", "base"], check=True)
    subprocess.run([*git_, "switch", "-qc", "other"], check=True)
    (workspace / "f.txt").write_text("other\n")
    subprocess.run([*git_, "commit", "-qam", "other"], check=True)
    subprocess.run([*git_, "switch", "-q", "main"], check=True)
    (workspace / "f.txt").write_text("main\n")
    subprocess.run([*git_, "commit", "-qam", "main"], check=True)
    subprocess.run([*git_, "rebase", "other"], capture_output=True)  # conflicts: left in progress
    assert (workspace / ".git" / "rebase-merge").exists() or (workspace / ".git" / "rebase-apply").exists()
    paths = configure(workspace, ["plan", "finish", "approve"])
    code, _ = run_supervisor(paths)
    assert code == 0 and State(paths)["status"] == "done"
    assert "could not be switched to the work branch" in prompts(workspace)[0]
    assert "rebase is in progress" in prompts(workspace)[0]


def test_the_run_directory_survives_git_clean_and_is_restored_if_deleted(workspace):
    paths = configure(workspace, ["plan", "gitclean", "work:a.txt", "wipe", "work:b.txt", "finish", "approve"])
    code, _ = run_supervisor(paths)
    assert code == 0 and State(paths)["status"] == "done"
    p = prompts(workspace)
    assert "were deleted" not in p[2]  # `git clean -fdx` left it alone
    assert "were deleted during the previous session" in p[4]
    plan = paths.plan.read_text()
    assert "- [x] create a.txt" in plan and "- [x] create b.txt" in plan  # restored from the backup, then finished
    assert (paths.root / ".git").is_dir()
    assert any(e["kind"] == "attention" and "files of .bananavibe/ were deleted" in e["msg"] for e in State(paths)["events"])


def test_one_supervisor_per_workspace_whatever_happens_to_the_lock_files(workspace):
    import fcntl
    paths = configure(workspace, ["plan"])
    # Held on two files: inside the run directory (seen alike by every process, whatever its $HOME or
    # BANANAVIBE_STATE_DIR, and by older versions) and outside the workspace (survives the run directory being
    # deleted). Either one held means a supervisor is running.
    assert paths.lock.is_relative_to(paths.root) and not paths.lock_external.is_relative_to(paths.workspace)
    for held in (paths.lock, paths.lock_external):
        fd = os.open(held, os.O_RDWR | os.O_CREAT)
        os.write(fd, b"4242")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            assert supervisor_pid(paths) == 4242
            code, _ = run_supervisor(paths)
            assert code == 2
        finally:
            os.close(fd)
    assert supervisor_pid(paths) is None


def test_a_conversation_that_keeps_failing_is_restarted_fresh(workspace, monkeypatch):
    monkeypatch.setattr(supervisor, "CONTINUE_AFTER", 0)
    paths = configure(workspace, ["sid-crash", "sid-crash", "sid-crash", "plan", "finish", "approve"])
    cfg = load(paths.config, paths.workspace)
    sup = Supervisor(paths, cfg, Console(quiet=True))
    sup.adapters["fake"].supports_resume = True
    assert sup.run() == 0
    p = prompts(workspace)
    assert "same conversation, resumed" in p[1] and "same conversation, resumed" in p[2]
    assert "same conversation, resumed" not in p[3]  # two failed resumes: a fresh conversation
    assert "Continuing an interrupted session" in p[3]


def test_a_crash_after_a_session_resumes_its_bookkeeping_not_the_session(workspace, monkeypatch):
    import pytest
    paths = configure(workspace, ["plan", "finish", "approve"])

    def boom(self):
        raise RuntimeError("bug after the session")

    monkeypatch.setattr(supervisor.Supervisor, "_enforce_branches", boom)
    with pytest.raises(RuntimeError):
        run_supervisor(paths)
    st = State(paths)
    assert st["iteration"] == 1 and st["pending"]["step"] == "post"
    assert "## Session 1" in paths.journal.read_text()
    monkeypatch.undo()
    code, _ = run_supervisor(paths)
    assert code == 0 and State(paths)["status"] == "done"
    assert len(prompts(workspace)) == 3  # the plan session was not run again
    assert paths.journal.read_text().count("## Session 1 ") == 1


def test_disk_full_waits_quietly_and_still_obeys_a_stop(workspace):
    import threading as th
    paths = configure(workspace, ["plan"])
    sup = Supervisor(paths, load(paths.config, paths.workspace), Console(quiet=True))
    set_control(paths, pause=True)  # a pending pause must not turn the wait into a busy loop
    outcome = {}

    def wait():
        try:
            sup._disk_full(OSError(28, "No space left on device"))
            outcome["r"] = "returned"
        except supervisor.StopRun as e:
            outcome["r"] = f"stop: {e}"
    t = th.Thread(target=wait)
    t.start()
    t.join(3)
    assert t.is_alive()  # still waiting, not spinning back
    set_control(paths, stop=True)
    t.join(10)
    assert outcome["r"] == "stop: stop requested by operator"


def test_ctrl_c_to_the_guardian_between_restarts_is_a_stop(tmp_path, monkeypatch):
    import signal
    import threading as th
    monkeypatch.setattr(guard, "BACKOFF", (30,))
    g, paths = _guardian(tmp_path, "import sys; sys.exit(1)\n")
    th.Timer(2, lambda: g._on_signal(signal.SIGINT, None)).start()
    started = time.monotonic()
    assert g.run() == 0 and time.monotonic() - started < 20
    assert read_control(paths).get("stop") is True and State(paths)["operator_stop"] is True


def test_start_repairs_a_config_broken_in_a_crashed_session(workspace):
    paths = configure(workspace, ["hang"])
    slow_idle(paths)
    good = paths.config.read_text()
    st = State(paths)
    st["accepted"] = {"goal": paths.goal.read_text(), "config": good}
    st["current"] = {"label": "work", "agent": "fake", "log": ""}
    st["status"] = "crashed"
    st.save()
    paths.config.write_text("broken = = =")
    r = cli(workspace, "start", "--auto")
    try:
        assert r.returncode == 0 and paths.config.read_text() == good
        wait_for(lambda: bool(supervisor_pid(paths)))
    finally:
        cli(workspace, "stop", "--now", "--wait")


def test_reload_approves_only_what_changed(workspace):
    paths = configure(workspace, ["plan"])
    st = State(paths)
    st["accepted"] = {"goal": paths.goal.read_text(), "config": paths.config.read_text()}
    st.save()
    paths.goal.write_text("# Mission\n\nNew.\n")
    r = cli(workspace, "reload", "--yes")
    assert r.returncode == 0 and "GOAL.md: +" in r.stdout and "+New." in r.stdout  # the diff is shown
    request = read_control(paths)["reload"]
    assert "goal" in request and "config" not in request
    assert cli(workspace, "reload", "nonsense").returncode == 2


def test_messages_survive_the_run_directory_being_deleted(workspace):
    import shutil
    paths = configure(workspace, ["plan"])
    add_message(paths, "Do not lose me.")
    shutil.rmtree(paths.inbox_dir)
    from bananavibe import housekeeping
    assert any("message" in r for r in housekeeping.restore_memory(paths))
    assert [m.text for m in pending_messages(paths)] == ["Do not lose me."]
    supervisor.consume_messages(paths, [m.id for m in pending_messages(paths)])
    shutil.rmtree(paths.inbox_dir)
    assert housekeeping.restore_memory(paths) == [] or all("message" not in r for r in housekeeping.restore_memory(paths))


def test_edit_starts_from_the_approved_version(workspace, tmp_path):
    paths = configure(workspace, ["plan"])
    approved = paths.goal.read_text()
    st = State(paths)
    st["accepted"] = {"goal": approved, "config": paths.config.read_text()}
    st["current"] = {"label": "work", "agent": "fake", "log": ""}  # a session is running
    st.save()
    paths.goal.write_text(approved + "\nTAMPER by an agent\n")  # mid-session edit by an agent
    editor = tmp_path / "editor.sh"
    editor.write_text('#!/bin/sh\necho "Operator note." >> "$1"\n')
    editor.chmod(0o755)
    r = cli(workspace, "edit", "goal", env={**os.environ, "EDITOR": str(editor)})
    assert r.returncode == 0 and "approved version" in r.stdout
    text = paths.goal.read_text()
    assert "Operator note." in text and "TAMPER" not in text
    assert "TAMPER" not in read_control(paths)["reload"]["goal"]


def test_edit_while_stopped_keeps_the_operators_own_changes(workspace, tmp_path):
    paths = configure(workspace, ["plan"])
    approved = paths.goal.read_text()
    st = State(paths)
    st["accepted"] = {"goal": approved, "config": paths.config.read_text()}
    st.save()
    paths.goal.write_text(approved + "\nMy own change, made in my editor while the run was stopped.\n")
    editor = tmp_path / "editor.sh"
    editor.write_text('#!/bin/sh\necho "Operator note." >> "$1"\n')
    editor.chmod(0o755)
    r = cli(workspace, "edit", "goal", env={**os.environ, "EDITOR": str(editor)})
    assert r.returncode == 0 and "approved version" not in r.stdout
    assert "My own change" in paths.goal.read_text() and "Operator note." in paths.goal.read_text()


def test_successive_edits_while_stopped_build_on_each_other(workspace, tmp_path):
    paths = configure(workspace, ["plan"])
    st = State(paths)
    st["accepted"] = {"goal": paths.goal.read_text(), "config": paths.config.read_text()}
    st.save()
    editor = tmp_path / "editor.sh"
    for value in ("7", "2"):  # back to the value approved before: must not look like "no change"
        editor.write_text(f"#!/bin/sh\nsed -i 's/stall_limit = [0-9]*/stall_limit = {value}/' \"$1\"\n")
        editor.chmod(0o755)
        r = cli(workspace, "edit", "config", env={**os.environ, "EDITOR": str(editor)})
        assert r.returncode == 0, r.stdout + r.stderr
    goal_editor = tmp_path / "goal.sh"
    goal_editor.write_text('#!/bin/sh\necho "More." >> "$1"\n')
    goal_editor.chmod(0o755)
    assert cli(workspace, "edit", "goal", env={**os.environ, "EDITOR": str(goal_editor)}).returncode == 0
    request = read_control(paths)["reload"]
    assert "stall_limit = 2" in request["config"] and "More." in request["goal"]  # both approvals kept
    sup = Supervisor(paths, load(paths.config, paths.workspace), Console(quiet=True))
    sup._startup()
    assert sup.cfg.stall_limit == 2 and "stall_limit = 2" in paths.config.read_text()
