"""Git snapshots of the repositories the agents work on."""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

AUTHOR = ["-c", "user.name=BananaVibe", "-c", "user.email=bananavibe@localhost"]
# Read-only commands must not take the index lock: an agent may be running git at the same moment.
NO_LOCKS = "--no-optional-locks"


def git(repo: Path, *args: str, check: bool = False, timeout: float = 300) -> subprocess.CompletedProcess:
    """Run git. Never raises for a timeout or a missing git unless `check` is set: it returns a failure."""
    # Hooks are never run: they are code an agent could have written, and the supervisor's own commits,
    # switches and pushes are bookkeeping (snapshots already skipped pre-commit checks with --no-verify).
    argv = ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *args]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never wait for a password nobody will type
    try:
        # A session of its own: no controlling terminal, so ssh can't stop to ask for a passphrase or a host
        # key either (the user's own ssh settings, `core.sshCommand` included, are left alone).
        return subprocess.run(argv, capture_output=True, text=True, errors="replace", check=check, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    except subprocess.TimeoutExpired:
        if check:
            raise
        return subprocess.CompletedProcess(argv, 124, "", f"git {args[0] if args else ''} timed out after {timeout:g}s")
    except FileNotFoundError:
        if check:
            raise
        return subprocess.CompletedProcess(argv, 127, "", "git is not installed")


def is_repo(path: Path) -> bool:
    return (path / ".git").exists()


def discover(workspace: Path, configured: list[str]) -> list[Path]:
    if configured:
        return [(workspace / r).resolve() for r in configured]
    found = [workspace.resolve()] if is_repo(workspace) else []
    try:
        children = sorted(workspace.iterdir())
    except OSError:
        return found
    for child in children:
        try:
            if child.is_dir() and not child.name.startswith(".") and is_repo(child):
                found.append(child.resolve())
        except OSError:
            continue
    return found


def _has_identity(repo: Path) -> bool:
    return bool(git(repo, "config", "user.email").stdout.strip())


def _author(repo: Path) -> list[str]:
    return [] if _has_identity(repo) else AUTHOR


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def current_branch(repo: Path) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def git_path(repo: Path, name: str) -> Path | None:
    """Where git keeps `name` (e.g. info/exclude, MERGE_HEAD) for this repository: also right for worktrees,
    submodules and separate git dirs, where `.git` is a file rather than a directory."""
    r = git(repo, "rev-parse", "--git-path", name)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    path = Path(r.stdout.strip())
    return path if path.is_absolute() else repo / path


def exclude_path(repo: Path, pattern: str) -> None:
    exclude = git_path(repo, "info/exclude")
    if exclude is None:
        return
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text() if exclude.exists() else ""
        if pattern not in existing.splitlines():
            exclude.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + pattern + "\n")
    except OSError:
        pass


def init_repo(path: Path, exclude: tuple[str, ...] = ()) -> None:
    git(path, "init", "-q", check=True)
    for pattern in exclude:  # before the first `add`, so excluded paths never enter the history
        exclude_path(path, pattern)
    r = git(path, "add", "-A", timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"git add failed in {path}: {r.stderr.strip()[-500:]}")
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
    return bool(git(repo, NO_LOCKS, "status", "--porcelain").stdout.strip())


