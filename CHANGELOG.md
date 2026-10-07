# Changelog

## 1.0.0 — 2026-10-07

First release: a supervisor that keeps coding agents working on a goal for as long as it takes.

- Drives Claude Code, Codex, OpenCode or any CLI agent headless, with fresh context every session and memory in
  `.bananavibe/` (goal, plan, notes, journal) and git.
- Done claims are checked against the plan and your `[[checks]]`, then audited by an independent reviewer agent;
  configurable number of consecutive approvals.
- Never gives up: usage limits wait for the parsed reset time, outages back off, hung sessions are killed, logouts
  and config errors are retried and reported, work from cut-off sessions is committed, and multiple workers fail
  over to each other.
- Checkpoint reviews and Markdown reports every N sessions, optional pause for human review.
- `init`, `run`, `start` (tmux), `attach`, `status`, `pause`, `resume`, `stop`, `retry-now`, `say`, `report`,
  `logs`, `doctor --live`.
- Usage tracking per agent in `status` and reports: tokens, API-price equivalent, and Claude subscription
  window utilization (5-hour and weekly).
- Standard library only; Python 3.11+.
