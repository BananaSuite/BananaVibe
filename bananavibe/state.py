"""Durable task metadata on the issue repository, with optimistic concurrency."""

import base64
import copy
import hashlib
import json
import time

from .forge import Conflict


def task_id(repository, number):
    return hashlib.sha256(f"{repository}#{int(number)}".encode()).hexdigest()[:16]


class StateStore:
    def __init__(self, forge, clock=time.time):
        self.forge, self.clock = forge, clock

    def ensure(self):
        config = self.forge.config
        if self.forge.branch_sha(config.control_repository, config.state_branch):
            return
        repo = self.forge.repo(config.control_repository)
        default = repo["default_branch"]
        if default == config.state_branch:
            raise ValueError("The state branch cannot be the control repository's default branch.")
        sha = self.forge.branch_sha(config.control_repository, default)
        if not sha:
            raise ValueError("Commit the workflow and configuration to the default branch first.")
        try:
            self.forge.create_branch(config.control_repository, config.state_branch, sha, source_branch=default)
        except Conflict:
            if not self.forge.branch_sha(config.control_repository, config.state_branch):
                raise

    def path(self, number):
        return f".bananavibe-state/tasks/{task_id(self.forge.config.control_repository, number)}.json"

    def read(self, number):
        record = self.forge.content(self.path(number))
        if not record:
            return None, None
        raw = base64.b64decode(record.get("content", ""))
        if len(raw) > 500000:
            raise ValueError("Task state is too large.")
        data = json.loads(raw)
        if data.get("issue") != int(number) or data.get("schema") != 1:
            raise ValueError("The saved task state is invalid.")
        return data, record["sha"]

    def mutate(self, number, change):
        for attempt in range(8):
            old, sha = self.read(number)
            new = change(copy.deepcopy(old))
            if new is None or new == old:
                return old
            new["updated_at"] = self.clock()
            encoded = json.dumps(new, sort_keys=True, separators=(",", ":")).encode()
            if len(encoded) > 500000:
                raise ValueError("Task state exceeds the storage limit.")
            try:
                self.forge.put_content(self.path(number), encoded, sha)
                return new
            except Conflict:
                if attempt == 7:
                    raise RuntimeError("Task state changed repeatedly; retry the command.") from None
                time.sleep(min(0.1 * 2 ** attempt, 2))

    def claim(self, number, runner):
        def change(state):
            if not state or state["desired"] != "running" or state["status"] == "complete":
                return None
            if state.get("lease_until", 0) > self.clock() and state.get("runner") != runner:
                return None
            state.update(runner=runner, lease_until=self.clock() + self.forge.config.lease_seconds, status="running")
            return state
        state = self.mutate(number, change)
        return state if state and state.get("runner") == runner else None

    def owned(self, number, runner, **changes):
        def change(state):
            if not state or state.get("runner") != runner:
                raise RuntimeError("This runner no longer owns the task lease.")
            state.update(changes)
            state["lease_until"] = self.clock() + self.forge.config.lease_seconds
            return state
        return self.mutate(number, change)

    def release(self, number, runner, **changes):
        def change(state):
            if not state or state.get("runner") != runner:
                return None
            state.update(changes, runner="", lease_until=0)
            return state
        return self.mutate(number, change)

    def all(self):
        entries = self.forge.content(".bananavibe-state/tasks") or []
        if not isinstance(entries, list):
            raise ValueError("Unexpected task state directory.")
        if len(entries) >= 1000:
            raise ValueError("Archive finished task records before the state directory reaches 1,000 entries.")
        for entry in entries:
            if entry.get("type") == "file" and entry.get("name", "").endswith(".json"):
                record = self.forge.content(entry["path"])
                if record and int(record.get("size", 0)) <= 500000:
                    data = json.loads(base64.b64decode(record["content"]))
                    if data.get("schema") == 1:
                        yield data
