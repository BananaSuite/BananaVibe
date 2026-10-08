# Changelog

## 1.1.0 — 2026-10-08

- `bananavibe start` runs the supervisor in the background by itself (new session, survives logging out); tmux is
  no longer needed (`start --tmux` keeps the old behaviour).
- `bananavibe attach` is a live view without tmux: supervisor events plus the running agent's full transcript
  (messages, thinking, tool calls with input and output; `--full`, `--raw`). `show <#>` prints past sessions,
  `sessions` lists them with the agents' own session ids, and `open <#>` opens one in the agent's interactive UI
  (`claude --resume`, `codex resume`, `opencode --session`).
- `bananavibe pause --now` interrupts the running session, commits its work and pauses; `resume` continues the same
  session. `stop --now` (now also via the control file, plus `--wait`) remembers the session too, so the next
  `start` continues it. Claude Code resumes the same conversation natively (`resume_sessions`); other agents get
  the interrupted session's last activity.
- `bananavibe ask "…"`: a read-only agent answers questions about the run, while it runs or after it ended,
  with the state, plan, handoffs, latest review and live activity as context. Q&A history in `ANSWERS.md`.
- `bananavibe export` / `import`: move a run to another machine (run directory plus git bundles of every
  repository), continuing where it stopped.
- `resume --start` starts the supervisor if it isn't running. Custom agents' plain-text output is now their final
  message.

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
