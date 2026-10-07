"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from bananavibe import __version__, gitops, report, templates
from bananavibe.adapters import OK, make_adapter
from bananavibe.config import KNOWN_TYPES, ConfigError, load
from bananavibe.runner import run_session
from bananavibe.state import (
    Paths,
    State,
    append_inbox,
    plan_stats,
    read_control,
    set_control,
    supervisor_pid,
)
from bananavibe.supervisor import Supervisor
from bananavibe.util import Console, human_duration, now, parse_iso, read_text, usage_text, write_atomic


def _paths(args) -> Paths:
    return Paths(Path(args.workspace).expanduser())


def _load(paths: Paths):
    try:
        return load(paths.config, paths.workspace)
    except ConfigError as e:
        print(f"bananavibe: config error: {e}", file=sys.stderr)
        sys.exit(2)


def _session_name(name: str) -> str:
    return "bananavibe-" + re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")


# ------------------------------------------------------------------ init

def cmd_init(args) -> int:
    paths = _paths(args)
    if not paths.workspace.is_dir():
        print(f"bananavibe: {paths.workspace} is not a directory", file=sys.stderr)
        return 2
    if paths.config.exists() and not args.force:
        print(f"bananavibe: {paths.config} already exists (use --force to overwrite the config)", file=sys.stderr)
        return 2
    paths.root.mkdir(exist_ok=True)
    workers = [w.strip() for w in args.workers.split(",") if w.strip()]
    for w in [*workers, args.reviewer]:
        if w and w not in KNOWN_TYPES:
            print(f"bananavibe: unknown agent '{w}' (built-in: claude, codex, opencode); "
                  "define custom agents in the config afterwards", file=sys.stderr)
            return 2
    checks = ""
    for spec in args.check or []:
        name, _, run = spec.partition("=")
        if not run:
            name, run = f"check-{len(checks.split('[[checks]]'))}", spec
        checks += templates.CHECK.format(name=json.dumps(name.strip()), run=json.dumps(run.strip()))
    reviewer = args.reviewer or (next((t for t in ("codex", "claude") if t not in workers[:1]), workers[0]))
    write_atomic(paths.config, templates.CONFIG.format(
        name=args.name or paths.workspace.name, workers=json.dumps(workers), reviewer=reviewer,
        checks=checks or templates.NO_CHECKS,
        claude_model=args.claude_model, claude_effort=args.claude_effort,
        codex_model=args.codex_model, codex_effort=args.codex_effort, opencode_model=args.opencode_model,
    ))
    goal = args.goal or ""
    if args.goal_file:
        goal = Path(args.goal_file).expanduser().read_text(encoding="utf-8")
    if goal or not paths.goal.exists():
        if goal.lstrip().startswith("#"):
            write_atomic(paths.goal, goal.rstrip() + "\n")
        else:
            write_atomic(paths.goal, templates.GOAL.format(goal=goal.strip() or "<!-- Describe what the agents should achieve. -->"))
    for path, text in ((paths.plan, templates.PLAN), (paths.notes, templates.NOTES)):
        if not path.exists():
            write_atomic(path, text)
    repos = gitops.discover(paths.workspace, [])
    if not repos:
        if args.git_init:
            gitops.init_repo(paths.workspace)
            gitops.exclude_path(paths.workspace, "/.bananavibe/")
            print(f"Initialized a git repository in {paths.workspace} so every session can be snapshotted.")
        else:
            print("Note: no git repository found; snapshots need git. Re-run with --git-init or run `git init`.")
    elif gitops.is_repo(paths.workspace):
        gitops.exclude_path(paths.workspace, "/.bananavibe/")
    print(f"Created {paths.rel(paths.config)} and {paths.rel(paths.goal)} in {paths.workspace}.")
    print("Next: describe the mission in GOAL.md, add [[checks]] to config.toml, run `bananavibe doctor`, "
          "then `bananavibe start`.")
    return 0


# ------------------------------------------------------------------ run

