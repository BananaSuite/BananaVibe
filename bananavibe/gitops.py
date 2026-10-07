"""Git snapshots of the repositories the agents work on."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

AUTHOR = ["-c", "user.name=BananaVibe", "-c", "user.email=bananavibe@localhost"]


def git(repo: Path, *args: str, check: bool = False, timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          errors="replace", check=check, timeout=timeout)


def is_repo(path: Path) -> bool:
    return (path / ".git").exists()


def discover(workspace: Path, configured: list[str]) -> list[Path]:
    if configured:
        return [(workspace / r).resolve() for r in configured]
    if is_repo(workspace):
        found = [workspace.resolve()]
    else:
        found = []
    for child in sorted(workspace.iterdir()):
        if child.is_dir() and not child.name.startswith(".") and is_repo(child):
            found.append(child.resolve())
    return found


def _has_identity(repo: Path) -> bool:
    return bool(git(repo, "config", "user.email").stdout.strip())


def _author(repo: Path) -> list[str]:
    return [] if _has_identity(repo) else AUTHOR


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def current_branch(repo: Path) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def exclude_path(repo: Path, pattern: str) -> None:
    info = repo / ".git" / "info"
    if not info.is_dir():
        return
    exclude = info / "exclude"
    existing = exclude.read_text() if exclude.exists() else ""
    if pattern not in existing.splitlines():
        exclude.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + pattern + "\n")


def init_repo(path: Path) -> None:
    git(path, "init", "-q", check=True)
    git(path, "add", "-A")
    git(path, *_author(path), "commit", "-q", "--allow-empty", "-m", "Initial snapshot before BananaVibe", check=True)


def ensure_branch(repo: Path, branch: str) -> str:
    """Switch to the work branch (creating it from the current HEAD), keeping uncommitted changes."""
    if not branch or current_branch(repo) == branch:
        return current_branch(repo)
    if git(repo, "rev-parse", "--verify", "HEAD").returncode != 0:
        git(repo, *_author(repo), "commit", "-q", "--allow-empty", "-m", "Initial snapshot before BananaVibe")
    exists = git(repo, "rev-parse", "--verify", f"refs/heads/{branch}").returncode == 0
    r = git(repo, "switch", branch) if exists else git(repo, "switch", "-c", branch)
    if r.returncode != 0:
        raise RuntimeError(f"cannot switch {repo} to branch {branch}: {r.stderr.strip()}")
    return branch


def dirty(repo: Path) -> bool:
    return bool(git(repo, "status", "--porcelain").stdout.strip())


def snapshot(repo: Path, message: str) -> str | None:
    """Commit everything in the working tree. Returns the new commit, or None if nothing changed."""
    if not dirty(repo):
        return None
    git(repo, "add", "-A")
    r = git(repo, *_author(repo), "commit", "-q", "--no-verify", "-m", message)
    if r.returncode != 0:
        return None
    return head(repo)


def push(repo: Path, remote: str, branch: str) -> str:
    r = git(repo, "push", "-q", "-u", remote, branch, timeout=600)
    return "" if r.returncode == 0 else (r.stderr.strip() or r.stdout.strip())


def commits_since(repo: Path, base: str) -> int:
    if not base:
        return 0
    r = git(repo, "rev-list", "--count", f"{base}..HEAD")
    return int(r.stdout.strip() or 0) if r.returncode == 0 else 0


def diffstat(repo: Path, base: str) -> str:
    if not base:
        return ""
    return git(repo, "diff", "--shortstat", base, "HEAD").stdout.strip()


def fingerprint(repo: Path) -> str:
    """Identifies the current tree state including uncommitted changes, to detect progress."""
    status = git(repo, "status", "--porcelain").stdout
    diff = git(repo, "diff", "HEAD").stdout if status else ""
    return f"{head(repo)}:{hashlib.sha1((status + diff).encode()).hexdigest()}"
