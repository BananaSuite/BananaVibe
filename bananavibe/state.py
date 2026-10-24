"""The run directory `.bananavibe/`: memory files shared with the agents, run state, control and messages."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import socket
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from bananavibe.util import iso, now, parse_iso, pid_alive, read_json, read_text, write_atomic, write_json

DIRNAME = ".bananavibe"


def machine_dir(workspace: Path) -> Path | None:
    """Per-workspace directory outside the workspace (locks, backups of the run's memory), or None.

    Kept outside so that an agent deleting or cleaning `.bananavibe/` can't release the single-instance lock
    or take the plan and notes with it. BANANAVIBE_STATE_DIR overrides the location (default:
    $XDG_STATE_HOME or ~/.local/state, then bananavibe/<hash of the workspace path>).
    """
    base = Path("~/.local/state").expanduser()
    for var in ("BANANAVIBE_STATE_DIR", "XDG_STATE_HOME"):
        value = Path(os.environ.get(var, "")).expanduser()
        if os.environ.get(var) and value.is_absolute():  # a relative value would depend on the current directory
            base = value
            break
    path = base / "bananavibe" / hashlib.sha1(str(workspace).encode()).hexdigest()[:16]
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)  # backups may include service.env (API keys)
        try:
            if path.stat().st_mode & 0o077:
                os.chmod(path, 0o700)
        except OSError:
            pass
        if not (path / "workspace").exists():
            (path / "workspace").write_text(str(workspace) + "\n")
        return path
    except OSError:
        return None


class Paths:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.root = self.workspace / DIRNAME
        self.config = self.root / "config.toml"
        self.goal = self.root / "GOAL.md"
        self.plan = self.root / "PLAN.md"
        self.notes = self.root / "NOTES.md"
        self.journal = self.root / "JOURNAL.md"
        self.handoff = self.root / "HANDOFF.md"
        self.done = self.root / "DONE.md"
        self.review = self.root / "REVIEW.md"
        self.feedback = self.root / "FEEDBACK.md"
        self.inbox = self.root / "INBOX.md"          # legacy single-file inbox; still accepted
        self.inbox_dir = self.root / "inbox"         # one file per operator message
        self.inbox_archive = self.root / "INBOX.archive.md"
        self.state = self.root / "state.json"
        self.state_backup = self.root / "state.json.bak"
        self.control = self.root / "control.json"
        self.control_lock = self.root / ".control.lock"
        self.machine = machine_dir(self.workspace)
        # Locks are held on two files: one in the run directory (it follows the files whatever path, user or
        # environment a process sees them through) and one outside (it survives the run directory being deleted).
        self.lock = self.root / "supervisor.lock"
        self.lock_external = (self.machine / "supervisor.lock") if self.machine else None
        self.guardian_lock = self.root / "guardian.lock"
        self.guardian_lock_external = (self.machine / "guardian.lock") if self.machine else None
        self.backup = (self.machine / "backup") if self.machine else None
        self.heartbeat = self.root / "heartbeat.json"
        self.logs = self.root / "logs"
        self.reports = self.root / "reports"
        self.prompts = self.root / "prompts"
        self.reverted = self.root / "reverted"     # protected files changed without `bananavibe reload`
        self.supervisor_log = self.root / "logs" / "supervisor.log"
        self.supervisor_out = self.root / "logs" / "supervisor.out"  # stdout/stderr of a background supervisor
        self.answers = self.root / "ANSWERS.md"
        self.answers_lock = self.root / ".answers.lock"

    def session_log(self, iteration: int, label: str) -> Path:
        return self.logs / f"{iteration:04d}-{label}-{now().strftime('%Y%m%d-%H%M%S')}.log"

    def rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace))
        except ValueError:
            return str(path)

    @property
    def initialized(self) -> bool:
        return self.config.exists()


PLAN_ITEM = re.compile(r"^\s*[-*]\s+\[(?P<mark>[ xX~!])\]\s+(?P<text>.+)$")


@dataclass
class PlanStats:
    total: int = 0
    done: int = 0
    dropped: int = 0
    open: int = 0
    blocked: int = 0
    open_items: list[str] = field(default_factory=list)

    @property
    def finished_fraction(self) -> float:
        return (self.done + self.dropped) / self.total if self.total else 0.0


def plan_stats(text: str) -> PlanStats:
    s = PlanStats()
    for line in text.splitlines():
        m = PLAN_ITEM.match(line)
        if not m:
            continue
        s.total += 1
        mark = m.group("mark")
        if mark in "xX":
            s.done += 1
        elif mark == "~":
            s.dropped += 1
        elif mark == "!":
            s.blocked += 1
            s.open_items.append(m.group("text").strip())
        else:
            s.open += 1
            s.open_items.append(m.group("text").strip())
    return s


DEFAULT_STATE = {
    "status": "new",  # new | running | waiting | paused | done | stopped | crashed
    "detail": "",
    "iteration": 0,
    "attempts": 0,
    "started_at": None,
    "updated_at": None,
    "finished_at": None,
    "current_agent": None,
    "session_started_at": None,
    "wait_until": None,
    "approvals": 0,
    "stalls": 0,
    "last_fingerprint": "",
    "last_checkpoint": 0,
    "last_checks": [],
    "base_commits": {},
    "agents": {},  # name -> {"sessions", "failures", "cooldown_until", "last_error", "consecutive_failures"}
    "events": [],
    "pid": None,
    "host": None,         # machine the supervisor runs (or last ran) on
    "current": None,      # the session running now: {label, agent, log, session_id, started, pid, pstart}
    "interrupted": None,  # a session cut short, continued by the next session of its kind
    "pending": None,      # post-session work still to do: {iteration, agent, step, claim}
    "restore_pending": None,  # an attempt ended but its changes to GOAL.md / config.toml aren't undone yet
    "before_attempt": None,   # memory files and messages before the attempt whose deletions are still to repair
    "feedback": [],       # what the next work session must address first (survives crashes)
    "history": [],        # every session attempt: {n, iteration, label, agent, session_id, log, ...}
    "last_session_end": None,
}


class State:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.load_error = ""
        self.data = {**copy.deepcopy(DEFAULT_STATE), **self._load()}
        self._saves = 0

    def _load(self) -> dict:
        if not self.paths.state.exists():
            return {}
        try:
            data = json.loads(self.paths.state.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            raise ValueError("not a JSON object")
        except (OSError, ValueError) as e:
            backup = read_json(self.paths.state_backup, None)
            if isinstance(backup, dict):
                self.load_error = f"state.json is unreadable ({e}); using state.json.bak"
                return backup
            self.load_error = f"state.json is unreadable ({e}) and there is no usable backup"
            return {}

    def __getitem__(self, key: str):
        return self.data[key]

    def __setitem__(self, key: str, value) -> None:
        self.data[key] = value

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def agent(self, name: str) -> dict:
        info = self.data["agents"].setdefault(
            name, {"sessions": 0, "failures": 0, "cooldown_until": None, "last_error": "",
                   "consecutive_failures": 0, "seconds": 0.0})
        for key, value in (("sessions", 0), ("failures", 0), ("consecutive_failures", 0), ("last_error", ""),
                           ("cooldown_until", None), ("seconds", 0.0)):
            info.setdefault(key, value)
        return info

    def event(self, kind: str, msg: str) -> None:
        self.data["events"].append({"at": iso(now()), "kind": kind, "msg": msg[:500]})
        del self.data["events"][:-200]

    def record_session(self, entry: dict) -> None:
        history = self.data["history"]
        entry = {"n": history[-1]["n"] + 1 if history else 1, **entry}
        self.data["history"].append(entry)
        del self.data["history"][:-500]

    def save(self, backup: bool = False) -> None:
        self.data["updated_at"] = iso(now())
        write_json(self.paths.state, self.data)
        self._saves += 1
        if backup or self._saves % 25 == 1:
            write_json(self.paths.state_backup, self.data, durable=False)


# ------------------------------------------------------------------ locking


@contextmanager
def file_lock(path: Path, shared: bool = False):
    """An advisory lock held while the block runs (between processes and threads)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ------------------------------------------------------------------ control
# Small commands left by the CLI for the supervisor. Keys:
#   pause, pause_now (interrupt the session, then pause), stop, stop_now, retry_now,
#   deliver_now (interrupt a work session so it continues with new operator messages),
#   reload ({"config": text, "goal": text, "at": iso}: operator-approved new versions of protected files).


def read_control(paths: Paths) -> dict:
    data = read_json(paths.control, {})
    return data if isinstance(data, dict) else {}


def _write_control(paths: Paths, data: dict) -> None:
    write_json(paths.control, data, durable=False)
    if paths.backup:  # mirrored outside the workspace: a stop or an approval survives `.bananavibe/` being deleted
        mirror = paths.backup / "control.json"
        try:
            write_json(mirror, data, durable=False)
        except OSError:
            try:
                mirror.unlink(missing_ok=True)  # better none than a stale one (it could bring back an old stop)
            except OSError:
                pass


def set_control(paths: Paths, **values) -> None:
    """Update control keys atomically with respect to other writers (CLI commands and the supervisor)."""
    with file_lock(paths.control_lock):
        data = read_control(paths)
        for key, value in values.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        _write_control(paths, data)


def take_control(paths: Paths, *keys: str) -> dict:
    """Read and clear `keys` in one step, so a command is acted on exactly once."""
    with file_lock(paths.control_lock):
        data = read_control(paths)
        taken = {k: data.pop(k) for k in keys if k in data}
        if taken:
            _write_control(paths, data)
    return taken


def clear_control_if(paths: Paths, key: str, expected: object) -> bool:
    """Clear `key` only if it still holds `expected` (a newer request that arrived meanwhile is kept)."""
    with file_lock(paths.control_lock):
        data = read_control(paths)
        if key not in data or data[key] != expected:
            return False
        data.pop(key)
        _write_control(paths, data)
    return True


def approved_text(paths: Paths, key: str) -> str | None:
    """The operator's latest approved text of GOAL.md (`goal`) or config.toml (`config`): an approval still
    waiting for the supervisor (`reload`/`edit`) comes first, then what the supervisor last accepted."""
    pending = read_control(paths).get("reload")
    if isinstance(pending, dict) and isinstance(pending.get(key), str):
        return pending[key]
    value = (State(paths).get("accepted") or {}).get(key)
    return value if isinstance(value, str) else None


# ------------------------------------------------------------------ operator messages


MSG_NAME = re.compile(r"^(\d{8}T\d{6})(?:\.(\d{1,6}))?-[\w.-]+\.md$")


@dataclass
class Message:
    id: str
    at: str
    text: str
    urgent: bool = False


def add_message(paths: Paths, text: str, urgent: bool = False) -> Message:
    """Queue a message for the agents. One file per message: concurrent senders never lose each other's."""
    paths.inbox_dir.mkdir(parents=True, exist_ok=True)
    t = now()
    name = f"{t:%Y%m%dT%H%M%S}.{t.microsecond:06d}-{os.getpid()}{'-now' if urgent else ''}.md"
    write_atomic(paths.inbox_dir / name, text.strip() + "\n")
    if paths.backup:  # a copy outside the workspace, until delivered: not even deleting .bananavibe/ loses it
        try:
            write_atomic(paths.backup / "inbox" / name, text.strip() + "\n")
        except OSError:
            pass
    return Message(name, iso(t), text.strip(), urgent)


def _migrate_legacy_inbox(paths: Paths) -> None:
    if not paths.inbox.exists():
        return
    with file_lock(paths.control_lock):
        if not paths.inbox.exists():
            return
        if not read_text(paths.inbox).strip():
            paths.inbox.unlink(missing_ok=True)
            return
        paths.inbox_dir.mkdir(parents=True, exist_ok=True)
        t = now()
        os.replace(paths.inbox, paths.inbox_dir / f"{t:%Y%m%dT%H%M%S}.{t.microsecond:06d}-legacy.md")


def pending_messages(paths: Paths) -> list[Message]:
    """Messages not yet delivered to a completed work session, oldest first."""
    _migrate_legacy_inbox(paths)
    if not paths.inbox_dir.is_dir():
        return []
    out = []
    for p in sorted(paths.inbox_dir.iterdir()):
        m = MSG_NAME.match(p.name)
        if not m or not p.is_file():
            continue
        text = read_text(p).strip()
        if not text:
            continue
        stamp = m.group(1)
        at = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}"
        out.append(Message(p.name, at, text, p.name.endswith("-now.md")))
    return out


