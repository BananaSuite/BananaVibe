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
- **It runs unattended.** Start it in tmux on a server, check `bananavibe status` from your phone, read the
  checkpoint reports, send instructions with `bananavibe say`, pause, resume or stop it.

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

- Linux or macOS, Python 3.11+ (standard library only, nothing else to install), git, tmux (optional).
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
bananavibe doctor --live        # verifies every agent answers
bananavibe start                # runs in a detached tmux session
```

Then walk away. Later:

```bash
bananavibe status               # what it's doing, plan, checks, limits, usage (tokens, cost, Claude windows)
bananavibe attach               # watch live (detach: Ctrl-b d)
cat .bananavibe/reports/latest.md
bananavibe say "Prioritise the API over the UI, and don't touch the database schema."
bananavibe pause | resume | stop [--now] | retry-now
```

Everything the agents did is committed on the `bananavibe/work` branch of each repository, one commit per
session (plus their own commits), so `git log` and `git diff main` show exactly what changed.

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
   them.

### Resilience

| Problem | What BananaVibe does |
| --- | --- |
| Usage limit (5-hour, weekly, rate limit) | Reads the reset time (Claude's `rate_limit_event`, "resets 3pm (Europe/Rome)", "try again in 2 hours", epoch stamps…), waits until then (or retries every `limit_fallback_seconds`), fails over to other workers meanwhile |
| Overloaded / 5xx / network / crash | Exponential backoff from `initial_seconds` to `max_seconds`, forever |
| Agent hangs | Killed after `idle_timeout_minutes` without output, with its whole process group; retried |
| Session runs too long | Stopped at `session_timeout_minutes`; work kept, next session continues |
| Logged out / bad key / bad model | Retried every `auth_seconds` / `max_seconds`; status and `notify` say what to fix |
| Work done before a failure | Committed as `cut off: <reason>` and journaled |
| Supervisor itself crashes | `bananavibe run` restarts it (state is on disk) |
| Server reboots | Run `bananavibe start` again (or use the systemd unit in [docs/server.md](docs/server.md)); it resumes from the state on disk |

## Configuration

`bananavibe init` writes `.bananavibe/config.toml` with every option commented. The important ones:

```toml
[run]
workers = ["claude", "codex"]   # order of preference; failover on limits
reviewer = "codex"              # a different model catches more
checkpoint_every = 5
done_approvals = 2

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

## Writing a good mission

The supervisor keeps agents going; the mission decides where they go. Spell out the scope, a concrete
definition of done, and what must not change, then back it with checks. See
[docs/writing-goals.md](docs/writing-goals.md) and [examples/](examples/).

## Safety

BananaVibe runs agents with **all permission prompts disabled**, by design: nobody is there to approve them. Run it
on a machine or VM where that's acceptable (a dedicated VPS, a container, a throwaway VM), never on your laptop's
main account, and give it only the credentials it needs. Agents are told not to push or deploy unless you set
`allow_push = true`. See [docs/server.md](docs/server.md) for a hardened setup.

## Subscriptions and terms

BananaVibe drives the official CLIs exactly as you would in a terminal, with whatever login they have: a Claude
subscription in Claude Code, a ChatGPT plan in Codex, API keys anywhere. Respect each provider's terms and usage
policies. In particular, use a Claude subscription only through Claude Code itself; for OpenCode or custom agents,
use API keys or providers that allow that use.

## License

AGPL-3.0-only. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
