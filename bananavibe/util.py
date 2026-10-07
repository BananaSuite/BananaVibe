"""Small helpers shared by the other modules: time, files and console output."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def iso(t: dt.datetime | None) -> str | None:
    return t.isoformat(timespec="seconds") if t else None


def parse_iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json(path: Path, data: object) -> None:
    write_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def read_json(path: Path, default: object = None) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default


def tail(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return "[...truncated...]\n" + text[-max_chars:]


class Console:
    """Prints timestamped lines to the terminal and mirrors them to a log file."""

    def __init__(self, logfile: Path | None = None, quiet: bool = False):
        self.logfile = logfile
        self.quiet = quiet
        self.color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def _emit(self, prefix: str, msg: str, color: str) -> None:
        stamp = now().strftime("%m-%d %H:%M:%S")
        line = f"{stamp} {prefix} {msg}"
        if not self.quiet:
            if self.color and color:
                print(f"\033[{color}m{line}\033[0m", flush=True)
            else:
                print(line, flush=True)
        if self.logfile:
            try:
                with self.logfile.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def info(self, msg: str) -> None:
        self._emit("[bananavibe]", msg, "33")

    def good(self, msg: str) -> None:
        self._emit("[bananavibe]", msg, "32")

    def warn(self, msg: str) -> None:
        self._emit("[bananavibe] !", msg, "31")

    def agent(self, name: str, msg: str) -> None:
        self._emit(f"  [{name}]", msg, "2")


def usage_text(a: dict) -> str:
    parts = []
    if a.get("tokens"):
        parts.append(f"{a['tokens'] / 1e6:.1f}M tokens")
    if a.get("cost_usd"):
        parts.append(f"${a['cost_usd']:.2f} at API prices")
    for window, value in (a.get("windows") or {}).items():
        if isinstance(value, (int, float)):
            start = (a.get("windows_at_start") or {}).get(window)
            parts.append(f"{window.replace('_', '-')} window {value:.0%}"
                         + (f" (was {start:.0%} at first session)" if isinstance(start, (int, float)) else ""))
    return ("; " + ", ".join(parts)) if parts else ""
