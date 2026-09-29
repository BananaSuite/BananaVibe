"""Durable task records on the control repository's state branch.

Each task is one JSON file written through the contents API with the file's
previous blob SHA, so concurrent writers lose cleanly (compare-and-swap)
instead of overwriting each other. The format is schema 1, unchanged from
2.x, so tasks started by an older version continue after an upgrade.
"""

import base64
import copy
import hashlib
import json
import re
import tempfile
import time

from .forge import Conflict
from .gitutil import Git

SCHEMA = 1
TASK_BRANCH = re.compile(r"bananavibe/[0-9a-f]{16}-[1-9][0-9]*")
DIRECTORY = ".bananavibe-state/tasks"
MAX_BYTES = 500_000
MAX_EVENTS = 200
MAX_GUIDANCE = 100
MAX_GUIDANCE_CHARS = 200_000
ACTIVE = {"queued", "running", "stopping", "publishing"}
TERMINAL = {"complete"}
STATE_README = b"""This branch holds BananaVibe task records. It is managed automatically.
Do not merge it or delete it while tasks are active.
"""


def task_id(repository, number):
    return hashlib.sha256(f"{repository}#{int(number)}".encode()).hexdigest()[:16]


def issue_digest(issue):
    """Fingerprint of the issue text a maintainer approved."""
    text = f"{issue.get('title') or ''}\0{issue.get('body') or ''}"
    return hashlib.sha256(text.encode()).hexdigest()


def new_task(config, number):
    return {"schema": SCHEMA, "issue": int(number), "task_id": task_id(config.control_repository, number),
            "target_repository": config.target_repository, "base_branch": config.base_branch,
            "generation": 1, "branch": "", "base_sha": "", "checkpoint": "", "model": config.default_model,
            "status": "queued", "desired": "running", "request": 0, "operation": "continue", "runner": "",
            "lease_until": 0, "iterations": 0, "guidance": [], "events": [], "pr_url": "", "reason": "",
            "phase": "", "approved": "", "status_comment": 0}


def normalize(state):
    """Fill fields that older records may lack."""
    if state is None:
        return None
    defaults = {"generation": 1, "branch": "", "base_sha": "", "checkpoint": "", "request": 0,
                "operation": "continue", "runner": "", "lease_until": 0, "iterations": 0, "guidance": [],
                "events": [], "pr_url": "", "reason": "", "phase": "", "approved": "", "status_comment": 0}
    for key, value in defaults.items():
        state.setdefault(key, copy.deepcopy(value))
    return state


class StateError(RuntimeError):
    pass


