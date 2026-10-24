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
import tempfile
import threading
import time
import traceback
from pathlib import Path

from bananavibe import __version__, gitops, handover, housekeeping, report, service, templates, watch
from bananavibe.adapters import OK, make_adapter
from bananavibe.ask import ask
from bananavibe.config import KNOWN_TYPES, ConfigError, load, loads
from bananavibe.guard import Guardian, guardian_pid
from bananavibe.runner import run_session
from bananavibe.state import (
    approved_text,
    cancel_message,
    Paths,
    State,
    add_message,
    heartbeat_age,
    pending_messages,
    plan_stats,
    read_control,
    set_control,
    supervisor_pid,
    take_control,
)
from bananavibe.supervisor import Supervisor, recover_protected_files
from bananavibe.util import (
    Console,
    ascii_only,
    clean,
    human_duration,
    iso,
    now,
    parse_iso,
    read_text,
    short_time,
    symbol,
    usage_text,
    use_color,
    write_atomic,
)


def _paths(args) -> Paths:
    return Paths(Path(args.workspace).expanduser())


def _load(paths: Paths):
    try:
        return load(paths.config, paths.workspace)
    except ConfigError as e:
        print(f"bananavibe: config error: {e}", file=sys.stderr)
        sys.exit(2)


def _require_init(paths: Paths, restore: bool = False) -> None:
    """Exit unless there is a run here. With `restore` (automatic restarts only), a run directory that was
    deleted (by an agent, while the supervisor was down) is first rebuilt from its backup outside the workspace."""
    if not paths.config.exists() and restore:
        try:
            restored = housekeeping.restore_run_dir(paths)
        except OSError as e:
            restored = []
            print(f"bananavibe: cannot restore {paths.rel(paths.root)} from its backup: {e}", file=sys.stderr)
        if restored:
            message = f"{paths.rel(paths.root)} was missing: restored it from the backup in {paths.backup} " \
                      f"({', '.join(restored)})"
            print(f"{now():%Y-%m-%d %H:%M:%S} {message}")
            try:
                state = State(paths)
                state.event("attention", message)
                state.save()
            except OSError:
                pass
    if not paths.config.exists():
        if not restore and (_running(paths) or _workspace_supervisors(paths)):
            print(f"bananavibe: {paths.rel(paths.root)} is missing but a supervisor is running here: it was deleted "
                  "during the current session and is restored when the session ends. Try again then.",
                  file=sys.stderr)
            sys.exit(2)
        hint = (f"; a backup of the run that was here is in {paths.backup}"
                if housekeeping.has_backup(paths) else "")
        print(f"bananavibe: no run in {paths.workspace} (run `bananavibe init` first, or pass -w <workspace>)"
              + hint, file=sys.stderr)
        sys.exit(2)


def _session_name(name: str) -> str:
    return "bananavibe-" + re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")


def _running(paths: Paths, settle: float = 0) -> bool:
    """Whether a supervisor (or its guardian) runs here. With `settle`, give a guardian whose supervisor just
    exited a moment to exit too (as after `stop --now`)."""
    if supervisor_pid(paths):
        return True
    deadline = time.monotonic() + settle
    while guardian_pid(paths):
        if time.monotonic() >= deadline:
            return True
        time.sleep(0.2)
        if supervisor_pid(paths):
            return True
    return False


