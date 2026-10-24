"""Keeping a run healthy for weeks: log retention, disk and memory checks.

A run that lasts days writes a lot: raw agent event streams (with every tool output), check output, the
supervisor log. Old session logs are gzip-compressed and, past a size budget, the oldest are deleted, so a
long run never fills the disk by itself.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from bananavibe.state import Paths
from bananavibe.util import read_json, write_atomic

SESSION_LOG = re.compile(r"^\d{4}-.+\.log(?:\.gz)?$")
CHECK_DIR = re.compile(r"^\d{4}-checks$")


def disk_free_mb(path: Path) -> float | None:
    try:
        return shutil.disk_usage(path).free / 1e6
    except OSError:
        return None


def memory_available_mb() -> float | None:
    """MemAvailable from /proc/meminfo (Linux); None where unknown."""
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _size(path: Path) -> int:
    try:
        if path.is_dir():
            return sum(_size(p) for p in path.iterdir())
        return path.stat().st_size
    except OSError:
        return 0


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _gzip(path: Path) -> bool:
    target = path.with_name(path.name + ".gz")
    tmp = path.with_name("." + path.name + ".gz.tmp")
    try:
        with path.open("rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        os.utime(tmp, (_mtime(path), _mtime(path)))
        os.replace(tmp, target)
        path.unlink()
        return True
    except OSError:
        tmp.unlink(missing_ok=True)
        return False


def prune_logs(paths: Paths, compress_after: int, max_mb: float, protect: set[str] | None = None) -> list[str]:
    """Compress and delete old logs. Returns a description of what was done."""
    if not paths.logs.is_dir():
        return []
    protect = {str(Path(p).resolve()) for p in (protect or set())}
    done: list[str] = []
    sessions = sorted((p for p in paths.logs.iterdir() if p.is_file() and SESSION_LOG.match(p.name)),
                      key=_mtime)
    plain = [p for p in sessions if p.name.endswith(".log")]
    if compress_after:
        compressed = 0
        for p in plain[:-compress_after] if len(plain) > compress_after else []:
            if str(p.resolve()) in protect or time.time() - _mtime(p) < 60:
                continue
            if _gzip(p):
                compressed += 1
        if compressed:
            done.append(f"compressed {compressed} old session log(s)")
    if max_mb:
        budget = max_mb * 1e6
        total = _size(paths.logs)
        if total > budget:
            candidates = [p for p in paths.logs.iterdir()
                          if (p.is_file() and SESSION_LOG.match(p.name)) or (p.is_dir() and CHECK_DIR.match(p.name))]
            ask_dir = paths.logs / "ask"
            if ask_dir.is_dir():
                candidates += [p for p in ask_dir.iterdir() if p.is_file()]
            candidates += [p for p in paths.logs.glob("supervisor.log.*")]
            candidates.sort(key=_mtime)
            newest = {str(p.resolve()) for p in sorted((c for c in candidates if c.is_file()
                                                        and SESSION_LOG.match(c.name)), key=_mtime)[-3:]}
            removed = 0
            for p in candidates:
                if total <= budget * 0.8:
                    break
                key = str(p.resolve())
                if key in protect or key in newest:
                    continue
                size = _size(p)
                try:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
                except OSError:
                    continue
                total -= size
                removed += 1
            if removed:
                done.append(f"deleted {removed} old log(s) to stay under {max_mb:g} MB")
    return done


def remove_stale_temp_files(paths: Paths, older_than: float = 3600) -> None:
    """Temporary files left behind by a process killed in the middle of an atomic write."""
    for folder in (paths.root, paths.inbox_dir, paths.reports, paths.prompts):
        if not folder.is_dir():
            continue
        for p in folder.glob(".*.tmp"):
            if time.time() - _mtime(p) > older_than:
                p.unlink(missing_ok=True)


def rotate_journal(paths: Paths, max_bytes: int = 4_000_000, keep_bytes: int = 1_000_000) -> bool:
    """Move old handoffs from JOURNAL.md to JOURNAL.archive.md once it gets large (prompts only use the end)."""
    try:
        if paths.journal.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False
    text = paths.journal.read_text(encoding="utf-8", errors="replace")
    cut = text.find("\n## Session ", max(0, len(text) - keep_bytes))
    if cut <= 0:
        return False
    archive = paths.root / "JOURNAL.archive.md"
    with archive.open("a", encoding="utf-8") as f:
        f.write(text[:cut])
    write_atomic(paths.journal, "<!-- Older sessions are in JOURNAL.archive.md -->\n" + text[cut:])
    return True


MEMORY_FILES = ("PLAN.md", "NOTES.md", "JOURNAL.md", "ANSWERS.md", "INBOX.archive.md")
# What it takes to start the run again if `.bananavibe/` disappears altogether. (control.json is mirrored there
# on every change, by `set_control`. service.env may hold API keys: the backup directory is the user's own.)
RUN_FILES = ("config.toml", "GOAL.md", "state.json", "service.json", "service.env")
SETTINGS = ("config.toml", "GOAL.md")


def protect_run_dir(paths: Paths) -> None:
    """Make `.bananavibe/` a (tiny, empty) git repository of its own: `git clean -fdx` and `git stash --all` in
    the project then leave it alone, instead of deleting the run's memory."""
    if (paths.root / ".git").exists() or not paths.root.is_dir():
        return
    try:
        subprocess.run(["git", "init", "-q", "--template=", str(paths.root)], capture_output=True, timeout=60,
                       stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


def _copy_in(src: Path, backup: Path, name: str) -> None:
    tmp = backup / f".{name}.tmp"
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # private from the start (service.env)
    with os.fdopen(fd, "wb") as dst, src.open("rb") as f:
        shutil.copyfileobj(f, dst)
    shutil.copystat(src, tmp)
    os.replace(tmp, backup / name)




def backup_memory(paths: Paths, settings: bool = True) -> None:
    """Copy the run's memory files, state and queued messages outside the workspace, in case an agent deletes
    them. GOAL.md and config.toml are copied too when `settings` is set: only while they are the approved
    versions (never an empty one over a real one)."""
    if not paths.backup:
        return
    try:
        paths.backup.mkdir(parents=True, exist_ok=True)
        for name in (*MEMORY_FILES, *RUN_FILES):
            src = paths.root / name
            if not src.is_file() or (name in SETTINGS and (not settings or not src.read_text().strip())):
                continue
            _copy_in(src, paths.backup, name)
        if paths.inbox_dir.is_dir():
            # Only added here, never removed: a queued message's copy goes only once it is delivered
            # (`consume_messages`), so an agent deleting it from the inbox can't make it disappear.
            inbox = paths.backup / "inbox"
            inbox.mkdir(exist_ok=True)
            for p in paths.inbox_dir.glob("*.md"):
                if not (inbox / p.name).exists():
                    shutil.copy2(p, inbox / p.name)
    except OSError:
        pass


def backup_state(paths: Paths) -> None:
    """Copy just state.json (what the run is doing right now) to the backup."""
    if not paths.backup or not paths.state.is_file():
        return
    try:
        paths.backup.mkdir(parents=True, exist_ok=True)
        _copy_in(paths.state, paths.backup, "state.json")
    except OSError:
        pass


def clear_backup(paths: Paths, everything: bool = False) -> None:
    """Retire the backup of an earlier run (a new run, or an imported one, starts here: it must never come back).
    It is kept aside, once, in case it was still wanted. A backup without config.toml is the new run's own (a
    message or a command sent before its first start) and stays."""
    if not paths.backup or not paths.backup.is_dir():
        return
    if not has_backup(paths):
        if everything:
            shutil.rmtree(paths.backup, ignore_errors=True)
        return
    old = paths.backup.with_name("backup.previous")
    shutil.rmtree(old, ignore_errors=True)
    try:
        os.replace(paths.backup, old)
    except OSError:
        shutil.rmtree(paths.backup, ignore_errors=True)


def has_backup(paths: Paths) -> bool:
    return bool(paths.backup) and (paths.backup / "config.toml").is_file()


def should_be_running(paths: Paths) -> bool:
    """From the backup: was this run meant to keep running (not stopped by the operator, not finished)?"""
    if not paths.backup:
        return False
    state = read_json(paths.backup / "state.json", {})
    control = read_json(paths.backup / "control.json", {})
    state = state if isinstance(state, dict) else {}
    control = control if isinstance(control, dict) else {}
    if state.get("operator_stop") or control.get("stop") or control.get("stop_now"):
        return False
    if state.get("status") == "done" and not any((paths.backup / "inbox").glob("*.md")):
        from bananavibe.config import ConfigError, loads
        try:
            return loads((paths.backup / "config.toml").read_text(), paths.workspace).after_done == "wait"
        except (ConfigError, OSError):
            return False
    return True


def restore_run_dir(paths: Paths) -> list[str]:
    """`.bananavibe/` is gone (an agent deleted it while no supervisor could notice): rebuild it from the backup
    outside the workspace, so the run can start again by itself. Returns what was restored. A run that was
    stopped on purpose, or is finished, is left alone (its deletion may well have been deliberate too)."""
    if paths.config.exists() or not has_backup(paths) or not paths.workspace.is_dir() \
            or not should_be_running(paths):
        return []
    paths.root.mkdir(exist_ok=True)
    restored = restore_missing(paths, (*RUN_FILES, "control.json"))
    restored += restore_memory(paths)
    protect_run_dir(paths)
    return restored


def restore_missing(paths: Paths, names: tuple[str, ...]) -> list[str]:
    """Copy these files back from the backup where they are missing in the run directory."""
    restored = []
    if not paths.backup:
        return restored
    for name in names:
        src, dst = paths.backup / name, paths.root / name
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)
            restored.append(name)
    return restored


