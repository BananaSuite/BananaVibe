"""One task run: claim the lease, work in the sandbox, check, publish, release.

The loop is bounded by wall-clock minutes and agent iterations. Every
iteration ends with a pushed checkpoint, so a stop, a crash or an exhausted
budget leaves resumable work on the task branch. Completion needs the
agent's claim *and* passing checks on a clean copy of the committed tree.
"""

import json
import secrets
import shutil
import signal
import tempfile
import threading
import time

from . import summary
from .engine import Engine, EngineError, ensure_image
from .forge import Conflict, ForgeError
from .gitutil import GitError
from .state import TASK_BRANCH, LeaseLost, StateError, issue_digest
from .workspace import BranchMoved, Workspace, clear_report, read_report

INSTRUCTIONS = """You are preparing a change that a maintainer will review as a draft pull request.
Implement the full request below, following the repository's own conventions and contribution guidance.
Run the project's tests yourself while you work. A plan or partial implementation is not completion.
Do not push, merge, open pull requests or contact anyone; BananaVibe publishes your work after review checks.
Never copy the issue text, maintainer guidance, credentials or this conversation into files.
Do not edit these protected paths (changes to them are discarded): {protected}.
When the work is complete, write .bananavibe-task/result.json containing {{"status": "complete"}}.
If you cannot continue without a decision from a person, write {{"status": "blocked", "question": "..."}}
with one precise question. Otherwise keep working, or write {{"status": "continue"}}.
After you report completion, BananaVibe runs these checks on a clean checkout of your committed files:
{checks}
"""

CONTINUE = ("Continue the implementation. When everything requested is done and the checks pass, write the "
            "completion report as instructed; if you need a decision, write a blocked report with one question.")


class Interrupted(Exception):
    """Stop working now; `outcome` is what the task becomes."""

    def __init__(self, outcome, reason):
        super().__init__(reason)
        self.outcome, self.reason = outcome, reason


class Redirected(Exception):
    """A maintainer changed the task while it ran: new guidance, model or restart."""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


class TaskError(RuntimeError):
    pass


EXPECTED = (TaskError, EngineError, GitError, BranchMoved, ForgeError, StateError, ValueError, OSError, RuntimeError)