class _Colors:
    def __init__(self, stream=None):
        self.on = use_color(stream)

    def __call__(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text


# ------------------------------------------------------------------ init

def cmd_init(args) -> int:
    paths = _paths(args)
    if not paths.workspace.is_dir():
        print(f"bananavibe: {paths.workspace} is not a directory", file=sys.stderr)
        return 2
    if _running(paths) or _workspace_supervisors(paths):
        print("bananavibe: a supervisor is running in this workspace" + (
              f" ({paths.rel(paths.root)} was deleted during the current session: it is restored when the session "
              "ends)" if not paths.config.exists() else "") + "; stop it first to start a new run.", file=sys.stderr)
        return 1
    if paths.config.exists() and not args.force:
        print(f"bananavibe: {paths.config} already exists (use --force to overwrite the config)", file=sys.stderr)
        return 2
    workers = [w.strip() for w in args.workers.split(",") if w.strip()]
    for w in [*workers, args.reviewer]:
        if w and w not in KNOWN_TYPES:
            print(f"bananavibe: unknown agent '{w}' (built-in: claude, codex, opencode); "
                  "define custom agents in the config afterwards", file=sys.stderr)
            return 2
    if not workers:
        print("bananavibe: --workers needs at least one agent", file=sys.stderr)
        return 2
    goal = args.goal or ""
    if args.goal_file:
        try:
            goal = Path(args.goal_file).expanduser().read_text(encoding="utf-8")
        except OSError as e:
            print(f"bananavibe: cannot read {args.goal_file}: {e}", file=sys.stderr)
            return 2
    if not paths.state.exists():
        # A new run: a backup of an earlier one here must never come back (only a backup of this run's own
        # messages from before its first start, when `.bananavibe/` still exists, is kept).
        housekeeping.clear_backup(paths, everything=not paths.root.exists())
    paths.root.mkdir(exist_ok=True)
    checks = ""
    for spec in args.check or []:
        name, _, run = spec.partition("=")
        if not run:
            name, run = f"check-{len(checks.split('[[checks]]'))}", spec
        checks += templates.CHECK.format(name=json.dumps(name.strip()), run=json.dumps(run.strip()))
    reviewer = args.reviewer or (next((t for t in ("codex", "claude") if t not in workers[:1]), workers[0]))
    write_atomic(paths.config, templates.CONFIG.format(
        name=json.dumps(args.name or paths.workspace.name)[1:-1], workers=json.dumps(workers), reviewer=reviewer,
        checks=checks or templates.NO_CHECKS,
        claude_model=args.claude_model, claude_effort=args.claude_effort,
        codex_model=args.codex_model, codex_effort=args.codex_effort, opencode_model=args.opencode_model,
    ))
    if goal or not paths.goal.exists():
        if goal.lstrip().startswith("#"):
            write_atomic(paths.goal, goal.rstrip() + "\n")
        else:
            write_atomic(paths.goal, templates.GOAL.format(goal=goal.strip() or "<!-- Describe what the agents should achieve. -->"))
    for path, text in ((paths.plan, templates.PLAN), (paths.notes, templates.NOTES)):
        if not path.exists():
            write_atomic(path, text)
    housekeeping.protect_run_dir(paths)
    repos = gitops.discover(paths.workspace, [])
    if not repos:
        if args.git_init:
            try:
                gitops.init_repo(paths.workspace, exclude=("/.bananavibe/", "/.bananavibe.*/"))
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                print(f"bananavibe: could not create the git repository: {e}", file=sys.stderr)
                return 1
            print(f"Initialized a git repository in {paths.workspace} so every session can be snapshotted.")
        else:
            print("Note: no git repository found; snapshots need git. Re-run with --git-init or run `git init`.")
    elif gitops.is_repo(paths.workspace):
        gitops.exclude_path(paths.workspace, "/.bananavibe/")
    print(f"Created {paths.rel(paths.config)} and {paths.rel(paths.goal)} in {paths.workspace}.")
    print("Next: describe the mission in GOAL.md, add [[checks]] to config.toml, run `bananavibe doctor`, "
          "then `bananavibe start` (it also sets up restarting at boot).")
    return 0


# ------------------------------------------------------------------ run and start

def _child_env() -> dict[str, str]:
    # Carry the package location (when running from a source checkout) into the supervisor processes.
    pythonpath = os.pathsep.join(p for p in (service.source_root(), os.environ.get("PYTHONPATH", "")) if p)
    return {**os.environ, "PYTHONPATH": pythonpath}


def _run_argv(paths: Paths, again: bool) -> list[str]:
    return [*service.base_argv(paths), "run"] + (["--again"] if again else [])


def _reset_done(paths: Paths) -> None:
    state = State(paths)
    if state["status"] == "done":
        state["status"] = "running"
        state["detail"] = "continuing a finished run"
        state["approvals"] = 0
        state["finished_at"] = None
        state.save()


def _clear_operator_stop(paths: Paths) -> None:
    """An explicit start overrides an earlier `bananavibe stop` (and any leftover pause)."""
    set_control(paths, stop=None, stop_now=None, pause=None, pause_now=None)
    state = State(paths)
    if state.get("operator_stop"):
        state["operator_stop"] = False
        state.save()


def _stop_pending(paths: Paths) -> bool:
    control = read_control(paths)
    return bool(control.get("stop") or control.get("stop_now")) or bool(State(paths).get("operator_stop"))


def cmd_run(args) -> int:
    paths = _paths(args)
    automatic = bool(args.child or args.service)  # restarted by the guardian or systemd: nobody is watching
    _require_init(paths, restore=automatic)
    console = Console(paths.supervisor_log, quiet=args.daemon)
    paths.logs.mkdir(parents=True, exist_ok=True)
    if args.child or args.no_guard or not supervisor_pid(paths):
        # A config.toml or GOAL.md an agent changed in a session cut off by a crash is put back first (and on
        # automatic restarts, a config.toml that no longer parses).
        for line in recover_protected_files(paths, repair_broken=automatic)[0]:
            console.warn(line)
    cfg = _load(paths)
    if args.child:
        try:
            return Supervisor(paths, cfg, console).run()
        except KeyboardInterrupt:
            return 130
        except Exception:
            console.warn(f"supervisor crashed:\n{traceback.format_exc()}")
            return 1
    if _running(paths, settle=8):
        print("A supervisor is already running in this workspace (`bananavibe status`).")
        return 0 if args.service else 1
    if args.service:
        # Started by systemd (at boot, or after a failure): a stop the operator asked for stands.
        if take_control(paths, "start_requested"):
            _clear_operator_stop(paths)
        elif _stop_pending(paths):
            console.info("not starting: the run was stopped on purpose (`bananavibe start` to continue)")
            return 0
    else:
        _clear_operator_stop(paths)  # running it by hand is an explicit start
    state = State(paths)
    messages = pending_messages(paths)
    if state["status"] == "done" and cfg.after_done == "exit" and not args.again and not messages:
        print("This run is already complete. Give new instructions with `bananavibe say \"...\"` "
              "(then run again), or use `bananavibe run --again` to re-verify.")
        return 0
    if state["status"] == "done" and (args.again or messages):
        _reset_done(paths)
    if args.no_guard:
        return _run_unguarded(paths, cfg, console, args)
    child = [*service.base_argv(paths), "run", "--child"] + (["--daemon"] if args.daemon else [])
    os.environ.update(_child_env())
    return Guardian(paths, child, console, cfg.watchdog_minutes).run()


def _run_unguarded(paths: Paths, cfg, console: Console, args) -> int:
    """The supervisor in this process, restarted in-process after its own crashes (no guardian)."""
    restarts = 0
    while True:
        started = time.monotonic()
        try:
            return Supervisor(paths, cfg, console).run()
        except KeyboardInterrupt:
            return 130
        except Exception:  # the supervisor must survive its own bugs, too
            restarts = 1 if time.monotonic() - started > 3600 else restarts + 1
            console.warn(f"supervisor crashed (restart {restarts}):\n{traceback.format_exc()}")
            if not args.keep_alive:
                return 1
            delay = min(60 * restarts, 900)
            console.warn(f"restarting the supervisor in {delay}s")
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                return 130
            try:
                cfg = load(paths.config, paths.workspace)
            except ConfigError as e:
                console.warn(f"config error, keeping the previous configuration: {e}")


def _spawn_daemon(paths: Paths, again: bool, quiet: bool = False, auto: bool = False) -> int:
    paths.logs.mkdir(parents=True, exist_ok=True)
    out_path = paths.supervisor_out
    try:
        if out_path.exists() and out_path.stat().st_size > 20 * 1024 * 1024:
            os.replace(out_path, out_path.with_name(out_path.name + ".1"))
    except OSError:
        pass
    with out_path.open("ab") as out:
        out.write(f"\n--- {now():%Y-%m-%d %H:%M:%S} bananavibe start ---\n".encode())
        out.flush()
        # A new session: no controlling terminal, immune to the shell or SSH connection closing.
        # From autostart (`--auto`) it runs like the service would: respecting a stop issued in the meantime.
        argv = [*_run_argv(paths, again), "--daemon"] + (["--service"] if auto else [])
        proc = subprocess.Popen(argv, cwd=paths.workspace, env=_child_env(),
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if supervisor_pid(paths):
            break
        if proc.poll() is not None:
            text = read_text(out_path).rsplit("--- ", 1)[-1].split("\n", 1)[-1].strip()
            if not quiet or proc.returncode:
                print(text or f"The supervisor exited right away (code {proc.returncode}).")
            return proc.returncode or 0
        time.sleep(0.2)
    else:
        if not quiet:
            print(f"The supervisor has not started yet; check `bananavibe status` and "
                  f"{paths.rel(out_path)}.")
        return 0 if quiet else 1
    if quiet:
        print(f"{now():%Y-%m-%d %H:%M:%S} started the run (it was down)")
        return 0
    print(f"Started in the background (pid {supervisor_pid(paths)}, guarded: restarted automatically if it "
          "crashes, is killed or hangs).")
    _print_start_help()
    return 0


def _print_start_help() -> None:
    print("  watch:   bananavibe dashboard   (or `attach` for the live transcript; q / Ctrl-C only stop watching)")
    print("  talk:    bananavibe chat        (ask questions, send instructions, pause/resume)")
    print("  status:  bananavibe status")
    print("  pause:   bananavibe pause [--now]   stop: bananavibe stop [--now]")


def _auto_start(paths: Paths) -> int:
    """From cron: start the run only if it should be running and isn't. Quiet unless something happens."""
    if _running(paths):
        return 0
    if not paths.config.exists() and _workspace_supervisors(paths):
        return 0  # .bananavibe/ was deleted under a running supervisor: it restores it after the session
    _require_init(paths, restore=True)
    if _stop_pending(paths):
        return 0  # stopped on purpose: nothing is touched
    # A config.toml broken or changed by an agent in a session cut off by a crash is repaired first (else the
    # run could never start again by itself).
    for line in recover_protected_files(paths, repair_broken=True)[0]:
        print(f"{now():%Y-%m-%d %H:%M:%S} {line}")
    cfg = _load(paths)
    if State(paths)["status"] == "done" and cfg.after_done == "exit" and not pending_messages(paths):
        return 0
    info = service.installed(paths)
    if info and info["kind"].startswith("systemd"):
        return 0  # systemd takes care of it
    try:
        service.refresh_cron(paths, cfg)  # set up by an older version
    except (service.ServiceError, OSError):
        pass
    return _spawn_daemon(paths, again=False, quiet=True, auto=True)


def cmd_start(args) -> int:
    paths = _paths(args)
    if args.auto:
        return _auto_start(paths)
    _require_init(paths)
    if not _running(paths):
        # A config.toml or GOAL.md changed by an agent in a session cut off by a crash is put back first.
        for line in recover_protected_files(paths)[0]:
            print(line)
    cfg = _load(paths)
    if _running(paths, settle=8):
        print("A supervisor is already running in this workspace. `bananavibe dashboard` or `attach` to watch it.")
        return 1
    _clear_operator_stop(paths)
    if args.tmux:
        return _start_tmux(paths, cfg, args)
    info = service.installed(paths)
    if cfg.autostart and not args.no_autostart:
        try:
            for line in service.install(paths, cfg):
                print(line[0].upper() + line[1:] + ".")
            info = service.installed(paths)
        except service.ServiceError as e:
            print(f"Note: automatic start at boot is not set up: {e}")
    elif not info:
        print("Note: autostart is off: the run restarts itself after crashes, but not after a reboot.")
    if info and info["kind"].startswith("systemd"):
        if args.again:
            _reset_done(paths)
        set_control(paths, start_requested=True)
        ok, msg = service.start(paths, info)
        if ok:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not supervisor_pid(paths):
                time.sleep(0.3)
            if supervisor_pid(paths):
                print(f"Started via systemd ({info['unit']}): it runs in the background, restarts itself after "
                      "crashes and starts again after a reboot.")
                _print_start_help()
                return 0
            print(f"systemd started {info['unit']} but no supervisor showed up; see "
                  f"`journalctl {'--user ' if info['kind'] == 'systemd-user' else ''}-u {info['unit']}`.")
            return 1
        take_control(paths, "start_requested")
        print(f"{msg}; starting it directly instead.")
    return _spawn_daemon(paths, args.again)


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
    _require_init(paths)
    cfg = _load(paths)
    if args.tmux:
        os.execvp("tmux", ["tmux", "attach", "-t", _session_name(cfg.name)])
    return watch.follow(paths, cfg, raw=args.raw, max_lines=0 if args.full else args.max_lines,
                        history=args.history)


def cmd_dashboard(args) -> int:
    from bananavibe import tui
    paths = _paths(args)
    _require_init(paths)
    return tui.run(paths, _load(paths), interval=args.interval)


def cmd_chat(args) -> int:
    from bananavibe import chat
    paths = _paths(args)
    _require_init(paths)
    return chat.run(paths, _load(paths), agent=args.agent or None)


# ------------------------------------------------------------------ status and control

def _bar(fraction: float, width: int, asc: bool) -> str:
    full = int(round(max(0.0, min(1.0, fraction)) * width))
    return symbol("bar", asc) * full + symbol("bar_empty", asc) * (width - full)


def status_lines(paths: Paths, events: int = 8, color: _Colors | None = None, asc: bool = False) -> list[str]:
    """The `bananavibe status` screen, also used by the dashboard and chat."""
    c = color or _Colors(None)
    c.on = c.on if color else False
    state = State(paths)
    d = state.data
    pid = supervisor_pid(paths)
    gpid = guardian_pid(paths)
    stats = plan_stats(read_text(paths.plan))
    control = read_control(paths)
    status = d["status"]
    status_color = {"running": "32", "waiting": "33", "paused": "33", "done": "1;32", "stopped": "2",
                    "crashed": "1;31", "new": "2"}.get(status, "")
    lines = []
    shown = c(status_color, status)
    if status in ("running", "waiting", "paused") and not pid:
        shown += c("1;31", " (supervisor not running" + (", restarting" if gpid else "") + ")")
    lines.append(f"Status      {shown} — {d['detail']}")
    if d["status"] == "waiting" and d["wait_until"]:
        until = parse_iso(d["wait_until"])
        if until:
            lines.append(f"Waiting     until {short_time(until)} ({human_duration((until - now()).total_seconds())} left)")
    if pid:
        age = heartbeat_age(paths)
        beat = f", heartbeat {human_duration(age)} ago" if age is not None else ""
        if age is not None and age > 300:
            beat = c("1;31", f", no heartbeat for {human_duration(age)}")
        lines.append(f"Supervisor  pid {pid}" + (f" on {d['host']}" if d.get("host") else "")
                     + (f", guarded by pid {gpid}" if gpid else ", not guarded") + beat)
    elif gpid:
        ending = d.get("status") in ("done", "stopped")
        lines.append(f"Supervisor  {'exiting' if ending else 'restarting'} (guardian pid {gpid})")
    info = service.installed(paths)
    lines.append(f"Autostart   {service.describe(info)}")
    cur = d.get("current") or {}
    intr = d.get("interrupted") or {}
    continuing = bool(cur and intr and intr.get("label") == cur.get("label"))
    if d.get("current_agent") and d.get("session_started_at"):
        s = parse_iso(d["session_started_at"])
        if s:
            lines.append(f"Agent       {cur.get('label', 'work')} session with {d['current_agent']} for "
                         f"{human_duration((now() - s).total_seconds())}"
                         + (f" (session {cur['session_id']})" if cur.get("session_id") else "")
                         + (f", continuing one cut short ({intr.get('reason', '')})" if continuing else ""))
    if intr and not continuing:
        lines.append(f"Continuing  interrupted {intr.get('label')} session by {intr.get('agent')} "
                     f"({intr.get('reason', '')}, {str(intr.get('at', ''))[:16].replace('T', ' ')})")
    if d.get("pending"):
        lines.append(f"After       session {d['pending'].get('iteration')}: {d['pending'].get('step')} to do")
    if control.get("pause_now") and d["status"] != "paused":
        lines.append("Pending     pause now (interrupting the session)")
    elif control.get("pause") and d["status"] != "paused":
        lines.append("Pending     pause after the current session")
    if control.get("stop") or control.get("stop_now"):
        if pid:
            lines.append("Pending     stop" + (" now" if control.get("stop_now") else " after the current session"))
        else:
            lines.append("Stopped     by you: it won't start by itself (not even after a reboot) until "
                         "`bananavibe start`")
    if stats.total:
        frac = stats.finished_fraction
        lines.append(f"Plan        {c('32', _bar(frac, 20, asc))} {frac:4.0%}  {stats.done}/{stats.total} done, "
                     f"{stats.open} open, {stats.blocked} blocked" + (f", {stats.dropped} dropped" if stats.dropped else ""))
    else:
        lines.append("Plan        (empty: the first session writes it)")
    if d["last_checks"]:
        lines.append("Checks      " + "  ".join(f"{x['name']} " + (c("32", "PASS") if x["ok"] else c("31", "FAIL"))
                                                for x in d["last_checks"]))
    started = parse_iso(d["started_at"])
    ended = parse_iso(d["finished_at"]) or (parse_iso(d["updated_at"]) if d["status"] in ("stopped", "crashed")
                                            else None)
    elapsed = human_duration(((ended or now()) - started).total_seconds()) if started else ""
    lines.append(f"Sessions    {d['iteration']} completed, {d['attempts']} attempts"
                 + (f", {'ran' if ended else 'running'} for {elapsed}" if started else "")
                 + f"; approvals {d['approvals']} in a row")
    messages = pending_messages(paths)
    if messages:
        lines.append(f"Messages    {len(messages)} waiting for the next session" +
                     (" (one marked urgent)" if any(m.urgent for m in messages) else ""))
    first = True
    for name, a in d["agents"].items():
        cool = parse_iso(a.get("cooldown_until"))
        extra = c("33", f", cooling down until {short_time(cool)}") if cool and cool > now() else ""
        lines.append(f"{'Agents' if first else '':12}{name:10} {a.get('sessions', 0)} sessions, "
                     f"{a.get('failures', 0)} failed attempts{extra}{usage_text(a)}")
        first = False
    if events:
        evs = d["events"][-events:]
        for i, e in enumerate(evs):
            kind = e.get("kind", "")
            kc = "31" if kind in ("attention", "auth", "fatal", "crash", "restart") else \
                "33" if kind in ("limit", "transient", "changes", "rejected", "restored") else "2"
            lines.append(f"{'Events' if i == 0 else '':12}{e.get('at', '')[5:16].replace('T', ' ')}  "
                         f"{c(kc, f'{kind:10}')} {str(e.get('msg', ''))[:150]}")
    latest = paths.reports / "latest.md"
    if latest.exists():
        lines.append(f"Report      {latest}")
    return lines


def cmd_status(args) -> int:
    paths = _paths(args)
    if not paths.state.exists():
        if not paths.config.exists():
            if _running(paths) or _workspace_supervisors(paths):
                print(f"A supervisor is running, but {paths.rel(paths.root)} was deleted during the current "
                      "session; it is restored when the session ends. (Don't run `init`.)")
            else:
                print("Not initialized: `bananavibe init`.")
            return 0
        n = len(pending_messages(paths))
        print("Not started yet: `bananavibe start`." + (f" {n} message(s) waiting for the first session." if n else ""))
        return 0
    if args.json:
        state = State(paths)
        print(json.dumps({**state.data, "supervisor_running": bool(supervisor_pid(paths)),
                          "guardian_running": bool(guardian_pid(paths)),
                          "heartbeat_age": heartbeat_age(paths),
                          "pending_messages": len(pending_messages(paths)),
                          "autostart": service.installed(paths)}, indent=2))
        return 0
    for line in status_lines(paths, args.events, _Colors(sys.stdout), ascii_only()):
        print(line)
    return 0


def _signal_supervisor(paths: Paths) -> bool:
    pid = supervisor_pid(paths)
    if pid and pid > 0:
        try:
            os.kill(pid, 15)
            return True
        except (ProcessLookupError, PermissionError):
            return False
    return False


def _wait_stopped(paths: Paths, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _running(paths):
            return True
        time.sleep(0.5)
    return False


def _mark_operator_stop(paths: Paths) -> None:
    state = State(paths)
    if paths.state.exists() and not state.get("operator_stop"):
        state["operator_stop"] = True
        state.save()
        housekeeping.backup_state(paths)


def _workspace_supervisors(paths: Paths) -> list[int]:
    """`bananavibe run` processes of this workspace, found by their command line (Linux). Unlike the lock
    files, this works when `.bananavibe/` was deleted and the other process keeps its state elsewhere."""
    found: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit() or int(entry) in (os.getpid(), os.getppid()):
            continue
        try:
            raw = Path(f"/proc/{entry}/cmdline").read_bytes()
        except OSError:
            continue
        args = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
        start = next((i for i, a in enumerate(args[:8]) if a == "bananavibe" or a.endswith("/bananavibe")), None)
        if start is None:
            continue
        ws, command, i = ".", "", start + 1
        while i < len(args):  # the global options, then the command
            a = args[i]
            if a in ("-w", "--workspace"):
                ws, i = (args[i + 1] if i + 1 < len(args) else ws), i + 2
            elif a.startswith("--workspace="):
                ws, i = a.split("=", 1)[1], i + 1
            elif a.startswith("-w") and len(a) > 2:
                ws, i = a[2:], i + 1
            elif a.startswith("-"):
                i += 1
            else:
                command = a
                break
        if command != "run":
            continue
        try:
            cwd = os.readlink(f"/proc/{entry}/cwd")
            if (Path(cwd) / Path(ws).expanduser()).resolve() == paths.workspace:
                found.append(int(entry))
        except OSError:
            continue
    return found


def request_stop(paths: Paths, now_: bool) -> str:
    """Stop the run (after the current session, or now). The request is recorded durably first: it holds
    across crashes and reboots, and autostart respects it, until an explicit `bananavibe start`."""
    running = _running(paths)
    set_control(paths, stop=True, **({"stop_now": True} if now_ else {}))
    if not running:
        _mark_operator_stop(paths)
        return ("No supervisor is running. It stays stopped (also after a reboot) until `bananavibe start`.")
    if now_:
        # A signal works on this machine; the control file also works from anywhere the workspace is visible.
        _signal_supervisor(paths)
        return ("Stopping now: the agent is interrupted and its work so far is committed. "
                "The next `bananavibe start` continues that session.")
    return "The run will stop after the current session (use --now to interrupt it)."


def cmd_stop(args) -> int:
    paths = _paths(args)
    _require_init(paths)
    running = _running(paths)
    print(request_stop(paths, args.now))
    if not running:
        return 0
    if args.wait:
        if not _wait_stopped(paths, args.wait_timeout):
            print("Still running after the timeout.")
            return 1
        print("Stopped.")
    return 0


def cmd_pause(args) -> int:
    paths = _paths(args)
    _require_init(paths)
    if args.now:
        set_control(paths, pause=True, pause_now=True)
        print("Pausing now: the agent is interrupted and its work so far is committed. "
              "`bananavibe resume` continues the same session.")
    else:
        set_control(paths, pause=True)
        print("The run will pause after the current session (use --now to interrupt it). "
              "Resume with `bananavibe resume`.")
    if not _running(paths):
        print("(No supervisor is running right now: a new `bananavibe start` begins unpaused.)")
    return 0


def cmd_resume(args) -> int:
    paths = _paths(args)
    _require_init(paths)
    set_control(paths, pause=None, pause_now=None)
    if _running(paths):
        print("Resumed.")
        return 0
    if not args.start:
        print("Pause cleared. No supervisor is running: `bananavibe start` (or `bananavibe resume --start`).")
        return 0
    args.again = False
    args.tmux = False
    args.auto = False
    args.no_autostart = False
    return cmd_start(args)


def cmd_retry_now(args) -> int:
    paths = _paths(args)
    _require_init(paths)
    set_control(paths, retry_now=True)
    print("Cooldowns will be cleared and the agents retried right away.")
    return 0


def send_message(paths: Paths, message: str, urgent: bool) -> str:
    """Queue a message for the agents; returns a description of when it will be read."""
    add_message(paths, message, urgent)
    state = State(paths)
    current = state["current"] or {}
    running = bool(supervisor_pid(paths))
    if urgent:
        set_control(paths, deliver_now=True)
        if running and current.get("label") == "work":
            return ("Delivering now: the running session is interrupted (its work is kept) and continues right away "
                    "with your message" + (" in the same conversation." if current.get("agent") == "claude" or
                                            "claude" in str(current.get("agent")) else "."))
        if running and current:
            return f"A {current.get('label')} session is running; the message goes to the next work session."
    if running and current.get("label") == "work":
        return ("Queued. The running session can see it now (agents check for new messages between tasks); every "
                "following session gets it in its instructions. `--now` delivers it immediately.")
    if not running and state["status"] == "done":
        return "Queued. The run had finished; `bananavibe start` to act on it."
    if not running:
        return "Queued for the next session (no supervisor is running: `bananavibe start`)."
    return "Queued: the next session reads it first."


def cmd_say(args) -> int:
    paths = _paths(args)
    _require_init(paths)
    if args.list:
        messages = pending_messages(paths)
        if not messages:
            print("No messages waiting.")
        for m in messages:
            print(f"{m.at.replace('T', ' ')}{' (urgent)' if m.urgent else ''}  {m.id}\n  "
                  + m.text.replace("\n", "\n  "))
        if messages:
            print("(`bananavibe say --cancel <id>` withdraws one before a session gets it)")
        return 0
    if args.cancel:
        if cancel_message(paths, args.cancel):
            print(f"Withdrawn: {args.cancel}" + (" (a running session may already have read it)"
                                                  if State(paths).get("current") else ""))
            return 0
        print(f"bananavibe: no queued message {args.cancel} (`bananavibe say --list`)", file=sys.stderr)
        return 1
    message = " ".join(args.message or []).strip()
    if message == "-":
        message = sys.stdin.read()
    if not message.strip():
        print("bananavibe: empty message", file=sys.stderr)
        return 2
    print(send_message(paths, message, args.now) + " (To get an answer instead, use `bananavibe ask`.)")
    return 0


def _reload_request(paths: Paths, keys: list[str]) -> tuple[dict | None, str]:
    request: dict = {"at": iso(now())}
    for key in keys:
        path = paths.goal if key == "goal" else paths.config
        text = read_text(path)
        if key == "config":
            try:
                loads(text, paths.workspace, str(paths.config))
            except ConfigError as e:
                return None, str(e)
        request[key] = text
    return request, ""


def _changed_protected(paths: Paths) -> dict[str, tuple[str, list[str]]]:
    """Which of GOAL.md / config.toml differ from the version the supervisor last approved: a summary and the
    diff lines, for each."""
    import difflib
    out = {}
    for key, path in (("goal", paths.goal), ("config", paths.config)):
        old, new = approved_text(paths, key), read_text(path)
        if old is None or old == new:
            continue
        diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), "approved", "now", lineterm="", n=1))
        added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
        removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
        out[key] = (f"{path.name}: +{added} -{removed} lines", diff)
    return out


