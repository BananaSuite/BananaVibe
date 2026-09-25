"""Git metadata and credentials stay outside the directory mounted into OpenCode."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys


def read_task_json(project, name, limit):
    """Read a bounded regular report without traversing task-controlled links."""
    try:
        directory = os.open(Path(project) / ".bananavibe-task", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                return None
            raw = source.read(limit + 1)
        if len(raw) > limit:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def clear_task_report(project):
    try:
        directory = os.open(Path(project) / ".bananavibe-task", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.unlink("result.json", dir_fd=directory)
        finally:
            os.close(directory)
    except FileNotFoundError:
        pass


class Workspace:
    def __init__(self, root, forge, task_branch):
        self.root = Path(root)
        self.path = self.root / "project"
        self.git_dir = self.root / "repository.git"
        self.control = self.root / "control"
        self.forge = forge
        self.branch = task_branch
        self.path.mkdir(parents=True, exist_ok=True)
        self.control.mkdir(mode=0o700, parents=True, exist_ok=True)
        helper = self.control / "askpass.py"
        helper.write_text("#!" + sys.executable + "\nimport os,sys\nprint(os.environ['BANANA_GIT_USER'] if 'username' in sys.argv[1].lower() else os.environ['BANANA_GIT_CREDENTIAL'])\n")
        helper.chmod(0o700)
        self.environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
                            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
                            "GIT_ASKPASS": str(helper), "BANANA_GIT_USER": str(forge.identity.get("login", "bananavibe")),
                            "BANANA_GIT_CREDENTIAL": forge.token, "GIT_LFS_SKIP_SMUDGE": "1"}
        self.options = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                        "-c", "credential.helper=", "-c", "protocol.ext.allow=never", "-c", "http.followRedirects=false",
                        "-c", "core.attributesFile=/dev/null", "-c", "diff.external=", "-c", "core.autocrlf=false",
                        "-c", "user.name=BananaVibe", "-c", "user.email=bananavibe@users.noreply.local"]

    def git(self, *args, check=True, text=True, timeout=180):
        result = subprocess.run([*self.options, "--git-dir", str(self.git_dir), "--work-tree", str(self.path), *args],
                                cwd=self.path, env=self.environment, capture_output=True, text=text, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(f"Git {args[0]} failed (exit {result.returncode}). Check target access or branch protection.")
        return result

    def prepare(self):
        if not self.git_dir.exists():
            subprocess.run([*self.options, "init", "--bare", str(self.git_dir)], env=self.environment,
                           capture_output=True, check=True)
        self.git("config", "core.bare", "false")
        self.git("config", "core.worktree", str(self.path))
        self.git("fetch", "--no-tags", self.forge.clone_url(), f"refs/heads/{self.branch}", timeout=600)
        self.git("checkout", "-B", self.branch, "FETCH_HEAD")
        (self.git_dir / "info" / "exclude").write_text(".bananavibe-task/\n.git\n")
        # A root-run Forgejo job still executes model tools as an unprivileged UID.
        if os.geteuid() == 0:
            for path in (self.path, *self.path.rglob("*")):
                if not path.is_symlink():
                    os.chown(path, 65532, 65532)

    def files(self):
        raw = self.git("ls-files", "--cached", "--others", "--exclude-standard", "-z", text=False).stdout
        return [name.decode("utf-8", "strict") for name in raw.split(b"\0") if name and not name.startswith(b".bananavibe-task/")]

    def verify(self, base=None):
        maximum = self.forge.config.max_workspace_mb * 1024 * 1024
        total = 0
        for root, directories, files in os.walk(self.path, followlinks=False):
            for name in (*directories, *files):
                path = Path(root) / name
                if path.is_symlink():
                    resolved = path.resolve()
                    if not resolved.is_relative_to(self.path.resolve()):
                        raise ValueError("The task created a symlink outside its workspace.")
                elif path.is_file():
                    total += path.stat().st_size
                    if path.stat().st_size > 128 * 1024 * 1024:
                        raise ValueError("A task file exceeds 128 MiB.")
                elif not path.is_dir():
                    raise ValueError("Task workspaces cannot contain devices, sockets, or named pipes.")
                if total > maximum:
                    raise ValueError("The task workspace exceeded its configured storage allowance.")
        self.git("add", "--all", "--", ".")
        self.git("reset", "--quiet", "HEAD", "--", ".bananavibe-task")
        changed = self.git("diff", "--cached", "--name-only", "-z", base or "HEAD", text=False).stdout.split(b"\0")
        for raw in changed:
            if not raw:
                continue
            name = raw.decode("utf-8", "strict")
            if any(name == forbidden.rstrip("/") or name.startswith(forbidden.rstrip("/") + "/")
                   for forbidden in self.forge.config.forbidden_paths):
                raise ValueError("The task changed protected workflow or agent configuration. A maintainer must handle that change separately.")
        return [name.decode() for name in changed if name]

    def checkpoint(self):
        self.verify()
        if self.git("diff", "--cached", "--quiet", check=False).returncode:
            self.git("commit", "--no-gpg-sign", "-m", "Maintenance draft checkpoint")
        self.git("push", "--porcelain", self.forge.clone_url(), f"HEAD:refs/heads/{self.branch}")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def diff(self, base):
        # No external diff drivers, text conversion, or repository hooks execute.
        return self.git("diff", "--no-ext-diff", "--no-textconv", base, "HEAD", "--", ".").stdout[:120000]

    def final_files(self, base):
        return self.git("diff", "--name-only", "-z", base, "HEAD", text=False).stdout.decode().rstrip("\0").split("\0")

    def assert_formatting(self, base):
        if self.git("diff", "--check", base, "HEAD", check=False).returncode:
            raise ValueError("git diff --check failed. Correct whitespace errors before completing the task.")

    def tree_digest(self):
        return self.git("write-tree").stdout.strip()

    def read_report(self):
        data = read_task_json(self.path, "result.json", 32000)
        if data is None or data.get("status") not in {"complete", "blocked", "continue"}:
            return {"status": "continue"}
        return {"status": data["status"], "question": str(data.get("question", ""))[:6000]}
