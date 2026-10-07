"""The supervisor loop: restart agents until the goal is met, verified and independently reviewed.

One iteration = one agent session that ended normally (or ran out of time). Usage limits,
outages, crashes and login problems never end the run: the supervisor waits, fails over to
another configured agent, and tries again, forever, until the operator stops it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import random
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from bananavibe import gitops, report
from bananavibe.adapters import AUTH, FATAL, LIMIT, OK, make_adapter
from bananavibe.checks import failure_text, run_checks
from bananavibe.config import Config
from bananavibe.prompts import review_prompt, worker_prompt
from bananavibe.runner import SessionResult, run_session
from bananavibe.state import (
    Lock,
    Paths,
    State,
    append_inbox,
    plan_stats,
    read_control,
    set_control,
    take_inbox,
)
from bananavibe.util import Console, human_duration, iso, now, parse_iso, read_text, tail, write_atomic

MAX_COOLDOWN = dt.timedelta(days=7)
VERDICT_RE = re.compile(r"^\s*\**\s*VERDICT\s*:\s*\**\s*(APPROVE|CHANGES[_ ]REQUESTED)", re.I | re.M)


class StopRun(Exception):
    pass


class Supervisor:
    def __init__(self, paths: Paths, cfg: Config, console: Console):
        self.paths = paths
        self.cfg = cfg
        self.console = console
        self.state = State(paths)
        self.stop_event = threading.Event()
        self.adapters = {name: make_adapter(a) for name, a in cfg.agents.items()}
        self.repos: list[Path] = []
        self.feedback: list[str] = []
        self.goal_text = ""

    # ---------------------------------------------------------------- setup

    def _install_signals(self) -> None:
        def handler(signum, frame):
            if self.stop_event.is_set():
                raise KeyboardInterrupt
            self.console.warn("stop requested: finishing up (press Ctrl-C again to abort immediately)")
            self.stop_event.set()
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def _setup_repos(self) -> None:
        self.repos = gitops.discover(self.paths.workspace, self.cfg.git_repos)
        if gitops.is_repo(self.paths.workspace):
            gitops.exclude_path(self.paths.workspace, "/.bananavibe/")
        if not self.repos:
            if self.cfg.git_snapshot:
                self.console.warn("no git repository found in the workspace: snapshots are disabled")
            return
        base = self.state["base_commits"]
        for repo in self.repos:
            if self.cfg.git_snapshot and self.cfg.git_branch:
                gitops.ensure_branch(repo, self.cfg.git_branch)
            base.setdefault(str(repo), gitops.head(repo))
        self.console.info("repositories: " + ", ".join(str(r) for r in self.repos)
                          + (f" (branch {self.cfg.git_branch})" if self.cfg.git_branch and self.cfg.git_snapshot else ""))

    # ---------------------------------------------------------------- helpers

    def _save(self) -> None:
        self.state.save()

    def _set_status(self, status: str, detail: str = "", wait_until: dt.datetime | None = None) -> None:
        self.state["status"] = status
        self.state["detail"] = detail
        self.state["wait_until"] = iso(wait_until)
        self._save()

    def notify(self, event: str, message: str) -> None:
        self.state.event(event, message)
        self._save()
        if not self.cfg.notify_command:
            return
        env = {**os.environ, "BANANAVIBE_EVENT": event, "BANANAVIBE_MESSAGE": message,
               "BANANAVIBE_RUN": self.cfg.name, "BANANAVIBE_WORKSPACE": str(self.paths.workspace)}
        try:
            subprocess.Popen(["bash", "-c", self.cfg.notify_command], env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            self.console.warn(f"notify command failed: {e}")

    def _check_control(self) -> None:
        """Apply commands left by `bananavibe pause/resume/stop`. Raises StopRun to end the run."""
        if self.stop_event.is_set():
            raise StopRun("interrupted")
        control = read_control(self.paths)
        if control.get("stop"):
            set_control(self.paths, stop=None)
            raise StopRun("stop requested by operator")
        if control.get("retry_now"):
            set_control(self.paths, retry_now=False)
            for name in self.cfg.agents:
                self.state.agent(name)["cooldown_until"] = None
            self.console.info("operator asked to retry now: cooldowns cleared")
        if control.get("pause"):
            if self.state["status"] != "paused":
                self.console.info("paused. Resume with `bananavibe resume`.")
                self._set_status("paused", "paused by operator")
            while read_control(self.paths).get("pause"):
                self._sleep(5, interruptible=False)
                if read_control(self.paths).get("stop") or self.stop_event.is_set():
                    set_control(self.paths, stop=None, pause=False)
                    raise StopRun("stop requested by operator")
            self.console.info("resumed")
            self._set_status("running", "resumed")

    def _sleep(self, seconds: float, interruptible: bool = True) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stop_event.is_set():
                raise StopRun("interrupted")
            if interruptible:
                control = read_control(self.paths)
                if control.get("stop") or control.get("pause") or control.get("retry_now"):
                    return
            time.sleep(min(2.0, max(0.0, end - time.monotonic())))

    def _wait_until(self, when: dt.datetime, why: str) -> None:
        seconds = (when - now()).total_seconds()
        if seconds <= 0:
            return
        self.console.info(f"{why}: waiting until {when.strftime('%a %H:%M')} ({human_duration(seconds)})")
        self._set_status("waiting", why, when)
        self._sleep(seconds)
        self._check_control()

    def _over_budget(self) -> str | None:
        if self.cfg.max_iterations and self.state["iteration"] >= self.cfg.max_iterations:
            return f"reached max_iterations = {self.cfg.max_iterations}"
        started = parse_iso(self.state["started_at"])
        if self.cfg.max_hours and started and (now() - started).total_seconds() > self.cfg.max_hours * 3600:
            return f"reached max_hours = {self.cfg.max_hours:g}"
        return None

    def _tree_state(self) -> str:
        parts = [gitops.fingerprint(r) for r in self.repos]
        for p in (self.paths.plan, self.paths.notes):
            parts.append(hashlib.sha1(read_text(p).encode()).hexdigest())
        return "|".join(parts)

    # ---------------------------------------------------------------- agents

    def _available(self, candidates: list[str]) -> tuple[str | None, dt.datetime | None]:
        """First candidate that is not cooling down, else the earliest time one becomes available."""
        earliest = None
        for name in candidates:
            until = parse_iso(self.state.agent(name)["cooldown_until"])
            if not until or until <= now():
                return name, None
            if earliest is None or until < earliest:
                earliest = until
        return None, earliest

    def _order_workers(self) -> list[str]:
        workers = list(self.cfg.workers)
        if self.cfg.strategy == "round-robin" and len(workers) > 1:
            k = self.state["iteration"] % len(workers)
            workers = workers[k:] + workers[:k]
        # After repeated stalls, prefer a different agent: a fresh pair of eyes.
        if self.state["stalls"] >= self.cfg.stall_limit and len(workers) > 1:
            k = (self.state["stalls"] // self.cfg.stall_limit) % len(workers)
            workers = workers[k:] + workers[:k]
        return workers

    def _register_failure(self, name: str, result: SessionResult) -> None:
        info = self.state.agent(name)
        info["failures"] += 1
        info["consecutive_failures"] += 1
        n = info["consecutive_failures"]
        oc = result.outcome
        info["last_error"] = f"{oc.kind}: {oc.message}"[:500]
        if oc.kind == LIMIT:
            if oc.retry_at and oc.retry_at > now():
                until = oc.retry_at + dt.timedelta(seconds=min(60, self.cfg.limit_fallback_seconds))
            else:
                until = now() + dt.timedelta(seconds=self.cfg.limit_fallback_seconds)
            label = "usage/rate limit"
        elif oc.kind == AUTH:
            until = now() + dt.timedelta(seconds=self.cfg.auth_retry_seconds)
            label = "authentication problem (log the agent in again)"
            self.notify("attention", f"{name}: {label}: {oc.message}")
        elif oc.kind == FATAL:
            until = now() + dt.timedelta(seconds=self.cfg.retry_max_seconds)
            label = "configuration problem (check model name and flags)"
            self.notify("attention", f"{name}: {label}: {oc.message}")
        else:
            delay = min(self.cfg.retry_initial_seconds * 2 ** (n - 1), self.cfg.retry_max_seconds)
            until = now() + dt.timedelta(seconds=delay * random.uniform(0.85, 1.15))
            label = "temporary failure (overload, outage, network or crash)"
        until = min(until, now() + MAX_COOLDOWN)
        info["cooldown_until"] = iso(until)
        self.console.warn(f"{name}: {label}: {tail(oc.message, 300) or 'exit ' + str(result.exit_code)}")
        self.console.warn(f"{name}: retrying after {until.strftime('%a %H:%M:%S')} (attempt {n} failed)")
        self.state.event(oc.kind, f"{name}: {oc.message[:300]}")
        self._save()

    def run_agent(self, candidates: list[str], make_prompt: Callable[[str], str], label: str,
                  on_partial: Callable[[str, SessionResult], None] | None = None) -> tuple[str, SessionResult]:
        """Run one session with the first available agent, retrying through any failure until it succeeds."""
        while True:
            self._check_control()
            name, wait_until = self._available(candidates)
            if name is None:
                names = ", ".join(candidates)
                self._wait_until(wait_until, f"all agents ({names}) are cooling down")
                continue
            prompt = make_prompt(name)
            self.paths.prompts.mkdir(parents=True, exist_ok=True)
            prompt_file = self.paths.prompts / f"{label}.md"
            write_atomic(prompt_file, prompt)
            iteration = self.state["iteration"] + 1
            log_path = self.paths.session_log(iteration, f"{label}-{name}")
            self.state["attempts"] += 1
            self.state["current_agent"] = name
            self.state["session_started_at"] = iso(now())
            self._set_status("running", f"{label} session with {name} (log: {self.paths.rel(log_path)})")
            self.console.info(f"▶ {label} session {iteration} with {name} (log: {self.paths.rel(log_path)})")
            result = run_session(self.adapters[name], prompt, prompt_file, self.paths.workspace, log_path,
                                 self.console, self.cfg.session_timeout_minutes * 60,
                                 self.cfg.idle_timeout_minutes * 60, self.stop_event)
            self.state["current_agent"] = None
            info = self.state.agent(name)
            info["seconds"] = info.get("seconds", 0) + result.seconds
            usage = result.outcome.usage
            if usage.get("cost_usd"):
                info["cost_usd"] = round(info.get("cost_usd", 0.0) + usage["cost_usd"], 4)
            if usage.get("tokens"):
                info["tokens"] = info.get("tokens", 0) + usage["tokens"]
            if usage.get("windows"):
                info["windows"] = usage["windows"]
                info.setdefault("windows_at_start", usage["windows"])
            if result.interrupted:
                if on_partial:
                    on_partial(name, result)
                raise StopRun("interrupted")
            if result.outcome.kind == OK:
                info["sessions"] += 1
                info["consecutive_failures"] = 0
                info["cooldown_until"] = None
                self.console.info(f"■ {name} finished after {human_duration(result.seconds)}"
                                  + (" (session time limit)" if result.timed_out else ""))
                self._save()
                return name, result
            if result.idle_killed:
                result.outcome.message = (f"no output for {self.cfg.idle_timeout_minutes:g} minutes; "
                                          "the session was killed")
            self._register_failure(name, result)
            if on_partial:
                on_partial(name, result)

    # ---------------------------------------------------------------- iteration

    def _record_handoff(self, iteration: int, agent: str, result: SessionResult) -> None:
        handoff = read_text(self.paths.handoff).strip()
        outcome = "ok" if result.outcome.kind == OK else f"{result.outcome.kind}: {result.outcome.message[:200]}"
        if result.timed_out:
            outcome = "session time limit reached"
        header = f"## Session {iteration} · {agent} · {now().strftime('%Y-%m-%d %H:%M')} · " \
                 f"{human_duration(result.seconds)} · {outcome}"
        if handoff:
            body = handoff
            self.paths.handoff.unlink(missing_ok=True)
        else:
            body = "(no HANDOFF.md written)\n\nLast message from the agent:\n\n" + tail(result.outcome.final_text, 3000)
            if result.outcome.kind == OK and not result.timed_out:
                self.feedback.append(f"The previous session did not write `{self.paths.rel(self.paths.handoff)}`. "
                                     "Always write it before you finish.")
        with self.paths.journal.open("a", encoding="utf-8") as f:
            f.write(f"\n{header}\n\n{body.strip()}\n")

    def _snapshot(self, message: str) -> int:
        if not self.cfg.git_snapshot:
            return 0
        made = 0
        for repo in self.repos:
            try:
                if repo == self.paths.workspace:
                    # Run memory never belongs in the project's history, even if an agent force-added it.
                    gitops.git(repo, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", ".bananavibe")
                if gitops.snapshot(repo, message):
                    made += 1
                if self.cfg.git_push and self.cfg.git_branch:
                    err = gitops.push(repo, self.cfg.git_remote, self.cfg.git_branch)
                    if err:
                        self.console.warn(f"push failed for {repo.name}: {tail(err, 300)}")
            except (OSError, subprocess.SubprocessError) as e:
                self.console.warn(f"snapshot failed for {repo}: {e}")
        return made

    def _write_feedback_file(self) -> str:
        text = "\n\n".join(self.feedback)
        if text:
            write_atomic(self.paths.feedback, text + "\n")
        else:
            self.paths.feedback.unlink(missing_ok=True)
        return text

    def work_iteration(self) -> None:
        inbox = take_inbox(self.paths)
        feedback = self._write_feedback_file()
        before = self._tree_state()
        iteration = self.state["iteration"] + 1

        def make_prompt(agent: str) -> str:
            return worker_prompt(self.cfg, self.paths, self.repos, iteration, agent, feedback, inbox,
                                 self.state["last_checks"])

        def on_partial(agent: str, result: SessionResult) -> None:
            # A session cut off by a limit or outage may still have done useful work: keep it.
            if self._tree_state() != before:
                self._record_handoff(iteration, agent, result)
                self._snapshot(f"bananavibe: session {iteration} by {agent} (cut off: {result.outcome.kind})")

        try:
            agent, result = self.run_agent(self._order_workers(), make_prompt, "work", on_partial)
        except StopRun:
            if inbox:
                append_inbox(self.paths, inbox)
            raise

        self.state["iteration"] = iteration
        self.feedback = []
        self._record_handoff(iteration, agent, result)
        progressed = self._tree_state() != before
        made = self._snapshot(f"bananavibe: session {iteration} by {agent}")
        self.console.info(f"session {iteration}: {'changes saved' if made else 'progress recorded' if progressed else 'no changes'}"
                          + (f" in {made} repo(s)" if made else ""))
        if result.timed_out:
            self.feedback.append("The previous session hit the session time limit and was stopped. Check its work, "
                                 "which may be half-finished, before continuing.")

        if read_text(self.paths.goal) != self.goal_text:
            write_atomic(self.paths.goal, self.goal_text)
            self.feedback.append(f"`{self.paths.rel(self.paths.goal)}` was modified during the previous session and "
                                 "has been restored. Never edit it; record your thoughts in NOTES.md instead.")
            self.console.warn("GOAL.md was modified by the agent: restored")

        if progressed or self.paths.done.exists():
            # A session that only verifies and then claims done is not a stall.
            self.state["stalls"] = 0
        else:
            self.state["stalls"] += 1
            self.feedback.append(
                "The previous session ended without changing any file or the plan. Do not stop early: pick the next "
                "open plan item and make real progress. If you believe everything is finished, verify it rigorously "
                f"and then write `{self.paths.rel(self.paths.done)}`; if not, add the remaining work to the plan.")
            self.console.warn(f"no progress in session {iteration} ({self.state['stalls']} in a row)")
            if self.state["stalls"] > 2 * self.cfg.stall_limit:
                # Something is persistently wrong; don't burn through the quota in a tight loop.
                delay = min(60 * self.state["stalls"], self.cfg.retry_max_seconds)
                self._save()
                self._wait_until(now() + dt.timedelta(seconds=delay), "agents keep stopping without progress")

        if self.cfg.checks:
            self._run_checks(iteration)
        self._save()

        stats = plan_stats(read_text(self.paths.plan))
        if self.paths.done.exists():
            self.verify_done(iteration, agent)
        elif (self.state["stalls"] >= self.cfg.stall_limit and stats.total and not stats.open and not stats.blocked
              and all(c["ok"] for c in self.state["last_checks"])):
            self.console.info("plan complete and checks green but no DONE claim: treating it as one")
            write_atomic(self.paths.done, "Implicit claim by the supervisor: the plan is complete and checks pass.\n")
            self.verify_done(iteration, agent)
        elif self.cfg.checkpoint_every and iteration - self.state["last_checkpoint"] >= self.cfg.checkpoint_every:
            self.checkpoint(iteration, agent)

    def _run_checks(self, iteration: int) -> list[dict]:
        self._set_status("running", "running checks")
        self.console.info("running checks: " + ", ".join(c.name for c in self.cfg.checks))
        results = run_checks(self.cfg.checks, self.paths.workspace,
                             self.paths.logs / f"{iteration:04d}-checks", self.stop_event)
        self.state["last_checks"] = [{k: r[k] for k in ("name", "ok", "exit", "seconds")} for r in results]
        for r in results:
            (self.console.good if r["ok"] else self.console.warn)(
                f"check {r['name']}: {'PASS' if r['ok'] else 'FAIL'} ({r['seconds']}s)")
        failures = failure_text(results)
        if failures:
            self.feedback.append("These checks failed after the previous session. Fix the causes (do not weaken the "
                                 "checks):\n\n" + failures)
        if self.stop_event.is_set():
            raise StopRun("interrupted")
        return results

    # ---------------------------------------------------------------- review

    def _diff_summary(self) -> str:
        lines = []
        for repo in self.repos:
            base = self.state["base_commits"].get(str(repo), "")
            n = gitops.commits_since(repo, base)
            stat = gitops.diffstat(repo, base)
            lines.append(f"- {repo.name}: {n} commits; {stat or 'no changes'} (base {base[:10]})")
        return "\n".join(lines)

    def run_review(self, mode: str, iteration: int, worker: str,
                   claim_file: Path | None = None) -> tuple[str, str, str]:
        """Run a reviewer session. Returns (verdict, findings, reviewer)."""
        self.paths.review.unlink(missing_ok=True)
        preferred = [self.cfg.reviewer_name]
        others = [w for w in self.cfg.workers if w not in preferred and w != worker]
        candidates = preferred + others + [w for w in self.cfg.workers if w not in preferred + others]
        diff = self._diff_summary()

        def make_prompt(agent: str) -> str:
            return review_prompt(self.cfg, self.paths, self.repos, mode, self.state["last_checks"], diff, claim_file)

        reviewer, result = self.run_agent(candidates, make_prompt, f"review-{mode}")
        if reviewer != self.cfg.reviewer_name:
            self.console.warn(f"the reviewer {self.cfg.reviewer_name} is unavailable; {reviewer} reviewed instead")
            self.state.event("failover", f"review by {reviewer} instead of {self.cfg.reviewer_name}")
        text = read_text(self.paths.review).strip()
        if not text:
            text = result.outcome.final_text.strip()
        archived = self.paths.reports / f"review-{iteration:04d}-{mode}.md"
        archived.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(archived, text + "\n")
        self.paths.review.unlink(missing_ok=True)
        if self._snapshot(f"bananavibe: changes made during {mode} review by {reviewer}"):
            self.console.warn("the reviewer modified files; the changes were kept and committed")
        m = VERDICT_RE.search(text)
        verdict = m.group(1).upper().replace(" ", "_") if m else "NONE"
        return verdict, text, reviewer

    def verify_done(self, iteration: int, worker: str) -> None:
        claim = read_text(self.paths.done)
        archive = self.paths.reports / f"done-claim-{iteration:04d}.md"
        archive.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(archive, claim)
        self.paths.done.unlink(missing_ok=True)
        self.console.info(f"{worker} claims the mission is complete: verifying")
        self.notify("claim", f"session {iteration}: {worker} claims the mission is complete; verifying")

        stats = plan_stats(read_text(self.paths.plan))
        problems = []
        if stats.open or stats.blocked:
            items = "\n".join(f"- {t}" for t in stats.open_items[:30])
            problems.append(f"`{self.paths.rel(self.paths.plan)}` still has {stats.open} open and {stats.blocked} "
                            f"blocked items:\n{items}")
        if not stats.total:
            problems.append(f"`{self.paths.rel(self.paths.plan)}` is empty: write the plan and evidence for each item.")
        if self.cfg.checks and not all(c["ok"] for c in self.state["last_checks"]):
            problems.append("Not all checks pass (see above).")
        if problems:
            self.state["approvals"] = 0
            self.feedback.insert(0, "Your DONE claim was rejected by the supervisor:\n\n" + "\n\n".join(problems))
            self.console.warn("done claim rejected: " + "; ".join(p.splitlines()[0] for p in problems))
            self.state.event("rejected", "done claim rejected by supervisor checks")
            self._save()
            return

        verdict, findings, reviewer = self.run_review("final", iteration, worker, claim_file=archive)
        self.state["last_checkpoint"] = iteration
        if verdict == "APPROVE":
            self.state["approvals"] += 1
            n, need = self.state["approvals"], self.cfg.done_approvals
            self.console.good(f"{reviewer} approved the work ({n}/{need})")
            self.state.event("approved", f"{reviewer} approved ({n}/{need})")
            if n >= need:
                raise Finished(f"mission complete: approved {n} time(s), last by {reviewer}")
            self.feedback.insert(0, (
                f"An independent reviewer approved the work (approval {n} of {need} required). Before the run can "
                "finish, do one more complete verification pass with fresh eyes: re-read the mission line by line, "
                "rebuild and re-test everything from a clean state, exercise every feature for real, and hunt for "
                "anything missed or fragile. Fix and add to the plan whatever you find. If, and only if, you find "
                f"nothing, write `{self.paths.rel(self.paths.done)}` again with the evidence.\n\nReviewer notes:\n\n"
                + tail(findings, 4000)))
        else:
            self.state["approvals"] = 0
            self.console.warn(f"{reviewer} requested changes" if verdict != "NONE" else
                              f"{reviewer} gave no verdict; treating it as changes requested")
            self.state.event("changes", f"{reviewer} requested changes")
            self.feedback.insert(0, (
                "Your DONE claim was reviewed by an independent reviewer and rejected. Add every finding below to "
                "the plan as open items, fix them all, and verify. Findings:\n\n" + tail(findings, 10000)))
        self._save()
        report.write(self.paths, self.cfg, self.state, self.repos, f"verification after session {iteration}")

    def checkpoint(self, iteration: int, worker: str) -> None:
        self.state["last_checkpoint"] = iteration
        self.console.info(f"checkpoint after session {iteration}")
        if self.cfg.review_at_checkpoint:
            verdict, findings, reviewer = self.run_review("checkpoint", iteration, worker)
            self.feedback.append(f"Checkpoint review by {reviewer}. Add these findings to the plan where they are "
                                 "valid and address them by priority:\n\n" + tail(findings, 10000))
        path = report.write(self.paths, self.cfg, self.state, self.repos, f"checkpoint after session {iteration}")
        self.notify("checkpoint", f"checkpoint after session {iteration}: {path}")
        self._save()
        if self.cfg.pause_at_checkpoint:
            set_control(self.paths, pause=True)
            self.console.info(f"paused at checkpoint. Read {self.paths.rel(path)}, then `bananavibe resume`.")

    # ---------------------------------------------------------------- main

    def run(self) -> int:
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        lock = Lock(self.paths.lock)
        if not lock.acquire():
            self.console.warn("another supervisor is already running in this workspace")
            return 2
        self._install_signals()
        try:
            set_control(self.paths, stop=None, retry_now=False, pause=False)
            self.goal_text = read_text(self.paths.goal)
            self.state["pid"] = os.getpid()
            self.state["started_at"] = self.state["started_at"] or iso(now())
            self.state["finished_at"] = None
            self._setup_repos()
            pending = read_text(self.paths.feedback).strip()
            if pending:
                self.feedback = [pending]
            self._set_status("running", "started")
            self.notify("start", f"run '{self.cfg.name}' started at iteration {self.state['iteration']}")
            self.console.info(f"run '{self.cfg.name}': workers {', '.join(self.cfg.workers)}, reviewer "
                              f"{self.cfg.reviewer_name}, {len(self.cfg.checks)} check(s)")
            while True:
                self._check_control()
                reason = self._over_budget()
                if reason:
                    raise StopRun(reason)
                self.work_iteration()
        except Finished as f:
            self._write_feedback_file()
            self.state["finished_at"] = iso(now())
            self._set_status("done", str(f))
            path = report.write(self.paths, self.cfg, self.state, self.repos, "final report")
            self.console.good(f"✅ {f}. Final report: {self.paths.rel(path)}")
            self.notify("done", f"{f}. Report: {path}")
            return 0
        except StopRun as s:
            self._snapshot("bananavibe: work in progress at stop")
            self._write_feedback_file()
            self._set_status("stopped", str(s))
            path = report.write(self.paths, self.cfg, self.state, self.repos, f"stopped: {s}")
            self.console.info(f"stopped ({s}). Report: {self.paths.rel(path)}. Continue with `bananavibe run`.")
            self.notify("stopped", str(s))
            return 0
        except KeyboardInterrupt:
            self._set_status("stopped", "aborted")
            return 130
        except Exception as e:  # record it before dying, so status shows why
            self._set_status("crashed", f"{type(e).__name__}: {e}")
            self.notify("attention", f"supervisor crashed: {type(e).__name__}: {e}")
            raise
        finally:
            self.state["pid"] = None
            self._save()
            lock.release()


class Finished(Exception):
    pass
