"""Keeping the supervisor alive for weeks.

`bananavibe run` (and `start`, which runs it in the background) is a small guardian process that runs the
real supervisor as a child and restarts it whenever it dies for any reason other than finishing or being
stopped by the operator: a Python exception, `kill -9`, the kernel's OOM killer, or a hang (the supervisor
writes a heartbeat; if its main loop stops making progress, the guardian kills and restarts it). The
supervisor picks up from its state on disk, continuing the interrupted session, so nothing is lost.

Also here: systemd notifications (status line in `systemctl status`) and OOM-killer tuning.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from bananavibe.state import Lock, Paths, State, read_control, supervisor_pid
from bananavibe.util import Console, human_duration, iso, now, read_json, write_json

EXIT_RESTART = 75          # the supervisor asks to be restarted right away
EXIT_SIGNALLED = 75        # the guardian itself was terminated without an operator stop: restart me (systemd)
BACKOFF = (5, 15, 30, 60, 120, 300)
STARTUP_GRACE = 300        # seconds a new supervisor has to write its first heartbeat


def sd_notify(message: str) -> bool:
    """Send a notification to systemd, if we run under it with NotifyAccess enabled."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(message.encode("utf-8", errors="replace"))
        return True
    except OSError:
        return False


def protect_from_oom(value: int = -500) -> None:
    """Ask the OOM killer to spare this process (only possible with privileges; silently ignored otherwise)."""
    path = Path("/proc/self/oom_score_adj")
    try:
        if int(path.read_text().strip()) > value:
            path.write_text(str(value))
    except (OSError, ValueError):
        pass


def describe_exit(code: int) -> str:
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        hint = " (out of memory, or killed by hand)" if -code == signal.SIGKILL else ""
        return f"killed by {name}{hint}"
    if code == EXIT_RESTART:
        return "restart requested (watchdog)"
    return f"exited with code {code}"


