"""Background runs, pause/stop now with continuation, ask, sessions and moving a run between machines."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from conftest import configure, prompts, run_supervisor

from bananavibe import watch
from bananavibe.cli import main
from bananavibe.config import load
from bananavibe.state import Paths, State, set_control, supervisor_pid

ROOT = os.path.dirname(os.path.dirname(__file__))


def cli(ws: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "bananavibe", "-w", str(ws), *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=120, **kw)


def wait_for(cond, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.2)
    raise AssertionError("timed out waiting")


def git_log(ws):
    return subprocess.run(["git", "-C", str(ws), "log", "--format=%s"], capture_output=True, text=True).stdout


def slow_idle(paths: Paths) -> None:
    paths.config.write_text(paths.config.read_text().replace("idle_timeout_minutes = 0.1", "idle_timeout_minutes = 5"))


def test_pause_now_interrupts_and_resume_continues_the_session(workspace):
    paths = configure(workspace, ["hang", "plan", "finish", "approve"])
    slow_idle(paths)
    proc = subprocess.Popen([sys.executable, "-m", "bananavibe", "-w", str(workspace), "run"], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(lambda: (workspace / ".fake" / "prompt-000.md").exists())
        (workspace / "wip.txt").write_text("half way\n")
        wait_for(lambda: (State(paths)["current"] or {}).get("label") == "work")
        assert cli(workspace, "pause", "--now").returncode == 0
        wait_for(lambda: State(paths)["status"] == "paused")
        st = State(paths)
        assert st["interrupted"]["label"] == "work" and st["interrupted"]["reason"] == "pause now"
        assert "cut off: interrupted" in git_log(workspace)
        assert "interrupted by the operator" in paths.journal.read_text()
        assert st["history"][-1]["outcome"] == "interrupted"
        assert not (workspace / ".fake" / "prompt-001.md").exists()  # really paused
        assert cli(workspace, "resume").returncode == 0
        assert proc.wait(timeout=90) == 0
    finally:
        proc.kill()
    p = prompts(workspace)
    assert "Continuing an interrupted session" in p[1]
    assert "starting..." in p[1]  # the interrupted session's last activity
    assert "Continuing an interrupted session" not in p[2]
    st = State(paths)
    assert st["status"] == "done" and st["interrupted"] is None


def test_background_start_and_stop_now_then_continue_after_restart(workspace):
    paths = configure(workspace, ["hang", "plan", "finish", "approve"])
    slow_idle(paths)
    r = cli(workspace, "start")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "background" in r.stdout
    try:
        wait_for(lambda: (workspace / ".fake" / "prompt-000.md").exists())
        assert supervisor_pid(paths)
        assert "Supervisor: pid" in cli(workspace, "status").stdout
        # Via the control file only (as from another shell or machine that can't signal the process).
        set_control(paths, stop_now=True)
        wait_for(lambda: not supervisor_pid(paths))
        st = State(paths)
        assert st["status"] == "stopped" and st["interrupted"]["reason"] == "stop now"
        assert "Continuing: interrupted work session" in cli(workspace, "status").stdout

        r = cli(workspace, "start")
        assert r.returncode == 0, r.stdout + r.stderr
        wait_for(lambda: State(paths)["status"] == "done" and not supervisor_pid(paths), 90)
    finally:
        pid = supervisor_pid(paths)
        if pid and pid > 0:
            os.kill(pid, 9)
    assert "Continuing an interrupted session" in prompts(workspace)[1]
    assert paths.supervisor_out.exists()


def test_stop_now_with_wait(workspace):
    paths = configure(workspace, ["hang"])
    slow_idle(paths)
    assert cli(workspace, "start").returncode == 0
    wait_for(lambda: (workspace / ".fake" / "prompt-000.md").exists())
    r = cli(workspace, "stop", "--now", "--wait")
    assert r.returncode == 0 and "Stopped." in r.stdout
    assert State(paths)["status"] == "stopped"


def test_ask_while_done_keeps_history(workspace, capsys):
    paths = configure(workspace, ["plan", "finish", "approve"])
    code, _ = run_supervisor(paths)
    assert code == 0
    assert main(["-w", str(workspace), "ask", "-q", "is it done?"]) == 0
    assert "the run status is done" in capsys.readouterr().out
    assert main(["-w", str(workspace), "ask", "-q", "and what changed?"]) == 0
    asks = sorted((workspace / ".fake").glob("ask-*.md"))
    assert len(asks) == 2
    second = asks[1].read_text()
    assert "is it done?" in second and "Earlier questions" in second  # follow-ups see earlier answers
    assert "# Plan" in second and "create a.txt" in second
    capsys.readouterr()
    assert main(["-w", str(workspace), "ask", "--history"]) == 0
    assert "and what changed?" in capsys.readouterr().out
    # The supervisor's own session sequence was not disturbed.
    assert len(prompts(workspace)) == 3


def test_ask_while_running(workspace):
    paths = configure(workspace, ["hang", "plan", "finish", "approve"])
    slow_idle(paths)
    proc = subprocess.Popen([sys.executable, "-m", "bananavibe", "-w", str(workspace), "run"], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(lambda: (State(paths)["current"] or {}).get("label") == "work")
        r = cli(workspace, "ask", "-q", "what is it doing?")
        assert r.returncode == 0 and "the run status is running" in r.stdout
        prompt = next((workspace / ".fake").glob("ask-*.md")).read_text()
        assert "In progress:" in prompt and "starting..." in prompt
    finally:
        proc.send_signal(15)
        proc.wait(timeout=60)


def test_sessions_show_and_live_view(workspace, capsys):
    paths = configure(workspace, ["plan", "finish", "approve"])
    run_supervisor(paths)
    capsys.readouterr()
    assert main(["-w", str(workspace), "sessions"]) == 0
    out = capsys.readouterr().out
    assert "work" in out and "review-final" in out
    assert len(State(paths)["history"]) == 3
    assert main(["-w", str(workspace), "show", "1"]) == 0
    assert "# argv:" in capsys.readouterr().out
    # Custom agents report no session id, so there is nothing to open.
    assert main(["-w", str(workspace), "open", "latest"]) == 1
    cfg = load(paths.config, paths.workspace)
    assert watch.follow(paths, cfg, once=True) == 0
    assert "review-final" in capsys.readouterr().out


def test_export_and_import_on_another_machine(workspace, tmp_path):
    paths = configure(workspace, ["plan", "work:a.txt", "work:b.txt", "finish", "approve"],
                      extra="max_iterations = 2")
    code, _ = run_supervisor(paths)
    assert code == 0 and State(paths)["iteration"] == 2
    (workspace / "uncommitted.txt").write_text("left behind\n")
    archive = tmp_path / "run.tar.gz"
    r = cli(workspace, "export", "-o", str(archive))
    assert r.returncode == 0, r.stderr
    assert "work in progress at export" in git_log(workspace)

    other = tmp_path / "elsewhere" / "ws"
    r = cli(other, "import", str(archive))
    assert r.returncode == 0, r.stderr
    assert (other / "a.txt").exists() and (other / "uncommitted.txt").exists()
    assert subprocess.run(["git", "-C", str(other), "branch", "--show-current"], capture_output=True,
                          text=True).stdout.strip() == "bananavibe/work"
    moved = Paths(other)
    st = State(moved)
    assert list(st["base_commits"]) == [str(other)]
    assert st["iteration"] == 2 and st["events"][-1]["kind"] == "imported"
    assert "/.bananavibe/" in (other / ".git" / "info" / "exclude").read_text()
    # A second import over the same run needs --force.
    assert cli(other, "import", str(archive)).returncode == 1

    # Continue there (the fake agent's script lives in the workspace, so point it at the new one).
    (other / ".fake").mkdir()
    with (other / ".git" / "info" / "exclude").open("a") as f:
        f.write("/.fake/\n")
    configure(other, ["work:b.txt", "finish", "approve"])
    code, _ = run_supervisor(moved)
    assert code == 0
    assert State(moved)["status"] == "done" and State(moved)["iteration"] == 4
    assert (other / "b.txt").exists()
    report = (moved.reports / "latest.md").read_text()
    assert "commits since" in report


def test_import_refuses_to_overwrite_diverged_work(workspace, tmp_path):
    paths = configure(workspace, ["plan"], extra="max_iterations = 1")
    run_supervisor(paths)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    (workspace / "x.txt").write_text("x\n")
    subprocess.run([*git, "-C", str(workspace), "add", "-A"], check=True)
    subprocess.run([*git, "-C", str(workspace), "commit", "-q", "-m", "x"], check=True)
    archive = tmp_path / "run.tar.gz"
    assert cli(workspace, "export", "-o", str(archive), "--no-logs").returncode == 0
    exported = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout.strip()

    other = tmp_path / "b"
    assert cli(other, "import", str(archive)).returncode == 0
    subprocess.run(["git", "-C", str(other), "reset", "-q", "--hard", "HEAD~1"], check=True)
    (other / "y.txt").write_text("y\n")
    subprocess.run([*git, "-C", str(other), "add", "-A"], check=True)
    subprocess.run([*git, "-C", str(other), "commit", "-q", "-m", "diverged"], check=True)
    shutil.rmtree(other / ".bananavibe")
    r = cli(other, "import", str(archive))
    assert r.returncode == 1 and "has commits the export doesn't" in r.stderr
    r = cli(other, "import", str(archive), "--force")
    assert r.returncode == 0, r.stderr
    assert subprocess.run(["git", "-C", str(other), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip() == exported
    assert json.loads(cli(other, "status", "--json").stdout)["iteration"] == 1


def test_continuation_resumes_natively_only_on_the_same_host(workspace):
    paths = configure(workspace, ["plan"])
    from bananavibe.supervisor import Supervisor
    from bananavibe.util import Console
    sup = Supervisor(paths, load(paths.config, paths.workspace), Console(quiet=True))
    sup.adapters["fake"].supports_resume = True
    sup.state["interrupted"] = {"label": "work", "agent": "fake", "host": sup.host, "session_id": "abc",
                                "activity": ["🔧 Bash $ make"], "reason": "pause now", "started": "x"}
    note, resume = sup._continuation("work", "fake")
    assert resume == "abc" and "same conversation, resumed" in note and "make" in note
    assert sup._continuation("work", "fake2")[1] is None  # another agent can't resume it
    sup.state["interrupted"]["host"] = "elsewhere"
    note, resume = sup._continuation("work", "fake")
    assert resume is None and "Continuing an interrupted session" in note
    assert sup._continuation("review-final", "fake") == ("", None)
    assert sup.state["interrupted"] is None