def consume_messages(paths: Paths, ids: list[str]) -> None:
    """Mark messages as delivered: archive them and remove them from the queue."""
    if not ids:
        return
    delivered = paths.inbox_dir / "delivered"
    delivered.mkdir(parents=True, exist_ok=True)
    with file_lock(paths.control_lock):
        archive = []
        for mid in ids:
            src = paths.inbox_dir / mid
            if not src.exists():
                continue
            archive.append(f"\n### {mid}\n{read_text(src).strip()}\n")
            os.replace(src, delivered / mid)
            if paths.backup:
                (paths.backup / "inbox" / mid).unlink(missing_ok=True)
        if archive:
            with paths.inbox_archive.open("a", encoding="utf-8") as f:
                f.write("".join(archive))


def cancel_message(paths: Paths, mid: str) -> bool:
    """Withdraw a queued message (and its backup copy) before a session carries it out."""
    if not MSG_NAME.match(mid):
        return False
    with file_lock(paths.control_lock):
        found = (paths.inbox_dir / mid).is_file()
        (paths.inbox_dir / mid).unlink(missing_ok=True)
        if paths.backup:
            backup = paths.backup / "inbox" / mid
            found = found or backup.is_file()
            backup.unlink(missing_ok=True)
    return found


def append_inbox(paths: Paths, message: str) -> None:
    """Backwards-compatible name for queuing a message."""
    add_message(paths, message)


