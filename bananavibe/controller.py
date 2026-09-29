"""Turns forge events into recorded maintainer intent.

The controller never does task work itself: it validates who is asking,
records the request in the task's state with compare-and-swap, and tells
the caller which issue (if any) now needs a runner.
"""

import time

from .commands import HELP, Command, parse
from .state import ACTIVE, MAX_GUIDANCE, MAX_GUIDANCE_CHARS, StateStore, issue_digest, new_task

RESUMABLE = {"paused", "blocked", "failed", "stopped", "interrupted", "queued"}


class Controller:
    def __init__(self, forge, store=None, *, log=print):
        self.forge, self.config, self.log = forge, forge.config, log
        self.store = store or StateStore(forge)

    # Events

    def event(self, name, payload, actor=""):
        """Handle one Actions event. Returns an issue number to run, or None."""
        if name == "schedule":
            self.watchdog()
            return None
        if name == "workflow_dispatch":
            inputs = payload.get("inputs") or {}
            try:
                number = int(str(inputs.get("issue", "0")).strip().lstrip("#"))
            except ValueError:
                raise ValueError("The workflow_dispatch input 'issue' must be an issue number.") from None
            command = parse(str(inputs.get("command") or "/banana status"))
            event_id = f"dispatch:{payload.get('run_id') or time.time_ns()}"
        elif name in {"issues", "issue_comment"}:
            sender = payload.get("sender") or {}
            if sender.get("type") == "Bot":
                return None
            issue = payload.get("issue") or {}
            if issue.get("pull_request") or issue.get("is_pull"):
                return None
            number = int(issue.get("number") or 0)
            if name == "issues":
                if payload.get("action") != "opened" or not self.config.open_issues:
                    return None
                command, event_id = Command("start"), f"issue:{issue.get('id', number)}"
            else:
                if payload.get("action") != "created":
                    return None
                comment = payload.get("comment") or {}
                command, event_id = parse(comment.get("body") or ""), f"comment:{comment.get('id', '')}"
            actor = sender.get("login") or actor
        else:
            self.log(f"Ignoring unsupported event {name!r}.")
            return None
        if not command or number < 1 or not actor or actor == self.forge.login:
            return None
        repository = (payload.get("repository") or {}).get("full_name")
        if repository and repository.casefold() != self.config.control_repository.casefold():
            raise ValueError("The event came from a different repository than the configured control repository.")
        if not self.forge.authorized(actor):
            # Stay silent: replying would let anyone make the bot post comments.
            self.log(f"Ignoring /banana {command.name} from {actor}: no write access to both repositories.")
            return None
        self.log(f"Issue #{number}: /banana {command.name} from {actor}.")
        return self.command(number, command, actor, event_id)

    # Commands

    def command(self, number, command, actor, event_id):
        if command.name == "help":
            self.forge.comment(number, HELP)
            return None
        if command.name == "models":
            lines = [f"- `{alias}`: {model.provider} `{model.model}`" + (" (default)" if alias == self.config.default_model else "")
                     for alias, model in self.config.models.items()]
            self.forge.comment(number, "Configured models:\n\n" + "\n".join(lines) + "\n\nSwitch with `/banana model ALIAS`.")
            return None
        if command.name == "status":
            self.reconcile(number)
            state, _ = self.store.read(number)
            self.forge.comment(number, describe(state) if state else "No task yet. Use `/banana start` to begin.")
            return None
        if command.name == "model" and command.argument not in self.config.models:
            self.forge.comment(number, f"`{command.argument[:80]}` is not a configured model alias. "
                                       "Use `/banana models` to list them.")
            return None

        approved = ""
        if command.name in {"start", "restart", "resume", "answer"}:
            issue = self.forge.issue(number)
            if issue.get("pull_request") or issue.get("is_pull"):
                return None
            approved = issue_digest(issue)
        self.store.ensure()
        outcome = {"changed": False, "reopen": False, "note": ""}

        def update(state):
            outcome.update(changed=False, reopen=False, note="")
            if state and event_id in state["events"]:
                return None
            live = bool(state and state["runner"] and state["lease_until"] > self.store.clock())
            if not state:
                if command.name not in {"start", "restart"}:
                    return None
                state = new_task(self.config, number)
            complete = state["status"] == "complete"
            name = command.name
            if complete and name in {"start", "stop", "fail"}:
                return None
            if name in {"stop", "fail"}:
                if state["status"] in {"stopped", "failed"} and name == "stop":
                    return None
                state["desired"] = "failed" if name == "fail" else "stopped"
                state["status"] = "stopping" if live else state["desired"]
                state["reason"] = command.argument[:4000] if name == "fail" else "A maintainer stopped the task."
            elif name == "model":
                state["model"] = command.argument
            elif name == "restart":
                state.update(desired="running", status="running" if live else "queued", operation="restart",
                             approved=approved, reason="")
                outcome["reopen"] = complete
            elif name == "answer":
                guidance = state["guidance"]
                if len(guidance) >= MAX_GUIDANCE or sum(len(item.get("text", "")) for item in guidance) \
                        + len(command.argument) > MAX_GUIDANCE_CHARS:
                    outcome["note"] = "This task has reached its guidance limit; open a new issue instead."
                    return None
                guidance.append({"author": actor, "text": command.argument})
                state.update(desired="running", status="running" if live else "queued", approved=approved, reason="")
                if complete:
                    state["operation"] = "revise"
                    outcome["reopen"] = True
            elif name in {"start", "resume"}:
                if name == "start" and state["status"] in ACTIVE and state["desired"] == "running" and state["events"]:
                    return None
                state.update(desired="running", status="running" if live else "queued", approved=approved, reason="")
                if complete:
                    state["operation"] = "revise"
                    outcome["reopen"] = True
            # Every accepted command bumps the request counter, so a runner
            # that is just finishing notices it instead of losing it.
            state["request"] += 1
            state["events"].append(event_id)
            state["last_actor"] = actor
            outcome["changed"] = True
            return state

        state = self.store.mutate(number, update)
        if not outcome["changed"]:
            if outcome["note"]:
                self.forge.comment(number, outcome["note"])
            elif state is None:
                self.forge.comment(number, "There is no task for this issue yet. Use `/banana start` first.")
            elif event_id not in state["events"] and command.name not in {"start"}:
                self.forge.comment(number, describe(state))
            return None
        if outcome["reopen"]:
            self.forge.set_issue_state(number, "open")
        if command.name != "start":
            self.forge.comment(number, f"Recorded `/banana {command.name}`. " + describe(state))
        return number if state["desired"] == "running" else None

    # Recovery

    def reconcile(self, number):
        """Mark a task whose runner stopped renewing its lease as interrupted."""
        expired = {"value": False}

        def change(state):
            expired["value"] = False
            if not state or not state["runner"] or state["lease_until"] > self.store.clock():
                return None
            expired["value"] = True
            running = state["desired"] == "running"
            state.update(runner="", lease_until=0, status="interrupted" if running else state["desired"],
                         desired="paused" if running else state["desired"],
                         reason="The runner stopped renewing its lease (cancelled, timed out or lost).")
            return state

        state = self.store.mutate(number, change)
        if expired["value"]:
            self.forge.comment(number, "The previous runner stopped without finishing. " + describe(state))
        return state

    def watchdog(self):
        for state in self.store.all():
            if state["runner"] and state["lease_until"] <= self.store.clock():
                self.reconcile(state["issue"])


def describe(state):
    text = f"Task **{state['status']}**, model `{state['model']}`"
    if state.get("branch"):
        text += f", branch `{state['branch']}`"
    if state.get("checkpoint"):
        text += f", checkpoint `{state['checkpoint'][:12]}`"
    text += "."
    if state.get("pr_url"):
        text += f" [Draft pull request]({state['pr_url']})."
    if state.get("reason") and state["status"] not in {"running", "queued", "complete"}:
        text += f"\n\n> {state['reason'][:1500]}"
    if state["status"] in RESUMABLE - {"queued"}:
        text += ("\n\nNext: `/banana resume` to continue, `/banana answer TEXT` to add guidance, "
                 "or `/banana restart` to start over.")
    elif state["status"] == "complete":
        text += "\n\nTo revise the open pull request, use `/banana answer TEXT`."
    return text
