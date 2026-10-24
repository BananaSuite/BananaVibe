<img src="banana-logo.png" alt="BananaVibe logo" width="64">

# BananaVibe

**Keep coding agents working on a goal for hours, days or weeks, until it is really done.**

Claude Code, Codex and OpenCode are built to finish one task and hand control back. Give them a big job
("make these three projects production-ready") and they stop after an hour or two, convinced they are done,
or because they hit a usage limit, the model was overloaded, or the context filled up.

BananaVibe is a small supervisor that sits on top of those agents and doesn't let the work stop:

- **It restarts the agent with a fresh context after every session.** Memory lives in files: the mission
  (`GOAL.md`), a master checklist (`PLAN.md`), notes, a journal of handoffs, and the git history.
- **"Done" is verified, not believed.** When an agent claims it's finished, BananaVibe checks the plan, runs
  your checks (tests, builds, acceptance scripts), then has an *independent reviewer* (ideally a different model)
  audit the work against the mission. Any finding goes back to the workers. The run ends only after enough
  consecutive approvals.
- **It never gives up on errors.** Usage limits wait for the reset time (parsed from the agent's own messages),
  outages and crashes back off and retry, logouts are retried until you log in again, and work done before a
  failure is committed. With several agents configured, it fails over to whichever one is available.
- **It never stops on its own.** A guardian process restarts the supervisor if it crashes, is killed (including by
  the kernel's OOM killer) or hangs; `bananavibe start` also sets up starting at boot and whenever the run is down
  (systemd or cron). Everything in flight is on disk, so a crash or reboot continues the interrupted session, with
  your messages, the feedback and any done claim under review intact. Only you (or a budget you set) end a run.
- **You can talk to it while it works.** `bananavibe chat` answers questions about the run and takes instructions;
  `bananavibe say` reaches the running agent between its tasks, `say --now` immediately (the session continues in
  the same conversation). `bananavibe dashboard` is a live full-screen view with keys to pause, resume, message
  and ask. Pause or stop now and continue later, and move a run to another machine with `export` / `import`.

It works with any project and any goal: implementing features, getting to production readiness, refactoring,
raising test coverage, migrations, documentation. One workspace can hold several repositories.

From a real test run (Claude Code working, Codex reviewing; the first limit was simulated, the later one was real):

```
17:05:55 ▶ work session 1 with claude
17:05:55 ! claude: usage/rate limit: You've hit your limit · resets soon
17:05:55 all agents (claude) are cooling down: waiting until Wed 17:07 (1m 44s)
17:07:40 ▶ work session 1 with claude
17:10:14 ■ claude finished after 2m 34s            ← stopped with 2 plan items open; restarted
17:13:36 check tests: PASS · check acceptance: PASS
17:13:36 claude claims the mission is complete: verifying
17:18:50 ! codex requested changes                  ← incr crashed on 5,000-digit ints, NaN expiry kept keys alive
17:20:52 claude claims the mission is complete: verifying
17:25:06 ! codex requested changes
   …
17:32:46 ! codex: usage/rate limit: … try again at Nov 4th, 2026 8:39 PM
17:32:46 ▶ review-final session 6 with claude       ← reviewer failover
17:34:18 claude approved the work (1/2)             ← one more fresh-eyes pass required
   …
17:44:48 ✅ mission complete: approved 2 time(s). Final report: .bananavibe/reports/report-0007-….md
```

All checks passed after the second session; the reviewer still sent it back three times with real bugs before
approving. That gap is what BananaVibe exists for.

## Requirements

- Linux or macOS, Python 3.11+ (standard library only, nothing else to install), git.
- At least one agent CLI, installed and logged in:
  [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude`),
  [Codex CLI](https://github.com/openai/codex) (`codex`) or [OpenCode](https://opencode.ai) (`opencode`).
  Any other CLI agent can be plugged in with a command template.

## Install

```bash
pipx install git+https://github.com/BananaSuite/BananaVibe     # or: uv tool install git+https://…
# or from a checkout, without installing anything:
git clone https://github.com/BananaSuite/BananaVibe && cd BananaVibe && ./scripts/install.sh
```

`scripts/install.sh` puts a `bananavibe` launcher in `~/.local/bin` that runs the checkout directly.

## Quick start

```bash
cd ~/work                      # a repository, or a folder containing several repositories
bananavibe init --workers claude,codex --reviewer codex \
  --check "tests=pytest -q" \
  --goal "Make every project in this folder production ready: ..."
$EDITOR .bananavibe/GOAL.md     # describe the mission and the definition of done
$EDITOR .bananavibe/config.toml # models, checks, cadence
bananavibe doctor --live        # verifies every agent answers, and what could stop a long run
bananavibe start                # runs in the background, restarts itself, and comes back after a reboot
```

Then walk away. Later:

```bash
bananavibe dashboard            # live full-screen view: status, plan, checks, agents, transcript; keys to act
bananavibe chat                 # ask questions and give instructions in one place, while it works
bananavibe status               # what it's doing, plan, checks, limits, usage (tokens, cost, Claude windows)
bananavibe attach               # live transcript: messages, thinking, every command and its output (Ctrl-C quits)
bananavibe ask "Is it done? If not, what is left and what is blocking it?"
cat .bananavibe/reports/latest.md
bananavibe say "Prioritise the API over the UI, and don't touch the database schema."
bananavibe say --now "Stop the refactor: the login is broken in production, fix that first."
bananavibe edit goal | edit config     # change the mission or settings while it runs (applied before the next session)
bananavibe pause [--now] | resume | stop [--now] | retry-now
bananavibe sessions             # every session with its agent session id
bananavibe open 12              # open session 12 in the agent's own UI (claude --resume <id>, codex resume <id>)
```

Everything the agents did is committed on the `bananavibe/work` branch of each repository, one commit per
session (plus their own commits), so `git log` and `git diff main` show exactly what changed.

## Watching, asking, pausing, moving

- **Dashboard.** `bananavibe dashboard` (or `top`) shows the status, plan progress, checks, agents and limits,
  recent events and the live transcript, refreshed every second, and lets you act with single keys: `p`/`P` pause
  (after the session / now), `r` resume, `s`/`n` send a message (queued / now), `a` ask, `R` retry now, `x` stop,
  `q` quit (the run keeps going).
- **Talk to it.** `bananavibe chat` is a prompt where plain lines are questions (answered like `ask`, with the
  conversation so far as context) and `/say`, `/now`, `/pause`, `/resume`, `/stop`, `/status`, `/log` act on the run.
  Messages sent with `say` are visible to the running agent at once (agents check for new messages between tasks)
  and are given to every next session until one has completed with them, so nothing you send is lost, even if a
  session fails or the machine restarts. `say --now` interrupts the work session and continues it straight away
  with your message (Claude Code: in the same conversation). `say --list` shows the queue, `say --cancel <id>`
  withdraws a message.
- **See what it is doing.** `bananavibe attach` follows the supervisor and the running agent's full transcript:
  its messages and thinking, every tool call with its input, and the output (`--full` for untruncated output,
  `--raw` for the raw event stream). `bananavibe show <#>` prints any past session; `bananavibe open <#>` opens
  it in the agent's own interactive UI, to inspect it or carry on by hand.
- **Ask questions.** `bananavibe ask "…"` hands your question to a separate, read-only agent session together
  with the run's state, plan, latest handoffs, review and live activity, and prints the answer. It works while
  the run is going (the loop is not disturbed), paused, stopped or finished. Answers are kept in
  `.bananavibe/ANSWERS.md` and follow-ups see the earlier ones (`bananavibe ask --history`).
- **Pause or stop right now.** `bananavibe pause --now` and `bananavibe stop --now` interrupt the agent
  mid-session, commit what it did so far, and remember the session. `resume` (or the next `start`) continues
  it: Claude Code resumes the very same conversation; other agents get a fresh session told exactly what the
  interrupted one was doing. Without `--now` they act after the current session ends.
- **Change the mission or settings while it runs.** `bananavibe edit goal` / `edit config` (or edit the files and
  run `bananavibe reload goal|config`): the change applies before the next session, without interrupting the
  current one.
  Changes made by agents to `GOAL.md` or `config.toml` are reverted (a copy is kept in `.bananavibe/reverted/`).
- **Continue on another machine.** `bananavibe export --stop -o run.tar.gz` packs the run (state, memory files,
  reports, logs, a git bundle of every repository and, when a Claude Code session was interrupted, its
  conversation); on the new machine `bananavibe -w ~/work import run.tar.gz && bananavibe -w ~/work start`
  continues where it stopped, interrupted session included. The archive is checksummed and verified on import.
  Moving it back works the same way (`import --force` over the old copy). The old machine won't restart it.

## How a run works

```
           ┌────────────────────────────────────────────────────────────────────┐
           │                                                                    │
  GOAL.md ─┤  work session (fresh agent) ──► snapshot commit ──► checks         │
  PLAN.md  │        ▲                                              │            │
  NOTES.md │        │ feedback: failed checks, review findings,    ▼            │
  JOURNAL  │        │ stall warnings, your messages      DONE.md claimed?       │
           │        │                                     │ no        │ yes     │
           │        └─────────── every N sessions: ◄──────┘     plan complete?  │
           │                     checkpoint review + report      checks green?  │
           │                                                     reviewer says  │
           │                                                     APPROVE ×N ──► done
           └────────────────────────────────────────────────────────────────────┘
     Any failure (limit, outage, crash, hang, logout) → keep the work, wait/back off or fail over, retry.
```

1. **Session.** The supervisor builds a prompt from the mission, the plan's progress, feedback from the last cycle,
   your messages and the latest handoffs, and runs the agent headless with full permissions
   (`claude -p --dangerously-skip-permissions`, `codex exec --dangerously-bypass-approvals-and-sandbox`,
   `opencode run --auto`). The first session audits the workspace and writes the plan.
2. **After each session** it appends the agent's `HANDOFF.md` to the journal, commits all changes, notices if
   nothing changed (a stall: the next prompt pushes harder, and after `stall_limit` stalls it hands over to another
   agent), and runs your checks. Failures become feedback for the next session.
3. **Checkpoints** (every `checkpoint_every` sessions): the reviewer audits the work so far, its findings go to the
   workers, and a report is written to `.bananavibe/reports/`. Set `pause_at_checkpoint = true` to read each
   report before it continues.
4. **Done.** When a worker writes `DONE.md`, the supervisor rejects the claim if plan items are open or checks fail.
   Otherwise the reviewer audits it; `CHANGES_REQUESTED` sends the findings back, while `APPROVE` counts towards
   `done_approvals` (default 2: after the first approval a worker does one more fresh-eyes verification pass).
   If agents stall with a complete plan and green checks but don't claim done, the supervisor makes the claim for
   them. Once approved, the run is `done`; by default (`after_done = "wait"`) the supervisor stays up and starts
   working again as soon as you `say` something new. A run otherwise only ends when you stop it or when it reaches
   `max_hours` / `max_iterations` if you set them.

### Resilience

| Problem | What BananaVibe does |
| --- | --- |
| Usage limit (5-hour, weekly, rate limit) | Reads the reset time (Claude's `rate_limit_event`, "resets 3pm (Europe/Rome)", "try again in 2 hours", epoch stamps…), waits until then (or retries every `limit_fallback_seconds`), fails over to other workers meanwhile |
| Overloaded / 5xx / network / crash | Exponential backoff from `initial_seconds` to `max_seconds`, forever |
| Agent hangs | Killed after `idle_timeout_minutes` without output, with its whole process group; retried |
| Session runs too long | Stopped at `session_timeout_minutes`; work kept, next session continues |
| Logged out / bad key / bad model | Retried every `auth_seconds` / `max_seconds`; status and `notify` say what to fix |
| Work done before a failure | Committed as `cut off: <reason>` and journaled; a session that had done real work is continued (same conversation for Claude Code) |
| Supervisor crashes, is killed (`kill -9`, OOM killer) or hangs | A guardian process restarts it within seconds (hangs are detected by a heartbeat watchdog); the cut-off session's leftover processes are stopped and the session is continued |
| Server reboots, or the guardian itself is killed | `bananavibe start` installed a systemd service (restart on failure, start at boot, `OOMPolicy=continue`) or cron entries (`@reboot` and every 10 minutes if down) |
| Out of memory | Agents are marked as the OOM killer's preferred victims (`oom_score_adj`), so an agent (retried) dies, not the supervisor; low memory delays the next session instead of starting one that would be killed |
| Disk filling up | Old logs are compressed and pruned to a budget; on low or full disk it cleans up and waits instead of failing |
| Stopped or crashed in the middle of anything | Post-session work (checks, a done claim's review, a checkpoint), feedback and your messages are on disk and resumed |
| Agent edits `GOAL.md` / `config.toml`, switches branch, or leaves huge files | Edits are reverted, the branch is switched back, files over `max_file_mb` are kept out of git; the agent is told why |
| Agent runs `git clean -fdx` or deletes `.bananavibe/` | `git clean` leaves it alone; if deleted, it is restored (state from memory, plan, notes, journal and queued messages from a backup outside the workspace); if the supervisor died too, its automatic restart rebuilds the whole directory from that backup |

## Configuration

`bananavibe init` writes `.bananavibe/config.toml` with every option commented. The important ones:

```toml
[run]
workers = ["claude", "codex"]   # order of preference; failover on limits
reviewer = "codex"              # a different model catches more
checkpoint_every = 5
done_approvals = 2
after_done = "wait"             # stay up after approval and continue on new instructions ("exit" to end)
autostart = true                # `start` sets up starting at boot / when down (systemd or cron)

[notify]                        # optional: a command and/or a webhook (ntfy, Slack, Discord, JSON)
url = "https://ntfy.sh/my-secret-topic"
update_every_hours = 6          # a short progress update every 6 hours

[[checks]]                      # the objective definition of "working"
name = "tests"
run = "pytest -q"

[agents.claude]
model = "opus"
effort = "max"

[agents.codex]
model = "gpt-5.6-terra"
effort = "ultra"
```

See [docs/configuration.md](docs/configuration.md) for everything, including custom agents and notifications.

## Is BananaVibe an agent?

No. It has no model of its own and makes no model API calls: it is a supervisor that runs the real agent CLIs
(Claude Code, Codex, OpenCode, or any CLI via `type = "custom"`) headless, with their own logins and
subscriptions, and keeps them going, checked and reviewed. Every session is an ordinary conversation of that
agent: `bananavibe open <#>` reopens it in the agent's own UI, `bananavibe show <#> > chat.md` exports a readable
transcript.

## Writing a good mission

The supervisor keeps agents going; the mission decides where they go. Spell out the scope, a concrete
definition of done, and what must not change, then back it with checks. See
[docs/writing-goals.md](docs/writing-goals.md) and [examples/](examples/).

## Safety

BananaVibe runs agents with **all permission prompts disabled**, by design: nobody is there to approve them. Run it
on a machine or VM where that's acceptable (a dedicated VPS, a container, a throwaway VM), never on your laptop's
main account, and give it only the credentials it needs. Agents are told not to push or deploy unless you set
`allow_push = true`. See [docs/server.md](docs/server.md) for a hardened setup.

The agents run as the same user as the supervisor, so its protections (reverting edits to `GOAL.md` and
`config.toml`, keeping agents on the work branch, never killing processes it can't prove are its own, validating
imported archives) guard against mistakes, not against a deliberately hostile agent: isolation comes from the
machine you give it.

## Subscriptions and terms

BananaVibe drives the official CLIs exactly as you would in a terminal, with whatever login they have: a Claude
subscription in Claude Code, a ChatGPT plan in Codex, API keys anywhere. Respect each provider's terms and usage
policies. In particular, use a Claude subscription only through Claude Code itself; for OpenCode or custom agents,
use API keys or providers that allow that use.

## License

AGPL-3.0-only. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