# ------------------------------------------------------------------ the supervisor process


class Lock:
    """Only one supervisor (or guardian) per workspace: an exclusive lock held on every given file at once."""

    def __init__(self, *paths: Path | None):
        self.paths = [p for p in paths if p is not None]
        self.fds: list[int] = []

    def acquire(self) -> bool:
        for path in self.paths:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            except OSError:
                if path is self.paths[0]:
                    self.release()
                    return False
                continue  # the outside copy is a safety net: without it the inside lock still holds
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                self.release()
                return False
            self.fds.append(fd)
            try:
                os.ftruncate(fd, 0)
                os.write(fd, str(os.getpid()).encode())
            except OSError:
                pass  # e.g. a full disk: the lock itself is what matters (status then shows the pid as unknown)
        return True

    def release(self) -> None:
        for fd in self.fds:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                os.close(fd)
        self.fds = []


def lock_holder(path: Path) -> int | None:
    """PID of the process holding the lock file `path` (-1 if unknown), or None if nobody holds it."""
    if not path.exists():
        return None
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            text = read_text(path).strip()
            return int(text) if text.isdigit() else -1
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def supervisor_pid(paths: Paths) -> int | None:
    """PID of a running supervisor, or None."""
    pid = lock_holder(paths.lock)
    if pid is None and paths.lock_external:
        pid = lock_holder(paths.lock_external)  # e.g. the run directory was deleted under a running supervisor
    return pid


def write_heartbeat(paths: Paths, **extra) -> None:
    # (If an agent deleted the run directory mid-session, this recreates it: the heartbeat must go on, or the
    # guardian would take the supervisor for hung. The rest is restored after the session.)
    try:
        write_json(paths.heartbeat, {"at": iso(now()), "pid": os.getpid(), "host": socket.gethostname(), **extra},
                   durable=False)
    except OSError:
        try:
            os.utime(paths.heartbeat)  # e.g. a full disk: touching the file needs no space and still shows life
        except OSError:
            pass


def heartbeat_age(paths: Paths) -> float | None:
    """Seconds since the supervisor last showed it is alive (None if unknown)."""
    data = read_json(paths.heartbeat, {})
    t = parse_iso(data.get("at")) if isinstance(data, dict) else None
    if not t:
        return None
    if isinstance(data, dict) and data.get("pid") and data.get("host") == socket.gethostname() \
            and not pid_alive(data["pid"]):
        return None
    return (now() - t).total_seconds()


def write_text(path: Path, text: str) -> None:
    write_atomic(path, text)
