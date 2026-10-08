"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from bananavibe import __version__, gitops, handover, report, templates, watch
from bananavibe.adapters import OK, make_adapter
from bananavibe.ask import ask
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
    # In the background nobody reads stdout: everything goes to logs/supervisor.log only.
    console = Console(paths.supervisor_log, quiet=args.daemon)
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


def _child_env() -> dict[str, str]:
    # Carry the package location (when running from a source checkout) into the detached supervisor.
    source_root = str(Path(__file__).resolve().parent.parent)
    pythonpath = os.pathsep.join(p for p in (source_root, os.environ.get("PYTHONPATH", "")) if p)
    return {**os.environ, "PYTHONPATH": pythonpath}


def _run_argv(paths: Paths, again: bool) -> list[str]:
    return [sys.executable, "-P", "-m", "bananavibe", "-w", str(paths.workspace), "run"] + (["--again"] if again else [])


def cmd_start(args) -> int:
    paths = _paths(args)
    cfg = _load(paths)
    if supervisor_pid(paths):
        print("A supervisor is already running in this workspace. `bananavibe attach` to watch it.")
        return 1
    if args.tmux:
        return _start_tmux(paths, cfg, args)
    paths.logs.mkdir(parents=True, exist_ok=True)
    with paths.supervisor_out.open("ab") as out:
        out.write(f"\n--- {now():%Y-%m-%d %H:%M:%S} bananavibe start ---\n".encode())
        out.flush()
        # A new session: no controlling terminal, immune to the shell or SSH connection closing.
        proc = subprocess.Popen([*_run_argv(paths, args.again), "--daemon"], cwd=paths.workspace,
                                env=_child_env(), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if supervisor_pid(paths) == proc.pid:
            break
        if proc.poll() is not None:
            text = read_text(paths.supervisor_out).rsplit("--- ", 1)[-1].split("\n", 1)[-1].strip()
            print(text or f"The supervisor exited right away (code {proc.returncode}).")
            return proc.returncode or 0
        time.sleep(0.2)
    else:
        print(f"The supervisor (pid {proc.pid}) has not started yet; check `bananavibe status` and "
              f"{paths.rel(paths.supervisor_out)}.")
        return 1
    print(f"Started in the background (pid {proc.pid}). It keeps running after you log out.")
    print("  watch:   bananavibe attach          (live transcript; Ctrl-C only stops watching)")
    print("  status:  bananavibe status")
    print("  ask:     bananavibe ask \"is it done? what's left?\"")
    print("  pause:   bananavibe pause [--now]   stop: bananavibe stop [--now]")
    return 0


def _start_tmux(paths: Paths, cfg, args) -> int:
    if not shutil.which("tmux"):
        print("bananavibe: tmux is not installed; run `bananavibe start` without --tmux.", file=sys.stderr)
        return 2
    session = _session_name(cfg.name)
    if subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode == 0:
        print(f"tmux session {session} already exists: `tmux attach -t {session}` (or kill it first).")
        return 1
    shell = (f"cd {shlex.quote(str(paths.workspace))} && {shlex.join(_run_argv(paths, args.again))}; "
             "echo; echo '[bananavibe] supervisor exited. Press Enter to close this window.'; read _")
    env = _child_env()
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-x", "220", "-y", "50",
                    "-e", f"PATH={env.get('PATH', '')}", "-e", f"PYTHONPATH={env['PYTHONPATH']}",
                    "bash", "-c", shell], check=True)
    subprocess.run(["tmux", "set-option", "-t", session, "history-limit", "100000"], capture_output=True)
    print(f"Started in tmux session '{session}'.")
    print(f"  watch:   tmux attach -t {session} (detach with Ctrl-b d), or `bananavibe attach`")
    print("  status:  bananavibe status")
    print("  stop:    bananavibe stop [--now]")
    return 0


def cmd_attach(args) -> int:
    paths = _paths(args)
    cfg = _load(paths)
    if args.tmux:
        os.execvp("tmux", ["tmux", "attach", "-t", _session_name(cfg.name)])
    return watch.follow(paths, cfg, raw=args.raw, max_lines=0 if args.full else args.max_lines,
                        history=args.history)


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
    if pid:
        print(f"Supervisor: pid {pid}" + (f" on {d['host']}" if d.get("host") else ""))
    if d.get("current_agent") and d.get("session_started_at"):
        s = parse_iso(d["session_started_at"])
        cur = d.get("current") or {}
        print(f"Agent:      {d['current_agent']} working for {human_duration((now() - s).total_seconds())}"
              + (f" (session {cur['session_id']})" if cur.get("session_id") else ""))
    if d.get("interrupted"):
        i = d["interrupted"]
        print(f"Continuing: interrupted {i['label']} session by {i['agent']} ({i.get('reason', '')}, "
              f"{i.get('at', '')[:16].replace('T', ' ')})")
    if control.get("pause_now") and d["status"] != "paused":
        print("Pending:    pause now (interrupting the session)")
    elif control.get("pause") and d["status"] != "paused":
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


