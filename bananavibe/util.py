"""Small helpers shared by the other modules: time, files, text and console output."""

from __future__ import annotations

import datetime as dt
import errno
import gzip
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unicodedata
from collections import deque
from pathlib import Path

# ------------------------------------------------------------------ time


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def iso(t: dt.datetime | None) -> str | None:
    return t.isoformat(timespec="seconds") if t else None


def parse_iso(s: str | None) -> dt.datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()


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


def short_time(t: dt.datetime | None) -> str:
    """'Fri 14:05' for times within a week, else the date."""
    if not t:
        return "-"
    if abs((t - now()).total_seconds()) < 6 * 86400:
        return t.astimezone().strftime("%a %H:%M")
    return t.astimezone().strftime("%Y-%m-%d %H:%M")


# ------------------------------------------------------------------ files


def is_disk_full(e: BaseException) -> bool:
    return isinstance(e, OSError) and e.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1))


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_atomic(path: Path, text: str, durable: bool = True) -> None:
    """Replace `path` with `text` so readers see either the old or the new content, never a mix.

    With `durable`, the data is on disk before the rename and the rename itself is flushed, so a power cut
    can't leave an empty file behind. The existing file's permissions are kept (new files are private).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            if durable:
                f.flush()
                os.fsync(f.fileno())
        try:
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    if durable:
        _fsync_dir(path.parent)


def write_json(path: Path, data: object, durable: bool = True) -> None:
    write_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n", durable=durable)


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


def existing_log(path: Path) -> Path:
    """A session log may have been compressed since it was recorded: return whichever file exists."""
    if not path.exists():
        gz = path.with_name(path.name + ".gz")
        if gz.exists():
            return gz
    return path


def open_log(path: Path):
    """Open a (possibly gzip-compressed) text log for reading."""
    path = existing_log(path)
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open(encoding="utf-8", errors="replace")


def read_tail(path: Path, max_bytes: int = 256_000) -> str:
    """The end of a (possibly huge or compressed) text file, starting at a line boundary."""
    path = existing_log(path)
    try:
        if path.name.endswith(".gz"):
            chunks: deque[str] = deque()
            size = 0
            with open_log(path) as f:
                while True:
                    block = f.read(65536)
                    if not block:
                        break
                    chunks.append(block)
                    size += len(block)
                    while size - len(chunks[0]) > max_bytes:
                        size -= len(chunks.popleft())
            text = "".join(chunks)
            cut = len(text) > max_bytes
            text = text[-max_bytes:]
        else:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                end = f.tell()
                start = max(0, end - max_bytes)
                f.seek(start)
                text = f.read().decode("utf-8", errors="replace")
                cut = start > 0
    except OSError:
        return ""
    if cut and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


def tail(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return "[...truncated...]\n" + text[-max_chars:]


# ------------------------------------------------------------------ text for terminals

# Escape sequences (CSI, OSC, other ESC sequences and 8-bit CSI) that agents' tools print: colours, cursor
# movement, screen clearing, window titles. Shown raw they corrupt the terminal, so they are removed.
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"
    r"|\x1b[@-Z\\-_]"
    r"|\x9b[0-?]*[ -/]*[@-~]"
)
# Control characters (except newline) and Unicode direction overrides, which can reorder what is displayed.
_CTRL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def clean(text: str) -> str:
    """Make arbitrary program output safe to print: no escape sequences, no control characters."""
    if not text:
        return ""
    text = _ANSI_RE.sub("", str(text))
    if "\r" in text:
        text = text.replace("\r\n", "\n")
        # Progress bars redraw a line with \r: keep what a terminal would end up showing.
        text = "\n".join(_last_cr_segment(line) for line in text.split("\n"))
    if "\t" in text:
        text = text.expandtabs(4)
    return _CTRL_RE.sub("", text)


def _last_cr_segment(line: str) -> str:
    if "\r" not in line:
        return line
    parts = [p for p in line.split("\r") if p.strip()]
    return parts[-1] if parts else ""


def char_width(ch: str) -> int:
    if unicodedata.combining(ch) or ch in "\u200b\u200c\u200d\ufe0e\ufe0f":
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def text_width(text: str) -> int:
    return sum(char_width(c) for c in text)


def fit(text: str, width: int, ellipsis: str = "…") -> str:
    """Cut `text` to at most `width` terminal columns."""
    if width <= 0:
        return ""
    if text_width(text) <= width:
        return text
    out, used = [], 0
    limit = width - text_width(ellipsis)
    for ch in text:
        w = char_width(ch)
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return "".join(out) + ellipsis


def ascii_only(stream=None) -> bool:
    """Whether to avoid emoji and box-drawing characters (BANANAVIBE_ASCII=1 or a non-UTF-8 terminal)."""
    if os.environ.get("BANANAVIBE_ASCII", "").strip() not in ("", "0"):
        return True
    enc = (getattr(stream or sys.stdout, "encoding", "") or "").lower().replace("-", "")
    return bool(enc) and enc not in ("utf8", "utf8sig")


SYMBOLS = {
    "say": ("💬", ">"), "think": ("💭", "~"), "tool": ("🔧", "$"), "out": ("↳", "|"), "edit": ("✏", "*"),
    "ok": ("✔", "+"), "fail": ("✖", "x"), "warn": ("⚠", "!"), "wait": ("⏳", "z"), "search": ("🔎", "?"),
    "result": ("■", "#"), "todo": ("☐", "[ ]"), "todo_done": ("☑", "[x]"), "start": ("▶", ">"),
    "dot": ("·", "-"), "bar": ("█", "#"), "bar_empty": ("░", "."), "ellipsis": ("…", "..."), "rule": ("─", "-"),
}


def symbol(name: str, ascii_mode: bool = False) -> str:
    uni, asc = SYMBOLS[name]
    return asc if ascii_mode else uni


def use_color(stream=None) -> bool:
    stream = stream or sys.stdout
    try:
        tty = stream.isatty()
    except (AttributeError, ValueError):
        tty = False
    return tty and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


# ------------------------------------------------------------------ console


class Console:
    """Prints timestamped lines to the terminal and mirrors them to a log file.

    Thread-safe. If the terminal goes away (closed SSH session, broken pipe), printing stops and logging
    continues, instead of crashing the supervisor. The log file is rotated when it gets large.
    """

    max_log_bytes = 64 * 1024 * 1024
    keep_logs = 3

    def __init__(self, logfile: Path | None = None, quiet: bool = False, stream=None):
        self.logfile = logfile
        self.quiet = quiet
        self.stream = stream
        self.color = use_color(stream)
        self._lock = threading.Lock()
        self._written = 0

    def _emit(self, prefix: str, msg: str, color: str) -> None:
        stamp = now().strftime("%m-%d %H:%M:%S")
        line = f"{stamp} {prefix} {msg}"
        with self._lock:
            if not self.quiet:
                try:
                    out = self.stream or sys.stdout
                    out.write((f"\033[{color}m{line}\033[0m" if self.color and color else line) + "\n")
                    out.flush()
                except (OSError, ValueError):
                    self.quiet = True  # the terminal is gone: keep going, log only
            if self.logfile:
                try:
                    with self.logfile.open("a", encoding="utf-8") as f:
                        f.write(line + "\n")
                    self._written += len(line) + 1
                    if self._written > 1024 * 1024:
                        self._written = 0
                        self._rotate()
                except OSError:
                    pass

    def _rotate(self) -> None:
        try:
            if self.logfile.stat().st_size < self.max_log_bytes:
                return
            for i in range(self.keep_logs - 1, 0, -1):
                older = self.logfile.with_name(f"{self.logfile.name}.{i}")
                if older.exists():
                    os.replace(older, self.logfile.with_name(f"{self.logfile.name}.{i + 1}"))
            os.replace(self.logfile, self.logfile.with_name(f"{self.logfile.name}.1"))
            stale = self.logfile.with_name(f"{self.logfile.name}.{self.keep_logs + 1}")
            stale.unlink(missing_ok=True)
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


# ------------------------------------------------------------------ processes


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def boot_id() -> str:
    """Identifies the current boot of this machine ("" if unknown): processes from before a reboot are gone."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        pass
    try:
        r = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=10)
        return " ".join(r.stdout.split()) if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def process_has_marker(pid: int, marker: str) -> bool:
    """Whether process `pid` was started with BANANAVIBE_SESSION=<marker> in its environment."""
    if not marker:
        return False
    needle = f"BANANAVIBE_SESSION={marker}"
    try:
        return needle.encode() in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        pass
    try:
        r = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                           timeout=10)
        return r.returncode == 0 and needle in r.stdout.split()
    except (OSError, subprocess.SubprocessError):
        return False


def process_start(pid: int | None) -> str:
    """An identifier of when process `pid` started ("" if unknown), to tell it apart from a reused pid."""
    if not pid or pid <= 0:
        return ""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return "linux:" + stat.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        pass
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
        return ("ps:" + " ".join(r.stdout.split())) if r.returncode == 0 and r.stdout.strip() else ""
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_styles(text: str) -> str:
    return _SGR_RE.sub("", text)


def fit_styled(text: str, width: int) -> str:
    """Like `fit`, for text that contains our own colour codes: they don't take space and are kept intact."""
    if text_width(strip_styles(text)) <= width:
        return text
    out, used, styled = [], 0, False
    limit = max(0, width - 1)
    pos = 0
    while pos < len(text):
        m = _SGR_RE.match(text, pos)
        if m:
            out.append(m.group())
            styled = True
            pos = m.end()
            continue
        ch = text[pos]
        w = char_width(ch)
        if used + w > limit:
            break
        out.append(ch)
        used += w
        pos += 1
    return "".join(out) + "…" + ("\x1b[0m" if styled else "")