def cmd_run(args) -> int:
    paths = _paths(args)
    cfg = _load(paths)
    console = Console(paths.supervisor_log)
    paths.logs.mkdir(parents=True, exist_ok=True)
    state = State(paths)
    if state["status"] == "done" and not args.again and not read_text(paths.inbox).strip():
        print("This run is already complete. Give new instructions with `bananavibe say \"...\"` "
              "(then run again), or use `bananavibe run --again` to re-verify.")
        return 0
    if state["status"] == "done":
        state["status"] = "running"
        state["approvals"] = 0
        state.save()
    restarts = 0
    while True:
        try:
            return Supervisor(paths, cfg, console).run()
        except KeyboardInterrupt:
            return 130
        except Exception:  # the supervisor must survive its own bugs, too
            restarts += 1
            tb = traceback.format_exc()
            console.warn(f"supervisor crashed (restart {restarts}):\n{tb}")
            if not args.keep_alive:
                return 1
            delay = min(60 * restarts, 900)
            console.warn(f"restarting the supervisor in {delay}s")
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                return 130
            cfg = _load(paths)


def cmd_start(args) -> int:
    paths = _paths(args)
    cfg = _load(paths)
    if not shutil.which("tmux"):
        print("bananavibe: tmux is not installed (apt install tmux), or use `bananavibe run` directly.",
              file=sys.stderr)
        return 2
    if supervisor_pid(paths):
        print("A supervisor is already running in this workspace. `bananavibe attach` to watch it.")
        return 1
    session = _session_name(cfg.name)
    if subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode == 0:
        print(f"tmux session {session} already exists: `tmux attach -t {session}` (or kill it first).")
        return 1
    run = [sys.executable, "-P", "-m", "bananavibe", "-w", str(paths.workspace), "run"] + (["--again"] if args.again else [])
    shell = (f"cd {shlex.quote(str(paths.workspace))} && {shlex.join(run)}; "
             "echo; echo '[bananavibe] supervisor exited. Press Enter to close this window.'; read _")
    # Carry PATH (agent CLIs) and the package location (when running from a source checkout) into tmux.
    source_root = str(Path(__file__).resolve().parent.parent)
    pythonpath = os.pathsep.join(p for p in (source_root, os.environ.get("PYTHONPATH", "")) if p)
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-x", "220", "-y", "50",
                    "-e", f"PATH={os.environ.get('PATH', '')}", "-e", f"PYTHONPATH={pythonpath}",
                    "bash", "-c", shell], check=True)
    subprocess.run(["tmux", "set-option", "-t", session, "history-limit", "100000"], capture_output=True)
    print(f"Started in tmux session '{session}'.")
    print(f"  watch:   bananavibe attach      (or tmux attach -t {session}; detach with Ctrl-b d)")
    print("  status:  bananavibe status")
    print("  stop:    bananavibe stop [--now]")
    return 0


def cmd_attach(args) -> int:
    cfg = _load(_paths(args))
    os.execvp("tmux", ["tmux", "attach", "-t", _session_name(cfg.name)])


# ------------------------------------------------------------------ status and control

def cmd_status(args) -> int:
    paths = _paths(args)
    if not paths.state.exists():
        print("No run yet in this workspace." if paths.config.exists() else "Not initialized: `bananavibe init`.")
        return 0
    state = State(paths)
    d = state.data
    pid = supervisor_pid(paths)
    if args.json:
        print(json.dumps({**d, "supervisor_running": bool(pid)}, indent=2))
        return 0
    stats = plan_stats(read_text(paths.plan))
    started = parse_iso(d["started_at"])
    control = read_control(paths)
    status = d["status"]
    if status in ("running", "waiting", "paused") and not pid:
        status += " (supervisor not running!)"
    print(f"Status:     {status} — {d['detail']}")
    if d["status"] == "waiting" and d["wait_until"]:
        until = parse_iso(d["wait_until"])
        print(f"Waiting:    until {until:%a %H:%M} ({human_duration((until - now()).total_seconds())} left)")
    if d.get("current_agent") and d.get("session_started_at"):
        s = parse_iso(d["session_started_at"])
        print(f"Agent:      {d['current_agent']} working for {human_duration((now() - s).total_seconds())}")
    if control.get("pause") and d["status"] != "paused":
        print("Pending:    pause after the current session")
    if control.get("stop"):
        print("Pending:    stop after the current session")
    ended = parse_iso(d["finished_at"]) or (parse_iso(d["updated_at"]) if d["status"] in ("stopped", "crashed")
                                            else None)
    elapsed = human_duration(((ended or now()) - started).total_seconds()) if started else ""
    print(f"Sessions:   {d['iteration']} completed, {d['attempts']} attempts"
          + (f", {'ran' if ended else 'running'} for {elapsed}" if started else ""))
    print(f"Plan:       {stats.done}/{stats.total} done, {stats.open} open, {stats.blocked} blocked")
    print(f"Approvals:  {d['approvals']} in a row")
    if d["last_checks"]:
        print("Checks:     " + ", ".join(f"{c['name']} {'PASS' if c['ok'] else 'FAIL'}" for c in d["last_checks"]))
    for name, a in d["agents"].items():
        cool = parse_iso(a.get("cooldown_until"))
        extra = f", cooling down until {cool:%a %H:%M}" if cool and cool > now() else ""
        print(f"  {name:10} {a['sessions']} sessions, {a['failures']} failed attempts{extra}{usage_text(a)}")
    for e in d["events"][-args.events:]:
        print(f"  {e['at'][5:16].replace('T', ' ')}  {e['kind']:10} {e['msg'][:150]}")
    latest = paths.reports / "latest.md"
    if latest.exists():
        print(f"Report:     {latest}")
    return 0


