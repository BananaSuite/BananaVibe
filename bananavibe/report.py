"""Human-readable progress reports, written to `.bananavibe/reports/`."""

from __future__ import annotations

from pathlib import Path

from bananavibe import gitops
from bananavibe.config import Config
from bananavibe.state import Paths, State, plan_stats
from bananavibe.util import human_duration, now, parse_iso, read_text, tail, usage_text, write_atomic


def render(paths: Paths, cfg: Config, state: State, repos: list[Path], title: str) -> str:
    d = state.data
    started = parse_iso(d["started_at"])
    end = parse_iso(d["finished_at"]) or now()
    elapsed = human_duration((end - started).total_seconds()) if started else "-"
    stats = plan_stats(read_text(paths.plan))
    lines = [
        f"# BananaVibe report: {cfg.name}",
        "",
        f"_{title} · {now().strftime('%Y-%m-%d %H:%M %Z')}_",
        "",
        f"- **Status:** {d['status']} — {d['detail']}",
        f"- **Sessions completed:** {d['iteration']} (attempts including retries: {d['attempts']})",
        f"- **Running for:** {elapsed}",
        f"- **Plan:** {stats.done}/{stats.total} done, {stats.open} open, {stats.blocked} blocked, "
        f"{stats.dropped} dropped",
        f"- **Reviewer approvals in a row:** {d['approvals']}/{cfg.done_approvals}",
    ]
    if d["last_checks"]:
        lines.append("- **Checks:** " + ", ".join(f"{c['name']} {'✅' if c['ok'] else '❌'}" for c in d["last_checks"]))
    lines += ["", "## Agents", "", "| Agent | Sessions | Failed attempts | Time | Usage | Last error |",
              "|---|---|---|---|---|---|"]
    for name, a in d["agents"].items():
        lines.append(f"| {name} | {a['sessions']} | {a['failures']} | {human_duration(a.get('seconds', 0))} | "
                     f"{usage_text(a).removeprefix('; ') or '-'} | {(a['last_error'] or '').replace('|', '/')[:120]} |")
    if repos:
        lines += ["", "## Repositories", ""]
        for repo in repos:
            base = d["base_commits"].get(str(repo), "")
            lines.append(f"- `{repo}` on `{gitops.current_branch(repo)}`: {gitops.commits_since(repo, base)} commits "
                         f"since `{base[:10]}`; {gitops.diffstat(repo, base) or 'no changes'}")
        lines.append("")
        lines.append("Inspect with `git log --stat <base>..HEAD` and `git diff <base>` in each repository.")
    if stats.open_items:
        lines += ["", "## Open plan items", ""] + [f"- {t}" for t in stats.open_items[:40]]
    reviews = sorted(paths.reports.glob("review-*.md")) if paths.reports.exists() else []
    if reviews:
        lines += ["", f"## Latest review ({reviews[-1].name})", "", tail(read_text(reviews[-1]).strip(), 5000)]
    journal = read_text(paths.journal)
    if journal:
        entries = journal.split("\n## ")
        lines += ["", "## Latest sessions", ""]
        for entry in entries[-3:]:
            entry = entry.strip()
            if entry:
                lines.append("### " + tail(entry.removeprefix("## "), 2500))
                lines.append("")
    events = d["events"][-15:]
    if events:
        lines += ["", "## Recent events", ""] + [f"- {e['at']} **{e['kind']}** {e['msg']}" for e in events]
    return "\n".join(lines) + "\n"


def write(paths: Paths, cfg: Config, state: State, repos: list[Path], title: str) -> Path:
    paths.reports.mkdir(parents=True, exist_ok=True)
    text = render(paths, cfg, state, repos, title)
    path = paths.reports / f"report-{state['iteration']:04d}-{now().strftime('%Y%m%d-%H%M%S')}.md"
    write_atomic(path, text)
    write_atomic(paths.reports / "latest.md", text)
    return path
