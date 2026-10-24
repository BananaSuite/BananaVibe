"""`bananavibe dashboard`: a full-screen live view of the run, with keys to control it.

Plain ANSI (no curses): it redraws in place once a second in the terminal's alternate screen, cuts every line
to the terminal width (counting wide characters properly), and always restores the terminal on exit, even
after Ctrl-C or an error. Agent output is sanitized, so escape sequences in tool output can't garble it.
"""

from __future__ import annotations

import os
import select
import shutil
import signal
import sys
import termios
import threading
import time
import tty

from bananavibe.config import Config
from bananavibe.state import Paths, State, read_json, set_control, supervisor_pid
from bananavibe.util import (
    ascii_only,
    clean,
    existing_log,
    fit_styled,
    human_duration,
    now,
    parse_iso,
    read_tail,
    strip_styles,
    symbol,
    text_width,
    use_color,
)
from bananavibe.watch import Style, agent_from_log, latest_session_log, renderer_for

KEYS = [("p", "pause"), ("P", "now"), ("r", "resume"), ("s", "say"), ("n", "say now"), ("a", "ask"),
        ("R", "retry"), ("x", "stop"), ("q", "quit")]


class Terminal:
    """Raw-ish terminal handling: alternate screen, hidden cursor, single-key input, guaranteed restore."""

    def __init__(self, stream=None):
        self.out = stream or sys.stdout
        self.fd = sys.stdin.fileno()
        self.saved = None

    def __enter__(self) -> Terminal:
        self.saved = termios.tcgetattr(self.fd)
        self._enter()
        self._old_handlers = {}
        for sig, handler in ((signal.SIGTERM, self._on_exit), (signal.SIGHUP, self._on_exit),
                             (signal.SIGTSTP, self._on_suspend), (signal.SIGCONT, self._on_continue)):
            self._old_handlers[sig] = signal.signal(sig, handler)
        return self

    def __exit__(self, *exc) -> None:
        for sig, handler in getattr(self, "_old_handlers", {}).items():
            signal.signal(sig, handler)
        self._leave()

    def _enter(self) -> None:
        tty.setcbreak(self.fd)
        self.write("\x1b[?1049h\x1b[?25l\x1b[H\x1b[2J")

    def _leave(self) -> None:
        self.cooked()
        self.write("\x1b[?1049l")

    def _on_exit(self, signum, frame) -> None:
        raise KeyboardInterrupt  # unwinds through __exit__, which restores the terminal

    def _on_suspend(self, signum, frame) -> None:
        # Ctrl-Z: give the shell a normal terminal back, then really stop.
        self._leave()
        signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTSTP)

    def _on_continue(self, signum, frame) -> None:
        signal.signal(signal.SIGTSTP, self._on_suspend)
        self._enter()

    def write(self, text: str) -> None:
        try:
            self.out.write(text)
            self.out.flush()
        except (OSError, ValueError):
            pass

    def cooked(self) -> None:
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        self.write("\x1b[?25h\x1b[0m")

    def raw(self) -> None:
        tty.setcbreak(self.fd)
        self.write("\x1b[?25l")

    def key(self, timeout: float) -> str | None:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        data = os.read(self.fd, 64).decode("utf-8", errors="replace")
        if data.startswith("\x1b"):
            return None  # arrow keys and other escape sequences: ignored
        return data[:1] or None

    def ask_line(self, label: str, row: int) -> str:
        """Read a line of text at the bottom of the screen (normal line editing)."""
        self.cooked()
        self.write(f"\x1b[{row};1H\x1b[2K{label}")
        try:
            return input()
        except (EOFError, KeyboardInterrupt):
            return ""  # Ctrl-C while typing cancels the input, not the dashboard
        finally:
            self.raw()