def _signal_supervisor(paths: Paths) -> bool:
    pid = supervisor_pid(paths)
    if pid and pid > 0:
        try:
            os.kill(pid, 15)
            return True
        except ProcessLookupError:
            return False
    return False


def cmd_stop(args) -> int:
    paths = _paths(args)
    if not supervisor_pid(paths):
        print("No supervisor is running.")
        return 0
    if args.now:
        _signal_supervisor(paths)
        print("Stopping now: the agent is interrupted and its work so far is committed.")
    else:
        set_control(paths, stop=True)
        print("The run will stop after the current session (use --now to interrupt it).")
    return 0


def cmd_pause(args) -> int:
    paths = _paths(args)
    set_control(paths, pause=True)
    print("The run will pause after the current session. Resume with `bananavibe resume`.")
    return 0


def cmd_resume(args) -> int:
    paths = _paths(args)
    set_control(paths, pause=False)
    print("Resumed." if supervisor_pid(paths) else "Pause cleared. No supervisor is running: `bananavibe start`.")
    return 0


def cmd_retry_now(args) -> int:
    set_control(_paths(args), retry_now=True)
    print("Cooldowns will be cleared and the agents retried right away.")
    return 0


def cmd_say(args) -> int:
    paths = _paths(args)
    message = " ".join(args.message).strip()
    if message == "-":
        message = sys.stdin.read()
    if not message:
        print("bananavibe: empty message", file=sys.stderr)
        return 2
    append_inbox(paths, message)
    print("Message queued: the next session reads it first.")
    if State(paths)["status"] == "done" and not supervisor_pid(paths):
        print("The run had finished; start it again with `bananavibe start` to act on the message.")
    return 0


def cmd_report(args) -> int:
    paths = _paths(args)
    cfg = _load(paths)
    state = State(paths)
    repos = gitops.discover(paths.workspace, cfg.git_repos)
    print(report.render(paths, cfg, state, repos, "on-demand report"))
    return 0


def cmd_logs(args) -> int:
    paths = _paths(args)
    if args.session:
        logs = sorted(p for p in paths.logs.glob("*.log") if p.name != "supervisor.log") if paths.logs.exists() else []
        if not logs:
            print("No session logs yet.")
            return 0
        target = max(logs, key=lambda p: p.stat().st_mtime)
    else:
        target = paths.supervisor_log
    if not target.exists():
        print("No logs yet.")
        return 0
    os.execvp("tail", ["tail", "-n", str(args.lines), *(["-F"] if args.follow else []), str(target)])


# ------------------------------------------------------------------ doctor