def _wait_stopped(paths: Paths, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not supervisor_pid(paths):
            return True
        time.sleep(0.5)
    return False


def cmd_stop(args) -> int:
    paths = _paths(args)
    if not supervisor_pid(paths):
        print("No supervisor is running.")
        set_control(paths, pause=False, pause_now=False)
        return 0
    if args.now:
        # A signal works on this machine; the control file also works from anywhere the workspace is visible.
        set_control(paths, stop_now=True)
        _signal_supervisor(paths)
        print("Stopping now: the agent is interrupted and its work so far is committed. "
              "The next `bananavibe start` continues that session.")
    else:
        set_control(paths, stop=True)
        print("The run will stop after the current session (use --now to interrupt it).")
    if args.wait:
        if not _wait_stopped(paths, args.wait_timeout):
            print("Still running after the timeout.")
            return 1
        print("Stopped.")
    return 0


def cmd_pause(args) -> int:
    paths = _paths(args)
    if args.now:
        set_control(paths, pause=True, pause_now=True)
        print("Pausing now: the agent is interrupted and its work so far is committed. "
              "`bananavibe resume` continues the same session.")
    else:
        set_control(paths, pause=True)
        print("The run will pause after the current session (use --now to interrupt it). "
              "Resume with `bananavibe resume`.")
    if not supervisor_pid(paths):
        print("(No supervisor is running right now: a new `bananavibe start` begins unpaused.)")
    return 0


def cmd_resume(args) -> int:
    paths = _paths(args)
    set_control(paths, pause=False, pause_now=False)
    if supervisor_pid(paths):
        print("Resumed.")
        return 0
    if not args.start:
        print("Pause cleared. No supervisor is running: `bananavibe start` (or `bananavibe resume --start`).")
        return 0
    args.again = False
    args.tmux = False
    return cmd_start(args)


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
    print("Message queued: the next session reads it first. (To get an answer instead, use `bananavibe ask`.)")
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


# ------------------------------------------------------------------ sessions, ask, handover

def _find_session(state: State, ref: str) -> dict | None:
    history = state["history"]
    if state["current"]:
        current = {**state["current"], "n": "now", "outcome": "running"}
        if ref in ("now", "current", "latest", "") and current.get("session_id"):
            return current
    if not history:
        return None
    if ref in ("", "latest", "now", "current"):
        return next((h for h in reversed(history) if h.get("session_id")), history[-1])
    for h in reversed(history):
        if str(h["n"]) == ref or (h.get("session_id") and h["session_id"].startswith(ref)):
            return h
    return None


def cmd_sessions(args) -> int:
    paths = _paths(args)
    state = State(paths)
    rows = state["history"][-args.limit:]
    if state["current"]:
        rows = [*rows, {**state["current"], "n": "now", "outcome": "running", "seconds": None}]
    if not rows:
        print("No sessions yet.")
        return 0
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{'#':>5}  {'started':16}  {'session':16} {'agent':10} {'took':>8}  {'outcome':12} agent session id")
    for h in rows:
        took = human_duration(h["seconds"]) if h.get("seconds") is not None else "…"
        print(f"{h['n']!s:>5}  {str(h.get('started', ''))[:16].replace('T', ' '):16}  "
              f"{h['iteration']:>4} {h['label']:11} {h['agent']:10} {took:>8}  {h.get('outcome', ''):12} "
              f"{h.get('session_id') or '-'}")
    print("\nOpen one in the agent's own UI: `bananavibe open <#|id>`. Transcript: `bananavibe show <#>`.")
    return 0


def cmd_show(args) -> int:
    paths = _paths(args)
    cfg = _load(paths)
    h = _find_session(State(paths), args.session)
    if not h:
        print("No such session.", file=sys.stderr)
        return 1
    path = paths.workspace / h["log"]
    if not path.exists():
        print(f"The log {h['log']} is gone.", file=sys.stderr)
        return 1
    style = watch.Style(sys.stdout.isatty() and not os.environ.get("NO_COLOR"))
    renderer = watch.renderer_for(cfg, h["agent"], style, 0 if args.full else args.max_lines)
    for line in watch.render_file(path, renderer, raw=args.raw):
        print(line)
    return 0


def cmd_open(args) -> int:
    paths = _paths(args)
    cfg = _load(paths)
    state = State(paths)
    h = _find_session(state, args.session)
    if not h or not h.get("session_id"):
        print("No session with a recorded agent session id (custom agents don't report one). "
              "See `bananavibe sessions`.", file=sys.stderr)
        return 1
    running = (state["current"] or {}).get("session_id") == h["session_id"] and supervisor_pid(paths)
    if running and not args.force:
        print("That session is running right now. Opening it would fork the conversation; watch it with "
              "`bananavibe attach` instead, or use --force to open it anyway (don't type into it).", file=sys.stderr)
        return 1
    if h.get("host") and h["host"] != socket.gethostname():
        print(f"Note: this session ran on {h['host']}; the agent may not have its history on this machine.")
    agent = cfg.agents.get(h["agent"])
    argv = make_adapter(agent).open_command(h["session_id"], paths.workspace) if agent else None
    if not argv:
        print(f"Don't know how to open sessions of {h['agent']}.", file=sys.stderr)
        return 1
    print("$ " + shlex.join(argv), flush=True)
    os.chdir(paths.workspace)
    env = {**os.environ, **agent.env}
    os.execvpe(argv[0], argv, env)


def cmd_ask(args) -> int:
    paths = _paths(args)
    if args.history:
        text = read_text(paths.answers).strip()
        print(text or "No questions asked yet.")
        return 0
    question = " ".join(args.question).strip()
    if question == "-":
        question = sys.stdin.read().strip()
    if not question:
        print("bananavibe: ask what? e.g. bananavibe ask \"is it done? what is left?\"", file=sys.stderr)
        return 2
    cfg = _load(paths)
    if args.agent and args.agent not in cfg.agents:
        print(f"bananavibe: unknown agent {args.agent}", file=sys.stderr)
        return 2
    ok, answer = ask(paths, cfg, question, args.agent, verbose=not args.quiet)
    print(answer)
    return 0 if ok else 1


def cmd_export(args) -> int:
    paths = _paths(args)
    cfg = _load(paths)
    if supervisor_pid(paths):
        if not args.stop:
            print("The supervisor is running. Stop it first (`bananavibe stop --now`), or pass --stop.",
                  file=sys.stderr)
            return 1
        set_control(paths, stop_now=True)
        _signal_supervisor(paths)
        print("Stopping the supervisor (the current session is interrupted and will be continued)...")
        if not _wait_stopped(paths, 600):
            print("It did not stop within 10 minutes.", file=sys.stderr)
            return 1
    output = Path(args.output or f"{_session_name(cfg.name)}-{now():%Y%m%d-%H%M}.tar.gz").expanduser().resolve()
    try:
        manifest = handover.export(paths, cfg, output, include_logs=not args.no_logs)
    except handover.HandoverError as e:
        print(f"bananavibe: {e}", file=sys.stderr)
        return 1
    size = output.stat().st_size / 1e6
    print(f"Exported '{cfg.name}' ({len(manifest['repos'])} repositories, {size:.1f} MB) to {output}")
    print("On the other machine:")
    print(f"  bananavibe import {output.name} -w <workspace> && bananavibe -w <workspace> doctor && "
          "bananavibe -w <workspace> start")
    print("Uncommitted ignored files (dependencies, build output, local data) are not included.")
    return 0


def cmd_import(args) -> int:
    paths = _paths(args)
    archive = Path(args.archive).expanduser().resolve()
    if supervisor_pid(paths):
        print("A supervisor is running in the target workspace; stop it first.", file=sys.stderr)
        return 1
    try:
        done = handover.import_run(archive, paths.workspace, force=args.force)
    except (handover.HandoverError, OSError) as e:
        print(f"bananavibe: {e}", file=sys.stderr)
        return 1
    for line in done:
        print("  " + line)
    state = State(paths)
    print(f"Imported into {paths.workspace}: status {state['status']}, {state['iteration']} sessions done"
          + (f", an interrupted {state['interrupted']['label']} session will be continued"
             if state["interrupted"] else "") + ".")
    print("Check that the agents are installed and logged in here (`bananavibe doctor --live`), then "
          "`bananavibe start`.")
    return 0


# ------------------------------------------------------------------ doctor

def cmd_doctor(args) -> int:
    paths = _paths(args)
    cfg = _load(paths)
    ok = True
    print(f"BananaVibe {__version__}, Python {sys.version.split()[0]}, running as "
          f"{'root' if os.geteuid() == 0 else os.environ.get('USER', 'user')}")
    print(f"  {'✔' if shutil.which('git') else '✖'} git")
    ok &= bool(shutil.which("git"))
    repos = gitops.discover(paths.workspace, cfg.git_repos)
    print(f"  {'✔' if repos else '✖'} repositories: {', '.join(str(r) for r in repos) or 'none'}")
    goal = read_text(paths.goal)
    if "Describe what the agents should achieve" in goal or not goal.strip():
        print("  ✖ GOAL.md still needs a mission")
        ok = False
    if not cfg.checks:
        print("  ! no [[checks]] configured: only the reviewer will verify the work")
    used = list(dict.fromkeys([*cfg.workers, cfg.reviewer_name, cfg.asker_name]))
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
    s.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("start", help="run the supervisor in the background (survives logging out)")
    s.add_argument("--again", action="store_true", help="continue a finished run")
    s.add_argument("--tmux", action="store_true", help="run it in a detached tmux session instead")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("attach", help="watch live: supervisor events and the agent's full transcript")
    s.add_argument("--raw", action="store_true", help="the agent's raw event stream (JSON lines)")
    s.add_argument("--full", action="store_true", help="don't shorten tool output")
    s.add_argument("--max-lines", type=int, default=25, help="lines shown per tool output (default 25)")
    s.add_argument("--history", type=int, default=60, help="earlier transcript lines shown first (default 60)")
    s.add_argument("--tmux", action="store_true", help="attach to the tmux session of `start --tmux`")
    s.set_defaults(func=cmd_attach)

    s = sub.add_parser("status", help="show progress")
    s.add_argument("--json", action="store_true")
    s.add_argument("--events", type=int, default=8, help="number of recent events to show")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("stop", help="stop after the current session")
    s.add_argument("--now", action="store_true", help="interrupt the current session (its work is kept and the "
                                                     "next start continues it)")
    s.add_argument("--wait", action="store_true", help="wait until the supervisor has exited")
    s.add_argument("--wait-timeout", type=float, default=600, help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_stop)
    s = sub.add_parser("pause", help="pause after the current session")
    s.add_argument("--now", action="store_true", help="interrupt the current session now; resume continues it")
    s.set_defaults(func=cmd_pause)
    s = sub.add_parser("resume", help="resume a paused run")
    s.add_argument("--start", action="store_true", help="start the supervisor in the background if it isn't running")
    s.set_defaults(func=cmd_resume)
    sub.add_parser("retry-now", help="skip the current limit/backoff wait").set_defaults(func=cmd_retry_now)

    s = sub.add_parser("say", help="send instructions to the next session ('-' reads stdin)")
    s.add_argument("message", nargs="+")
    s.set_defaults(func=cmd_say)

    s = sub.add_parser("ask", help="ask a question about the run and get an answer (running or finished)")
    s.add_argument("question", nargs="*", help="the question ('-' reads stdin)")
    s.add_argument("--agent", default="", help="agent to ask (default: [run].asker, else the reviewer)")
    s.add_argument("-q", "--quiet", action="store_true", help="print only the answer")
    s.add_argument("--history", action="store_true", help="show earlier questions and answers")
    s.set_defaults(func=cmd_ask)

    s = sub.add_parser("sessions", help="list agent sessions with their agent session ids")
    s.add_argument("-n", "--limit", type=int, default=30)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sessions)

    s = sub.add_parser("show", help="print a session's full transcript")
    s.add_argument("session", nargs="?", default="latest", help="# from `sessions`, an id prefix, or latest")
    s.add_argument("--raw", action="store_true")
    s.add_argument("--full", action="store_true", help="don't shorten tool output")
    s.add_argument("--max-lines", type=int, default=25)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("open", help="open a session in the agent's own interactive UI (claude --resume, ...)")
    s.add_argument("session", nargs="?", default="latest", help="# from `sessions`, an id prefix, or latest")
    s.add_argument("--force", action="store_true", help="open it even if it is running now")
    s.set_defaults(func=cmd_open)

    s = sub.add_parser("export", help="pack the run (state + git bundles) to continue it on another machine")
    s.add_argument("-o", "--output", default="", help="archive path (default: bananavibe-<name>-<time>.tar.gz)")
    s.add_argument("--stop", action="store_true", help="stop a running supervisor first (stop --now)")
    s.add_argument("--no-logs", action="store_true", help="leave session logs out (smaller archive)")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("import", help="restore an exported run into the workspace (-w), then `start`")
    s.add_argument("archive")
    s.add_argument("--force", action="store_true", help="replace an existing run / branch in the workspace")
    s.set_defaults(func=cmd_import)

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