def cmd_reload(args) -> int:
    paths = _paths(args)
    _require_init(paths)
    bad = [f for f in args.files if f not in ("goal", "config")]
    if bad:
        print(f"bananavibe: reload takes `goal` and/or `config`, not {', '.join(bad)}", file=sys.stderr)
        return 2
    changed = _changed_protected(paths)
    keys = args.files or (list(changed) if State(paths).get("accepted") else ["goal", "config"])
    if not keys:
        print("GOAL.md and config.toml are unchanged: nothing to reload.")
        return 0
    # Show exactly what gets approved: an agent may have edited the same file during its session, and
    # approving the file approves its lines too (`bananavibe edit` avoids that: it starts from the approved text).
    for key in keys:
        if key in changed:
            summary, diff = changed[key]
            print(summary)
            for line in diff[2:42]:
                print("  " + clean(line))
            if len(diff) > 42:
                print(f"  … {len(diff) - 42} more line(s)")
    if not args.yes:
        if not sys.stdin.isatty():
            print("bananavibe: not asking without a terminal: run it with --yes to approve these changes",
                  file=sys.stderr)
            return 2
        if input("Approve these changes? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing reloaded. (`bananavibe edit goal|config` edits the approved version.)")
            return 1
    request, error = _reload_request(paths, keys)
    if request is None:
        print(f"bananavibe: config.toml is invalid, nothing reloaded: {error}", file=sys.stderr)
        return 2
    pending = read_control(paths).get("reload")
    if isinstance(pending, dict):  # keep an approval of the other file that is still waiting
        request = {**{k: v for k, v in pending.items() if k in ("goal", "config")}, **request}
    set_control(paths, reload=request)
    names = ", ".join("GOAL.md" if k == "goal" else "config.toml" for k in keys)
    if supervisor_pid(paths):
        print(f"Approved {names}: applied before the next session (the running one is not interrupted).")
    else:
        print(f"Approved {names}. No supervisor is running; it applies when it starts.")
    return 0