def operation_in_progress(repo: Path) -> str:
    """A merge, rebase, cherry-pick or revert left unfinished (commits fail until it is resolved)."""
    for name, what in (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
                       ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert")):
        path = git_path(repo, name) if not (repo / ".git").is_dir() else repo / ".git" / name
        if path is not None and path.exists():
            return what
    return ""


@dataclass
class SnapshotResult:
    commit: str | None = None
    error: str = ""
    skipped: list[str] = field(default_factory=list)  # files too large to commit


def commit_all(repo: Path, message: str, max_file_mb: float = 0, exclude: tuple[str, ...] = ()) -> SnapshotResult:
    """Commit everything in the working tree, except files larger than `max_file_mb` and the `exclude` paths."""
    res = SnapshotResult()
    if not dirty(repo):
        return res
    busy = operation_in_progress(repo)
    if busy:
        res.error = f"a {busy} is in progress (unfinished); resolve or abort it so the work can be committed"
        return res
    # Files over the limit are left out of `git add` itself: adding them, even briefly, would store them in
    # .git/objects and grow the repository by their size at every snapshot.
    big = _big_changed_files(repo, max_file_mb * 1e6) if max_file_mb > 0 else []
    big = [b for b in big if not any(b == e or b.startswith(e.rstrip("/") + "/") for e in exclude)]
    # A path that is ignored anyway must not be named (`git add` fails on a pathspec naming an ignored path,
    # even one that excludes it).
    exclude = tuple(e for e in exclude if git(repo, "check-ignore", "-q", "--", e).returncode != 0)
    excludes = [f":(exclude,literal){name}" for name in [*exclude, *big[:2000]]]
    r = git(repo, "add", "-A", "--", ".", *excludes, timeout=900)
    if r.returncode != 0:
        res.error = "git add: " + (r.stderr.strip() or r.stdout.strip())[-500:]
    res.skipped += big
    if max_file_mb > 0:
        limit = max_file_mb * 1e6
        for flt, unstage in (("A", ["rm", "--cached", "-q", "--"]), ("M", ["reset", "-q", "--"])):
            names = git(repo, "diff", "--cached", "--name-only", "-z", f"--diff-filter={flt}").stdout.split("\0")
            big = []
            for name in names:
                if not name:
                    continue
                try:
                    if (repo / name).lstat().st_size > limit:
                        big.append(name)
                except OSError:
                    continue
            for i in range(0, len(big), 100):
                git(repo, *unstage, *big[i:i + 100])
            res.skipped += [b for b in big if b not in res.skipped]
    if git(repo, "diff", "--cached", "--quiet").returncode == 0:
        return res  # nothing (left) to commit
    r = git(repo, *_author(repo), "commit", "-q", "--no-verify", "-m", message, timeout=900)
    if r.returncode != 0:
        res.error = "git commit: " + (r.stderr.strip() or r.stdout.strip())[-500:]
        return res
    res.commit = head(repo)
    return res


def _big_changed_files(repo: Path, limit: float, max_entries: int = 200_000) -> list[str]:
    status = git(repo, NO_LOCKS, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    entries = status.split("\0")
    big, skip_next = [], False
    for entry in entries[:max_entries]:
        if skip_next:  # the original path of a rename
            skip_next = False
            continue
        if len(entry) < 4:
            continue
        code, name = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            skip_next = True
        if "D" in code:
            continue
        try:
            if (repo / name).lstat().st_size > limit:
                big.append(name)
        except OSError:
            continue
    return big


def snapshot(repo: Path, message: str) -> str | None:
    """Commit everything in the working tree. Returns the new commit, or None if nothing changed."""
    return commit_all(repo, message).commit


def _uses_lfs(repo: Path) -> bool:
    """Whether the repository stores files with Git LFS (a tracked .gitattributes names the lfs filter; a
    machine-wide `git lfs install` alone doesn't count)."""
    return git(repo, NO_LOCKS, "grep", "-q", "-F", "filter=lfs", "HEAD", "--", ":(glob)**/.gitattributes",
               ".gitattributes").returncode == 0


def push(repo: Path, remote: str, branch: str) -> str:
    if _uses_lfs(repo):
        # Git LFS normally uploads from its pre-push hook, and hooks are not run here: upload explicitly.
        r = git(repo, "lfs", "push", remote, branch, timeout=3600)
        if r.returncode != 0:
            return "git lfs push: " + (r.stderr.strip() or r.stdout.strip())
    r = git(repo, "push", "-q", "-u", remote, branch, timeout=600)
    return "" if r.returncode == 0 else (r.stderr.strip() or r.stdout.strip())


def commits_since(repo: Path, base: str) -> int:
    if not base:
        return 0
    r = git(repo, "rev-list", "--count", f"{base}..HEAD")
    try:
        return int(r.stdout.strip() or 0) if r.returncode == 0 else 0
    except ValueError:
        return 0


def commits_after(repo: Path, since_iso: str) -> int:
    r = git(repo, "rev-list", "--count", f"--since={since_iso}", "HEAD")
    try:
        return int(r.stdout.strip() or 0) if r.returncode == 0 else 0
    except ValueError:
        return 0


def diffstat(repo: Path, base: str) -> str:
    if not base:
        return ""
    return git(repo, "diff", "--shortstat", base, "HEAD").stdout.strip()


def fingerprint(repo: Path, max_entries: int = 20000) -> str:
    """Identifies the current tree state including uncommitted changes, to detect progress.

    Uses the status plus size and modification time of every changed file, which is cheap even when an
    agent has left huge uncommitted files behind (no diff is computed).
    """
    status = git(repo, NO_LOCKS, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    h = hashlib.sha1(status.encode(errors="replace"))
    for i, entry in enumerate(status.split("\0")):
        if i >= max_entries:
            break
        name = entry[3:] if len(entry) > 3 else ""
        if not name:
            continue
        try:
            st = (repo / name).lstat()
            h.update(f"{name}\0{st.st_size}\0{st.st_mtime_ns}\0".encode(errors="replace"))
        except OSError:
            continue
    return f"{head(repo)}:{h.hexdigest()}"


def valid_branch_name(repo: Path, name: str) -> bool:
    return bool(name) and not name.startswith("-") and \
        git(repo, "check-ref-format", "--branch", name).returncode == 0
