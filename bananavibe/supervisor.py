"""The supervisor loop: restart agents until the goal is met, verified and independently reviewed.

One iteration = one agent session that ended normally (or ran out of time). Usage limits,
outages, crashes and login problems never end the run: the supervisor waits, fails over to
another configured agent, and tries again, forever, until the operator stops it.

Everything the loop needs to continue is kept on disk (`state.json`): the session in progress (so a
supervisor that was killed continues it after a restart), the post-session steps still to do (checks, the
verification of a done claim, a checkpoint review), the feedback for the next session, and the operator's
messages until a session has actually received them. Stopping, crashing, rebooting or moving the run to
another machine therefore never loses work or instructions.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import random
import re
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from bananavibe import gitops, housekeeping, report
from bananavibe.adapters import AUTH, FATAL, LIMIT, OK, TRANSIENT, make_adapter
from bananavibe.checks import failure_text, run_checks
from bananavibe.config import Config, ConfigError, loads
from bananavibe.guard import sd_notify
from bananavibe.notify import Notifier
from bananavibe.prompts import review_prompt, worker_prompt
from bananavibe.runner import SessionResult, group_alive, kill_process_group, run_session
from bananavibe.state import (
    Lock,
    Message,
    Paths,
    State,
    consume_messages,
    pending_messages,
    plan_stats,
    read_control,
    set_control,
    clear_control_if,
    take_control,
    write_heartbeat,
)
from bananavibe.util import (
    Console,
    boot_id,
    human_duration,
    is_disk_full,
    iso,
    now,
    parse_iso,
    pid_alive,
    process_has_marker,
    process_start,
    read_tail,
    read_text,
    short_time,
    tail,
    write_atomic,
)

MAX_COOLDOWN = dt.timedelta(days=7)
VERDICT_RE = re.compile(r"^\s*\**\s*VERDICT\s*:\s*\**\s*(APPROVE|CHANGES[_ ]REQUESTED)", re.I | re.M)
CONTINUE_AFTER = 60  # a failed session that ran at least this long is continued rather than restarted
SKIP_DIRS = {".git", ".bananavibe", "node_modules", ".venv", "venv", "__pycache__", "target", "dist", "build",
             ".next", ".cache", ".tox", ".mypy_cache", ".pytest_cache"}


class StopRun(Exception):
    pass


def stop_orphaned_session(cur: dict) -> bool:
    """Stop the agent of a session whose supervisor died, if it is still running. Returns True if it was.

    Only processes that are certainly that session's are touched: same boot, and the group leader's start
    time (or its session marker) must match, so a reused pid is never killed.
    """
    pgid = cur.get("pid")
    if not isinstance(pgid, int) or pgid <= 1 or not cur.get("boot") or cur.get("boot") != boot_id():
        return False  # after a reboot nothing from before can still run
    if pid_alive(pgid):
        ours = (bool(cur.get("pstart")) and process_start(pgid) == cur["pstart"]) or \
            process_has_marker(pgid, cur.get("marker", ""))
    else:
        ours = True  # the leader is gone, but what it started may still run in its process group
    if ours and group_alive(pgid):
        kill_process_group(pgid, grace=10)
        return True
    return False


def recover_protected_files(paths: Paths, repair_broken: bool = False) -> tuple[list[str], set[str]]:
    """Before a supervisor starts: undo changes an agent made to GOAL.md / config.toml during a session that
    was cut off by a crash (nothing could check them then). With `repair_broken` (automatic restarts, where
    nobody would see the error), also put back the last approved config.toml if the current one doesn't parse.

    Changes made while the run was stopped normally are the operator's and are kept, as are versions the
    operator approved with `reload` / `edit` that the supervisor had not applied yet.
    """
    state = State(paths)
    accepted = state.get("accepted") or {}
    if not isinstance(accepted, dict):
        return [], set()
    reload = read_control(paths).get("reload")
    reload = reload if isinstance(reload, dict) else {}
    messages, restored = [], set()
    # In flight: a session was running, or one ended but undoing its changes had failed.
    in_flight = bool(state.get("current")) or bool(state.get("restore_pending"))
    for key, path in (("goal", paths.goal), ("config", paths.config)):
        good = accepted.get(key)
        if not isinstance(good, str) or not good.strip():
            continue
        if not path.exists():  # deleted (by an agent, or code it wrote): never a change to keep
            write_atomic(path, good)
            restored.add(key)
            messages.append(f"restored the last approved {path.name} (it had been deleted)")
            continue
        text = read_text(path)
        if text == good:
            continue
        broken = False
        if key == "config" and repair_broken:
            try:
                loads(text, paths.workspace)
            except ConfigError:
                broken = True
        if not broken and (not in_flight or text == reload.get(key)):
            continue
        saved = paths.reverted / f"{path.name}.{now():%Y%m%d-%H%M%S}"
        write_atomic(saved, text)
        write_atomic(path, good)
        restored.add(key)
        why = "it no longer parses" if broken else "it was changed during a session cut off by a crash"
        messages.append(f"restored the last approved {path.name} ({why}); the changed version is in "
                        f"{paths.rel(saved)}")
    return messages, restored


class Finished(Exception):
    pass


class Supervisor:
    def __init__(self, paths: Paths, cfg: Config, console: Console):
        self.paths = paths
        self.cfg = cfg
        self.console = console
        self.state = State(paths)
        self.stop_event = threading.Event()  # end the run
        self.interrupt = threading.Event()   # cut the current agent session short (stop, pause or message now)
        self.interrupt_reason = ""
        self.host = socket.gethostname()
        self.adapters = {name: make_adapter(a) for name, a in cfg.agents.items()}
        self.notifier = Notifier(cfg, str(paths.workspace), console)
        self.repos: list[Path] = []
        self.feedback: list[str] = []
        self.goal_text = ""
        self.accepted: dict[str, str] = {}  # operator-approved content of GOAL.md and config.toml
        self.operator_stop = False
        self._signal_note = ""
        self._tick = time.monotonic()
        self._low_resources_since: float | None = None
        self._warned_big: set[str] = set()
        self._boot = boot_id()
        self._resource_override_until = 0.0
        self._carry: list[str] = []  # feedback produced during a session's attempts, for the session after it
        self._restored_messages: set[str] = set()  # queued messages deleted during the last attempt, put back

    @property
    def _restore_pending(self) -> bool:
        """An attempt ended but its changes to GOAL.md / config.toml are not undone yet (kept in state.json, so a
        restart in between doesn't take them for the operator's either)."""
        return bool(self.state.get("restore_pending"))

    @_restore_pending.setter
    def _restore_pending(self, value: bool) -> None:
        self.state["restore_pending"] = True if value else None

    @property
    def _before_attempt(self) -> tuple[set[str], set[str], float] | None:
        """The memory files and queued messages there were when the current (or last unrepaired) agent attempt
        or check run started (see housekeeping.run_files). Kept in state.json: if the supervisor dies before
        repairing what the attempt deleted, the repair happens after the restart."""
        value = self.state.get("before_attempt")
        if not isinstance(value, dict):
            return None
        return set(value.get("files") or []), set(value.get("messages") or []), float(value.get("t") or 0)

    @_before_attempt.setter
    def _before_attempt(self, value: tuple[set[str], set[str], float] | None) -> None:
        self.state["before_attempt"] = None if value is None else \
            {"files": sorted(value[0]), "messages": sorted(value[1]), "t": value[2]}

    def _start_attempt(self) -> None:
        """Note what exists before agent code runs (merged with an earlier note whose repair is still due)."""
        now_files = housekeeping.run_files(self.paths)
        earlier = self._before_attempt
        if earlier:
            now_files = (earlier[0] | now_files[0], earlier[1] | now_files[1], min(earlier[2], now_files[2]))
        self._before_attempt = now_files

    # ---------------------------------------------------------------- setup

    def _install_signals(self) -> None:
        def handler(signum, frame):
            # Keep this minimal: no printing or I/O here (it could interrupt a print and corrupt it).
            if self.stop_event.is_set():
                raise KeyboardInterrupt
            if signum == signal.SIGINT:
                self.operator_stop = True  # Ctrl-C in a terminal: the operator
            self._signal_note = (f"stop requested ({signal.Signals(signum).name}): finishing up "
                                 "(send it again to abort immediately)")
            self.interrupt_reason = self.interrupt_reason or "stop now"
            self.stop_event.set()
            self.interrupt.set()
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, signal.SIG_IGN)  # survive the terminal that started it closing

    def _flush_signal_note(self) -> None:
        if self._signal_note:
            note, self._signal_note = self._signal_note, ""
            self.console.warn(note)

    def _background(self, done: threading.Event) -> None:
        """Watcher thread: operator commands that must act mid-session, heartbeat, pings, periodic updates."""
        last_beat = last_ping = 0.0
        last_update = time.monotonic()
        last_status = ""
        while not done.wait(1):
            try:
                self._watch_control()
                t = time.monotonic()
                main_age = round(t - self._tick)
                if t - last_beat >= 15:
                    last_beat = t
                    write_heartbeat(self.paths, main_age=main_age, status=self.state.get("status"))
                    status = f"STATUS={self.state.get('status')}: {str(self.state.get('detail'))[:200]}"
                    if status != last_status:
                        last_status = status
                        sd_notify(status)
                healthy = not self.cfg.watchdog_minutes or main_age < self.cfg.watchdog_minutes * 60
                if self.cfg.ping_url and healthy and t - last_ping >= self.cfg.ping_every_minutes * 60:
                    last_ping = t
                    self.notifier.ping()
                if self.cfg.update_every_hours and t - last_update >= self.cfg.update_every_hours * 3600:
                    last_update = t
                    self._periodic_update()
            except Exception as e:  # the watcher must never die
                self.console.warn(f"watcher: {type(e).__name__}: {e}")

    def _watch_control(self) -> None:
        """Turn `stop --now`, `pause --now` and `say --now` into an interrupt of the running session."""
        control = read_control(self.paths)
        current = self.state.get("current") or {}
        if control.get("stop_now") and not self.stop_event.is_set():
            # The request stays recorded (as a plain stop) until an explicit start, so a crash or reboot in
            # the middle of stopping can't make autostart undo it.
            set_control(self.paths, stop=True, stop_now=None)
            self.operator_stop = True
            self.console.warn("stop requested by operator: interrupting the session")
            self.interrupt_reason = "stop now"
            self.stop_event.set()
            self.interrupt.set()
        elif control.get("pause_now") and current and not self.interrupt.is_set():
            self.console.info("pause requested by operator: interrupting the session")
            self.interrupt_reason = "pause now"
            self.interrupt.set()
        elif control.get("deliver_now") and current.get("label") == "work" and not self.interrupt.is_set():
            take_control(self.paths, "deliver_now")
            self.console.info("new message from the operator: interrupting the session to deliver it now")
            self.interrupt_reason = "operator message"
            self.interrupt.set()

    def _periodic_update(self) -> None:
        snapshot = State(self.paths)  # a consistent copy from disk, not the main thread's live object
        text = report.digest(self.paths, self.cfg, snapshot, self.repos, self.cfg.update_every_hours)
        path = self.paths.reports / "update-latest.md"
        write_atomic(path, report.render(self.paths, self.cfg, snapshot, self.repos, "periodic update"),
                     durable=False)
        self.notifier.send("update", text, {"BANANAVIBE_REPORT": str(path)})

    def _setup_repos(self) -> None:
        self.repos = gitops.discover(self.paths.workspace, self.cfg.git_repos)
        if gitops.is_repo(self.paths.workspace):
            gitops.exclude_path(self.paths.workspace, "/.bananavibe/")
            gitops.exclude_path(self.paths.workspace, "/.bananavibe.*/")
        if not self.repos:
            if self.cfg.git_snapshot:
                self.console.warn("no git repository found in the workspace: snapshots are disabled")
            return
        base = self.state["base_commits"]
        for repo in self.repos:
            if not gitops.is_repo(repo):
                self.console.warn(f"configured repository {repo} is not a git repository: skipped")
                continue
            if self.cfg.git_snapshot and self.cfg.git_branch:
                try:
                    gitops.ensure_branch(repo, self.cfg.git_branch)
                except RuntimeError as e:
                    # E.g. a rebase or merge left half-done by an interrupted session: the agents can fix that,
                    # so the run goes on (on the current branch) and they are told.
                    busy = gitops.operation_in_progress(repo)
                    self.console.warn(f"{repo.name}: {e}")
                    self.feedback.append(f"`{repo.name}` could not be switched to the work branch "
                                         f"`{self.cfg.git_branch}`" + (f" because a {busy} is in progress" if busy
                                                                        else "") + f": {tail(str(e), 400)}. Resolve "
                                         f"it first (finish or abort the {busy or 'operation'}), then switch to "
                                         f"`{self.cfg.git_branch}`.")
                    self.attention(f"branch:{repo}", f"{repo.name} can't be switched to {self.cfg.git_branch}: {e}")
            base.setdefault(str(repo), gitops.head(repo))
        self.repos = [r for r in self.repos if gitops.is_repo(r)]
        self.console.info("repositories: " + ", ".join(str(r) for r in self.repos)
                          + (f" (branch {self.cfg.git_branch})" if self.cfg.git_branch and self.cfg.git_snapshot else ""))

    def _startup(self) -> None:
        if self.state.load_error:
            self.console.warn(self.state.load_error)
            if self.paths.state.exists():
                kept = self.paths.root / f"state.json.corrupt-{now():%Y%m%d-%H%M%S}"
                try:
                    os.replace(self.paths.state, kept)
                    self.console.warn(f"the unreadable state file was kept as {self.paths.rel(kept)}")
                except OSError:
                    pass
            self.notify("attention", self.state.load_error)
        # A pending stop or pause is kept: only an explicit `start`/`run`/`resume` clears it, so a crash or a
        # reboot can't silently resume a run the operator stopped or paused. One-shot nudges are void.
        set_control(self.paths, retry_now=None, pause_now=None, deliver_now=None)
        # First, as every save writes it back to state.json.
        self.feedback = [f for f in self.state["feedback"] if isinstance(f, str)]
        if not self.feedback:
            pending = read_text(self.paths.feedback).strip()
            if pending:
                self.feedback = [pending]
        messages, restored = recover_protected_files(self.paths)
        for line in messages:
            self.console.warn(line)
        if "config" in restored:
            try:
                self._apply_config(loads(read_text(self.paths.config), self.paths.workspace))
            except ConfigError:
                pass
        self._restore_pending = False  # just checked by recover_protected_files
        self._take_reload()
        self.goal_text = read_text(self.paths.goal)
        self.accepted = {"goal": self.goal_text, "config": read_text(self.paths.config)}
        self.state["accepted"] = dict(self.accepted)
        housekeeping.remove_stale_temp_files(self.paths)
        housekeeping.protect_run_dir(self.paths)
        self.state["pid"] = os.getpid()
        self.state["host"] = self.host
        self.state["operator_stop"] = False
        self.state["started_at"] = self.state["started_at"] or iso(now())
        self.state["finished_at"] = None
        self._setup_repos()
        self._recover_lost_session()
        self.state.save(backup=True)
        housekeeping.backup_memory(self.paths)  # GOAL.md and config.toml are the approved versions right now

    def _recover_lost_session(self) -> None:
        """A previous supervisor died during a session (killed, OOM, reboot): clean up and continue it."""
        cur = self.state["current"]
        if not cur:
            return
        self.state["current"] = None
        self.state["current_agent"] = None
        if stop_orphaned_session(cur):
            self.console.warn(f"the {cur.get('label')} session of the previous supervisor was still running "
                              f"(process group {cur.get('pid')}): stopped it")
        agent = cur.get("agent", "?")
        log = self.paths.workspace / cur["log"] if cur.get("log") else None
        activity = []
        if log and agent in self.cfg.agents:
            parser = self.adapters[agent].parser()
            for line in read_tail(log, 400_000).splitlines():
                if not line.startswith("# "):
                    activity += parser.feed(line)
            if parser.session_id and not cur.get("session_id"):
                cur["session_id"] = parser.session_id
        self.console.warn(f"recovered a {cur.get('label')} session by {agent} that was cut off when the previous "
                          "supervisor stopped unexpectedly; it will be continued")
        self.state.record_session({
            "iteration": cur.get("iteration", self.state["iteration"] + 1), "label": cur.get("label", "work"),
            "agent": agent, "host": self.host, "session_id": cur.get("session_id", ""), "log": cur.get("log", ""),
            "started": cur.get("started"), "seconds": None, "outcome": "lost (supervisor died)",
        })
        self.state["interrupted"] = {
            "label": cur.get("label", "work"), "agent": agent, "host": self.host,
            "session_id": cur.get("session_id", ""), "started": cur.get("started"), "at": iso(now()),
            "activity": [str(a) for a in activity[-30:]], "reason": "the supervisor stopped unexpectedly",
        }
        self.state.event("recovered", f"continuing the {cur.get('label')} session cut off by a supervisor restart")
        if cur.get("label") == "work":
            made = self._snapshot(f"bananavibe: session {cur.get('iteration', '?')} by {agent} "
                                  "(cut off: supervisor restart)")
            if made:
                self.console.info(f"committed the work of the cut-off session in {made} repo(s)")

    # ---------------------------------------------------------------- helpers

    def _save(self) -> None:
        self.state["feedback"] = list(self.feedback)
        self.state.save()

    def _tick_now(self) -> None:
        self._tick = time.monotonic()

    def _set_status(self, status: str, detail: str = "", wait_until: dt.datetime | None = None) -> None:
        self.state["status"] = status
        self.state["detail"] = detail
        self.state["wait_until"] = iso(wait_until)
        self._save()

    def notify(self, event: str, message: str, extra_env: dict[str, str] | None = None) -> None:
        self.state.event(event, message)
        self._save()
        self.notifier.send(event, message, extra_env)

    def attention(self, key: str, message: str) -> None:
        """Something needs the operator. Repeated alerts about the same problem are spaced out."""
        self.state.event("attention", message)
        self._save()
        if not self.notifier.throttled(key):
            self.notifier.send("attention", message)

    def _check_control(self) -> None:
        """Apply commands left by `bananavibe pause/resume/stop/reload`. Raises StopRun to end the run."""
        self._tick_now()
        self._flush_signal_note()
        if self.stop_event.is_set():
            raise StopRun("stop requested by operator" if self.operator_stop else "interrupted")
        control = read_control(self.paths)
        if control.get("stop") or control.get("stop_now"):
            # Not consumed: it stays until an explicit start, so a crash while stopping can't undo it.
            self.operator_stop = True
            raise StopRun("stop requested by operator")
        if control.get("retry_now"):
            take_control(self.paths, "retry_now")
            for name in self.cfg.agents:
                self.state.agent(name)["cooldown_until"] = None
            self._resource_override_until = time.monotonic() + 1800
            self.console.info("operator asked to retry now: cooldowns cleared")
        if control.get("reload"):
            self._take_reload()
        if control.get("pause"):
            if control.get("pause_now"):
                take_control(self.paths, "pause_now")
            if not self.stop_event.is_set():
                self.interrupt.clear()
            if self.state["status"] != "paused":
                self.console.info("paused. Resume with `bananavibe resume`.")
                self._set_status("paused", "paused by operator")
            while read_control(self.paths).get("pause"):
                self._sleep(1, interruptible=False)
                control = read_control(self.paths)
                if control.get("stop") or control.get("stop_now") or self.stop_event.is_set():
                    self.operator_stop = True
                    raise StopRun("stop requested by operator")
                if control.get("reload"):
                    self._take_reload()
            self.console.info("resumed")
            self._set_status("running", "resumed")

    def _sleep(self, seconds: float, interruptible: bool = True) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._tick_now()
            if self.stop_event.is_set():
                self._flush_signal_note()
                raise StopRun("stop requested by operator" if self.operator_stop else "interrupted")
            if interruptible:
                control = read_control(self.paths)
                if control.get("stop") or control.get("pause") or control.get("retry_now") or control.get("reload"):
                    return
            time.sleep(min(2.0, max(0.0, end - time.monotonic())))

    def _wait_until(self, when: dt.datetime, why: str) -> None:
        seconds = (when - now()).total_seconds()
        if seconds <= 0:
            return
        self.console.info(f"{why}: waiting until {short_time(when)} ({human_duration(seconds)})")
        self._set_status("waiting", why, when)
        # Re-measured against the wall clock in short steps: after a suspend or clock change the wait ends at
        # the right time instead of being stretched.
        while True:
            left = (when - now()).total_seconds()
            if left <= 0:
                break
            self._sleep(min(left, 30))
            control = read_control(self.paths)
            if control.get("stop") or control.get("stop_now") or control.get("pause") or control.get("retry_now") \
                    or control.get("reload"):
                break
        self._check_control()

    def _over_budget(self) -> str | None:
        if self.cfg.max_iterations and self.state["iteration"] >= self.cfg.max_iterations:
            return f"reached max_iterations = {self.cfg.max_iterations}"
        started = parse_iso(self.state["started_at"])
        if self.cfg.max_hours and started and (now() - started).total_seconds() > self.cfg.max_hours * 3600:
            return f"reached max_hours = {self.cfg.max_hours:g}"
        return None

    def _workspace_fingerprint(self, limit: int = 50000) -> str:
        """Progress detection for workspaces without git: names, sizes and times of the files."""
        h = hashlib.sha1()
        n = 0
        for root, dirs, files in os.walk(self.paths.workspace):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for f in sorted(files):
                p = os.path.join(root, f)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                h.update(f"{p}\0{st.st_size}\0{st.st_mtime_ns}\0".encode(errors="replace"))
                n += 1
                if n >= limit:
                    return h.hexdigest()
        return h.hexdigest()

    def _tree_state(self) -> str:
        parts = [gitops.fingerprint(r) for r in self.repos] if self.repos else [self._workspace_fingerprint()]
        for p in (self.paths.plan, self.paths.notes):
            parts.append(hashlib.sha1(read_text(p).encode()).hexdigest())
        return "|".join(parts)

    # ---------------------------------------------------------------- protected files and live reload

    def _protected(self) -> dict[str, Path]:
        return {"goal": self.paths.goal, "config": self.paths.config}

    def _apply_config(self, cfg: Config) -> None:
        old = self.cfg
        self.cfg = cfg
        self.adapters = {name: make_adapter(a) for name, a in cfg.agents.items()}
        self.notifier.cfg = cfg
        if (cfg.git_repos, cfg.git_branch, cfg.git_snapshot) != (old.git_repos, old.git_branch, old.git_snapshot):
            self._setup_repos()

    def _apply_reload(self, request: dict) -> None:
        """New versions of GOAL.md / config.toml approved by the operator (`bananavibe reload` or `edit`)."""
        applied = []
        for key, path in self._protected().items():
            text = request.get(key)
            if not isinstance(text, str):
                continue
            if key == "config":
                try:
                    cfg = loads(text, self.paths.workspace)
                except ConfigError as e:
                    self.console.warn(f"reload: the new config.toml is invalid, keeping the old one: {e}")
                    self.state.event("attention", f"reload rejected: {e}")
                    continue
            if read_text(path) != text:
                write_atomic(path, text)
            self.accepted[key] = text
            self.state["accepted"] = dict(self.accepted)
            if key == "config":
                self._apply_config(cfg)
            else:
                self.goal_text = text
            applied.append(path.name)
        if applied:
            self.console.info("reloaded " + " and ".join(applied) + " (approved by the operator)")
            self.state.event("reload", "reloaded " + ", ".join(applied))
            self._save()

    def _take_reload(self) -> None:
        """Apply the versions approved with `reload` / `edit`. The request is cleared only once the new
        approved versions are saved in state.json (and only if no newer one arrived meanwhile): a crash in
        between must not lose the approval."""
        request = read_control(self.paths).get("reload")
        if not isinstance(request, dict):
            if request is not None:
                take_control(self.paths, "reload")
            return
        self._apply_reload(request)
        self._save()
        clear_control_if(self.paths, "reload", request)

    def _accept_operator_edits(self) -> None:
        """Between sessions no agent is running, so a changed protected file is the operator's edit: accept it.
        Unless restoring the files after the last attempt failed: then the change may be the agent's. A missing
        or empty file is never an edit (an agent, or code it wrote, deleted the run directory)."""
        try:
            self._ensure_run_dir(after_attempt=False)
        except OSError as e:
            self.console.warn(f"cannot restore the run's files yet ({e})")
            return
        if self._restore_pending:
            try:
                self._restore_protected()
            except OSError as e:
                if not self.notifier.throttled("restore-failed", 60):
                    self.console.warn(f"cannot restore GOAL.md / config.toml yet ({e}); not accepting changes to "
                                      "them until it works")
                return
        for key, path in self._protected().items():
            text = read_text(path)
            if text == self.accepted.get(key) or not path.exists():
                continue
            if not text.strip():
                if not self.notifier.throttled(f"empty-{key}", 60):
                    self.console.warn(f"{path.name} is empty: ignored (still using the previous version)")
                continue
            if key == "config":
                try:
                    cfg = loads(text, self.paths.workspace)
                except ConfigError as e:
                    if not self.notifier.throttled("bad-config-edit", 60):
                        self.console.warn(f"config.toml was edited but is invalid; still using the previous "
                                          f"configuration until it is fixed: {e}")
                    continue
                self._apply_config(cfg)
            else:
                self.goal_text = text
            self.accepted[key] = text
            self.state["accepted"] = dict(self.accepted)
            self.console.info(f"{path.name} was edited: the new version applies from the next session")
            self.state.event("reload", f"{path.name} edited by the operator")

    def _ensure_run_dir(self, after_attempt: bool = True) -> None:
        """Put back what an agent (or code it wrote) deleted from `.bananavibe/`.

        The state, mission, config, plan and pending commands are always put back. Memory files and queued
        messages only right after an attempt: between sessions, one that is missing was removed by the operator.
        """
        backup = self.paths.backup
        if not self.paths.control.exists() and backup and (backup / "control.json").is_file():
            # Pending commands (a stop, a pause, an approved edit) are mirrored outside the workspace.
            self.paths.root.mkdir(exist_ok=True)
            write_atomic(self.paths.control, read_text(backup / "control.json"), durable=False)
        lost = [p.name for p in (self.paths.state, self.paths.plan) if not p.exists()]
        fixed = []
        for key, path in self._protected().items():
            accepted = self.accepted.get(key)
            if accepted and accepted.strip() and not read_text(path).strip():
                # Missing or emptied: never a version to run with (nor an edit to accept).
                fixed.append(path.name)
                write_atomic(path, accepted)
        if fixed and not lost:
            self.console.warn(f"{' and '.join(fixed)} had been deleted or emptied: restored the approved version")
            self.state.event("restored", f"{', '.join(fixed)} deleted or emptied; restored")
            if after_attempt:
                note = (f"{' and '.join(f'`{n}`' for n in fixed)} in `.bananavibe/` had been deleted or emptied "
                        "and were restored. Never touch them.")
                self.feedback.append(note)
                self._carry.append(note)
        pending = self._before_attempt  # an attempt (or checks) ran: what disappeared since is put back
        restored: list[str] = []
        if lost or after_attempt or pending:
            # After a wipe everything that can come back does; after an attempt, what disappeared during it.
            restored = housekeeping.restore_memory(self.paths, self._restored_messages, None if lost else pending)
        self._before_attempt = None
        if not lost:
            if restored:
                self.console.warn(f"files of the run were deleted during the session; restored from the backup: "
                                  f"{', '.join(restored)}")
                self.state.event("restored", f"deleted during a session, restored: {', '.join(restored)}")
            return
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        restored += housekeeping.restore_missing(self.paths, ("service.json", "service.env"))
        self.state.save(backup=True)  # from memory
        housekeeping.protect_run_dir(self.paths)
        if gitops.is_repo(self.paths.workspace):
            gitops.exclude_path(self.paths.workspace, "/.bananavibe/")
        msg = (f"files of .bananavibe/ were deleted ({', '.join(lost)}); the state, mission and config were "
               "restored" + (f", and from the last backup: {', '.join(restored)}" if restored else ""))
        self.console.warn(msg)
        self.attention("rundir", msg)
        note = ("Files of `.bananavibe/` (the run's memory) were deleted during the previous session. Never "
                "delete, move or clean them (no `rm -rf .bananavibe`, no `git clean -x` without "
                "`-e .bananavibe`). They were restored from a backup: check that the plan and notes are complete.")
        self.feedback.append(note)
        self._carry.append(note)

    def _after_attempt(self) -> None:
        """Repair what an agent attempt may have broken. Until this has fully succeeded, a changed GOAL.md or
        config.toml is never taken for an edit by the operator (see `_accept_operator_edits`)."""
        self._restore_pending = True
        for step in (self._ensure_run_dir, self._restore_protected):
            try:
                step()
            except OSError as e:
                self.console.warn(f"could not check or restore the run's files ({step.__name__}): {e}")

    def _restore_protected(self) -> None:
        """After every agent attempt (completed, failed or interrupted): a protected file the agent changed is
        restored, and a copy of its version kept."""
        self._restore_pending = True
        self._take_reload()  # the operator approved a new version during the session
        for key, path in self._protected().items():
            text = read_text(path)
            accepted = self.accepted.get(key)
            if accepted is None or text == accepted:
                continue
            saved: Path | None = self.paths.reverted / f"{path.name}.{now():%Y%m%d-%H%M%S}"
            write_atomic(path, accepted)  # first: the restore matters more than the copy
            try:
                write_atomic(saved, text)
            except OSError:
                saved = None
            note = (f"`{self.paths.rel(path)}` was modified during the previous session (or the checks after it) and "
                    "has been restored. Never edit it, nor write code that does; record your thoughts in NOTES.md "
                    "instead.")
            self.feedback.append(note)
            self._carry.append(note)
            kept = f"The modified version is in {self.paths.rel(saved)}" if saved else \
                "The modified version could not be kept"
            self.console.warn(f"{path.name} was modified during the session or its checks: restored. {kept}; if that "
                              f"edit was yours, use `bananavibe edit {key}` or `bananavibe reload` next time")
            self.state.event("restored", f"{path.name} modified during a session; restored")
        self._restore_pending = False

    # ---------------------------------------------------------------- resources

    def _resources_ok(self) -> bool:
        """Wait (a while) instead of starting a session that would fail for lack of disk space or memory."""
        if time.monotonic() < self._resource_override_until:  # `bananavibe retry-now`: the operator says go
            self._low_resources_since = None
            return True
        problems = []
        free = housekeeping.disk_free_mb(self.paths.workspace)
        if free is not None and self.cfg.min_free_disk_mb and free < self.cfg.min_free_disk_mb:
            done = housekeeping.prune_logs(self.paths, 1, min(self.cfg.logs_max_mb or 500, 500))
            for d in done:
                self.console.info("low disk space: " + d)
            free = housekeeping.disk_free_mb(self.paths.workspace)
            if free is not None and free < self.cfg.min_free_disk_mb:
                problems.append(f"only {free:.0f} MB of disk space free (min_free_disk_mb = "
                                f"{self.cfg.min_free_disk_mb:g})")
        mem = housekeeping.memory_available_mb()
        if mem is not None and self.cfg.min_free_memory_mb and mem < self.cfg.min_free_memory_mb:
            problems.append(f"only {mem:.0f} MB of memory available (min_free_memory_mb = "
                            f"{self.cfg.min_free_memory_mb:g})")
        if not problems:
            self._low_resources_since = None
            return True
        text = "; ".join(problems)
        t = time.monotonic()
        if self._low_resources_since is None:
            self._low_resources_since = t
            self.attention("resources", f"{text}. Waiting for it to recover before starting a session.")
        if t - self._low_resources_since < 1800:
            self._wait_until(now() + dt.timedelta(seconds=120), text)
            return False
        # Waited long enough: try anyway, and let the agent help (it may be what filled the disk).
        self._low_resources_since = None
        self.feedback.append(f"The machine is low on resources: {text}. Free what you can (build artifacts, "
                             "caches, containers, stray processes you started) before continuing.")
        return True

    # ---------------------------------------------------------------- agents

    def _available(self, candidates: list[str]) -> tuple[str | None, dt.datetime | None]:
        """First candidate that is not cooling down, else the earliest time one becomes available."""
        earliest = None
        for name in candidates:
            until = parse_iso(self.state.agent(name)["cooldown_until"])
            if not until or until <= now():
                return name, None
            if earliest is None or until < earliest:
                earliest = until
        return None, earliest

    def _order_workers(self) -> list[str]:
        workers = list(self.cfg.workers)
        if self.cfg.strategy == "round-robin" and len(workers) > 1:
            k = self.state["iteration"] % len(workers)
            workers = workers[k:] + workers[:k]
        # After repeated stalls, prefer a different agent: a fresh pair of eyes.
        if self.state["stalls"] >= self.cfg.stall_limit and len(workers) > 1:
            k = (self.state["stalls"] // self.cfg.stall_limit) % len(workers)
            workers = workers[k:] + workers[:k]
        return workers

    def _register_failure(self, name: str, result: SessionResult) -> None:
        info = self.state.agent(name)
        info["failures"] += 1
        info["consecutive_failures"] += 1
        n = info["consecutive_failures"]
        oc = result.outcome
        info["last_error"] = f"{oc.kind}: {oc.message}"[:500]
        if oc.kind == LIMIT:
            if oc.retry_at and oc.retry_at > now():
                until = oc.retry_at + dt.timedelta(seconds=min(60, self.cfg.limit_fallback_seconds))
            else:
                until = now() + dt.timedelta(seconds=self.cfg.limit_fallback_seconds)
            if self.cfg.limit_recheck_seconds and not oc.retry_exact:
                # A reset time read from a message may be wrong: look again now and then.
                until = min(until, now() + dt.timedelta(seconds=self.cfg.limit_recheck_seconds))
            label = "usage/rate limit"
        elif oc.kind == AUTH:
            until = now() + dt.timedelta(seconds=self.cfg.auth_retry_seconds)
            label = "authentication problem (log the agent in again)"
            self.attention(f"{name}:auth", f"{name}: {label}: {oc.message[:300]}. Retrying every "
                                           f"{human_duration(self.cfg.auth_retry_seconds)}; "
                                           "`bananavibe retry-now` after fixing it.")
        elif oc.kind == FATAL:
            until = now() + dt.timedelta(seconds=self.cfg.retry_max_seconds)
            label = "configuration problem (check model name and flags)"
            self.attention(f"{name}:fatal", f"{name}: {label}: {oc.message[:300]}")
        else:
            delay = min(self.cfg.retry_initial_seconds * 2 ** min(n - 1, 30), self.cfg.retry_max_seconds)
            until = now() + dt.timedelta(seconds=delay * random.uniform(0.85, 1.15))
            label = "temporary failure (overload, outage, network or crash)"
            if n == 10:
                self.attention(f"{name}:transient", f"{name} has failed {n} times in a row; last error: "
                                                    f"{oc.message[:300]}")
        until = min(until, now() + MAX_COOLDOWN)
        info["cooldown_until"] = iso(until)
        self.console.warn(f"{name}: {label}: {tail(oc.message, 300) or 'exit ' + str(result.exit_code)}")
        self.console.warn(f"{name}: retrying after {short_time(until)} (attempt {n} failed)")
        self.state.event(oc.kind, f"{name}: {oc.message[:300]}")
        self._save()

    def _continuation(self, label: str, agent: str) -> tuple[str, str | None]:
        """Prompt note (and native session to resume) when the last session of this kind was cut short."""
        intr = self.state["interrupted"]
        if not intr:
            return "", None
        if intr.get("label") != label:
            # Something else runs next (e.g. a review was interrupted, a work session starts): nothing to continue.
            self.state["interrupted"] = None
            return "", None
        resume = None
        adapter = self.adapters.get(agent)
        if (self.cfg.resume_sessions and intr.get("session_id") and intr.get("agent") == agent
                and intr.get("host") == self.host and adapter and adapter.supports_resume):
            resume = intr["session_id"]
        activity = "\n".join(intr.get("activity") or []) or "(nothing recorded)"
        reason = intr.get("reason", "stop")
        by_operator = reason in ("pause now", "stop now", "operator message")
        cause = (f"interrupted by the operator ({reason})" if by_operator else f"cut off ({reason})")
        note = (f"\n# Continuing an interrupted session\n\nThe previous {label} session (by {intr.get('agent')}, "
                f"started {intr.get('started', '?')}) was {cause} before it finished. Whatever it changed is still "
                "in the workspace (and committed). Continue where it left off: check the current state of the files "
                "and the plan, then finish the job. "
                + ("This is the same conversation, resumed: you can rely on what you already know, but re-check "
                   "the files, they may have changed while it was stopped. " if resume else "")
                + ("The operator sent new messages (see above): read them first, they are why the session was "
                   "interrupted. " if reason == "operator message" else "")
                + f"Its last recorded activity:\n\n```\n{activity}\n```\n")
        return note, resume

    def _remember_interruption(self, label: str, agent: str, result: SessionResult, started: str,
                               reason: str) -> None:
        self.state["interrupted"] = {
            "label": label, "agent": agent, "host": self.host, "session_id": result.session_id,
            "started": started, "at": iso(now()), "activity": result.activity[-30:], "reason": reason,
        }

    def run_agent(self, candidates: list[str], make_prompt: Callable[[str], str], label: str,
                  on_partial: Callable[[str, SessionResult], None] | None = None,
                  on_success: Callable[[str, SessionResult], None] | None = None) -> tuple[str, SessionResult]:
        """Run one session with the first available agent, retrying through any failure until it succeeds.

        `on_success` updates the state for a completed session; it is saved together with the session's end."""
        while True:
            self._check_control()
            name, wait_until = self._available(candidates)
            if name is None:
                names = ", ".join(candidates)
                self._wait_until(wait_until, f"all agents ({names}) are cooling down")
                continue
            if not self._resources_ok():
                continue
            self._accept_operator_edits()
            note, resume_id = self._continuation(label, name)
            prompt = make_prompt(name) + note
            self.paths.prompts.mkdir(parents=True, exist_ok=True)
            prompt_file = self.paths.prompts / f"{label}.md"
            write_atomic(prompt_file, prompt, durable=False)
            iteration = self.state["iteration"] + 1
            log_path = self.paths.session_log(iteration, f"{label}-{name}")
            started = iso(now())
            if not self.stop_event.is_set():
                # A request that arrived as the previous session was ending must not cut this one short.
                self.interrupt.clear()
                self.interrupt_reason = ""
            self._restored_messages = set()
            self._start_attempt()
            self.state["attempts"] += 1
            self.state["current_agent"] = name
            self.state["session_started_at"] = started
            self.state["current"] = {"label": label, "agent": name, "log": self.paths.rel(log_path),
                                     "session_id": resume_id or "", "started": started, "iteration": iteration}
            self._set_status("running", f"{label} session with {name} (log: {self.paths.rel(log_path)})")
            self.console.info(f"▶ {label} session {iteration} with {name} (log: {self.paths.rel(log_path)})"
                              + (f", resuming {resume_id}" if resume_id else ""))

            def on_session_id(sid: str) -> None:
                if self.state["current"]:
                    self.state["current"]["session_id"] = sid
                    self._save()

            marker = f"{os.getpid()}-{time.monotonic_ns()}"  # tags the agent's processes (see recovery)

            def on_start(pid: int, marker: str = marker) -> None:
                if self.state["current"]:
                    # The pid first (a crash right now must still find the agent), then its start time.
                    self.state["current"].update(pid=pid, boot=self._boot, marker=marker)
                    self._save()
                    self.state["current"]["pstart"] = process_start(pid)
                    self._save()
                    # So that even if `.bananavibe/` and this supervisor both disappear during the session, the
                    # restored run still knows which agent to stop and which session to continue.
                    housekeeping.backup_state(self.paths)

            try:
                result = run_session(self.adapters[name], prompt, prompt_file, self.paths.workspace, log_path,
                                     self.console, self.cfg.session_timeout_minutes * 60,
                                     self.cfg.idle_timeout_minutes * 60, self.interrupt,
                                     resume_id=resume_id, on_session_id=on_session_id, on_start=on_start,
                                     on_tick=self._tick_now, oom_score_adj=self.cfg.agent_oom_score_adj,
                                     extra_env={"BANANAVIBE_SESSION": marker})
            finally:
                # Whatever happened to the attempt (even an exception), the agent's edits to protected files are
                # undone before anything else could take them for the operator's.
                self._after_attempt()
            self._flush_signal_note()
            self.state["current_agent"] = None
            self.state["current"] = None
            self.state.record_session({
                "iteration": iteration, "label": label, "agent": name, "host": self.host,
                "session_id": result.session_id or resume_id or "", "log": self.paths.rel(log_path),
                "started": started, "seconds": round(result.seconds),
                "outcome": "interrupted" if result.interrupted else result.outcome.kind,
            })
            info = self.state.agent(name)
            info["seconds"] = info.get("seconds", 0) + result.seconds
            usage = result.outcome.usage
            if usage.get("cost_usd"):
                info["cost_usd"] = round(info.get("cost_usd", 0.0) + usage["cost_usd"], 4)
            if usage.get("tokens"):
                info["tokens"] = info.get("tokens", 0) + usage["tokens"]
            if usage.get("windows"):
                info["windows"] = usage["windows"]
                info.setdefault("windows_at_start", usage["windows"])
            if result.interrupted:
                reason = self.interrupt_reason or ("stop now" if self.stop_event.is_set() else "pause now")
                self._remember_interruption(label, name, result, started, reason)
                if on_partial:
                    on_partial(name, result)
                if self.stop_event.is_set():
                    self._save()
                    raise StopRun("stop requested by operator" if self.operator_stop else "interrupted")
                # Paused or messaged now: the pause happens in _check_control, then the same session continues.
                self.interrupt.clear()
                self._save()
                if reason == "operator message":
                    self.console.info(f"{label} session interrupted to deliver a message; continuing it now")
                else:
                    self.console.info(f"{label} session interrupted for a pause; it continues after "
                                      "`bananavibe resume`")
                continue
            if result.outcome.kind == OK:
                info["sessions"] += 1
                info["consecutive_failures"] = 0
                info["cooldown_until"] = None
                for kind in ("auth", "fatal", "transient"):
                    self.notifier.reset(f"{name}:{kind}")
                self.state["interrupted"] = None
                if on_success:
                    on_success(name, result)
                self.console.info(f"■ {name} finished after {human_duration(result.seconds)}"
                                  + (" (session time limit)" if result.timed_out else ""))
                self._save()
                return name, result
            if resume_id and result.outcome.kind not in (LIMIT, AUTH) and result.seconds < CONTINUE_AFTER:
                # The conversation could not be resumed (expired, other login...): start it fresh, no penalty.
                self.console.warn(f"{name}: could not resume session {resume_id}; starting a fresh one")
                if self.state["interrupted"]:
                    self.state["interrupted"]["session_id"] = ""
                self._save()
                continue
            if result.idle_killed:
                result.outcome.message = (f"no output for {self.cfg.idle_timeout_minutes:g} minutes; "
                                          "the session was killed")
            if result.session_id and result.seconds >= CONTINUE_AFTER:
                # A session cut off by a limit, an outage or a hang after doing real work: continue it (in the
                # same conversation where the agent supports it) rather than starting over.
                kind = {LIMIT: "usage limit", AUTH: "login problem", FATAL: "configuration problem",
                        TRANSIENT: "outage or crash"}.get(result.outcome.kind, result.outcome.kind)
                failed_resumes = (self.state["interrupted"] or {}).get("failed_resumes", 0) if resume_id else 0
                if resume_id and result.outcome.kind not in (LIMIT, AUTH):
                    failed_resumes += 1
                self._remember_interruption(label, name, result, started,
                                            "hung, no output" if result.idle_killed else kind)
                self.state["interrupted"]["failed_resumes"] = failed_resumes
                if failed_resumes >= 2:
                    # The conversation keeps failing at the same point: the next attempt starts a fresh one
                    # (still told what this one was doing).
                    self.state["interrupted"]["session_id"] = ""
            self._register_failure(name, result)
            if on_partial:
                on_partial(name, result)

    # ---------------------------------------------------------------- iteration

    def _journal(self, iteration: int, agent: str, seconds: float, outcome: str, final_text: str,
                 complain: bool, key: str = "") -> None:
        """Append the session's handoff to the journal (then remove HANDOFF.md, never before).

        With a `key`, the entry is written only once: after a crash between writing it and recording that it
        was written, the retry finds it (and must not add a second one claiming HANDOFF.md was missing)."""
        marker = f"<!-- bv:{key} -->" if key else ""
        if marker and marker in read_tail(self.paths.journal, 400_000):
            self.paths.handoff.unlink(missing_ok=True)
            return
        handoff = read_text(self.paths.handoff).strip()
        header = f"## Session {iteration} · {agent} · {now().strftime('%Y-%m-%d %H:%M')} · " \
                 f"{human_duration(seconds)} · {outcome}" + (f" {marker}" if marker else "")
        if handoff:
            body = handoff
        else:
            body = "(no HANDOFF.md written)\n\nLast message from the agent:\n\n" + tail(final_text, 3000)
            if complain:
                self.feedback.append(f"The previous session did not write `{self.paths.rel(self.paths.handoff)}`. "
                                     "Always write it before you finish.")
        with self.paths.journal.open("a", encoding="utf-8") as f:
            f.write(f"\n{header}\n\n{body.strip()}\n")
            f.flush()
            os.fsync(f.fileno())
        if handoff:
            self.paths.handoff.unlink(missing_ok=True)

    def _enforce_branches(self) -> None:
        """Agents are told to stay on the work branch; if one switched away, switch back before committing."""
        branch = self.cfg.git_branch
        if not (self.cfg.git_snapshot and branch):
            return
        for repo in self.repos:
            cur = gitops.current_branch(repo)
            if not cur or cur == branch:
                continue
            r = gitops.git(repo, "switch", branch)
            if r.returncode == 0:
                self.console.warn(f"{repo.name} was left on {cur}: switched back to {branch}")
                self.feedback.append(f"The previous session left `{repo.name}` on `{cur}` instead of `{branch}`. The "
                                     f"supervisor switched it back (uncommitted changes came along). If you "
                                     f"committed work on `{cur}`, merge it into `{branch}`; stay on `{branch}`.")
            else:
                self.console.warn(f"{repo.name} is on {cur}, not {branch}, and could not be switched back")
                self.feedback.append(f"`{repo.name}` is on `{cur}` instead of `{branch}` and the supervisor could not "
                                     f"switch it back: {tail(r.stderr.strip(), 400)}. Commit or stash, switch "
                                     f"back to `{branch}` and merge your work there.")

    def _snapshot(self, message: str) -> int:
        if not self.cfg.git_snapshot:
            return 0
        made = 0
        for repo in self.repos:
            try:
                if repo == self.paths.workspace:
                    # Run memory never belongs in the project's history, even if an agent force-added it.
                    gitops.git(repo, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", ".bananavibe")
                skip = (".bananavibe",) if repo == self.paths.workspace else ()
                res = gitops.commit_all(repo, message, self.cfg.git_max_file_mb, exclude=skip)
                if res.commit:
                    made += 1
                new_big = [f for f in res.skipped if f"{repo}:{f}" not in self._warned_big]
                if new_big:
                    self._warned_big.update(f"{repo}:{f}" for f in new_big)
                    listing = ", ".join(new_big[:10]) + (" ..." if len(new_big) > 10 else "")
                    self.console.warn(f"{repo.name}: not committing files over {self.cfg.git_max_file_mb:g} MB: "
                                      f"{listing}")
                    self.feedback.append(f"These files in `{repo.name}` are larger than "
                                         f"{self.cfg.git_max_file_mb:g} MB and were not committed: {listing}. If they "
                                         "are build output, data or dependencies, add them to `.gitignore`; "
                                         "otherwise find a way to avoid them.")
                if res.error:
                    self.console.warn(f"snapshot of {repo.name} failed: {res.error}")
                    self.feedback.append(f"The supervisor could not commit the work in `{repo.name}`: {res.error}. "
                                         "Fix this first so your work is saved.")
                if self.cfg.git_push and self.cfg.git_branch:
                    err = gitops.push(repo, self.cfg.git_remote, self.cfg.git_branch)
                    if err:
                        self.console.warn(f"push failed for {repo.name}: {tail(err, 300)}")
            except (OSError, subprocess.SubprocessError) as e:
                if is_disk_full(e):
                    raise
                self.console.warn(f"snapshot failed for {repo}: {e}")
        return made

    def _write_feedback_file(self) -> str:
        text = "\n\n".join(self.feedback)
        if text:
            write_atomic(self.paths.feedback, text + "\n")
        else:
            self.paths.feedback.unlink(missing_ok=True)
        return text

    def work_iteration(self) -> None:
        feedback = self._write_feedback_file()
        before = self._tree_state()
        iteration = self.state["iteration"] + 1
        delivered: list[Message] = []

        def make_prompt(agent: str) -> str:
            messages = pending_messages(self.paths)
            delivered[:] = messages
            take_control(self.paths, "deliver_now")  # this session gets them all
            return worker_prompt(self.cfg, self.paths, self.repos, iteration, agent, feedback, messages,
                                 self.state["last_checks"], self.state.get("last_work_session"))

        def on_partial(agent: str, result: SessionResult) -> None:
            # A session cut off by a limit or outage may still have done useful work: keep it.
            if self._tree_state() != before:
                why = "interrupted" if result.interrupted else result.outcome.kind
                label = "interrupted by the operator" if result.interrupted else \
                    f"{result.outcome.kind}: {result.outcome.message[:200]}"
                self._journal(iteration, agent, result.seconds, label, result.outcome.final_text, False)
                self._snapshot(f"bananavibe: session {iteration} by {agent} (cut off: {why})")

        def on_success(agent: str, result: SessionResult) -> None:
            # Saved in the same write that ends the session: after a crash from then on, the steps below are
            # resumed, rather than the session being run again or forgotten.
            self.state["iteration"] = iteration
            self.state["last_work_session"] = {"started": session_started, "ended": iso(now())}
            self.feedback, self._carry = self._carry, []
            counts = self.state.get("redelivered") or {}
            # (At most 3 times: an agent that keeps deleting a message must not get it forever.)
            again = [m.id for m in delivered if m.id in self._restored_messages and counts.get(m.id, 0) < 3]
            self.state["redelivered"] = {mid: counts.get(mid, 0) + 1 for mid in again} | \
                {mid: n for mid, n in counts.items() if mid not in again and (self.paths.inbox_dir / mid).exists()}
            if again:
                # Messages deleted from the inbox during the session may never have been carried out: they stay
                # queued for the next one.
                self.feedback.append(f"{len(again)} of the operator's messages below were deleted from "
                                     "`.bananavibe/inbox/` during the previous session, which may not have carried "
                                     "them out: they are repeated. Skip what is already done.")
            self.state["pending"] = {
                "iteration": iteration, "agent": agent, "step": "post", "before": before, "progressed": None,
                "messages": [m.id for m in delivered if m.id not in again], "timed_out": result.timed_out,
                "seconds": round(result.seconds), "final_text": tail(result.outcome.final_text, 3000),
            }

        session_started = iso(now())
        self._carry = []
        self.run_agent(self._order_workers(), make_prompt, "work", on_partial, on_success)
        self._continue_pending()

    def _post_session(self, p: dict) -> None:
        """Bookkeeping after a completed work session (resumable: see `pending`)."""
        iteration, agent = p.get("iteration", self.state["iteration"]), p.get("agent", "?")
        if p.get("progressed") is None:  # measured before anything below changes the tree
            p["progressed"] = self._tree_state() != p.get("before") if p.get("before") else True
            self._save()
        # Messages count as delivered only once a session that received them has completed.
        consume_messages(self.paths, list(p.get("messages") or []))
        if not p.get("journaled"):
            label = "session time limit reached" if p.get("timed_out") else "ok"
            self._journal(iteration, agent, p.get("seconds", 0), label, p.get("final_text", ""),
                          complain=not p.get("timed_out"), key=f"session:{iteration}")
            p["journaled"] = True
            self._save()
        progressed = p.get("progressed", True)
        self._enforce_branches()
        made = self._snapshot(f"bananavibe: session {iteration} by {agent}")
        self.console.info(f"session {iteration}: {'changes saved' if made else 'progress recorded' if progressed else 'no changes'}"
                          + (f" in {made} repo(s)" if made else ""))
        if p.get("timed_out"):
            self.feedback.append("The previous session hit the session time limit and was stopped. Check its work, "
                                 "which may be half-finished, before continuing.")
        housekeeping.backup_memory(self.paths, settings=not self._restore_pending)

        if progressed or self.paths.done.exists():
            # A session that only verifies and then claims done is not a stall.
            self.state["stalls"] = 0
            self.notifier.reset("stalling")
        else:
            self.state["stalls"] += 1
            self.feedback.append(
                "The previous session ended without changing any file or the plan. Do not stop early: pick the next "
                "open plan item and make real progress. If you believe everything is finished, verify it rigorously "
                f"and then write `{self.paths.rel(self.paths.done)}`; if not, add the remaining work to the plan.")
            self.console.warn(f"no progress in session {iteration} ({self.state['stalls']} in a row)")
        p["step"] = "checks"
        self._save()
        if self.state["stalls"] > 2 * self.cfg.stall_limit:
            # Something is persistently wrong; don't burn through the quota in a tight loop.
            self.attention("stalling", f"agents keep ending sessions without progress ({self.state['stalls']} in a "
                                       "row). Consider `bananavibe say` with guidance, or check the plan.")
            delay = min(60 * self.state["stalls"], self.cfg.retry_max_seconds)
            self._wait_until(now() + dt.timedelta(seconds=delay), "agents keep stopping without progress")

    def _continue_pending(self) -> None:
        """The steps after a work session: checks, then verifying a done claim or a checkpoint review."""
        p = self.state["pending"]
        if not isinstance(p, dict) or "step" not in p:
            self.state["pending"] = None
            return
        iteration, agent = p.get("iteration", self.state["iteration"]), p.get("agent", "")
        if p["step"] == "post":
            self._post_session(p)
        if p["step"] == "checks":
            if self.cfg.checks:
                self._run_checks(iteration)
            p["step"] = "decide"
            self._save()
        if p["step"] == "decide":
            stats = plan_stats(read_text(self.paths.plan))
            implicit = (not self.paths.done.exists() and self.state["stalls"] >= self.cfg.stall_limit
                        and stats.total and not stats.open and not stats.blocked
                        and all(c["ok"] for c in self.state["last_checks"]))
            if implicit:
                self.console.info("plan complete and checks green but no DONE claim: treating it as one")
                write_atomic(self.paths.done, "Implicit claim by the supervisor: the plan is complete and checks "
                                              "pass.\n")
            if self.paths.done.exists():
                claim = read_text(self.paths.done)
                archive = self.paths.reports / f"done-claim-{iteration:04d}.md"
                write_atomic(archive, claim)
                p.update(step="verify", claim=self.paths.rel(archive))
                self._save()  # recorded before DONE.md goes away, so a crash here can't lose the claim
                self.paths.done.unlink(missing_ok=True)
            elif self.cfg.checkpoint_every and iteration - self.state["last_checkpoint"] >= self.cfg.checkpoint_every:
                p["step"] = "checkpoint"
            else:
                p["step"] = "none"
            self._save()
        try:
            if p["step"] == "verify":
                self.verify_done(iteration, agent, self.paths.workspace / p.get("claim", ""))
            elif p["step"] == "checkpoint":
                self.checkpoint(iteration, agent)
        except Finished:
            self.state["pending"] = None
            self._save()
            raise
        self.state["pending"] = None
        self._save()

    def _run_checks(self, iteration: int) -> list[dict]:
        self._set_status("running", "running checks")
        self.console.info("running checks: " + ", ".join(c.name for c in self.cfg.checks))
        # Recorded first: if the supervisor dies while the checks run, their changes to GOAL.md / config.toml are
        # still undone after the restart.
        self._restore_pending = True
        self._start_attempt()
        self._save()
        try:
            results = run_checks(self.cfg.checks, self.paths.workspace,
                                 self.paths.logs / f"{iteration:04d}-checks", self.stop_event, on_tick=self._tick_now)
        finally:
            self._after_attempt()  # checks run the project's code, written by the agents: same rules as a session
        if self.stop_event.is_set():
            raise StopRun("stop requested by operator" if self.operator_stop else "interrupted")
        self.state["last_checks"] = [{k: r[k] for k in ("name", "ok", "exit", "seconds")} for r in results]
        for r in results:
            (self.console.good if r["ok"] else self.console.warn)(
                f"check {r['name']}: {'PASS' if r['ok'] else 'FAIL'} ({r['seconds']}s)")
        failures = failure_text(results)
        if failures:
            self.feedback.append("These checks failed after the previous session. Fix the causes (do not weaken the "
                                 "checks):\n\n" + failures)
        self._save()
        return results

    # ---------------------------------------------------------------- review

    def _diff_summary(self) -> str:
        lines = []
        for repo in self.repos:
            base = self.state["base_commits"].get(str(repo), "")
            n = gitops.commits_since(repo, base)
            stat = gitops.diffstat(repo, base)
            lines.append(f"- {repo.name}: {n} commits; {stat or 'no changes'} (base {base[:10]})")
        return "\n".join(lines)

    def run_review(self, mode: str, iteration: int, worker: str,
                   claim_file: Path | None = None) -> tuple[str, str, str]:
        """Run a reviewer session. Returns (verdict, findings, reviewer)."""
        self.paths.review.unlink(missing_ok=True)
        preferred = [self.cfg.reviewer_name]
        others = [w for w in self.cfg.workers if w not in preferred and w != worker]
        candidates = preferred + others + [w for w in self.cfg.workers if w not in preferred + others]
        diff = self._diff_summary()

        def make_prompt(agent: str) -> str:
            return review_prompt(self.cfg, self.paths, self.repos, mode, self.state["last_checks"], diff, claim_file)

        reviewer, result = self.run_agent(candidates, make_prompt, f"review-{mode}")
        if reviewer != self.cfg.reviewer_name:
            self.console.warn(f"the reviewer {self.cfg.reviewer_name} is unavailable; {reviewer} reviewed instead")
            self.state.event("failover", f"review by {reviewer} instead of {self.cfg.reviewer_name}")
        text = read_text(self.paths.review).strip()
        if not text:
            text = result.outcome.final_text.strip()
        archived = self.paths.reports / f"review-{iteration:04d}-{mode}.md"
        archived.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(archived, text + "\n")
        self.paths.review.unlink(missing_ok=True)
        if self._snapshot(f"bananavibe: changes made during {mode} review by {reviewer}"):
            self.console.warn("the reviewer modified files; the changes were kept and committed")
        m = VERDICT_RE.search(text)
        verdict = m.group(1).upper().replace(" ", "_") if m else "NONE"
        return verdict, text, reviewer

    def verify_done(self, iteration: int, worker: str, archive: Path | None = None) -> None:
        if archive is None or not archive.is_file():
            archive = self.paths.reports / f"done-claim-{iteration:04d}.md"
            if self.paths.done.exists():
                write_atomic(archive, read_text(self.paths.done))
                self.paths.done.unlink(missing_ok=True)
        self.console.info(f"{worker} claims the mission is complete: verifying")
        self.notify("claim", f"session {iteration}: {worker} claims the mission is complete; verifying")

        stats = plan_stats(read_text(self.paths.plan))
        problems = []
        if stats.open or stats.blocked:
            items = "\n".join(f"- {t}" for t in stats.open_items[:30])
            problems.append(f"`{self.paths.rel(self.paths.plan)}` still has {stats.open} open and {stats.blocked} "
                            f"blocked items:\n{items}")
        if not stats.total:
            problems.append(f"`{self.paths.rel(self.paths.plan)}` is empty: write the plan and evidence for each item.")
        if self.cfg.checks and not all(c["ok"] for c in self.state["last_checks"]):
            problems.append("Not all checks pass (see above).")
        if pending_messages(self.paths):
            problems.append("The operator sent new instructions that no session has carried out yet (see the "
                            "messages above). Carry them out before claiming the mission is complete.")
        if problems:
            self.state["approvals"] = 0
            self.feedback.insert(0, "Your DONE claim was rejected by the supervisor:\n\n" + "\n\n".join(problems))
            self.console.warn("done claim rejected: " + "; ".join(p.splitlines()[0] for p in problems))
            self.state.event("rejected", "done claim rejected by supervisor checks")
            self._save()
            return

        verdict, findings, reviewer = self.run_review("final", iteration, worker, claim_file=archive)
        self.state["last_checkpoint"] = iteration
        if verdict == "APPROVE":
            self.state["approvals"] += 1
            n, need = self.state["approvals"], self.cfg.done_approvals
            self.console.good(f"{reviewer} approved the work ({n}/{need})")
            self.state.event("approved", f"{reviewer} approved ({n}/{need})")
            if n >= need and pending_messages(self.paths):
                # Instructions arrived while the work was being reviewed: they come first.
                self.state["approvals"] = 0
                self.console.info("approved, but new operator messages arrived meanwhile: back to work")
                self.feedback.insert(0, "The work was approved, but the operator sent new instructions in the "
                                        "meantime (see the messages above). Carry them out, verify everything "
                                        "again, then claim the mission complete again.")
                self._save()
                return
            if n >= need:
                raise Finished(f"mission complete: approved {n} time(s), last by {reviewer}")
            self.feedback.insert(0, (
                f"An independent reviewer approved the work (approval {n} of {need} required). Before the run can "
                "finish, do one more complete verification pass with fresh eyes: re-read the mission line by line, "
                "rebuild and re-test everything from a clean state, exercise every feature for real, and hunt for "
                "anything missed or fragile. Fix and add to the plan whatever you find. If, and only if, you find "
                f"nothing, write `{self.paths.rel(self.paths.done)}` again with the evidence.\n\nReviewer notes:\n\n"
                + tail(findings, 4000)))
        else:
            self.state["approvals"] = 0
            self.console.warn(f"{reviewer} requested changes" if verdict != "NONE" else
                              f"{reviewer} gave no verdict; treating it as changes requested")
            self.state.event("changes", f"{reviewer} requested changes")
            self.feedback.insert(0, (
                "Your DONE claim was reviewed by an independent reviewer and rejected. Add every finding below to "
                "the plan as open items, fix them all, and verify. Findings:\n\n" + tail(findings, 10000)))
        self._save()
        report.write(self.paths, self.cfg, self.state, self.repos, f"verification after session {iteration}")

    def checkpoint(self, iteration: int, worker: str) -> None:
        self.console.info(f"checkpoint after session {iteration}")
        if self.cfg.review_at_checkpoint:
            verdict, findings, reviewer = self.run_review("checkpoint", iteration, worker)
            self.feedback.append(f"Checkpoint review by {reviewer}. Add these findings to the plan where they are "
                                 "valid and address them by priority:\n\n" + tail(findings, 10000))
        self.state["last_checkpoint"] = iteration
        path = report.write(self.paths, self.cfg, self.state, self.repos, f"checkpoint after session {iteration}")
        self.notify("checkpoint", f"checkpoint after session {iteration}: {path}", {"BANANAVIBE_REPORT": str(path)})
        self._save()
        if self.cfg.pause_at_checkpoint:
            set_control(self.paths, pause=True)
            self.console.info(f"paused at checkpoint. Read {self.paths.rel(path)}, then `bananavibe resume`.")

    # ---------------------------------------------------------------- main

    def _housekeeping(self) -> None:
        protect = set()
        for key in ("current", "interrupted"):
            entry = self.state[key] or {}
            if entry.get("log"):
                protect.add(str(self.paths.workspace / entry["log"]))
        for line in housekeeping.prune_logs(self.paths, self.cfg.logs_compress_after, self.cfg.logs_max_mb, protect):
            self.console.info("logs: " + line)
        if housekeeping.rotate_journal(self.paths):
            self.console.info("moved older journal entries to JOURNAL.archive.md")

    def _finish(self, f: Finished) -> None:
        self._write_feedback_file()
        self.state["finished_at"] = iso(now())
        self._set_status("done", str(f))
        path = report.write(self.paths, self.cfg, self.state, self.repos, "final report")
        self.console.good(f"✅ {f}. Final report: {self.paths.rel(path)}")
        self.notify("done", f"{f}. Report: {path}", {"BANANAVIBE_REPORT": str(path)})

    def _wait_for_new_work(self) -> None:
        """after_done = "wait": stay up, and continue as soon as the operator sends new instructions."""
        self.console.info("waiting for new instructions (`bananavibe say \"...\"`); `bananavibe stop` to exit")
        detail = str(self.state["detail"] or "mission complete").split("; waiting for new")[0]
        self._set_status("done", f"{detail}; waiting for new instructions")
        while not pending_messages(self.paths):
            self._sleep(2, interruptible=False)
            control = read_control(self.paths)
            if control.get("stop") or control.get("stop_now"):
                self.operator_stop = True
                raise StopRun("stop requested by operator")
            if control.get("reload"):
                self._take_reload()
        self.console.info("new instructions received: continuing the run")
        self.state["approvals"] = 0
        self.state["finished_at"] = None
        self._set_status("running", "new instructions received")
        self.notify("start", "new instructions received: the run continues")

    def _disk_full(self, e: OSError) -> None:
        self.console.warn(f"the disk is full ({e}); cleaning up logs and waiting")
        try:
            housekeeping.prune_logs(self.paths, 1, 200)
        except OSError:
            pass
        if not self.notifier.throttled("disk-full"):
            self.notifier.send("attention", f"the disk is full on {self.host}: {e}. The run waits until space is "
                                            "freed (it retries every 5 minutes).")
        try:
            self._set_status("waiting", "disk full: free some space", now() + dt.timedelta(minutes=5))
        except OSError:
            pass
        # A plain wait that only a stop (or `retry-now`) cuts short: pending pauses or reloads must not turn it
        # into a busy loop.
        end = time.monotonic() + 300
        while time.monotonic() < end:
            self._tick_now()
            control = read_control(self.paths)
            if self.stop_event.is_set() or control.get("stop") or control.get("stop_now"):
                self.operator_stop = self.operator_stop or bool(control.get("stop") or control.get("stop_now"))
                raise StopRun("stop requested by operator" if self.operator_stop else "interrupted")
            if control.get("retry_now"):
                take_control(self.paths, "retry_now")
                return
            time.sleep(2)

    def run(self) -> int:
        try:
            self.paths.logs.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.console.warn(f"cannot create {self.paths.logs}: {e}")
            return 2
        lock = Lock(self.paths.lock, self.paths.lock_external)
        if not lock.acquire():
            self.console.warn("another supervisor is already running in this workspace")
            return 2
        self._install_signals()
        watcher_done = threading.Event()
        crashed = False
        try:
            was_done = self.state["status"] == "done"
            # The watcher (heartbeat, stop now) runs from the start, so a slow or blocked startup is not taken
            # for a hang.
            threading.Thread(target=self._background, args=(watcher_done,), daemon=True, name="watcher").start()
            while True:
                try:
                    self._startup()
                    break
                except OSError as e:
                    if not is_disk_full(e):
                        raise
                    self._disk_full(e)
            if was_done and self.cfg.after_done == "wait" and not pending_messages(self.paths):
                # A finished run that stays available: wait for new instructions instead of exiting.
                self._wait_for_new_work()
            else:
                if was_done:
                    self.state["approvals"] = 0
                    self.state["finished_at"] = None
                self._set_status("running", "started")
                self.notify("start", f"run '{self.cfg.name}' started at iteration {self.state['iteration']} "
                                     f"on {self.host}")
            if self.state["interrupted"]:
                self.console.info(f"the interrupted {self.state['interrupted']['label']} session will be continued")
            if self.state["pending"]:
                self.console.info(f"continuing after session {self.state['pending'].get('iteration')}: "
                                  f"{self.state['pending'].get('step')}")
            self.console.info(f"run '{self.cfg.name}': workers {', '.join(self.cfg.workers)}, reviewer "
                              f"{self.cfg.reviewer_name}, {len(self.cfg.checks)} check(s)")
            while True:
                try:
                    self._check_control()
                    reason = self._over_budget()
                    if reason:
                        raise StopRun(reason)
                    if self.state["pending"]:
                        self._continue_pending()
                    else:
                        self.work_iteration()
                    self._housekeeping()
                except Finished as f:
                    self._finish(f)
                    if self.cfg.after_done != "wait":
                        return 0
                    self._wait_for_new_work()
                except OSError as e:
                    if not is_disk_full(e):
                        raise
                    self._disk_full(e)
        except StopRun as stop:
            reason = str(stop)
            control = read_control(self.paths)
            if control.get("stop") or control.get("stop_now"):
                self.operator_stop = True  # e.g. `stop --now`, whose signal arrived before the watcher saw it
                reason = "stop requested by operator"
            # Stopped on purpose (by the operator, or a max_hours/max_iterations limit): autostart leaves it
            # stopped. Recorded first, before anything else that could fail.
            self.state["operator_stop"] = self.operator_stop or reason.startswith("reached max_")
            try:
                self._save()
            except OSError:
                pass
            try:
                self._snapshot("bananavibe: work in progress at stop")
            except OSError as e:
                self.console.warn(f"final snapshot failed: {e}")
            try:
                self._write_feedback_file()
            except OSError:
                pass
            if self.state["status"] == "done":
                self._set_status("done", self.state["detail"].split("; waiting for new")[0])
            else:
                self._set_status("stopped", reason)
            path = report.write(self.paths, self.cfg, self.state, self.repos, f"stopped: {reason}")
            self.console.info(f"stopped ({reason}). Report: {self.paths.rel(path)}. Continue with `bananavibe start`.")
            self.notify("stopped", reason)
            return 0
        except KeyboardInterrupt:
            crashed = True
            self.state["operator_stop"] = self.operator_stop
            try:
                self._set_status("stopped", "aborted")
            except OSError:
                pass
            return 130
        except Exception as e:  # record it before dying, so status shows why
            crashed = True
            try:
                self._set_status("crashed", f"{type(e).__name__}: {e}")
                self.attention("crash", f"supervisor crashed: {type(e).__name__}: {e}"
                               + (" (it restarts automatically)" if os.environ.get("BANANAVIBE_GUARDIAN") else ""))
            except Exception:
                pass
            raise
        finally:
            watcher_done.set()
            self.state["pid"] = None
            if not crashed:
                # After a crash the session in progress stays recorded: the next start continues it.
                self.state["current"] = None
                self.state["current_agent"] = None
            try:
                self._save()
            except OSError:
                pass
            housekeeping.backup_state(self.paths)  # e.g. that it was stopped on purpose
            self.notifier.flush(20)
            lock.release()