def _editor() -> list[str]:
    for var in ("VISUAL", "EDITOR"):
        if os.environ.get(var):
            return shlex.split(os.environ[var])
    for name in ("nano", "vim", "vi"):
        if shutil.which(name):
            return [name]
    return []


def cmd_edit(args) -> int:
    paths = _paths(args)
    _require_init(paths)
    editor = _editor()
    if not editor:
        print("bananavibe: no editor found; set $EDITOR", file=sys.stderr)
        return 2
    target = {"goal": paths.goal, "config": paths.config, "plan": paths.plan, "notes": paths.notes}[args.what]
    if args.what in ("plan", "notes"):
        if supervisor_pid(paths) and (State(paths)["current"] or {}):
            print(f"Note: an agent may be editing {target.name} right now; keep your change small.")
        return subprocess.call([*editor, str(target)])
    original = read_text(target)
    # Mid-session (or after a crash mid-session), the file may hold an agent's change, which is reverted after
    # the session: edit what was approved instead. Otherwise the file on disk is the operator's own version.
    state = State(paths)
    approved = approved_text(paths, args.what) if state.get("current") or state.get("restore_pending") else None
    if approved is not None and approved != original:
        print(f"Note: {target.name} differs from the version last approved (probably changed by an agent during "
              "the current session, which is reverted after it). You are editing the approved version.")
        original = approved
    with tempfile.TemporaryDirectory(prefix="bananavibe-edit-") as tmp:
        draft = Path(tmp) / target.name
        draft.write_text(original, encoding="utf-8")
        while True:
            if subprocess.call([*editor, str(draft)]) != 0:
                print("Editor exited with an error; nothing changed.")
                return 1
            text = draft.read_text(encoding="utf-8")
            if text == original:
                print("No changes.")
                return 0
            if args.what != "config":
                break
            try:
                loads(text, paths.workspace, "config.toml")
                break
            except ConfigError as e:
                print(f"The config is invalid: {e}")
                if not sys.stdin.isatty() or input("Edit again? [Y/n] ").strip().lower() in ("n", "no"):
                    print("Nothing changed.")
                    return 2
    write_atomic(target, text)
    # Exactly the text edited here is approved (not the file re-read: an agent could have written it since).
    request = {"at": iso(now()), args.what: text}
    pending = read_control(paths).get("reload")
    if isinstance(pending, dict):  # keep an approval of the other file that is still waiting
        request = {**{k: v for k, v in pending.items() if k in ("goal", "config")}, **request}
    set_control(paths, reload=request)
    print(f"Saved {target.name}." + (" It applies before the next session." if supervisor_pid(paths) else ""))
    return 0


