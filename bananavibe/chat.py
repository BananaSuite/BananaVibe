"""`bananavibe chat`: talk to a run while it works, without ever stopping it.

Plain lines are questions: a separate read-only agent answers them from the run's state, plan, journal and
live activity (the working agent is not disturbed). Slash commands send instructions and control the run:

    /say <text>     instructions for the agents (the running session can see them; every next session gets them)
    /now <text>     deliver right away: the work session is interrupted and continues at once with the message
    /status         where the run is          /log [n]   the last lines of the live transcript
    /pause [now]    /resume    /stop [now]    /start     /retry (skip a limit wait)
    /history        earlier questions and answers          /help    /quit (or Ctrl-D)
"""

from __future__ import annotations

import sys
import threading
import time

from bananavibe.config import Config
from bananavibe.state import Paths, State, read_json, set_control, supervisor_pid
from bananavibe.util import ascii_only, clean, existing_log, read_tail, read_text, use_color
from bananavibe.watch import Style, agent_from_log, latest_session_log, renderer_for

HELP = __doc__.split("\n\n", 2)[2].rstrip()
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class _Spinner:
    def __init__(self, label: str, stream=None):
        self.label = label
        self.out = stream or sys.stderr
        self.stop = threading.Event()
        self.tty = use_color(self.out)
        self.frames = "|/-\\" if ascii_only(self.out) else SPINNER
        self.thread = threading.Thread(target=self._spin, daemon=True)

    def _spin(self) -> None:
        started = time.monotonic()
        i = 0
        while not self.stop.wait(0.2):
            secs = int(time.monotonic() - started)
            self.out.write(f"\r{self.frames[i % len(self.frames)]} {self.label} {secs // 60}:{secs % 60:02d} "
                           "(Ctrl-C cancels) ")
            self.out.flush()
            i += 1

    def __enter__(self) -> _Spinner:
        if self.tty:
            self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        if self.tty:
            self.thread.join(1)
            self.out.write("\r\x1b[2K")
            self.out.flush()


def _transcript(paths: Paths, cfg: Config, n: int) -> list[str]:
    state = read_json(paths.state, {}) or {}
    current = state.get("current") or {}
    if current.get("log"):
        path, agent = existing_log(paths.workspace / current["log"]), current.get("agent", "")
    else:
        path = latest_session_log(paths)
        if not path:
            return ["(no sessions yet)"]
        agent = agent_from_log(path.name)
    renderer = renderer_for(cfg, agent, Style(use_color()), 8, ascii_only())
    lines: list[str] = []
    for raw in read_tail(path, 300_000).splitlines()[-1500:]:
        lines += renderer.render(raw)
    return lines[-n:]


def run(paths: Paths, cfg: Config, agent: str | None = None, stdin=None, out=None) -> int:
    from bananavibe.ask import ask
    from bananavibe.cli import _Colors, cmd_start, request_stop, send_message, status_lines

    stdin = stdin or sys.stdin
    out = out or sys.stdout
    interactive = stdin.isatty()
    color = _Colors(out)
    if interactive:
        try:
            import readline  # noqa: F401  (line editing and history for input())
        except ImportError:
            pass

    def say(text: str = "") -> None:
        print(text, file=out, flush=True)

    state = State(paths)
    say(color("1", f"BananaVibe chat · {cfg.name}") + f" · status {state['status']}: {state['detail']}")
    say("Ask anything about the run, or give instructions with /say (/now delivers immediately). /help for more.")
    while True:
        try:
            if interactive:
                line = input(color("1;36", "you> "))
            else:
                line = stdin.readline()
                if not line:
                    break
        except EOFError:
            break
        except KeyboardInterrupt:
            say()
            continue
        line = line.strip()
        if not line:
            continue
        cmd, _, rest = line.partition(" ")
        rest = rest.strip()
        if not line.startswith("/"):
            cancel = threading.Event()
            try:
                with _Spinner(f"asking {agent or cfg.asker_name}…"):
                    ok, answer = ask(paths, cfg, line, agent, verbose=False, cancel=cancel)
            except KeyboardInterrupt:
                cancel.set()
                say(color("33", "cancelled"))
                continue
            say(color("1;32" if ok else "31", "bananavibe> ") + clean(answer))
            continue
        cmd = cmd.lower()
        if cmd in ("/quit", "/exit", "/q"):
            break
        if cmd in ("/help", "/?"):
            say(HELP)
        elif cmd in ("/say", "/now"):
            if not rest:
                say(f"usage: {cmd} <message>")
            else:
                say(send_message(paths, rest, cmd == "/now"))
        elif cmd == "/status":
            for ln in status_lines(paths, 6, color, ascii_only()):
                say(ln)
        elif cmd == "/log":
            n = int(rest) if rest.isdigit() else 30
            for ln in _transcript(paths, cfg, n):
                say(ln)
        elif cmd == "/pause":
            if rest == "now":
                set_control(paths, pause=True, pause_now=True)
                say("Pausing now: the session is interrupted and continues on /resume.")
            else:
                set_control(paths, pause=True)
                say("Pausing after the current session (/pause now to interrupt it).")
        elif cmd == "/resume":
            set_control(paths, pause=None, pause_now=None)
            say("Resumed." if supervisor_pid(paths) else "Pause cleared. No supervisor is running: /start.")
        elif cmd == "/stop":
            say(request_stop(paths, rest == "now"))
        elif cmd == "/start":
            class _A:
                workspace = str(paths.workspace)
                again = False
                tmux = False
                auto = False
                no_autostart = False
            cmd_start(_A())
        elif cmd == "/retry":
            set_control(paths, retry_now=True)
            say("Cooldowns cleared: the agents are retried right away.")
        elif cmd == "/history":
            say(read_text(paths.answers).strip() or "No questions asked yet.")
        else:
            say(f"unknown command {cmd}; /help lists them")
    say()
    return 0