class StateStore:
    def __init__(self, forge, *, clock=time.time, sleep=time.sleep):
        self.forge, self.config, self.clock, self.sleep = forge, forge.config, clock, sleep

    def path(self, number):
        return f"{DIRECTORY}/{task_id(self.config.control_repository, number)}.json"

    def ensure(self):
        """Create the state branch as an orphan branch holding only task records.

        Because it shares no files with the default branch, commits to it do
        not trigger the repository's push workflows.
        """
        repository = self.config.control_repository
        if self.forge.branch_sha(repository, self.config.state_branch):
            return
        if self.forge.repo(repository).get("default_branch") == self.config.state_branch:
            raise StateError("state_branch cannot be the control repository's default branch.")
        with tempfile.TemporaryDirectory(prefix="bananavibe-state-") as root:
            git = Git(f"{root}/state.git", token=self.forge.token, username=self.forge.login)
            blob = git.out("hash-object", "-w", "--stdin", input=STATE_README.decode())
            tree = git.out("mktree", input=f"100644 blob {blob}\tREADME.md\n")
            commit = git.out("commit-tree", tree, "-m", "Start BananaVibe task state")
            ref = "refs/heads/" + self.config.state_branch
            pushed = git.run("push", "--quiet", f"--force-with-lease={ref}:", self.forge.clone_url(repository),
                             f"{commit}:{ref}", check=False, timeout=120)
        if pushed.returncode and not self.forge.branch_sha(repository, self.config.state_branch):
            raise StateError("Could not create the state branch. The bot needs Contents write access "
                             "to the control repository.")

    def make_orphan(self):
        """Rewrite a 2.x state branch (a copy of the default branch) as an orphan.

        Keeps every task record, drops the copied project files, and pushes
        with a lease so a concurrent state write makes it fail harmlessly.
        Returns False when the branch already holds only task records.
        """
        repository, name = self.config.control_repository, self.config.state_branch
        if not self.forge.branch_sha(repository, name):
            raise StateError(f"The state branch {name} does not exist yet; nothing to convert.")
        if any(state["runner"] and state["lease_until"] > self.clock() for state in self.all()):
            raise StateError("A task is running. Wait for it to finish (or its lease to expire) first.")
        with tempfile.TemporaryDirectory(prefix="bananavibe-state-") as root:
            git = Git(f"{root}/state.git", token=self.forge.token, username=self.forge.login)
            url, ref = self.forge.clone_url(repository), f"refs/heads/{name}"
            git.fetch(url, ref, depth=1)
            head = git.out("rev-parse", "FETCH_HEAD")
            names = git.out("ls-tree", "--name-only", "FETCH_HEAD").split("\n")
            if set(names) <= {"README.md", ".bananavibe-state"} and git.run(
                    "rev-list", "--count", "FETCH_HEAD", check=False).stdout.strip() == "1":
                return False
            entries = [f"100644 blob {git.out('hash-object', '-w', '--stdin', input=STATE_README.decode())}\tREADME.md"]
            records = git.run("rev-parse", "--verify", "--quiet", "FETCH_HEAD:.bananavibe-state", check=False)
            if records.returncode == 0:
                entries.append(f"040000 tree {records.stdout.strip()}\t.bananavibe-state")
            tree = git.out("mktree", input="\n".join(entries) + "\n")
            commit = git.out("commit-tree", tree, "-m", "Keep only BananaVibe task records on the state branch")
            pushed = git.run("push", "--quiet", f"--force-with-lease={ref}:{head}", url, f"{commit}:{ref}",
                             check=False, timeout=120)
        if pushed.returncode:
            raise StateError("The state branch changed during conversion, or is protected against "
                             "force pushes. Retry, or allow the bot to force-push that branch once.")
        return True

    def read(self, number):
        record = self.forge.content(self.path(number))
        if not record:
            return None, None
        if not isinstance(record, dict) or record.get("type", "file") != "file":
            raise StateError("The task record is not a file.")
        if int(record.get("size") or 0) > MAX_BYTES:
            raise StateError("The task record is too large.")
        data = json.loads(base64.b64decode(record.get("content") or ""))
        if not isinstance(data, dict) or data.get("issue") != int(number) or data.get("schema") != SCHEMA:
            raise StateError("The saved task record is invalid.")
        return normalize(data), record["sha"]

    def mutate(self, number, change):
        """Apply change(copy) with compare-and-swap; None or no-op leaves it untouched."""
        for attempt in range(8):
            old, sha = self.read(number)
            new = change(copy.deepcopy(old))
            if new is None or new == old:
                return old
            new["events"] = new.get("events", [])[-MAX_EVENTS:]
            new["updated_at"] = self.clock()
            encoded = json.dumps(new, sort_keys=True, separators=(",", ":")).encode()
            if len(encoded) > MAX_BYTES:
                raise StateError("The task record would exceed its size limit. Start a new issue.")
            try:
                self.forge.put_content(self.path(number), encoded, sha)
                return new
            except Conflict:
                self.sleep(min(0.2 * 2 ** attempt, 3))
        raise StateError("The task record kept changing; retry the command.")

    def claim(self, number, runner):
        def change(state):
            if not state or state["desired"] != "running" or state["status"] in TERMINAL:
                return None
            if state["runner"] and state["runner"] != runner and state["lease_until"] > self.clock():
                return None
            state.update(runner=runner, lease_until=self.clock() + self.config.limits.lease_seconds, status="running")
            return state
        state = self.mutate(number, change)
        return state if state and state.get("runner") == runner else None

    def owned(self, number, runner, **changes):
        def change(state):
            if not state or state["runner"] != runner:
                raise LeaseLost("This runner no longer holds the task lease.")
            state.update(changes)
            state["lease_until"] = self.clock() + self.config.limits.lease_seconds
            return state
        return self.mutate(number, change)

    def release(self, number, runner, **changes):
        def change(state):
            if not state or state["runner"] != runner:
                return None
            state.update(changes, runner="", lease_until=0)
            return state
        return self.mutate(number, change)

    def all(self):
        """Every task record, read through one shallow Git fetch of the state branch."""
        with tempfile.TemporaryDirectory(prefix="bananavibe-state-") as root:
            git = Git(f"{root}/state.git", token=self.forge.token, username=self.forge.login)
            if not self.forge.branch_sha(self.config.control_repository, self.config.state_branch):
                return []
            git.fetch(self.forge.clone_url(self.config.control_repository),
                      f"refs/heads/{self.config.state_branch}", depth=1)
            files = git.read_files("FETCH_HEAD", DIRECTORY + "/", limit=MAX_BYTES)
        records = []
        for path, raw in sorted(files.items()):
            if not path.endswith(".json"):
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("schema") == SCHEMA and type(data.get("issue")) is int:
                records.append(normalize(data))
        return records


class LeaseLost(StateError):
    pass
