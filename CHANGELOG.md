# Changelog

## 1.7.3 — 2026-10-24

Never stops, never loses anything, and you can talk to it while it works.

**Keeps running by itself**
- `bananavibe run` / `start` now run a small guardian that restarts the supervisor after a crash, `kill -9`, the
  OOM killer or a hang (heartbeat watchdog, `watchdog_minutes`), with backoff, forever. The new supervisor stops
  the cut-off session's leftover processes (verified by start time and boot id, so a reused pid is never touched)
  and continues that session.
- `start` sets up autostart (`[run] autostart`, `bananavibe service`): a systemd system or user service
  (`Restart=on-failure`, `OOMPolicy=continue`, `KillMode=mixed`, `StartLimitIntervalSec=0`) or cron entries
  (`@reboot` and every 10 minutes if down). A run stopped with `bananavibe stop` stays stopped, across crashes and
  reboots, until you start it.
- Agents get `oom_score_adj = 500`, so under memory pressure an agent (retried) dies rather than the supervisor.
  Low disk space or memory delays the next session instead of starting one that would fail; old session logs are
  compressed and pruned to `[logs] max_mb`, the supervisor log is rotated, the journal is archived past 4 MB, and a
  full disk means waiting, not crashing.
- `after_done = "wait"` (new default): after approval the supervisor stays up and continues when you `say`
  something new (`"exit"` restores the old behaviour).
- A usage-limit reset time read from a message is re-checked at least hourly (`limit_recheck_seconds`).

**Nothing is lost**
- Post-session work (checks, the review of a done claim, a checkpoint review) is recorded before it starts and
  resumed after a stop or crash: stopping during the final review no longer throws the done claim away.
- Feedback for the next session (failed checks, review findings) is kept in `state.json`, not only in memory.
- Operator messages are one file each in `.bananavibe/inbox/` and only count as delivered once a session that
  received them has completed: a message is no longer lost when sent while a session starts, or when the
  supervisor dies.
- A session cut off by a limit, an outage or a hang after doing real work is continued (in the same conversation
  for Claude Code) instead of restarted.
- `state.json` is written durably (fsync) with a backup; an unreadable state falls back to the backup.
- The run's memory survives an agent "cleaning up": `.bananavibe/` is a tiny git repository of its own, so
  `git clean -fdx` and `git stash --all` leave it alone; if it is deleted anyway, the state, mission and config
  are restored from memory, and the plan, notes, journal and queued messages from a backup kept outside the
  workspace (`~/.local/state/bananavibe/`, `BANANAVIBE_STATE_DIR`). If the supervisor dies in the same session,
  the guardian, systemd or cron (whose script now lives outside the workspace too) rebuild the whole run
  directory from that backup and continue; a run you stopped on purpose is left alone. Pending commands
  (`control.json`) are mirrored to the backup, operator messages given to a session that deleted the directory are
  given again, and autostart pins the backup location. The single-instance lock
  is held on a file inside `.bananavibe/` and one outside, so two supervisors can never run the same workspace,
  whatever happens to either file.
- Everything after a completed session (journal, snapshot, checks, verification) is recorded first and resumed
  after a crash, without running the session again. A done claim is not accepted while operator messages are
  still waiting to be carried out.
- `export` / `import`: checksummed bundles verified on import, branches and tags, an atomic archive and run
  directory swap, a disk space check, the interrupted Claude Code conversation travels along and is resumed on the
  new machine, a session cut off by a dead supervisor is kept, and the old machine won't restart a moved run.

**Talk to it while it works**
- `bananavibe chat`: questions are answered by a read-only agent; `/say`, `/now`, `/pause`, `/resume`, `/stop`,
  `/status`, `/log`, `/history`.
- `bananavibe say --now`: interrupts the work session and continues it at once with the message. Plain `say`
  messages are visible to the running agent, which checks for new ones between tasks. `say --list` shows the queue,
  `say --cancel <id>` withdraws a message.
- `bananavibe edit goal|config|plan|notes` and `bananavibe reload [goal|config]`: change the mission or the
  configuration while it runs, validated, applied before the next session. `edit` starts from the last approved
  version (so an agent's concurrent edit is never approved along with yours); `reload` approves only the files
  that changed, shows the diff and asks first (without a terminal it needs `--yes`). Edits by agents to `GOAL.md` or
  `config.toml` are reverted after every attempt, including failed and interrupted ones (copies in
  `.bananavibe/reverted/`); if reverting fails (a full disk), nothing is taken for an operator edit until it works,
  even across a restart. The same applies to changes made while checks run (they run the agents' code) and, for
  agents that are read-only only by instruction, while `ask` answers.
  A `config.toml` that no longer parses is put back to the last approved version on automatic restarts; started
  by hand, you get the error instead.
- Notifications: webhooks (`[notify] url`, `format` text/json/slack/discord, `headers`, `events`), periodic
  progress updates (`update_every_hours`, `report --short`), a dead man's switch (`ping_url`), new `restart` and
  `update` events, repeated alerts throttled (`repeat_minutes`). All sent in the background with timeouts.

**Terminal UI**
- `bananavibe dashboard` (`top`): full-screen live view with single-key actions; restores the terminal on Ctrl-C,
  Ctrl-Z, SIGTERM and SIGHUP.
- A clearer, coloured `status` with a plan progress bar, heartbeat, guardian and autostart.
- Agent output is sanitized everywhere (escape sequences, progress bars, control and bidi characters), wide
  characters are measured properly, `BANANAVIBE_ASCII=1` (or a non-UTF-8 terminal) avoids emoji, no tracebacks on
  Ctrl-C or a closed pipe, and a terminal that disappears (closed SSH session) no longer crashes a foreground run.
- `show` and `attach` read compressed logs; `attach` follows a rotated supervisor log.

**Fixes**
- Reset times such as "try again at Nov 4th 8:39 PM" were an hour off across a daylight saving time change.
- Errors are classified from the agent CLI's own messages only, so a project's test output mentioning "429",
  "rate limit" or "401" no longer looks like a usage limit or a logout; Claude Code's API error messages are
  recognized; output lines that happen to be JSON numbers are no longer dropped from a custom agent's answer.
- `control.json` updates are locked (a `pause` could be overwritten); a stop or pause pending when the supervisor
  died is honoured after the restart.
- A signal arriving while the supervisor printed could crash it (re-entrant print in the signal handler).
- Agents and checks never outlive their session: the whole process group is killed on any exception, background
  processes that ignore SIGTERM get SIGKILL, and the supervisor's systemd variables are not passed to them.
- Agents left on another branch are switched back; files over `[git] max_file_mb` are kept out of git entirely
  (they never reach `.git/objects`); an unfinished merge or rebase is reported to the agents instead of silently
  skipping snapshots, and no longer stops the run at startup; git commands time out cleanly and never wait for a
  password prompt.
- Imports refuse repository paths outside the project (such as `.git/hooks`), invalid branch names and remote URLs.
- `.bananavibe/` is kept out of snapshots in git worktrees and submodules too (where `.git` is a file), and
  `init --git-init` no longer commits it in the initial snapshot.
- A crash right after a session ended can no longer make the supervisor forget that session or journal it twice.
- The supervisor's own git commands (snapshots, branch switches, pushes) run no git hooks: hooks are code an agent
  could have written (Git LFS objects are pushed explicitly instead). `init` refuses to run over a live supervisor (for example while `.bananavibe/` is being
  restored after an agent deleted it).
- The documented systemd unit used `Restart=always`, which restarted a run right after `bananavibe stop`.
- Config validation: section types, agent names, branch names, notification settings, positive timeouts.

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