def run_files(paths: Paths) -> tuple[set[str], set[str], float]:
    """What exists before an agent attempt: memory files, queued messages, and when. With `restore_memory`,
    only what disappeared during the attempt is put back, not what the operator removed before it."""
    files = {name for name in MEMORY_FILES if (paths.root / name).exists()}
    messages = {p.name for p in paths.inbox_dir.glob("*.md")} if paths.inbox_dir.is_dir() else set()
    return files, messages, time.time()


def restore_memory(paths: Paths, restored_messages: set[str] | None = None,
                   before: tuple[set[str], set[str], float] | None = None) -> list[str]:
    """Put back memory files (and queued messages) that are missing, from the last backup. With `before` (from
    `run_files`), only those that existed then, or messages queued since. The names of the messages put back
    are added to `restored_messages`."""
    restored: list[str] = []
    if not paths.backup or not paths.backup.is_dir():
        return restored
    paths.root.mkdir(parents=True, exist_ok=True)
    for name in MEMORY_FILES:
        src, dst = paths.backup / name, paths.root / name
        if src.is_file() and not dst.exists() and (before is None or name in before[0]):
            shutil.copy2(src, dst)
            restored.append(name)
    inbox = paths.backup / "inbox"
    if inbox.is_dir():
        delivered = paths.inbox_dir / "delivered"

        def wanted(p: Path) -> bool:
            if before is None or p.name in before[1]:
                return True
            try:
                return p.stat().st_mtime >= before[2] - 1  # queued during the attempt
            except OSError:
                return False

        missing = [p for p in inbox.glob("*.md") if not (paths.inbox_dir / p.name).exists()
                   and not (delivered / p.name).exists() and wanted(p)]
        if missing:
            paths.inbox_dir.mkdir(parents=True, exist_ok=True)
            for p in missing:
                shutil.copy2(p, paths.inbox_dir / p.name)
                if restored_messages is not None:
                    restored_messages.add(p.name)
            restored.append(f"{len(missing)} queued message(s)")
    return restored