def cmd_doctor(args) -> int:
    paths = _paths(args)
    cfg = _load(paths)
    ok = True
    print(f"BananaVibe {__version__}, Python {sys.version.split()[0]}, running as "
          f"{'root' if os.geteuid() == 0 else os.environ.get('USER', 'user')}")
    for tool in ("git", "tmux"):
        print(f"  {'✔' if shutil.which(tool) else '✖'} {tool}")
        ok &= bool(shutil.which(tool)) or tool == "tmux"
    repos = gitops.discover(paths.workspace, cfg.git_repos)
    print(f"  {'✔' if repos else '✖'} repositories: {', '.join(str(r) for r in repos) or 'none'}")
    goal = read_text(paths.goal)
    if "Describe what the agents should achieve" in goal or not goal.strip():
        print("  ✖ GOAL.md still needs a mission")
        ok = False
    if not cfg.checks:
        print("  ! no [[checks]] configured: only the reviewer will verify the work")
    used = list(dict.fromkeys([*cfg.workers, cfg.reviewer_name]))
    for name in used:
        agent = cfg.agents[name]
        exe = shutil.which(agent.command[0]) if agent.command else None
        version = ""
        if exe and agent.type != "custom":
            r = subprocess.run([*agent.command, "--version"], capture_output=True, text=True, timeout=60)
            version = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else ""
        print(f"  {'✔' if exe else '✖'} agent {name} ({agent.type}): {exe or agent.command[0] + ' not found'} {version}"
              + (f" model={agent.model}" if agent.model else "") + (f" effort={agent.effort}" if agent.effort else ""))
        ok &= bool(exe)
    if args.live:
        console = Console()
        scratch = paths.root / "doctor"
        scratch.mkdir(parents=True, exist_ok=True)
        for name in used:
            adapter = make_adapter(cfg.agents[name])
            prompt = "This is a connectivity test. Do not use any tools. Reply with exactly: PONG"
            prompt_file = scratch / "prompt.md"
            prompt_file.write_text(prompt)
            print(f"  … asking {name} for PONG")
            res = run_session(adapter, prompt, prompt_file, scratch, scratch / f"{name}.log", console,
                              300, 300, threading.Event())
            good = res.outcome.kind == OK and "PONG" in res.outcome.final_text
            print(f"  {'✔' if good else '✖'} {name}: {res.outcome.kind} {res.outcome.message[:300]}"
                  f" ({human_duration(res.seconds)})")
            ok &= good
    print("All good." if ok else "Some problems need fixing (see ✖ above).")
    return 0 if ok else 1


# ------------------------------------------------------------------ main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bananavibe", description="Keep coding agents working until the goal is "
                                "really done: supervised, verified, reviewed, and resilient to limits and outages.")
    p.add_argument("-V", "--version", action="version", version=f"bananavibe {__version__}")
    p.add_argument("-w", "--workspace", default=".", help="workspace directory (default: current directory)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="create .bananavibe/ with a config and a GOAL.md")
    s.add_argument("--name", default="")
    s.add_argument("--workers", default="claude", help="comma-separated worker agents (default: claude)")
    s.add_argument("--reviewer", default="", help="reviewer agent (default: codex, or claude if codex works)")
    s.add_argument("--goal", default="", help="mission text")
    s.add_argument("--goal-file", default="", help="file with the mission")
    s.add_argument("--check", action="append", metavar="NAME=COMMAND", help="verification command (repeatable)")
    s.add_argument("--claude-model", default="")
    s.add_argument("--claude-effort", default="")
    s.add_argument("--codex-model", default="")
    s.add_argument("--codex-effort", default="")
    s.add_argument("--opencode-model", default="")
    s.add_argument("--git-init", action="store_true", help="git init the workspace if it has no repository")
    s.add_argument("--force", action="store_true", help="overwrite an existing config")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("run", help="run the supervisor in the foreground")
    s.add_argument("--again", action="store_true", help="continue a finished run")
    s.add_argument("--no-keep-alive", dest="keep_alive", action="store_false",
                   help="exit instead of restarting when the supervisor itself crashes")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("start", help="run the supervisor in a detached tmux session")
    s.add_argument("--again", action="store_true", help="continue a finished run")
    s.set_defaults(func=cmd_start)
    sub.add_parser("attach", help="attach to the tmux session").set_defaults(func=cmd_attach)

    s = sub.add_parser("status", help="show progress")
    s.add_argument("--json", action="store_true")
    s.add_argument("--events", type=int, default=8, help="number of recent events to show")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("stop", help="stop after the current session")
    s.add_argument("--now", action="store_true", help="interrupt the current session (its work is kept)")
    s.set_defaults(func=cmd_stop)
    sub.add_parser("pause", help="pause after the current session").set_defaults(func=cmd_pause)
    sub.add_parser("resume", help="resume a paused run").set_defaults(func=cmd_resume)
    sub.add_parser("retry-now", help="skip the current limit/backoff wait").set_defaults(func=cmd_retry_now)

    s = sub.add_parser("say", help="send instructions to the next session ('-' reads stdin)")
    s.add_argument("message", nargs="+")
    s.set_defaults(func=cmd_say)

    sub.add_parser("report", help="print a fresh progress report").set_defaults(func=cmd_report)

    s = sub.add_parser("logs", help="show the supervisor log (or the latest session log)")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("-s", "--session", action="store_true", help="latest agent session log instead")
    s.add_argument("-n", "--lines", type=int, default=60)
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("doctor", help="check the setup")
    s.add_argument("--live", action="store_true", help="also send a tiny prompt to each agent")
    s.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args) or 0