def cmd_report(args) -> int:
    paths = _paths(args)
    _require_init(paths)
    cfg = _load(paths)
    state = State(paths)
    repos = gitops.discover(paths.workspace, cfg.git_repos)
    if args.short:
        print(report.digest(paths, cfg, state, repos, args.hours))
    else:
        print(report.render(paths, cfg, state, repos, "on-demand report"))
    return 0


def cmd_logs(args) -> int:
    paths = _paths(args)
    if args.session:
        target = watch.latest_session_log(paths)
        if not target:
            print("No session logs yet.")
            return 0
        if target.name.endswith(".gz"):
            os.execvp("sh", ["sh", "-c", f"gzip -dc {shlex.quote(str(target))} | tail -n {int(args.lines)}"])
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
              f"{h.get('iteration', ''):>4} {h.get('label', ''):11} {h.get('agent', ''):10} {took:>8}  "
              f"{h.get('outcome', ''):12} {h.get('session_id') or '-'}")
    print("\nOpen one in the agent's own UI: `bananavibe open <#|id>`. Transcript: `bananavibe show <#>`.")
    return 0


def cmd_show(args) -> int:
    paths = _paths(args)
    cfg = _load(paths)
    h = _find_session(State(paths), args.session)
    if not h or not h.get("log"):
        print("No such session.", file=sys.stderr)
        return 1
    path = paths.workspace / h["log"]
    from bananavibe.util import existing_log
    path = existing_log(path)
    if not path.exists():
        print(f"The log {h['log']} is gone (old logs are deleted past [logs].max_mb).", file=sys.stderr)
        return 1
    style = watch.Style(use_color())
    renderer = watch.renderer_for(cfg, h["agent"], style, 0 if args.full else args.max_lines, ascii_only())
    for line in watch.iter_file(path, renderer, raw=args.raw):
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
    _require_init(paths)
    cfg = _load(paths)
    if args.agent and args.agent not in cfg.agents:
        print(f"bananavibe: unknown agent {args.agent}", file=sys.stderr)
        return 2
    ok, answer = ask(paths, cfg, question, args.agent, verbose=not args.quiet)
    print(clean(answer))
    return 0 if ok else 1


