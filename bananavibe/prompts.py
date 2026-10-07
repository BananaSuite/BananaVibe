"""Prompts for worker and reviewer sessions."""

from __future__ import annotations

from pathlib import Path

from bananavibe.config import Config
from bananavibe.state import Paths, plan_stats
from bananavibe.util import read_text, tail


def _journal_tail(paths: Paths, entries: int = 3, max_chars: int = 8000) -> str:
    text = read_text(paths.journal)
    parts = text.split("\n## ")
    recent = "\n## ".join(parts[-entries:]) if len(parts) > 1 else text
    return tail(recent.strip(), max_chars)


def _checks_block(cfg: Config, last_checks: list[dict]) -> str:
    if not cfg.checks:
        return ("No automatic checks are configured, so nothing verifies your work except you and the reviewer. "
                "Find the project's own build, test and lint commands and run them yourself.")
    lines = ["The supervisor runs these commands after your session (from the workspace root unless noted). "
             "They must pass before the mission can be accepted:"]
    for c in cfg.checks:
        lines.append(f"- `{c.name}`: `{c.run}`" + (f" (in `{c.cwd}`)" if c.cwd not in ("", ".") else ""))
    if last_checks:
        status = ", ".join(f"{c['name']}: {'PASS' if c['ok'] else 'FAIL'}" for c in last_checks)
        lines.append(f"\nLast results: {status}")
    return "\n".join(lines)


def _repos_block(repos: list[Path], workspace: Path, branch: str) -> str:
    if not repos:
        return "The workspace is not under git."
    names = []
    for r in repos:
        rel = r.relative_to(workspace) if r != workspace and r.is_relative_to(workspace) else r
        names.append(f"`{rel if str(rel) != '.' else '. (workspace root)'}`")
    text = "Git repositories: " + ", ".join(names) + "."
    if branch:
        text += f" You are on the branch `{branch}`; stay on it."
    return text


RULES = """\
## How to work

1. **Orient.** Read `{plan}`, `{notes}` and the recent journal below. If the plan has no items yet, this is the first
   session: audit the workspace thoroughly (structure, how to build/run/test it, what is broken or missing relative to
   the mission) and write a detailed plan before changing code.
2. **Plan.** `{plan}` is the master checklist for the whole mission. Use `- [ ]` for open items, `- [x]` for done,
   `- [!]` for blocked (say why), `- [~]` for deliberately dropped (say why). Keep items small, concrete and
   verifiable, grouped by area and ordered by priority. Add every new problem you discover. Never silently delete
   unfinished items.
3. **Work for the whole session.** Take the highest-priority open items and complete as many as you can. Do not stop
   after one small change, and do not stop to ask for confirmation: no human is watching. When you face a decision,
   choose what a careful senior engineer would choose, and record it in `{notes}`.
4. **Verify everything.** Build it, run it, run the tests, add tests for what you change. Only tick an item when you
   have evidence it works. Never weaken, skip or fake tests or checks to make them pass.
5. **Save as you go.** Commit with clear messages after each meaningful step (the supervisor also snapshots your work
   after the session). Keep `{notes}` up to date with knowledge the next session needs: how to run things, gotchas,
   decisions.
6. **Hand off.** Before your session ends, overwrite `{handoff}` with: what you did, what state things are in, what is
   unverified or broken, and the exact next steps. The next session starts with a fresh context and depends on it.
7. **Declaring done.** Only when every plan item is ticked or justifiably dropped, the mission is met end-to-end, and
   you have run every check yourself, write `{done}` with the evidence (commands run and their results). The
   supervisor then runs the checks and an independent reviewer audits the work against the mission. Premature claims
   are rejected and cost a whole cycle, so be honest. If there is still work, do not write it.
8. **Boundaries.** Do not edit `{goal}`, `.bananavibe/config.toml` or `.bananavibe/state.json`. Never commit
   `.bananavibe/` to any repository (it is excluded on purpose) and do not change git configuration. Keep long-running
   processes (dev servers, watchers) in the background and stop them when finished; always use timeouts for commands
   that might hang. Everything you started is killed when your session ends, so never end a session to "wait" for a
   background job: wait for it and use its result now. {push_rule}
"""


