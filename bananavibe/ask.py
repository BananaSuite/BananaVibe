"""`bananavibe ask`: answer the operator's questions about a run, while it runs or after it ended.

The question goes to a separate, read-only agent session (it never touches the supervisor loop), with a
snapshot of the run: state, plan, recent handoffs, checks, the reviewer's last verdict and what the agent
is doing right now. Questions and answers are kept in ANSWERS.md, so follow-up questions have context.
"""

from __future__ import annotations

import re
import sys
import threading
from pathlib import Path

from bananavibe import gitops
from bananavibe.adapters import OK, make_adapter
from bananavibe.config import Config
from bananavibe.runner import run_session
from bananavibe.state import Paths, State, plan_stats, supervisor_pid
from bananavibe.util import Console, human_duration, now, parse_iso, read_text, tail, write_atomic
from bananavibe.watch import agent_from_log


def _live_activity(paths: Paths, cfg: Config, state: State, lines: int = 60) -> str:
    """The latest session's activity in short form (what `status` would show line by line)."""
    current = state["current"] or {}
    log = current.get("log")
    if log:
        path = paths.workspace / log
        agent = current.get("agent", "")
    else:
        logs = sorted(p for p in paths.logs.glob("*.log") if agent_from_log(p.name)) if paths.logs.exists() else []
        if not logs:
            return ""
        path, agent = logs[-1], agent_from_log(logs[-1].name)
    if agent not in cfg.agents or not path.exists():
        return ""
    parser = make_adapter(cfg.agents[agent]).parser()
    shown: list[str] = []
    for line in read_text(path).splitlines():
        if not line.startswith("# "):
            shown += parser.feed(line)
    header = f"{'In progress' if log else 'Last session'}: `{paths.rel(path)}` ({agent})"
    return header + "\n\n```\n" + "\n".join(shown[-lines:]) + "\n```"


def _previous_answers(paths: Paths, n: int = 3) -> str:
    entries = [e for e in re.split(r"\n(?=## Q )", read_text(paths.answers)) if e.startswith("## Q ")]
    return tail("\n".join(entries[-n:]).strip(), 6000)


def _status_block(paths: Paths, cfg: Config, state: State) -> str:
    d = state.data
    stats = plan_stats(read_text(paths.plan))
    running = bool(supervisor_pid(paths))
    lines = [
        f"- Status: {d['status']}{'' if running or d['status'] in ('done', 'stopped', 'new') else ' (supervisor not running)'}"
        f" — {d['detail']}",
        f"- Supervisor process: {'running' if running else 'not running'}"
        + (f" on {d['host']}" if d.get("host") else ""),
        f"- Sessions completed: {d['iteration']} ({d['attempts']} attempts)",
        f"- Plan: {stats.done}/{stats.total} done, {stats.open} open, {stats.blocked} blocked, {stats.dropped} dropped",
        f"- Reviewer approvals in a row: {d['approvals']} of {cfg.done_approvals} needed to finish",
    ]
    if d.get("current"):
        c = d["current"]
        started = parse_iso(c.get("started"))
        lines.append(f"- Now: {c['label']} session with {c['agent']}"
                     + (f" for {human_duration((now() - started).total_seconds())}" if started else ""))
    if d.get("interrupted"):
        lines.append(f"- Interrupted {d['interrupted']['label']} session waiting to be continued")
    if d["last_checks"]:
        lines.append("- Last checks: " + ", ".join(f"{c['name']} {'PASS' if c['ok'] else 'FAIL'}"
                                                    for c in d["last_checks"]))
    started = parse_iso(d["started_at"])
    if started:
        end = parse_iso(d["finished_at"]) or now()
        lines.append(f"- Run time: {human_duration((end - started).total_seconds())}")
    if d["events"]:
        lines.append("- Recent events:\n" + "\n".join(f"  - {e['at'][5:16].replace('T', ' ')} {e['kind']}: {e['msg'][:200]}"
                                                     for e in d["events"][-15:]))
    return "\n".join(lines)