def cmd_export(args) -> int:
    paths = _paths(args)
    _require_init(paths)
    cfg = _load(paths)
    if _running(paths):
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
    if manifest.get("agent_sessions"):
        print("The interrupted agent conversation is included; it is resumed on the other machine when possible.")
    for warning in manifest.get("warnings", []):
        print("Warning: " + warning)
    print("On the other machine:")
    print(f"  bananavibe -w <workspace> import {output.name} && bananavibe -w <workspace> doctor && "
          "bananavibe -w <workspace> start")
    print("Uncommitted ignored files (dependencies, build output, local data) are not included.")
    print("This machine won't restart the run by itself any more (it was stopped by you); `start` here resumes it.")
    return 0


def cmd_import(args) -> int:
    paths = _paths(args)
    archive = Path(args.archive).expanduser().resolve()
    if _running(paths):
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


# ------------------------------------------------------------------ service

def cmd_service(args) -> int:
    paths = _paths(args)
    _require_init(paths)
    cfg = _load(paths)
    info = service.installed(paths)
    try:
        if args.action == "install":
            done = service.install(paths, cfg, args.kind or None)
            print("\n".join(done) or f"Already installed: {service.describe(service.installed(paths))}.")
        elif args.action == "uninstall":
            done = service.uninstall(paths)
            print("\n".join(done) or "Nothing installed.")
        elif args.action == "show":
            kind = args.kind or (info or {}).get("kind") or service.detect()
            if kind in ("systemd-system", "systemd-user"):
                print(service.render_unit(paths, cfg, kind == "systemd-system"))
            else:
                print(f"Autostart via {kind or 'nothing available'}; see `bananavibe service install`.")
        else:
            print(f"Autostart: {service.status_text(info)}")
            kill = service.kill_user_processes()
            if kill and (not info or info["kind"] == "cron") and os.geteuid() != 0:
                print("Warning: logind kills your processes when you log out (KillUserProcesses=yes). Enable "
                      f"lingering (`sudo loginctl enable-linger {os.environ.get('USER', '$USER')}`), then "
                      "`bananavibe service install` to use a systemd user service.")
    except service.ServiceError as e:
        print(f"bananavibe: {e}", file=sys.stderr)
        return 1
    return 0


