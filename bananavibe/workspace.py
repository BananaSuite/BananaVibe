"""The task's working tree. Git metadata and credentials stay outside the
directory that is mounted into the sandbox."""

import json
import os
from pathlib import Path
import shutil
import stat

from .gitutil import Git, GitError

REPORT_DIR = ".bananavibe-task"
SANDBOX_UID = 65532


class BranchMoved(RuntimeError):
    pass


def read_report(project, name, limit):
    """Read a bounded JSON object from the report directory without following links."""
    try:
        directory = os.open(Path(project) / REPORT_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                return None
            raw = source.read(limit + 1)
        data = json.loads(raw) if len(raw) <= limit else None
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def clear_report(project, name="result.json"):
    """Remove a previous report; a linked report directory is left alone."""
    try:
        directory = os.open(Path(project) / REPORT_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    except OSError:
        raise OSError("The task report directory is not a plain directory.") from None
    try:
        os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass
    finally:
        os.close(directory)


def give_to_sandbox(root):
    """When the runner is root, hand the tree to the unprivileged sandbox user."""
    if os.geteuid() != 0:
        return
    for directory, directories, files in os.walk(root):
        for name in (*directories, *files):
            os.lchown(os.path.join(directory, name), SANDBOX_UID, SANDBOX_UID)
    os.lchown(root, SANDBOX_UID, SANDBOX_UID)


class Workspace:
    def __init__(self, root, forge, branch):
        self.root, self.forge, self.config, self.branch = Path(root), forge, forge.config, branch
        self.path = self.root / "project"
        self.path.mkdir(parents=True, exist_ok=True)
        self.url = forge.clone_url(self.config.target_repository)
        self.git = Git(self.root / "repository.git", work_tree=self.path, token=forge.token, username=forge.login,
                       control_dir=self.root / "control")
        self.remote_sha = None

    def prepare(self, expected_sha):
        """Check out the task branch and confirm it is the recorded checkpoint."""
        self.git.fetch(self.url, f"refs/heads/{self.branch}")
        head = self.git.out("rev-parse", "FETCH_HEAD")
        if expected_sha and head != expected_sha:
            raise BranchMoved(f"The task branch `{self.branch}` has commits BananaVibe did not make "
                              f"(expected `{expected_sha[:12]}`, found `{head[:12]}`). Review them, then "
                              "use `/banana restart`, or reset the branch to the checkpoint and resume.")
        self.git.run("checkout", "--quiet", "--force", "-B", self.branch, "FETCH_HEAD")
        self.remote_sha = head
        exclude = self.root / "repository.git" / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        exclude.write_text("\n".join((f"/{REPORT_DIR}/", *self.config.ignore)) + "\n")
        (self.path / REPORT_DIR).mkdir(exist_ok=True)
        give_to_sandbox(self.path)

    def head(self):
        return self.git.out("rev-parse", "HEAD")

    def _protected(self, name):
        return any(name == path.rstrip("/") or name.startswith(path.rstrip("/") + "/")
                   for path in self.config.forbidden_paths)

    def size_mb(self):
        total = 0
        for directory, _, files in os.walk(self.path):
            for name in files:
                try:
                    total += os.lstat(os.path.join(directory, name)).st_size
                except OSError:
                    pass
        return total / (1024 * 1024)

    def snapshot(self, message="Checkpoint maintenance draft"):
        """Commit the working tree. Returns the list of reverted paths.

        Changes to protected paths, embedded repositories and oversized
        files are undone rather than committed, and the caller tells the
        agent why. Call this only while the sandbox is paused or stopped.
        """
        if self.size_mb() > self.config.limits.max_workspace_mb:
            raise ValueError(f"The workspace exceeded limits.max_workspace_mb "
                             f"({self.config.limits.max_workspace_mb} MiB).")
        self.git.run("add", "--all", "--", ".")
        rejected, blobs = {}, {}
        raw = self.git.run("diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev", "HEAD", text=False).stdout
        fields = raw.split(b"\0")
        for info, name in zip(fields[0::2], fields[1::2], strict=False):
            if not info.startswith(b":"):
                continue
            old_mode, new_mode, _, new_oid, _ = info[1:].decode().split()
            name = name.decode("utf-8", "surrogateescape")
            if self._protected(name):
                rejected[name] = "protected path"
            elif new_mode == "160000" and old_mode != "160000":
                rejected[name] = "embedded Git repository"
            elif new_mode in {"100644", "100755"}:
                blobs[name] = new_oid
        if blobs:
            sizes = self.git.run("cat-file", "--batch-check=%(objectsize)",
                                 input="".join(oid + "\n" for oid in blobs.values())).stdout.split()
            for name, size in zip(blobs, sizes, strict=True):
                if int(size) > 50 * 1024 * 1024:
                    rejected[name] = "file larger than 50 MiB"
        for name in rejected:
            self._revert(name)
        if self.git.run("diff", "--cached", "--quiet", "HEAD", check=False).returncode:
            self.git.run("commit", "--quiet", "--no-verify", "-m", message)
        return rejected

    def _revert(self, name):
        self.git.run("reset", "--quiet", "HEAD", "--", name, check=False)
        if self.git.run("cat-file", "-e", f"HEAD:{name}", check=False).returncode == 0:
            self.git.run("checkout", "--quiet", "HEAD", "--", name)
        else:
            target = self.path / name
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()

    def push(self):
        """Push HEAD, but only if the remote branch is still where we left it."""
        head = self.head()
        if head == self.remote_sha:
            return head
        ref = f"refs/heads/{self.branch}"
        result = self.git.run("push", "--quiet", "--porcelain", f"--force-with-lease={ref}:{self.remote_sha}",
                              self.url, f"HEAD:{ref}", check=False)
        if result.returncode:
            output = result.stdout + result.stderr
            if any(reason in output for reason in ("stale info", "fetch first", "non-fast-forward")):
                raise BranchMoved(f"The task branch `{self.branch}` changed outside BananaVibe. "
                                  "Review it before resuming.")
            raise GitError("git push failed. Check that the bot can push to the target repository "
                           "and that bananavibe/* branches are not protected.")
        self.remote_sha = head
        return head

    def export(self, destination):
        """Write the committed tree (not the working tree) to destination."""
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        index = self.root / "export.index"
        env = {"GIT_INDEX_FILE": str(index)}
        self.git.run("read-tree", "HEAD", env=env)
        self.git.run("checkout-index", "--all", "--force", f"--prefix={destination}/", env=env)
        index.unlink(missing_ok=True)
        give_to_sandbox(destination)
        return destination

    def changed_files(self, base):
        raw = self.git.run("diff", "--name-only", "-z", base, "HEAD", text=False).stdout
        return [item.decode("utf-8", "replace") for item in raw.split(b"\0") if item]

    def diff(self, base, limit=120_000):
        text = self.git.run("diff", "--no-ext-diff", "--no-textconv", "--stat", "--patch", base, "HEAD").stdout
        return text if len(text) <= limit else text[:limit] + "\n[diff truncated]\n"

    def whitespace_errors(self, base):
        result = self.git.run("diff", "--check", base, "HEAD", check=False)
        return result.stdout[-4000:] if result.returncode else ""
