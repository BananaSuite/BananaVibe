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

from bananavibe.adapters import TRANSIENT, Adapter, Outcome
from bananavibe.util import Console


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
) -> SessionResult:
    inv = adapter.invocation(prompt, prompt_file, cwd, read_only=read_only, resume_id=resume_id)
    parser = adapter.parser()
    activity: deque[str] = deque(maxlen=40)
    env = {**os.environ, **inv.env}
    started = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        log.write("# argv: " + " ".join(a if len(a) < 200 else a[:200] + "..." for a in inv.argv) + "\n")
        try:
            proc = subprocess.Popen(
                inv.argv, cwd=cwd, env=env, start_new_session=True,
                stdin=subprocess.PIPE if inv.stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as e:
            log.write(f"# failed to start: {e}\n")
            return SessionResult(Outcome("fatal", f"cannot start {inv.argv[0]}: {e}"), -1, 0)

        if inv.stdin is not None:
            def feed() -> None:
                try:
                    proc.stdin.write(inv.stdin)
                    proc.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass
            threading.Thread(target=feed, daemon=True).start()

        lines: queue.Queue[str | None] = queue.Queue()

        def read() -> None:
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()

        last_output = time.monotonic()
        timed_out = idle_killed = interrupted = False
        eof = False
        exited_quiet = 0.0
        while not eof:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                line = ""
            if line is None:
                eof = True
            elif line:
                last_output = time.monotonic()
                log.write(line)
                log.flush()
                had_id = bool(parser.session_id)
                for shown in parser.feed(line.rstrip("\n")):
                    activity.append(shown)
                    console.agent(adapter.name, shown)
                if parser.session_id and not had_id and on_session_id:
                    on_session_id(parser.session_id)
            t = time.monotonic()
            if eof:
                break
            if line == "" and proc.poll() is not None:
                # The agent exited but something it started in the background still holds the output pipe.
                exited_quiet = exited_quiet or t
                if t - exited_quiet > 5:
                    kill_group(proc, grace=5)
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
            reader.join(timeout=10)
            while True:
                try:
                    rest = lines.get_nowait()
                except queue.Empty:
                    break
                if rest:
                    log.write(rest)
                    activity.extend(parser.feed(rest.rstrip("\n")))
            break

        try:
            exit_code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            kill_group(proc, grace=1)
            exit_code = proc.wait()
        # The agent may have left background processes in its group (dev servers, watchers).
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        log.write(f"\n# exit code {exit_code}\n")

    seconds = time.monotonic() - started
    if timed_out or idle_killed or interrupted:
        outcome = Outcome(TRANSIENT if idle_killed else "ok", final_text=parser.final_text,
                          message="idle" if idle_killed else "timeout" if timed_out else "interrupted",
                          usage=dict(parser.usage))
        return SessionResult(outcome, exit_code, seconds, timed_out, idle_killed, interrupted,
                             parser.session_id, list(activity))
    return SessionResult(parser.finish(exit_code), exit_code, seconds, session_id=parser.session_id,
                         activity=list(activity))