# ------------------------------------------------------------------ doctor

def cmd_doctor(args) -> int:
    paths = _paths(args)
    _require_init(paths)
    cfg = _load(paths)
    ok = True
    good, bad, note = "✔", "✖", "!"
    if ascii_only():
        good, bad, note = "+", "x", "!"
    print(f"BananaVibe {__version__}, Python {sys.version.split()[0]}, running as "
          f"{'root' if os.geteuid() == 0 else os.environ.get('USER', 'user')} on {socket.gethostname()}")
    print(f"  {good if shutil.which('git') else bad} git")
    ok &= bool(shutil.which("git"))
    repos = gitops.discover(paths.workspace, cfg.git_repos)
    print(f"  {good if repos else bad} repositories: {', '.join(str(r) for r in repos) or 'none'}")
    for repo in repos:
        busy = gitops.operation_in_progress(repo)
        if busy:
            print(f"  {note} {repo.name}: a {busy} is in progress; snapshots fail until it is resolved")
    goal = read_text(paths.goal)
    if "Describe what the agents should achieve" in goal or not goal.strip():
        print(f"  {bad} GOAL.md still needs a mission")
        ok = False
    if not cfg.checks:
        print(f"  {note} no [[checks]] configured: only the reviewer will verify the work")
    used = list(dict.fromkeys([*cfg.workers, cfg.reviewer_name, cfg.asker_name]))
    for name in used:
        agent = cfg.agents[name]
        exe = shutil.which(agent.command[0]) if agent.command else None
        version = ""
        if exe and agent.type != "custom":
            try:
                r = subprocess.run([*agent.command, "--version"], capture_output=True, text=True, timeout=60,
                                   env={**os.environ, **agent.env}, stdin=subprocess.DEVNULL)
                version = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else ""
            except (OSError, subprocess.TimeoutExpired) as e:
                version = f"(--version failed: {e})"
        print(f"  {good if exe else bad} agent {name} ({agent.type}): {exe or agent.command[0] + ' not found'} {version}"
              + (f" model={agent.model}" if agent.model else "") + (f" effort={agent.effort}" if agent.effort else ""))
        ok &= bool(exe)
    # Things that would stop a long run.
    free = housekeeping.disk_free_mb(paths.workspace)
    if free is not None:
        low = free < max(cfg.min_free_disk_mb, 2048)
        print(f"  {note if low else good} disk: {free / 1000:.1f} GB free" + (" (low for a long run)" if low else ""))
    mem = housekeeping.memory_available_mb()
    if mem is not None:
        low = mem < 1024
        print(f"  {note if low else good} memory: {mem / 1000:.1f} GB available" + (" (agents may be killed by the "
                                                                                     "OOM killer)" if low else ""))
    info = service.installed(paths)
    if info:
        print(f"  {good} autostart: {service.status_text(info)}")
    elif not cfg.autostart:
        print(f"  {note} autostart is off ([run] autostart = false): the run restarts itself after crashes, but not "
              "after a reboot")
    else:
        kind = service.detect()
        print(f"  {note} autostart: " + (f"not set up yet (`bananavibe start` will set up {kind})" if kind else
                                         "neither systemd nor cron is available here: the run restarts itself after "
                                         "crashes, but not after a reboot"))
    if service.kill_user_processes() and os.geteuid() != 0 and (not info or info["kind"] == "cron"):
        print(f"  {note} logind kills your processes at logout (KillUserProcesses=yes): enable lingering with "
              f"`sudo loginctl enable-linger {os.environ.get('USER', '$USER')}` and run `bananavibe service install`")
    if cfg.agent_oom_score_adj and not Path("/proc/self/oom_score_adj").exists():
        print(f"  {note} no /proc/self/oom_score_adj here: agents can't be marked for the OOM killer (fine on macOS)")
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
            good_answer = res.outcome.kind == OK and "PONG" in res.outcome.final_text
            print(f"  {good if good_answer else bad} {name}: {res.outcome.kind} {res.outcome.message[:300]}"
                  f" ({human_duration(res.seconds)})")
            ok &= good_answer
    print("All good." if ok else "Some problems need fixing (see above).")
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

    s = sub.add_parser("run", help="run the supervisor in the foreground (guarded: restarted if it dies)")
    s.add_argument("--again", action="store_true", help="continue a finished run")
    s.add_argument("--no-guard", action="store_true", help="no guardian process (restart only after Python errors)")
    s.add_argument("--no-keep-alive", dest="keep_alive", action="store_false",
                   help="with --no-guard: exit instead of restarting when the supervisor itself crashes")
    s.add_argument("--service", action="store_true", help=argparse.SUPPRESS)  # started by systemd
    s.add_argument("--child", action="store_true", help=argparse.SUPPRESS)    # the supervisor under a guardian
    s.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("start", help="run in the background, restarting by itself (and at boot)")
    s.add_argument("--again", action="store_true", help="continue a finished run")
    s.add_argument("--tmux", action="store_true", help="run it in a detached tmux session instead")
    s.add_argument("--no-autostart", action="store_true", help="don't set up starting at boot (systemd/cron)")
    s.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)  # from cron: start only if it should run
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("attach", help="watch live: supervisor events and the agent's full transcript")
    s.add_argument("--raw", action="store_true", help="the agent's raw event stream (JSON lines)")
    s.add_argument("--full", action="store_true", help="don't shorten tool output")
    s.add_argument("--max-lines", type=int, default=25, help="lines shown per tool output (default 25)")
    s.add_argument("--history", type=int, default=60, help="earlier transcript lines shown first (default 60)")
    s.add_argument("--tmux", action="store_true", help="attach to the tmux session of `start --tmux`")
    s.set_defaults(func=cmd_attach)

    for name in ("dashboard", "top"):
        s = sub.add_parser(name, help="full-screen live dashboard with keys for pause/resume/say/ask"
                           if name == "dashboard" else "alias of dashboard")
        s.add_argument("--interval", type=float, default=1.0, help="refresh interval in seconds")
        s.set_defaults(func=cmd_dashboard)

    s = sub.add_parser("chat", help="talk to the run: ask questions and give instructions while it works")
    s.add_argument("--agent", default="", help="agent that answers questions (default: [run].asker)")
    s.set_defaults(func=cmd_chat)

    s = sub.add_parser("status", help="show progress")
    s.add_argument("--json", action="store_true")
    s.add_argument("--events", type=int, default=8, help="number of recent events to show")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("stop", help="stop after the current session (and don't restart automatically)")
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

    s = sub.add_parser("say", help="send instructions to the agents ('-' reads stdin)")
    s.add_argument("message", nargs="*")
    s.add_argument("--now", action="store_true", help="deliver right away: interrupt the running work session, "
                                                     "which continues immediately with the message")
    s.add_argument("--list", action="store_true", help="show messages not yet delivered")
    s.add_argument("--cancel", metavar="ID", help="withdraw a queued message (ids from --list)")
    s.set_defaults(func=cmd_say)

    s = sub.add_parser("ask", help="ask a question about the run and get an answer (running or finished)")
    s.add_argument("question", nargs="*", help="the question ('-' reads stdin)")
    s.add_argument("--agent", default="", help="agent to ask (default: [run].asker, else the reviewer)")
    s.add_argument("-q", "--quiet", action="store_true", help="print only the answer")
    s.add_argument("--history", action="store_true", help="show earlier questions and answers")
    s.set_defaults(func=cmd_ask)

    s = sub.add_parser("reload", help="approve your edits to GOAL.md / config.toml for the running supervisor")
    s.add_argument("files", nargs="*", metavar="goal|config", help="which files (default: those that changed)")
    s.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    s.set_defaults(func=cmd_reload)
    s = sub.add_parser("edit", help="edit the mission, config, plan or notes safely while it runs")
    s.add_argument("what", choices=["goal", "config", "plan", "notes"])
    s.set_defaults(func=cmd_edit)

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

    s = sub.add_parser("service", help="automatic start at boot / when down (systemd or cron)")
    s.add_argument("action", nargs="?", default="status", choices=["status", "install", "uninstall", "show"])
    s.add_argument("--kind", default="", choices=["", "systemd-system", "systemd-user", "cron"],
                   help="force a mechanism instead of detecting the best one")
    s.set_defaults(func=cmd_service)

    s = sub.add_parser("report", help="print a fresh progress report")
    s.add_argument("--short", action="store_true", help="a short update, like the periodic notifications")
    s.add_argument("--hours", type=float, default=24, help="with --short: summarize this many recent hours")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("logs", help="show the supervisor log (or the latest session log)")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("-s", "--session", action="store_true", help="latest agent session log instead")
    s.add_argument("-n", "--lines", type=int, default=60)
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("doctor", help="check the setup, including what could stop a long run")
    s.add_argument("--live", action="store_true", help="also send a tiny prompt to each agent")
    s.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # never crash on a character the terminal can't show
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    except BrokenPipeError:
        # Output piped into `head` or a pager that quit: not an error.
        try:
            sys.stdout = open(os.devnull, "w")  # silence the flush at exit
        except OSError:
            pass
        return 0
