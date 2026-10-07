"""The run directory `.bananavibe/`: memory files shared with the agents, run state and control."""

from __future__ import annotations

import copy
import fcntl
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from bananavibe.util import iso, now, read_json, read_text, write_atomic, write_json

DIRNAME = ".bananavibe"


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
        self.inbox = self.root / "INBOX.md"
        self.state = self.root / "state.json"
        self.control = self.root / "control.json"
        self.lock = self.root / "supervisor.lock"
        self.logs = self.root / "logs"
        self.reports = self.root / "reports"
        self.prompts = self.root / "prompts"
        self.supervisor_log = self.root / "logs" / "supervisor.log"

    def session_log(self, iteration: int, label: str) -> Path:
        return self.logs / f"{iteration:04d}-{label}-{now().strftime('%Y%m%d-%H%M%S')}.log"

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.workspace))


PLAN_ITEM = re.compile(r"^\s*[-*]\s+\[(?P<mark>[ xX~!])\]\s+(?P<text>.+)$")


@dataclass
class PlanStats:
    total: int = 0
    done: int = 0
    dropped: int = 0
    open: int = 0
    blocked: int = 0
    open_items: list[str] = field(default_factory=list)


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
    "status": "new",  # new | running | waiting | paused | done | stopped
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
}


class State:
    def __init__(self, paths: Paths):
        self.paths = paths
        data = read_json(paths.state, {}) or {}
        self.data = {**copy.deepcopy(DEFAULT_STATE), **data}

    def __getitem__(self, key: str):
        return self.data[key]

    def __setitem__(self, key: str, value) -> None:
        self.data[key] = value

    def agent(self, name: str) -> dict:
        return self.data["agents"].setdefault(
            name, {"sessions": 0, "failures": 0, "cooldown_until": None, "last_error": "",
                   "consecutive_failures": 0, "seconds": 0.0})

    def event(self, kind: str, msg: str) -> None:
        self.data["events"].append({"at": iso(now()), "kind": kind, "msg": msg[:500]})
        del self.data["events"][:-200]

    def save(self) -> None:
        self.data["updated_at"] = iso(now())
        write_json(self.paths.state, self.data)


# Control: small commands left by `bananavibe pause/resume/stop/say` for the supervisor.

def read_control(paths: Paths) -> dict:
    return read_json(paths.control, {}) or {}


def set_control(paths: Paths, **values) -> None:
    data = read_control(paths)
    data.update(values)
    write_json(paths.control, data)


def append_inbox(paths: Paths, message: str) -> None:
    with paths.inbox.open("a", encoding="utf-8") as f:
        f.write(f"\n### {iso(now())}\n{message.strip()}\n")


def take_inbox(paths: Paths) -> str:
    text = read_text(paths.inbox).strip()
    if text:
        archive = paths.root / "INBOX.archive.md"
        with archive.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
        paths.inbox.unlink(missing_ok=True)
    return text


class Lock:
    """Only one supervisor per workspace."""

    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> bool:
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            return False
        os.ftruncate(self.fd, 0)
        os.write(self.fd, str(os.getpid()).encode())
        return True

    def release(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def supervisor_pid(paths: Paths) -> int | None:
    """PID of a running supervisor, or None."""
    if not paths.lock.exists():
        return None
    fd = os.open(paths.lock, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            text = read_text(paths.lock).strip()
            return int(text) if text.isdigit() else -1
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def write_text(path: Path, text: str) -> None:
    write_atomic(path, text)
