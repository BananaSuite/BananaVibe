"""Running one agent session as a child process, with timeouts and clean shutdown."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from bananavibe.adapters import FATAL, TRANSIENT, Adapter, Outcome
from bananavibe.util import Console, clean

# Environment variables that must not leak into agents: they belong to the supervisor's own service manager.
_PRIVATE_ENV = ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID", "LISTEN_FDS", "LISTEN_PID", "LISTEN_FDNAMES",
                "BANANAVIBE_GUARDIAN")


@dataclass
class SessionResult:
    outcome: Outcome
    exit_code: int
    seconds: float
    timed_out: bool = False
    idle_killed: bool = False
    interrupted: bool = False
    session_id: str = ""                               # the agent's own conversation id, if it reports one
    activity: list[str] = field(default_factory=list)  # the last human-readable lines of the session


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_process_group(pgid: int, grace: float = 10) -> None:
    """SIGTERM a whole process group, then SIGKILL whatever is left after `grace` seconds."""
    if pgid <= 1:
        return
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 2)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not group_alive(pgid):
                return
            time.sleep(0.1)


def kill_group(proc: subprocess.Popen, grace: float = 15) -> None:
    """Terminate the child and everything it started (servers, test runners, ...)."""
    if proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def set_oom_score_adj(pid: int, value: int) -> bool:
    """Linux: how willing the OOM killer is to kill `pid` (-1000..1000). Raising it never needs privileges."""
    try:
        Path(f"/proc/{pid}/oom_score_adj").write_text(str(int(value)))
        return True
    except OSError:
        return False


def agent_env(extra: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _PRIVATE_ENV}
    env.update(extra)
    return env


def run_session(
    adapter: Adapter,
    prompt: str,
    prompt_file: Path,
    cwd: Path,
    log_path: Path,
    console: Console,
    timeout_s: float,
    idle_timeout_s: float,
    stop: threading.Event,
    *,
    read_only: bool = False,
    resume_id: str | None = None,
    on_session_id: Callable[[str], None] | None = None,
    on_start: Callable[[int], None] | None = None,
    on_tick: Callable[[], None] | None = None,
    oom_score_adj: int = 0,
    extra_env: dict[str, str] | None = None,
) -> SessionResult:
    inv = adapter.invocation(prompt, prompt_file, cwd, read_only=read_only, resume_id=resume_id)
    parser = adapter.parser()
    activity: deque[str] = deque(maxlen=40)
    env = agent_env({**inv.env, **(extra_env or {})})
    started = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc: subprocess.Popen | None = None
    with log_path.open("w", encoding="utf-8") as log:
        log.write("# argv: " + " ".join(a if len(a) < 200 else a[:200] + "..." for a in inv.argv) + "\n")
        log.flush()
        try:
            proc = subprocess.Popen(
                inv.argv, cwd=cwd, env=env, start_new_session=True,
                stdin=subprocess.PIPE if inv.stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as e:
            log.write(f"# failed to start: {e}\n")
            return SessionResult(Outcome(FATAL, f"cannot start {inv.argv[0]}: {e}"), -1, 0)

        try:
            if oom_score_adj:
                set_oom_score_adj(proc.pid, oom_score_adj)
            if on_start:
                on_start(proc.pid)
            return _supervise(proc, inv.stdin, parser, adapter, log, console, activity, started, timeout_s,
                              idle_timeout_s, stop, on_session_id, on_tick)
        finally:
            # Whatever happened (an exception, Ctrl-C, a full disk), never leave the agent running unsupervised.
            if proc.poll() is None:
                kill_group(proc, grace=5)
            # The agent may have left background processes in its group (dev servers, watchers).
            kill_process_group(proc.pid, grace=3)


def _supervise(proc, stdin_text, parser, adapter, log, console, activity, started, timeout_s, idle_timeout_s,
               stop, on_session_id, on_tick) -> SessionResult:
    if stdin_text is not None:
        def feed() -> None:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        threading.Thread(target=feed, daemon=True, name="agent-stdin").start()

    lines: queue.Queue[str | None] = queue.Queue(maxsize=10000)

    def read() -> None:
        try:
            for line in proc.stdout:
                lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)
    reader = threading.Thread(target=read, daemon=True, name="agent-stdout")
    reader.start()

    def consume(line: str) -> None:
        log.write(line)
        had_id = bool(parser.session_id)
        for shown in parser.feed(line.rstrip("\n")):
            shown = clean(shown)
            activity.append(shown)
            console.agent(adapter.name, shown)
        if parser.session_id and not had_id and on_session_id:
            on_session_id(parser.session_id)

    last_output = time.monotonic()
    timed_out = idle_killed = interrupted = False
    exited_quiet = 0.0
    while True:
        try:
            line = lines.get(timeout=1)
        except queue.Empty:
            line = ""
        if on_tick:
            on_tick()
        if line is None:
            break
        if line:
            last_output = time.monotonic()
            consume(line)
            # Drain what is already buffered before flushing, so chatty agents don't cost a write per line.
            for _ in range(500):
                try:
                    more = lines.get_nowait()
                except queue.Empty:
                    break
                if more is None:
                    lines.put(None)
                    break
                consume(more)
            log.flush()
        t = time.monotonic()
        if line == "" and proc.poll() is not None:
            # The agent exited but something it started in the background still holds the output pipe.
            exited_quiet = exited_quiet or t
            if t - exited_quiet > 5:
                kill_group(proc, grace=5)
                kill_process_group(proc.pid, grace=2)
                break
            continue
        exited_quiet = 0.0
        if stop.is_set():
            interrupted = True
        elif t - started > timeout_s:
            timed_out = True
        elif t - last_output > idle_timeout_s:
            idle_killed = True
        else:
            continue
        console.warn("stopping agent: " + ("interrupted" if interrupted else
                                             "session time limit reached" if timed_out else
                                             f"no output for {int(idle_timeout_s // 60)} minutes"))
        kill_group(proc)
        kill_process_group(proc.pid, grace=2)
        reader.join(timeout=10)
        while True:
            try:
                rest = lines.get_nowait()
            except queue.Empty:
                break
            if rest:
                log.write(rest)
                activity.extend(clean(x) for x in parser.feed(rest.rstrip("\n")))
        break

    try:
        exit_code = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        kill_group(proc, grace=1)
        exit_code = proc.wait()
    log.write(f"\n# exit code {exit_code}\n")
    log.flush()

    seconds = time.monotonic() - started
    if timed_out or idle_killed or interrupted:
        outcome = Outcome(TRANSIENT if idle_killed else "ok", final_text=parser.final_text,
                          message="idle" if idle_killed else "timeout" if timed_out else "interrupted",
                          usage=dict(parser.usage))
        return SessionResult(outcome, exit_code, seconds, timed_out, idle_killed, interrupted,
                             parser.session_id, list(activity))
    return SessionResult(parser.finish(exit_code), exit_code, seconds, session_id=parser.session_id,
                         activity=list(activity))
