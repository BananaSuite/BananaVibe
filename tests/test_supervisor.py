import json
import os
import signal
import subprocess
import sys
import time

from conftest import configure, prompts, run_supervisor

from bananavibe.state import Paths, State, set_control


def git_log(ws):
    return subprocess.run(["git", "-C", str(ws), "log", "--format=%s"], capture_output=True, text=True).stdout


def test_runs_until_reviewed_done(workspace):
    paths = configure(workspace, ["plan", "work:a.txt", "work:b.txt", "finish", "approve"])
    code, sup = run_supervisor(paths)
    assert code == 0
    st = State(paths)
    assert st["status"] == "done"
    assert st["iteration"] == 4
    assert (workspace / "a.txt").exists() and (workspace / "final.txt").exists()
    log = git_log(workspace)
    assert "bananavibe: session 4 by fake" in log
    assert subprocess.run(["git", "-C", str(workspace), "branch", "--show-current"],
                          capture_output=True, text=True).stdout.strip() == "bananavibe/work"
    journal = paths.journal.read_text()
    assert "## Session 1" in journal and "Created a.txt." in journal
    assert (paths.reports / "latest.md").exists()
    # .bananavibe/ never ends up in the project's history.
    files = subprocess.run(["git", "-C", str(workspace), "ls-files"], capture_output=True, text=True).stdout
    assert ".bananavibe" not in files


def test_premature_done_claim_is_rejected(workspace):
    paths = configure(workspace, ["plan", "claim", "work:a.txt", "work:b.txt", "finish", "approve"])
    code, _ = run_supervisor(paths)
    assert code == 0
    p = prompts(workspace)
    assert "Your DONE claim was rejected by the supervisor" in p[2]
    assert "create a.txt" in p[2]
    assert State(paths)["status"] == "done"


def test_reviewer_rejection_feeds_back(workspace):
    paths = configure(workspace, ["plan", "finish", "reject", "finish", "approve"])
    code, _ = run_supervisor(paths)
    assert code == 0
    p = prompts(workspace)
    assert "rejected" in p[3] and "lacks a newline" in p[3]
    assert list(paths.reports.glob("review-*-final.md"))


def test_two_approvals_needed(workspace):
    paths = configure(workspace, ["plan", "finish", "approve", "finish", "approve"],
                      extra="")
    text = paths.config.read_text().replace("done_approvals = 1", "done_approvals = 2")
    paths.config.write_text(text)
    code, _ = run_supervisor(paths)
    assert code == 0
    p = prompts(workspace)
    assert "approval 1 of 2" in p[3]
    assert State(paths)["approvals"] == 2


def test_limits_outages_and_logouts_are_retried(workspace):
    paths = configure(workspace, ["limit", "overload", "auth", "plan", "finish", "approve"])
    code, _ = run_supervisor(paths)
    assert code == 0
    st = State(paths)
    assert st["status"] == "done"
    assert st.agent("fake")["failures"] == 3
    kinds = [e["kind"] for e in st["events"]]
    assert "limit" in kinds and "transient" in kinds and "auth" in kinds


def test_work_from_a_cut_off_session_is_kept(workspace):
    paths = configure(workspace, ["partial-limit", "plan", "finish", "approve"])
    code, _ = run_supervisor(paths)
    assert code == 0
    assert "cut off: limit" in git_log(workspace)
    tracked = subprocess.run(["git", "-C", str(workspace), "ls-files"], capture_output=True, text=True).stdout
    assert "partial.txt" in tracked


def test_failover_to_another_agent(workspace):
    paths = configure(workspace, ["limit", "plan", "finish", "approve"], workers='["fake", "fake2"]')
    code, _ = run_supervisor(paths)
    assert code == 0
    st = State(paths)
    assert st.agent("fake")["failures"] == 1
    assert st.agent("fake2")["sessions"] >= 1


def test_stalling_agent_is_pushed_and_implicit_done_verified(workspace):
    paths = configure(workspace, ["plan", "work:a.txt", "work:b.txt", "noop", "noop", "approve"])
    code, _ = run_supervisor(paths)
    assert code == 0
    p = prompts(workspace)
    assert "ended without changing any file" in p[4]
    assert State(paths)["status"] == "done"


def test_hung_agent_is_killed_and_retried(workspace):
    paths = configure(workspace, ["hang", "plan", "finish", "approve"])
    started = time.monotonic()
    code, _ = run_supervisor(paths)
    assert code == 0
    assert time.monotonic() - started < 60
    assert State(paths).agent("fake")["failures"] == 1


def test_failing_checks_block_done(workspace):
    checks = '[[checks]]\nname = "bfile"\nrun = "test -f b.txt"\n'
    paths = configure(workspace, ["plan", "finish", "work:b.txt", "finish", "approve"], checks=checks)
    code, _ = run_supervisor(paths)
    assert code == 0
    p = prompts(workspace)
    assert "Check `bfile` failed" in p[2]
    assert "Your DONE claim was rejected" in p[2]
    assert State(paths)["last_checks"][0]["ok"]