class Dashboard:
    def __init__(self, paths: Paths, cfg: Config, interval: float = 1.0):
        self.paths = paths
        self.cfg = cfg
        self.interval = max(0.2, interval)
        self.color = use_color()
        self.asc = ascii_only()
        self.style = Style(self.color)
        self.notice = ""
        self.notice_until = 0.0
        self._cache: tuple = ()
        self._cached_lines: list[str] = []

    def s(self, code: str, text: str) -> str:
        return self.style(code, text)

    def say(self, text: str, seconds: float = 6) -> None:
        self.notice, self.notice_until = text, time.monotonic() + seconds

    def transcript(self, max_lines: int) -> tuple[str, list[str]]:
        state = read_json(self.paths.state, {}) or {}
        current = state.get("current") or {}
        if current.get("log"):
            path = existing_log(self.paths.workspace / current["log"])
            agent = current.get("agent", "")
            title = f"live: {current.get('label', '')} session {current.get('iteration', '')} with {agent}"
        else:
            path = latest_session_log(self.paths)
            if not path:
                return "no sessions yet", []
            agent = agent_from_log(path.name)
            title = f"last session: {path.name}"
        try:
            st = path.stat()
            key = (str(path), st.st_size, st.st_mtime_ns, max_lines)
        except OSError:
            return title, []
        if key != self._cache:
            renderer = renderer_for(self.cfg, agent, self.style, 6, self.asc)
            lines: list[str] = []
            for raw in read_tail(path, 300_000).splitlines()[-1500:]:
                lines += renderer.render(raw)
            self._cache, self._cached_lines = key, lines[-max_lines:] if max_lines > 0 else []
        return title, self._cached_lines

    def frame(self) -> list[str]:
        from bananavibe.cli import _Colors, status_lines
        width, height = shutil.get_terminal_size((100, 30))
        width = max(20, width)
        rule = symbol("rule", self.asc)
        state = State(self.paths)
        started = state["started_at"]
        dot = symbol("dot", self.asc)
        clock = f" {now():%a %H:%M:%S} "
        title = fit_styled(f" BananaVibe {dot} {self.cfg.name} {dot} {self.paths.workspace}", width - len(clock))
        pad = max(0, width - text_width(strip_styles(title)) - len(clock))
        lines = [self.s("7", title + " " * pad + clock)]
        colors = _Colors(None)
        colors.on = self.color
        body = status_lines(self.paths, events=4, color=colors, asc=self.asc)
        body = [ln for ln in body if not ln.startswith("Report")]
        room = height - 4
        status_room = min(len(body), max(6, room // 2))
        lines += [fit_styled(ln, width) for ln in body[:status_room]]
        live_title, live = self.transcript(max(0, room - status_room - 1))
        sep = f"{rule * 2} {live_title} "
        lines.append(self.s("1;35", fit_styled(sep + rule * max(0, width - len(sep)), width)))
        live_room = room - status_room - 1
        lines += [fit_styled(ln, width) for ln in live[-live_room:]] if live_room > 0 else []
        while len(lines) < height - 2:
            lines.append("")
        lines = lines[: height - 2]
        since = parse_iso(started)
        if time.monotonic() < self.notice_until:
            notice = self.notice
        else:
            notice = f"run started {human_duration((now() - since).total_seconds())} ago" if since else ""
        lines.append(fit_styled(self.s("1", notice), width))
        keys = "  ".join(f"{k} {label}" for k, label in KEYS)
        lines.append(self.s("7", fit_styled(" " + keys + " " * max(0, width - len(keys) - 1), width)))
        return lines

    def handle(self, key: str, term: Terminal, height: int) -> bool:
        """Act on a key. Returns False to quit."""
        from bananavibe.cli import request_stop, send_message
        if key in ("q", "Q"):
            return False
        if key == "p":
            set_control(self.paths, pause=True)
            self.say("Pausing after the current session (P pauses now).")
        elif key == "P":
            set_control(self.paths, pause=True, pause_now=True)
            self.say("Pausing now: the session is interrupted and continues on resume.")
        elif key == "r":
            set_control(self.paths, pause=None, pause_now=None)
            self.say("Resumed." if supervisor_pid(self.paths) else "Pause cleared; no supervisor is running "
                                                                   "(`bananavibe start`).")
        elif key == "R":
            set_control(self.paths, retry_now=True)
            self.say("Cooldowns cleared: retrying now.")
        elif key in ("s", "n"):
            text = term.ask_line("Message for the agents" + (" (delivered now)" if key == "n" else "")
                                 + ", Enter to send, empty to cancel: ", height - 1).strip()
            if text:
                self.say(send_message(self.paths, text, key == "n"), 10)
            else:
                self.say("Cancelled.")
        elif key == "x":
            answer = term.ask_line("Stop the run after the current session? Type 'now' to interrupt it, "
                                   "'yes' to stop after it: ", height - 1).strip().lower()
            if answer == "now":
                self.say(request_stop(self.paths, True), 10)
            elif answer in ("y", "yes"):
                self.say(request_stop(self.paths, False), 10)
            else:
                self.say("Not stopped.")
        elif key == "a":
            self.ask(term, height)
        return True

    def ask(self, term: Terminal, height: int) -> None:
        from bananavibe.ask import ask
        question = term.ask_line("Question about the run (Enter to ask, empty to cancel): ", height - 1).strip()
        if not question:
            self.say("Cancelled.")
            return
        term.cooked()
        term.write("\x1b[?1049l")  # back to the normal screen, so the answer stays readable and scrollable
        print(f"\n? {question}\n(asking a read-only agent; Ctrl-C cancels)\n", flush=True)
        cancel = threading.Event()
        try:
            _, answer = ask(self.paths, self.cfg, question, verbose=False, cancel=cancel)
        except KeyboardInterrupt:
            cancel.set()
            answer = "Cancelled."
        print(clean(answer), flush=True)
        try:
            input("\n[Enter] back to the dashboard ")
        except (EOFError, KeyboardInterrupt):
            pass
        term.write("\x1b[?1049h")
        term.raw()

    def run(self) -> int:
        with Terminal() as term:
            while True:
                lines = self.frame()
                term.write("\x1b[H" + "\x1b[K\r\n".join(lines) + "\x1b[K\x1b[J")
                key = term.key(self.interval)
                if key and not self.handle(key, term, len(lines)):
                    return 0


def run(paths: Paths, cfg: Config, interval: float = 1.0) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        from bananavibe.cli import _Colors, status_lines
        for line in status_lines(paths, 8, _Colors(sys.stdout), ascii_only()):
            print(line)
        return 0
    try:
        return Dashboard(paths, cfg, interval).run()
    except KeyboardInterrupt:
        return 0