def ask_prompt(paths: Paths, cfg: Config, question: str) -> str:
    state = State(paths)
    rel = paths.rel
    repos = gitops.discover(paths.workspace, cfg.git_repos) if paths.workspace.is_dir() else []
    diff = []
    for repo in repos:
        base = state["base_commits"].get(str(repo), "")
        diff.append(f"- {repo.name}: {gitops.commits_since(repo, base)} commits since the run started; "
                    f"{gitops.diffstat(repo, base) or 'no changes'}")
    reviews = sorted(paths.reports.glob("review-*.md")) if paths.reports.exists() else []
    journal = read_text(paths.journal)
    sections = [
        "You answer the operator's questions about a long-running BananaVibe run, in which a supervisor keeps "
        "coding agents working on the mission below, session after session, until the work is complete, passes "
        "the checks and is approved by an independent reviewer. You are an observer, not a worker: do NOT modify, "
        "create or delete any file, do not commit, and do not start builds or servers. Read whatever you need "
        "(the files in `.bananavibe/`, the code, `git log`) to give an accurate answer.",
        "Answer the question directly, first, in plain language; then give the evidence (files, commits, check "
        "results, plan items). Be honest about uncertainty and about what is not done yet. When asked whether the "
        "work is done, use the supervisor's definition: every plan item ticked or dropped, all checks passing, and "
        f"{cfg.done_approvals} consecutive reviewer approval(s); status `done` means that was reached.",
        f"# Run status (as of {now():%Y-%m-%d %H:%M %Z})\n\n{_status_block(paths, cfg, state)}",
        f"# Mission (`{rel(paths.goal)}`)\n\n{tail(read_text(paths.goal).strip(), 8000)}",
        f"# Plan (`{rel(paths.plan)}`)\n\n{tail(read_text(paths.plan).strip(), 10000) or '(empty)'}",
        f"# Files\n\nNotes: `{rel(paths.notes)}`. Journal: `{rel(paths.journal)}`. Reports and reviews: "
        f"`{rel(paths.reports)}/`. Session logs: `{rel(paths.logs)}/`. Operator messages: `{rel(paths.inbox)}`."
        + ("\n\nChanges:\n" + "\n".join(diff) if diff else ""),
    ]
    if journal:
        sections.append("# Latest handoffs\n\n" + tail("\n## ".join(journal.split("\n## ")[-3:]).strip(), 8000))
    if reviews:
        sections.append(f"# Latest review (`{rel(reviews[-1])}`)\n\n{tail(read_text(reviews[-1]).strip(), 4000)}")
    live = _live_activity(paths, cfg, state)
    if live:
        sections.append("# Agent activity\n\n" + live)
    previous = _previous_answers(paths)
    if previous:
        sections.append("# Earlier questions and answers (for follow-ups)\n\n" + previous)
    sections.append(f"# Question\n\n{question.strip()}")
    return "\n\n".join(sections) + "\n"


def ask(paths: Paths, cfg: Config, question: str, agent: str | None = None, verbose: bool = True,
        out=None) -> tuple[bool, str]:
    """Ask a read-only agent. Tries the asker first, then the other configured agents. Returns (ok, answer)."""
    out = out or sys.stderr
    console = Console(paths.logs / "ask.log" if paths.logs.is_dir() else None, quiet=not verbose, stream=out)
    candidates = [agent] if agent else list(dict.fromkeys([cfg.asker_name, cfg.reviewer_name, *cfg.workers]))
    prompt = ask_prompt(paths, cfg, question)
    paths.prompts.mkdir(parents=True, exist_ok=True)
    prompt_file = paths.prompts / f"ask-{now():%Y%m%d-%H%M%S}.md"
    write_atomic(prompt_file, prompt)
    errors = []
    try:
        for name in candidates:
            adapter = make_adapter(cfg.agents[name])
            log = paths.logs / "ask" / f"ask-{name}-{now():%Y%m%d-%H%M%S}.log"
            if verbose:
                console.info(f"asking {name}" + ("" if adapter.enforces_read_only else
                                                 " (read-only by instruction only for this agent type)"))
            res = run_session(adapter, prompt, prompt_file, paths.workspace, log, console,
                              cfg.ask_timeout_minutes * 60, cfg.ask_timeout_minutes * 60, threading.Event(),
                              read_only=True)
            answer = res.outcome.final_text.strip()
            if res.outcome.kind == OK and answer:
                _record(paths, question, answer, name)
                return True, answer
            errors.append(f"{name}: {res.outcome.kind} {res.outcome.message[:300]}")
    finally:
        prompt_file.unlink(missing_ok=True)
    return False, "No agent could answer:\n" + "\n".join(errors)


def _record(paths: Paths, question: str, answer: str, agent: str) -> None:
    with Path(paths.answers).open("a", encoding="utf-8") as f:
        f.write(f"\n## Q {now():%Y-%m-%d %H:%M} ({agent})\n\n> " + question.strip().replace("\n", "\n> ")
                + f"\n\n{answer.strip()}\n")