def test_checkpoint_review_findings_reach_the_worker(workspace):
    paths = configure(workspace, ["plan", "work:a.txt", "reject", "work:b.txt", "finish", "approve"],
                      checkpoint_every=2)
    code, _ = run_supervisor(paths)
    assert code == 0
    p = prompts(workspace)
    assert "periodic checkpoint audit" in p[2]
    assert "Checkpoint review by fake" in p[3] and "lacks a newline" in p[3]


def test_operator_message_reaches_next_session(workspace):
    paths = configure(workspace, ["plan", "finish", "approve"])
    from bananavibe.cli import main
    main(["-w", str(workspace), "say", "Use tabs, not spaces."])
    code, _ = run_supervisor(paths)
    assert code == 0
    assert "Use tabs, not spaces." in prompts(workspace)[0]
    assert "Use tabs" not in prompts(workspace)[1]


def test_max_iterations_and_stop_flag(workspace):
    paths = configure(workspace, ["plan", "work:a.txt", "work:b.txt", "finish", "approve"],
                      extra="max_iterations = 2")
    code, _ = run_supervisor(paths)
    assert code == 0
    st = State(paths)
    assert st["status"] == "stopped" and st["iteration"] == 2
    set_control(paths, stop=True)
    code, _ = run_supervisor(paths)
    assert State(paths)["iteration"] == 2


def test_sigterm_interrupts_and_keeps_work(workspace):
    paths = configure(workspace, ["hang"])
    paths.config.write_text(paths.config.read_text().replace("idle_timeout_minutes = 0.1", "idle_timeout_minutes = 5"))
    proc = subprocess.Popen([sys.executable, "-m", "bananavibe", "-w", str(workspace), "run"],
                            cwd=os.path.dirname(os.path.dirname(__file__)),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        time.sleep(0.2)
        if (workspace / ".fake" / "prompt-000.md").exists():
            break
    (workspace / "wip.txt").write_text("work in progress\n")
    time.sleep(1)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=60) == 0
    st = State(Paths(workspace))
    assert st["status"] == "stopped"
    assert "wip.txt" in subprocess.run(["git", "-C", str(workspace), "ls-files"],
                                       capture_output=True, text=True).stdout
    status = subprocess.run([sys.executable, "-m", "bananavibe", "-w", str(workspace), "status", "--json"],
                            capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(__file__)))
    assert json.loads(status.stdout)["supervisor_running"] is False


def test_goal_edits_are_reverted(workspace):
    paths = configure(workspace, ["editgoal", "finish", "approve"])
    original = paths.goal.read_text()
    code, _ = run_supervisor(paths)
    assert code == 0
    assert paths.goal.read_text() == original
    assert "has been restored" in prompts(workspace)[1]


def test_pause_and_resume_from_another_process(workspace):
    paths = configure(workspace, ["plan", "finish", "approve"])
    set_control(paths, pause=False)
    root = os.path.dirname(os.path.dirname(__file__))
    cli = [sys.executable, "-m", "bananavibe", "-w", str(workspace)]
    subprocess.run([*cli, "pause"], cwd=root, check=True, capture_output=True)
    proc = subprocess.Popen([*cli, "run"], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # A pause left over from before the start is cleared, so the run proceeds to the end.
        assert proc.wait(timeout=60) == 0
        assert State(paths)["status"] == "done"
    finally:
        proc.kill()


def test_force_added_run_files_are_untracked(workspace):
    paths = configure(workspace, ["sneak", "finish", "approve"])
    code, _ = run_supervisor(paths)
    assert code == 0
    files = subprocess.run(["git", "-C", str(workspace), "ls-files"], capture_output=True, text=True).stdout
    assert ".bananavibe" not in files


def test_background_process_holding_output_does_not_look_like_a_hang(workspace):
    paths = configure(workspace, ["background-server", "finish", "approve"])
    paths.config.write_text(paths.config.read_text().replace("idle_timeout_minutes = 0.1", "idle_timeout_minutes = 1"))
    started = time.monotonic()
    code, _ = run_supervisor(paths)
    assert code == 0
    assert time.monotonic() - started < 50
    assert State(paths).agent("fake")["failures"] == 0


def test_verification_only_claim_is_not_a_stall_and_reviewer_sees_claim(workspace):
    paths = configure(workspace, ["plan", "work:a.txt", "work:b.txt", "verify-claim", "approve"])
    code, _ = run_supervisor(paths)
    assert code == 0
    assert State(paths)["stalls"] == 0
    assert "Verified everything" in (paths.reports / "done-claim-0004.md").read_text()
