"""Maintainer commands and their durable intent, independent of running tasks."""

import time

from .commands import Command, HELP, parse
from .state import StateStore, task_id


class Controller:
    def __init__(self, forge, store=None):
        self.forge = forge
        self.config = forge.config
        self.store = store or StateStore(forge)

    def event(self, name, event, actor=""):
        if name == "schedule":
            return self.watchdog()
        if name == "workflow_dispatch":
            inputs = event.get("inputs", {})
            number = int(inputs.get("issue", 0))
            command = parse(inputs.get("command", "/banana status"))
            event_id = "manual:" + str(event.get("run_id", time.time_ns()))
        elif name in {"issues", "issue_comment"}:
            if event.get("sender", {}).get("type") == "Bot":
                return None
            issue = event.get("issue", {})
            if issue.get("pull_request") or issue.get("is_pull"):
                return None
            number = int(issue.get("number", 0))
            if name == "issues":
                if event.get("action") != "opened" or not self.config.open_issues:
                    return None
                command = Command("start")
                event_id = "issue:" + str(issue.get("id", number))
            else:
                if event.get("action") != "created":
                    return None
                comment = event.get("comment", {})
                command = parse(comment.get("body", ""))
                event_id = "comment:" + str(comment.get("id", ""))
            actor = event.get("sender", {}).get("login", actor)
        else:
            return None
        if not command or number < 1 or actor == self.forge.identity.get("login"):
            return None
        actual_repo = event.get("repository", {}).get("full_name")
        if actual_repo and actual_repo.casefold() != self.config.control_repository.casefold():
            raise ValueError("The event repository does not match the trusted control repository.")
        if not self.forge.authorized(actor):
            # No comment for unauthorized users: avoid turning public issues into a bot spam endpoint.
            return None
        return self.command(number, command, actor, event_id)

    def command(self, number, command, actor, event_id):
        if command.name == "help":
            self.forge.comment(number, HELP)
            return None
        if command.name == "models":
            lines = [f"- `{alias}`: {model.provider}, `{model.model}`" for alias, model in self.config.models.items()]
            self.forge.comment(number, "Configured models:\n\n" + "\n".join(lines) + "\n\nSelect one with `/banana model ALIAS`.")
            return None
        if command.name == "status":
            self.reconcile_expired(number)
            state, _ = self.store.read(number)
            text = "No task yet. A maintainer can use `/banana start`." if not state else self.describe(state)
            self.forge.comment(number, text)
            return None
        if command.name == "model" and command.argument not in self.config.models:
            self.forge.comment(number, "That model alias is not configured. Use `/banana models` to list the available aliases.")
            return None
        self.store.ensure()
        changed = False
        def update(state):
            nonlocal changed
            if state and event_id in state.get("events", []):
                return None
            if state and state["status"] == "complete" and command.name != "restart":
                return None
            if not state:
                if command.name not in {"start", "restart"}:
                    return None
                state = {"schema": 1, "issue": int(number), "task_id": task_id(self.config.control_repository, number),
                         "target_repository": self.config.target_repository, "base_branch": self.config.base_branch,
                         "generation": 1, "branch": "", "base_sha": "", "checkpoint": "", "model": self.config.default_model,
                         "status": "queued", "desired": "running", "request": 0, "operation": "continue", "runner": "",
                         "lease_until": 0, "iterations": 0, "guidance": [], "events": [], "pr_url": ""}
            state["events"] = (state.get("events", []) + [event_id])[-500:]
            state["last_actor"] = actor
            changed = True
            active = state.get("lease_until", 0) > self.store.clock() and state.get("runner")
            if command.name in {"stop", "fail"}:
                state["desired"] = "failed" if command.name == "fail" else "stopped"
                state["status"] = "stopping" if active else state["desired"]
                state["reason"] = command.argument[:6000]
                state["request"] += 1
            elif command.name == "restart":
                state.update(desired="running", status="running" if active else "queued", operation="restart")
                state["request"] += 1
            elif command.name == "model":
                state["model"] = command.argument
                state["request"] += 1
            elif command.name == "answer":
                if len(state.get("guidance", [])) >= 100:
                    raise ValueError("This task has reached its guidance limit. Start a new issue with the remaining requirements.")
                state["guidance"].append({"author": actor, "text": command.argument})
                state.update(desired="running", status="running" if active else "queued")
                state["request"] += 1
            elif command.name in {"start", "resume"}:
                state.update(desired="running", status="running" if active else "queued")
            return state
        state = self.store.mutate(number, update)
        if not changed:
            if state and state["status"] == "complete":
                self.forge.comment(number, self.describe(state))
            elif not state:
                self.forge.comment(number, "There is no saved task. Use `/banana start` first.")
            return None
        if command.name == "restart":
            self.forge.reopen_issue(number)
        if command.name != "start":
            self.forge.comment(number, f"Recorded `/banana {command.name}`. " + self.describe(state))
        return number if state["desired"] == "running" else None

    def describe(self, state):
        text = f"Task state: **{state['status']}**. Model: `{state['model']}`."
        if state.get("branch"):
            text += f" Working branch: `{state['branch']}`."
        if state.get("checkpoint"):
            text += f" Saved checkpoint: `{state['checkpoint'][:12]}`."
        if state.get("pr_url"):
            text += f" [Review the draft pull request]({state['pr_url']})."
        if state["status"] in {"paused", "blocked", "failed", "stopped", "interrupted"}:
            text += " Use `/banana resume` to continue, `/banana answer TEXT` to provide guidance, or `/banana restart` to start a fresh branch."
        return text

    def reconcile_expired(self, number):
        expired = False
        def change(state):
            nonlocal expired
            if state and state.get("runner") and state.get("lease_until", 0) <= self.store.clock():
                expired = True
                state.update(runner="", lease_until=0, status="interrupted" if state["desired"] == "running" else state["desired"])
                if state["desired"] == "running":
                    state["desired"] = "paused"
                return state
            return None
        state = self.store.mutate(number, change)
        if expired:
            self.forge.comment(number, "The previous runner stopped sending heartbeats. No completion was recorded. " + self.describe(state))

    def watchdog(self):
        for state in self.store.all():
            self.reconcile_expired(state["issue"])
        return None
