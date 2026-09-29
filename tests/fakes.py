"""A forge double backed by real local Git repositories.

Branches, pushes and task-state files are real Git objects, so compare-and-
swap writes, the orphan state branch, shallow state listing, checkpoints and
lease-protected pushes all behave as they would against a real forge.
"""

import base64
from pathlib import Path
import subprocess

from bananavibe.config import Config, Model
from bananavibe.forge import Conflict, ForgeError

GIT_ENV = {"GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
           "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin:/usr/local/bin"}


def git(*args, cwd=None, input=None, env=None):
    result = subprocess.run(["git", *args], cwd=cwd, input=input, capture_output=True, text=True,
                            env={**GIT_ENV, **(env or {})})
    if result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def make_config(**changes):
    models = {"coding": Model("coding", "openai-compatible", "code-model", "https://models.example/v1", ""),
              "other": Model("other", "anthropic", "other-model", "https://models.example/v1", "")}
    values = dict(forge="github", server_url="https://forge.example", api_url="https://forge.example/api",
                  control_repository="team/prompts", target_repository="team/app", base_branch="main",
                  models=models, default_model="coding", checks=(("python3", "-m", "pytest"),))
    values.update(changes)
    return Config(**values)


def seed_repository(path, files):
    work = path.parent / (path.name + "-seed")
    work.mkdir()
    git("init", "-q", "-b", "main", str(work))
    for name, content in files.items():
        target = work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git("add", ".", cwd=work)
    git("commit", "-q", "-m", "Initial", cwd=work)
    git("clone", "-q", "--bare", str(work), str(path))
    return path


class LocalForge:
    def __init__(self, root, **config_changes):
        root = Path(root)
        self.config = make_config(**config_changes)
        self.token, self.github = "fixture-token", self.config.forge == "github"
        self.identity = {"login": "bananavibe-bot"}
        self.control = seed_repository(root / "control.git", {
            ".bananavibe.toml": "# trusted configuration\n",
            ".github/workflows/bananavibe.yml": "on: [issues]\n"})
        self.target = seed_repository(root / "target.git", {
            "app.py": "answer = 1\n", "test_app.py": "from app import answer\n", ".gitignore": "*.log\n"})
        self.maintainers = {"maintainer"}
        self.issues = {3: {"number": 3, "title": "Change the answer", "body": "PRIVATE: make the answer 2"}}
        self.comments_by_id, self.issue_state = {}, {}
        self.pulls = []
        self.calls = []

    @property
    def login(self):
        return self.identity["login"]

    @property
    def posts(self):
        return [body for (_, body) in self.comments_by_id.values()]

    def _repository(self, name):
        return self.control if name == self.config.control_repository else self.target

    def clone_url(self, repository):
        return str(self._repository(repository))

    def repo(self, name):
        return {"default_branch": "main", "permissions": {"push": True}, "full_name": name}

    def permission(self, repository, login):
        return "write" if login in self.maintainers else "read"

    def authorized(self, login):
        return login in self.maintainers

    def issue(self, number):
        if number not in self.issues:
            raise ForgeError(404, "read the issue")
        return dict(self.issues[number])

    def comments(self, number):
        return [{"id": key, "body": body} for key, (issue, body) in self.comments_by_id.items() if issue == number]

    def comment(self, number, text):
        key = len(self.comments_by_id) + 1
        self.comments_by_id[key] = (number, text)
        return key

    def edit_comment(self, key, text):
        number, _ = self.comments_by_id[key]
        self.comments_by_id[key] = (number, text)

    def set_issue_state(self, number, state):
        self.issue_state[number] = state

    def branch_sha(self, repository, name):
        result = subprocess.run(["git", "--git-dir", str(self._repository(repository)), "rev-parse", "--verify",
                                 "--quiet", f"refs/heads/{name}"], capture_output=True, text=True)
        return result.stdout.strip() or None

    def create_branch(self, repository, name, sha):
        if self.branch_sha(repository, name):
            raise Conflict(422, "create a branch")
        git("--git-dir", str(self._repository(repository)), "update-ref", f"refs/heads/{name}", sha)

    def _blob(self, path):
        head = self.branch_sha(self.config.control_repository, self.config.state_branch)
        if not head:
            return None, None
        entry = git("--git-dir", str(self.control), "ls-tree", head, "--", path)
        return head, (entry.split()[2] if entry else None)

    def content(self, path):
        head, blob = self._blob(path)
        if not blob:
            return None
        raw = subprocess.run(["git", "--git-dir", str(self.control), "cat-file", "blob", blob],
                             capture_output=True, check=True).stdout
        return {"type": "file", "sha": blob, "size": len(raw), "content": base64.b64encode(raw).decode()}

    def put_content(self, path, raw, previous_sha=None, message="state"):
        self.calls.append(("put", path))
        head, blob = self._blob(path)
        if not head:
            raise ForgeError(404, "save task state")
        if blob != previous_sha:
            raise Conflict(409, "save task state")
        repository = str(self.control)
        new_blob = subprocess.run(["git", "--git-dir", repository, "hash-object", "-w", "--stdin"], input=raw,
                                  capture_output=True, check=True).stdout.decode().strip()
        index = {"GIT_INDEX_FILE": str(self.control / "fixture.index")}
        git("--git-dir", repository, "read-tree", head, env=index)
        git("--git-dir", repository, "update-index", "--add", "--cacheinfo", f"100644,{new_blob},{path}", env=index)
        tree = git("--git-dir", repository, "write-tree", env=index)
        commit = git("--git-dir", repository, "commit-tree", tree, "-p", head, "-m", message)
        git("--git-dir", repository, "update-ref", f"refs/heads/{self.config.state_branch}", commit, head)
        return {"content": {"sha": new_blob}}

    def find_pull(self, branch):
        matches = [pull for pull in self.pulls if pull["head"]["ref"] == branch]
        return matches[-1] if matches else None

    @staticmethod
    def pull_open(pull):
        return pull["state"] == "open" and not pull.get("merged")

    def publish_pull(self, branch, title, body):
        existing = self.find_pull(branch)
        if existing:
            if not self.pull_open(existing):
                raise ValueError("The task's pull request was closed or merged.")
            existing.update(title=title, body=body, updates=existing.get("updates", 0) + 1)
            return existing
        pull = {"number": len(self.pulls) + 1, "head": {"ref": branch}, "state": "open", "title": title, "body": body,
                "html_url": f"https://forge.example/team/app/pull/{len(self.pulls) + 1}"}
        self.pulls.append(pull)
        return pull

    def show(self, branch, path):
        return git("--git-dir", str(self.target), "show", f"refs/heads/{branch}:{path}")