def worker_prompt(cfg: Config, paths: Paths, repos: list[Path], iteration: int, agent: str,
                  feedback: str, inbox: str, last_checks: list[dict]) -> str:
    goal = read_text(paths.goal).strip() or "(GOAL.md is empty: improve the project in the workspace.)"
    stats = plan_stats(read_text(paths.plan))
    rel = paths.rel
    push_rule = ("You may push to remotes when the mission requires it." if cfg.allow_push else
                 "Do not push to remotes, publish packages or deploy anything; the operator reviews and ships.")
    plan_line = (f"Plan progress: {stats.done} done, {stats.open} open, {stats.blocked} blocked, "
                 f"{stats.dropped} dropped." if stats.total else "The plan is empty: start with the audit.")
    if stats.open_items:
        plan_line += "\nNext open items:\n" + "\n".join(f"- {t}" for t in stats.open_items[:12])

    sections = [
        f"You are an autonomous senior software engineer working in a long-running BananaVibe run. This is session "
        f"{iteration} of an open-ended series: a supervisor restarts a fresh agent (you are `{agent}`) after every "
        "session until the mission is complete and independently verified. Your memory between sessions is the "
        "files in `.bananavibe/` and the git history.",
        f"# Mission (from `{rel(paths.goal)}`)\n\n{goal}",
        f"# Workspace\n\nRoot: `{paths.workspace}`. "
        + _repos_block(repos, paths.workspace, cfg.git_branch if cfg.git_snapshot else ""),
        f"# Checks\n\n{_checks_block(cfg, last_checks)}",
        f"# Plan\n\n{plan_line}",
    ]
    if feedback.strip():
        sections.append("# Supervisor feedback (address this first)\n\n" + tail(feedback.strip(), 12000))
    if inbox.strip():
        sections.append("# Messages from the operator (these take priority over the plan)\n\n" + tail(inbox.strip(), 6000))
    journal = _journal_tail(paths)
    if journal:
        sections.append("# Recent journal (handoffs from previous sessions)\n\n" + journal)
    sections.append(RULES.format(plan=rel(paths.plan), notes=rel(paths.notes), handoff=rel(paths.handoff),
                                 done=rel(paths.done), goal=rel(paths.goal), push_rule=push_rule))
    if cfg.prompt_extra.strip():
        sections.append("# Additional rules from the operator\n\n" + cfg.prompt_extra.strip())
    return "\n\n".join(sections) + "\n"


REVIEW_FORMAT = """\
Write your review to `{review}` and nothing else into `.bananavibe/`. Its first line must be exactly one of:

    VERDICT: APPROVE
    VERDICT: CHANGES_REQUESTED

followed by a numbered list of concrete findings, most important first. Each finding names the file or feature,
what is wrong or missing, and how to verify the fix. Approve only if you would sign off on shipping it yourself.
"""


def review_prompt(cfg: Config, paths: Paths, repos: list[Path], mode: str, last_checks: list[dict],
                  diff_summary: str, claim_file: Path | None = None) -> str:
    goal = read_text(paths.goal).strip()
    rel = paths.rel
    if mode == "final":
        claim = rel(claim_file) if claim_file else rel(paths.done)
        intro = (f"You are an independent, skeptical reviewer. A worker agent claims the mission below is complete "
                 f"(its claim and evidence are in `{claim}`). Your job is to find out whether that is true. Inspect the code, build and "
                 "run the software, run the tests and checks, exercise the main features, and compare everything "
                 "against every requirement in the mission. Look for gaps the worker skipped, fake or weakened tests, "
                 "TODOs, broken edge cases, and claims without evidence.")
    else:
        intro = ("You are an independent, skeptical reviewer doing a periodic checkpoint audit of an ongoing "
                 "autonomous run. Judge the work so far against the mission: is it heading the right way, is it "
                 "correct, what was missed, what was done badly, is the plan complete? Your findings become "
                 "instructions for the next worker sessions. Use VERDICT: APPROVE only if the whole mission is "
                 "already complete.")
    sections = [
        intro,
        "Do not modify project files: you are reviewing, not fixing. You may run any command, create temporary "
        "files outside the repositories, and start and stop processes.",
        f"# Mission\n\n{goal}",
        f"# Workspace\n\nRoot: `{paths.workspace}`. " + _repos_block(repos, paths.workspace, ""),
        f"# Context\n\nPlan: `{rel(paths.plan)}`. Notes: `{rel(paths.notes)}`. Journal: `{rel(paths.journal)}`.\n\n"
        + _checks_block(cfg, last_checks)
        + (f"\n\nChanges since the run started:\n{diff_summary}" if diff_summary else ""),
        "# Output\n\n" + REVIEW_FORMAT.format(review=rel(paths.review)),
    ]
    return "\n\n".join(sections) + "\n"