class TaskRunner:
    def __init__(self, forge, store, number, *, engine_factory=Engine, workspace_factory=Workspace,
                 image_factory=ensure_image, describe_pull=summary.describe, clock=time.monotonic, log=print):
        self.forge, self.store, self.number, self.config = forge, store, int(number), forge.config
        self.engine_factory, self.workspace_factory = engine_factory, workspace_factory
        self.image_factory, self.describe_pull, self.clock, self.log = image_factory, describe_pull, clock, log
        self.runner = secrets.token_hex(16)
        self.lock = threading.RLock()
        self.state = None
        self.phase = "Starting"
        self.deadline = None
        self.started = clock()
        self.seen_request = None
        self.model_alias = None
        self.guidance_seen = 0
        self.notes = []
        self.heartbeat_error = None
        self.stopping = threading.Event()
        self.cancelled = threading.Event()
        self.status_comment = None
        self.last_progress = clock()
        self.issue = None

    # Durable state (every access holds the lock, so the heartbeat thread
    # and the main thread never interleave compare-and-swap cycles).

    def _update(self, **changes):
        with self.lock:
            self.state = self.store.owned(self.number, self.runner, **changes)
            return self.state

    def _fresh(self):
        with self.lock:
            state, _ = self.store.read(self.number)
            if not state or state["runner"] != self.runner:
                raise LeaseLost("Another runner took over this task.")
            self.state = state
            return state

    def _heartbeat(self):
        lease = self.config.limits.lease_seconds
        failing_since = None
        while not self.stopping.wait(self.config.limits.poll_seconds):
            try:
                with self.lock:
                    state = self._fresh()
                    if state["lease_until"] - self.store.clock() < lease / 2:
                        self._update(phase=self.phase)
                failing_since = None
            except LeaseLost as error:
                self.heartbeat_error = error
                return
            except Exception as error:
                # Ride out forge hiccups; give up only while the lease is still ours.
                failing_since = failing_since or self.clock()
                self.log(f"Heartbeat failed ({type(error).__name__}); retrying.")
                if self.clock() - failing_since >= lease / 3:
                    self.heartbeat_error = error
                    return
                continue
            if self.clock() - self.last_progress >= self.config.limits.progress_seconds:
                self._progress()

    def pulse(self, phase=None):
        """Called often by long operations; raises when the task must stop or change."""
        if phase:
            self.phase = phase
        if isinstance(self.heartbeat_error, LeaseLost):
            raise self.heartbeat_error
        if self.heartbeat_error:
            raise Interrupted("paused", "BananaVibe lost contact with the forge and could not renew its lease "
                                        f"({type(self.heartbeat_error).__name__}).")
        if self.cancelled.is_set():
            raise Interrupted("paused", "The workflow run was cancelled.")
        if self.deadline and self.clock() >= self.deadline:
            raise Interrupted("paused", f"The {self.config.limits.max_minutes}-minute allowance for one run ended.")
        with self.lock:
            state = self.state
        if state["desired"] != "running":
            outcome = state["desired"] if state["desired"] in {"stopped", "failed"} else "paused"
            raise Interrupted(outcome, state.get("reason") or "A maintainer stopped the task.")
        if self.seen_request is not None and state["request"] != self.seen_request:
            if state["operation"] == "restart":
                raise Redirected("restart")
            if state["model"] != self.model_alias:
                raise Redirected("model")
            if len(state["guidance"]) > self.guidance_seen:
                raise Redirected("guidance")
            self.seen_request = state["request"]

    # Issue comments

    def _status_text(self, final=None):
        state = self.state or {}
        if final:
            return f"**BananaVibe** finished this run: **{final}**. Details below."
        minutes = int((self.clock() - self.started) / 60)
        lines = [f"**BananaVibe is working on this issue.** {self.phase}.",
                 f"Iteration {state.get('iterations', 0)}, {minutes} min elapsed, model `{state.get('model', '?')}`."]
        if state.get("branch"):
            lines.append(f"Branch `{state['branch']}`" + (f", checkpoint `{state['checkpoint'][:12]}`"
                                                           if state.get("checkpoint") else "") + ".")
        lines.append("Use `/banana stop` to stop or `/banana answer TEXT` to add guidance. "
                     f"_Updated {time.strftime('%H:%M UTC', time.gmtime())}._")
        return "\n".join(lines)

    def _progress(self, final=None):
        self.last_progress = self.clock()
        try:
            if self.status_comment:
                self.forge.edit_comment(self.status_comment, self._status_text(final))
            elif not final:
                self.status_comment = self.forge.comment(self.number, self._status_text())
        except (ForgeError, RuntimeError) as error:
            self.log(f"Could not update the status comment: {error}")

    # Run

    def run(self):
        with self.lock:
            self.state = self.store.claim(self.number, self.runner)
        if not self.state:
            self.log(f"Issue #{self.number} is not runnable or another runner holds it.")
            return "busy"
        self.log(f"Issue #{self.number}: runner {self.runner[:8]} claimed the task.")
        self._progress()
        worker = threading.Thread(target=self._heartbeat, name="bananavibe-heartbeat", daemon=True)
        worker.start()
        previous = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, lambda *_: self.cancelled.set())
        try:
            outcome, reason = self._run()
        except Interrupted as stop:
            outcome, reason = stop.outcome, stop.reason
        except LeaseLost as error:
            outcome, reason = "lost", str(error)
        except EXPECTED as error:
            outcome, reason = "failed", str(error) or type(error).__name__
        finally:
            self.stopping.set()
            worker.join(timeout=60)
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        return self._finish(outcome, reason)

    def _run(self):
        self.phase = "Preparing the sandbox image"
        image = self.image_factory(self.config, self.pulse, self.log)
        self.deadline = self.clock() + self.config.limits.max_minutes * 60
        while True:
            state = self._initialize()
            with tempfile.TemporaryDirectory(prefix="bananavibe-task-") as root:
                workspace = self.workspace_factory(root, self.forge, state["branch"])
                self.pulse("Restoring the task branch")
                workspace.prepare(state["checkpoint"])
                try:
                    outcome, reason = self._work(workspace, image)
                except Redirected as change:
                    self._save(workspace)
                    self.log(f"Maintainer changed the task ({change.kind}); restarting the session.")
                    continue
                except LeaseLost:
                    raise  # another runner owns the branch now; do not push to it
                except (Interrupted, *EXPECTED):
                    self._save(workspace)
                    raise
            if outcome != "complete" and self._more_requested():
                continue
            return outcome, reason

    def _more_requested(self):
        """True when a maintainer asked for more after this run last looked."""
        state = self._fresh()
        return (state["desired"] == "running" and state["request"] != self.seen_request
                and self.deadline and self.clock() < self.deadline - 60)

    def _initialize(self):
        state = self._fresh()
        if state["model"] not in self.config.models:
            raise TaskError(f"The model alias `{state['model']}` is no longer configured. "
                            "Choose one with `/banana models` and `/banana model ALIAS`.")
        self.config.models[state["model"]].api_key()
        if (state["target_repository"] != self.config.target_repository
                or state["base_branch"] != self.config.base_branch):
            raise TaskError("The target repository or base branch changed since this task started. "
                            "Use `/banana restart` to start over against the new target.")
        issue = self.forge.issue(self.number)
        if issue.get("pull_request") or issue.get("is_pull"):
            raise TaskError("Tasks must come from an issue, not a pull request.")
        digest = issue_digest(issue)
        if state["approved"] and state["approved"] != digest:
            raise Interrupted("blocked", "The issue title or description changed after a maintainer approved it. "
                                         "Review the edit, then use `/banana resume` to approve the current text.")
        self.issue = issue
        changes = {"approved": digest}
        if state["operation"] == "restart":
            changes.update(generation=state["generation"] + 1, branch="", base_sha="", checkpoint="",
                           iterations=0, pr_url="")
        elif state["operation"] == "revise" and state["pr_url"] and state["branch"]:
            pull = self.forge.find_pull(state["branch"])
            if pull and not self.forge.pull_open(pull):
                raise TaskError("The task's pull request was closed or merged, so it cannot be revised. "
                                "Use `/banana restart` for a new contribution.")
        changes["operation"] = "continue"
        state = self._update(**changes)
        self.seen_request, self.model_alias = state["request"], state["model"]
        self.guidance_seen = len(state["guidance"])
        if not state["branch"]:
            state = self._create_branch(state)
        elif not TASK_BRANCH.fullmatch(state["branch"]):
            # The state branch is writable by anyone with repository access;
            # never let an edited record aim the bot at another branch.
            raise TaskError("The saved branch is not a BananaVibe task branch. Use `/banana restart`.")
        return state

    def _create_branch(self, state):
        target = self.config.target_repository
        info = self.forge.repo(target)
        if info.get("archived") or (info.get("permissions") or {}).get("push") is False:
            raise TaskError(f"The bot cannot push to {target} (archived, or no write permission).")
        sha = self.forge.branch_sha(target, self.config.base_branch)
        if not sha:
            raise TaskError(f"The base branch `{self.config.base_branch}` does not exist in {target}.")
        branch = f"bananavibe/{state['task_id']}-{state['generation']}"
        try:
            self.forge.create_branch(target, branch, sha)
        except Conflict:
            # A lost response to an earlier attempt may already have created it.
            if self.forge.branch_sha(target, branch) != sha:
                raise TaskError(f"`{branch}` already exists with other work. Use `/banana restart`.") from None
        self.log(f"Created {branch} from {sha[:12]}.")
        return self._update(branch=branch, base_sha=sha, checkpoint=sha)

    # Work

    def _prompt(self):
        issue, state = self.issue, self.state
        guidance = "\n\n".join(f"- {item['text']}" for item in state["guidance"]) or "(none)"
        checks = "\n".join(f"- {json.dumps(list(command))}" for command in self.config.checks)
        protected = ", ".join(self.config.forbidden_paths) or "(none)"
        return (INSTRUCTIONS.format(protected=protected, checks=checks)
                + f"\nIssue title:\n{str(issue.get('title') or '')[:1000]}\n"
                + f"\nIssue description:\n{str(issue.get('body') or '')[:60000]}\n"
                + f"\nMaintainer guidance so far:\n{guidance[:100000]}\n"
                + "\nThe files may already contain earlier work on this task; continue from them.")

    def _work(self, workspace, image):
        state = self.state
        model = self.config.models[state["model"]]
        prompt = self._prompt()
        with self.engine_factory(self.config, model, workspace.path, self.pulse, image=image) as engine:
            engine.prepare()
            for _ in range(self.config.limits.max_iterations):
                try:
                    self.pulse("Working on the task")
                    self._update(iterations=self.state["iterations"] + 1)
                    clear_report(workspace.path)
                    reply = engine.prompt(prompt)
                    rejected = self._checkpoint(workspace, engine)
                    if reply["status"] == "blocked":
                        return "blocked", reply["question"]
                    report = read_report(workspace.path, "result.json", 32_000) or {}
                    status = report.get("status") if report.get("status") in {"complete", "blocked"} else "continue"
                    note = self._rejection_note(rejected)
                    if status == "blocked":
                        return "blocked", str(report.get("question") or "The coding agent needs guidance.")[:4000]
                    if status == "continue" or note:
                        prompt = (note + "\n\n" if note else "") + CONTINUE
                        continue
                    if self.state["checkpoint"] == self.state["base_sha"]:
                        return "blocked", ("The agent reported completion without changing any files. "
                                           "Clarify what should change with `/banana answer TEXT`.")
                    failures = self._validate(workspace, engine)
                    if failures:
                        prompt = ("The acceptance checks failed on a clean checkout of your committed files. "
                                  "Fix the causes (do not weaken the checks), then report completion again.\n\n"
                                  + "\n\n".join(failures))[:40000]
                        continue
                    return self._publish(workspace, model)
                except Redirected as change:
                    if change.kind != "guidance":
                        raise
                    prompt = self._new_guidance()  # mark it seen before anything pulses again
                    engine.interrupt()
                    self._checkpoint(workspace, engine)
        return "paused", (f"The {self.config.limits.max_iterations}-iteration allowance for one run ended "
                          "before the task was complete and checked.")

    def _new_guidance(self):
        state = self.state
        fresh = state["guidance"][self.guidance_seen:]
        self.guidance_seen, self.seen_request = len(state["guidance"]), state["request"]
        return ("A maintainer added guidance while you were working. Apply it, then continue:\n\n"
                + "\n\n".join(f"- {item['text']}" for item in fresh))

    @staticmethod
    def _rejection_note(rejected):
        if not rejected:
            return ""
        listed = "\n".join(f"- `{name}` ({why})" for name, why in sorted(rejected.items())[:50])
        return ("These changes were discarded because BananaVibe may not commit them. A maintainer must make "
                "such changes; adapt the work without them:\n" + listed)

    def _checkpoint(self, workspace, engine):
        self.pulse("Saving a checkpoint")
        with engine.paused():
            rejected = workspace.snapshot()
        sha = workspace.push()
        if sha != self.state["checkpoint"]:
            self._update(checkpoint=sha)
        return rejected

    def _save(self, workspace):
        """Best-effort final checkpoint after the engine has stopped."""
        try:
            workspace.snapshot()
            sha = workspace.push()
            if sha != self.state["checkpoint"]:
                self._update(checkpoint=sha)
        except (GitError, BranchMoved, StateError, ForgeError, ValueError, OSError, RuntimeError) as error:
            self.log(f"The final checkpoint could not be saved: {error}")

    def _validate(self, workspace, engine):
        base = self.state["base_sha"]
        failures = []
        whitespace = workspace.whitespace_errors(base)
        if whitespace:
            failures.append("`git diff --check` found whitespace errors:\n" + whitespace)
        tree = workspace.root / "check-tree"
        try:
            workspace.export(tree)
            failures += engine.check(tree)
        finally:
            shutil.rmtree(tree, ignore_errors=True)
        return failures

    def _publish(self, workspace, model):
        self.pulse("Opening the pull request")
        state = self._fresh()
        branch, base, head = state["branch"], state["base_sha"], workspace.head()
        if head != state["checkpoint"] or self.forge.branch_sha(self.config.target_repository, branch) != head:
            raise BranchMoved("The task branch changed after the checks passed. Review it, then resume.")
        files = workspace.changed_files(base)
        title, body, _ = self.describe_pull(model, self.config.target_repository, workspace.diff(base), files)
        checks = "\n".join(f"- `{' '.join(command)}`" for command in self.config.checks)
        body += (f"\n\n---\n**Checks:** on a clean checkout of `{head[:12]}`, `git diff --check` and every "
                 f"configured check passed:\n{checks}\n\n_Drafted with BananaVibe. A maintainer must review "
                 "the code and its behavior before merging._")
        pull = self.forge.publish_pull(branch, title, body)
        self._update(pr_url=pull["html_url"], status="publishing", checkpoint=head)
        return "complete", pull["html_url"]

    # Finish

    def _finish(self, outcome, reason):
        if outcome == "lost":
            self.log(f"Issue #{self.number}: {reason}")
            return outcome
        try:
            state, _ = self.store.read(self.number)
        except (StateError, ForgeError, RuntimeError) as error:
            self.log(f"Could not read the task record to finish: {error}")
            return outcome
        if not state or state["runner"] != self.runner:
            return outcome
        self.state = state
        self.log(f"Issue #{self.number}: {outcome}. {reason}")
        late = state["desired"] == "running" and state["request"] != self.seen_request
        note = " New instructions arrived as this run ended; use `/banana resume` to apply them." if late else ""
        # Release first: a failed comment must never leave the task leased.
        if outcome == "complete":
            self.store.release(self.number, self.runner, status="complete", desired="complete",
                               phase="Ready for review", reason=note.strip())
            checks = len(self.config.checks)
            text = (f"The draft pull request is ready for review: {reason}\n\n`git diff --check` and all {checks} "
                    f"configured check{'s' if checks != 1 else ''} passed on a clean checkout. Review, then merge "
                    "or close it. To revise it, reopen this issue with `/banana answer TEXT`." + note)
        else:
            reason += note
            self.store.release(self.number, self.runner, status=outcome, desired=outcome, reason=reason[:4000],
                               phase="")
            if outcome == "blocked":
                text = f"**Guidance needed.**\n\n> {reason[:4000]}\n\nReply with `/banana answer TEXT` to continue."
            else:
                text = (f"Task **{outcome}**. {reason[:4000]}\n\nThe work so far is saved on "
                        f"`{state['branch'] or '(none)'}`. Use `/banana resume` to continue, `/banana answer TEXT` "
                        "to add guidance, or `/banana restart` to start over.")
        self._progress(final=outcome)
        try:
            self.forge.comment(self.number, text)
            if outcome == "complete":
                self.forge.set_issue_state(self.number, "closed")
        except (ForgeError, RuntimeError) as error:
            self.log(f"The task was saved as {outcome}, but the issue could not be updated: {error}")
        return outcome