class Guardian:
    def __init__(self, paths: Paths, child_argv: list[str], console: Console, watchdog_minutes: float = 60):
        self.paths = paths
        self.argv = child_argv
        self.console = console
        self.watchdog = watchdog_minutes * 60
        self.stopping = False
        self.child: subprocess.Popen | None = None
        self._beat_seen = ""
        self._beat_changed = 0.0
        self._child_started = 0.0  # wall-clock time the current supervisor was started
        self._operator_interrupt = False

    def _on_signal(self, signum, frame) -> None:
        self.stopping = True
        if signum == signal.SIGINT:
            self._operator_interrupt = True  # Ctrl-C in a terminal: a deliberate stop
        child = self.child
        if child and child.poll() is None:
            try:
                child.send_signal(signal.SIGTERM if signum != signal.SIGINT else signal.SIGINT)
            except OSError:
                pass

    def _notify(self, event: str, message: str) -> None:
        try:
            from bananavibe.config import ConfigError, load
            from bananavibe.notify import Notifier
            cfg = load(self.paths.config, self.paths.workspace)
        except (ConfigError, OSError, ImportError):
            return
        n = Notifier(cfg, str(self.paths.workspace), self.console)
        n.send(event, message)
        n.flush(30)

    def _record(self, kind: str, message: str) -> None:
        """Note the event in the run state (only while no supervisor is running, so there is one writer)."""
        if supervisor_pid(self.paths) or not self.paths.config.exists():
            return  # (without config.toml the run directory is gone: a fresh state.json would block its restore)
        try:
            state = State(self.paths)
            state.event(kind, message)
            if state["status"] in ("running", "waiting"):
                state["detail"] = f"supervisor {message}; restarting"
            state.save()
        except OSError:
            pass

    def _stop_requested(self) -> bool:
        control = read_control(self.paths)
        return bool(control.get("stop") or control.get("stop_now"))

    def _stopped_on_purpose(self) -> bool:
        """The supervisor ended because it was meant to: an operator stop, a budget, or a finished run."""
        if self._stop_requested():
            return True
        state = State(self.paths)
        if state.get("operator_stop"):
            return True
        if state["status"] == "done":
            try:
                from bananavibe.config import ConfigError, load
                return load(self.paths.config, self.paths.workspace).after_done == "exit"
            except (ConfigError, OSError):
                return True
        return False

    def _remember_operator_stop(self) -> None:
        """Ctrl-C reached the guardian (perhaps while no supervisor was running): make the stop stick."""
        from bananavibe.state import set_control
        try:
            set_control(self.paths, stop=True)
        except OSError:
            pass
        self._record_operator_stop()

    def _record_operator_stop(self) -> None:
        if supervisor_pid(self.paths) or not self.paths.config.exists():
            return
        try:
            state = State(self.paths)
            if not state.get("operator_stop"):
                state["operator_stop"] = True
                if state["status"] in ("running", "waiting", "crashed"):
                    state["status"] = "stopped"
                    state["detail"] = "stop requested by operator"
                state.save()
        except OSError:
            pass

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.stopping and not self._stop_requested():
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))

    def _hung(self, started: float) -> str:
        """Why the running supervisor looks hung, or "".

        Staleness is measured on this process's monotonic clock (from when the heartbeat last changed), not
        from the wall-clock time written in it, so a suspended laptop or a clock change is not a hang.
        """
        if not self.watchdog or not self.child:
            return ""
        t = time.monotonic()
        beat = read_json(self.paths.heartbeat, {})
        beat = beat if isinstance(beat, dict) else {}
        try:
            mtime = self.paths.heartbeat.stat().st_mtime
        except OSError:
            mtime = 0.0
        # A heartbeat from this supervisor: its own content, or the file touched since it started (that is how
        # it beats when the disk is too full to write).
        ours = beat.get("pid") == self.child.pid or mtime >= self._child_started
        if not ours:
            if t - started > STARTUP_GRACE:
                return f"no heartbeat {human_duration(STARTUP_GRACE)} after starting"
            return ""
        key = f"{beat.get('at')}|{mtime}"
        if key != self._beat_seen:
            self._beat_seen, self._beat_changed = key, t
        age = t - self._beat_changed
        if age > max(120.0, self.watchdog / 4):
            return f"no heartbeat for {human_duration(age)}"
        main_age = beat.get("main_age") if beat.get("pid") == self.child.pid else None
        if isinstance(main_age, (int, float)) and main_age + age > self.watchdog:
            return f"main loop stuck for {human_duration(main_age + age)}"
        return ""

    def run(self) -> int:
        lock = Lock(self.paths.guardian_lock, self.paths.guardian_lock_external)
        if not lock.acquire():
            print("bananavibe: another `bananavibe run` is already guarding this workspace", file=sys.stderr)
            return 2
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        protect_from_oom(-800)
        info = self.paths.root / "guardian.json"
        restarts = 0
        sd_notify("READY=1")
        try:
            try:
                write_json(info, {"pid": os.getpid(), "started": iso(now()), "host": socket.gethostname()},
                           durable=False)
            except OSError:
                pass
            while not self.stopping:
                started = time.monotonic()
                env = {**os.environ, "BANANAVIBE_GUARDIAN": str(os.getpid())}
                try:
                    # Its own process group: a Ctrl-C in the terminal reaches only the guardian, which passes
                    # exactly one SIGINT on (the supervisor treats a second one as "abort now").
                    self._child_started = time.time()
                    self.child = subprocess.Popen(self.argv, env=env, stdin=subprocess.DEVNULL, process_group=0)
                except OSError as e:
                    self.console.warn(f"guardian: cannot start the supervisor: {e}")
                    code = 1
                else:
                    reason = ""
                    while self.child.poll() is None:
                        time.sleep(2)
                        if not reason and not self.stopping:
                            reason = self._hung(started)
                            if reason:
                                self.console.warn(f"guardian: the supervisor looks hung ({reason}); restarting it")
                                self._terminate_child()
                    code = self.child.returncode if not reason else EXIT_RESTART
                if self.stopping:
                    # Terminated from outside. A deliberate stop (Ctrl-C, `bananavibe stop`) is final; anything
                    # else (a service manager or a stray `pkill`) exits with a code that asks to be restarted.
                    if self._operator_interrupt:
                        self._remember_operator_stop()
                    return 0 if self._stopped_on_purpose() else EXIT_SIGNALLED
                if code == 2 and supervisor_pid(self.paths):
                    self.console.info("guardian: another supervisor is running this workspace: exiting")
                    return 0
                if self._stopped_on_purpose():
                    if self._stop_requested():
                        self._record_operator_stop()
                    return 0
                ran = time.monotonic() - started
                restarts = 1 if ran > 600 else restarts + 1
                delay = 0 if code == EXIT_RESTART else BACKOFF[min(restarts - 1, len(BACKOFF) - 1)]
                what = describe_exit(code)
                self.console.warn(f"guardian: supervisor {what} after {human_duration(ran)}; "
                                  f"restarting in {delay}s (restart {restarts})")
                self._record("restart", f"{what} after {human_duration(ran)}")
                if restarts == 1 or restarts % 10 == 0:
                    self._notify("restart", f"supervisor {what} after {human_duration(ran)}; restarting it "
                                            f"automatically (restart {restarts}). The run continues where it was.")
                self._sleep(delay)
                if self._stop_requested() and not supervisor_pid(self.paths):
                    self.console.info("guardian: stop requested while waiting to restart: not restarting")
                    self._record_operator_stop()
                    return 0
            if self._operator_interrupt:
                self._remember_operator_stop()
            return 0 if self._stopped_on_purpose() else EXIT_SIGNALLED
        finally:
            sd_notify("STOPPING=1")
            info.unlink(missing_ok=True)
            lock.release()

    def _terminate_child(self) -> None:
        child = self.child
        if not child or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=180)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def guardian_pid(paths: Paths) -> int | None:
    """PID of the guardian of this workspace, if one is running."""
    from bananavibe.state import lock_holder
    pid = lock_holder(paths.guardian_lock)
    if pid is None and paths.guardian_lock_external:
        pid = lock_holder(paths.guardian_lock_external)
    return pid
