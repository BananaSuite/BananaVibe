# Configuration

A run lives in `.bananavibe/` inside the workspace:

| File | Who writes it | Purpose |
| --- | --- | --- |
| `config.toml` | you | this file's subject |
| `GOAL.md` | you | the mission and its definition of done; agents must not edit it |
| `PLAN.md` | agents | master checklist: `- [ ]` open, `- [x]` done, `- [!]` blocked, `- [~]` dropped |
| `NOTES.md` | agents | durable knowledge: how to build and test, decisions, gotchas |
| `JOURNAL.md` | supervisor | every session's handoff, in order |
| `HANDOFF.md`, `DONE.md`, `REVIEW.md` | agents | per-session handoff, done claim, reviewer verdict (consumed by the supervisor) |
| `FEEDBACK.md` | supervisor | what the next session must address first |
| `INBOX.md` | `bananavibe say` | your messages for the next session |
| `ANSWERS.md` | `bananavibe ask` | your questions and the answers you got |
| `state.json`, `control.json` | supervisor / CLI | run state (including every session and its agent session id) and pending commands |
| `logs/` | supervisor | `supervisor.log`, `supervisor.out` (background process output), one raw log per session, check output, `ask/` |
| `reports/` | supervisor | checkpoint and final reports (`latest.md`), archived reviews and done claims |
| `prompts/` | supervisor | the last prompt sent for each session type |

When the workspace is itself a git repository, `.bananavibe/` is added to `.git/info/exclude`, so it never
shows up in the project's history.

Every key below is optional. Unknown keys are rejected, so typos don't pass silently.

## `[run]`

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | folder name | used in notifications, export file names and the tmux session of `start --tmux` |
| `workers` | `["claude"]` | agents that do the work, in order of preference |
| `reviewer` | first worker | agent that audits at checkpoints and on done claims; another model is better |
| `strategy` | `"failover"` | `failover`: always prefer the first available worker. `round-robin`: rotate every session |
| `session_timeout_minutes` | `120` | a session is stopped after this long; its work is kept and the next session continues |
| `idle_timeout_minutes` | `30` | a session with no output for this long is considered hung, killed and retried |
| `max_iterations` | `0` | stop after this many completed sessions (0 = unlimited) |
| `max_hours` | `0` | stop after this many hours since the run first started (0 = unlimited) |
| `checkpoint_every` | `5` | review and report every N sessions (0 = never) |
| `review_at_checkpoint` | `true` | run the reviewer at checkpoints (otherwise just write the report) |
| `pause_at_checkpoint` | `false` | pause after each checkpoint report until `bananavibe resume` |
| `done_approvals` | `2` | consecutive reviewer approvals needed to finish |
| `stall_limit` | `3` | sessions without any change before the supervisor switches to another worker (and, if the plan is complete and checks pass, verifies the work as done) |
| `resume_sessions` | `true` | after `pause --now` / `stop --now`, resume the interrupted agent conversation itself (Claude Code, same machine). Otherwise a fresh session is told what the interrupted one was doing |
| `asker` | the reviewer | agent that answers `bananavibe ask` |
| `ask_timeout_minutes` | `15` | how long an answer may take |

## `[retry]`

| Key | Default | Meaning |
| --- | --- | --- |
| `initial_seconds` | `30` | first backoff after an outage, overload, network error or crash; doubles per failure |
| `max_seconds` | `1800` | backoff cap; also the retry interval for configuration errors such as a bad model name |
| `limit_fallback_seconds` | `900` | retry interval for usage limits whose reset time could not be determined |
| `auth_seconds` | `600` | retry interval while an agent is logged out |

Nothing is ever retried a limited number of times: a run only ends when the mission is approved, when you stop it,
or when `max_iterations` or `max_hours` is reached. Use `bananavibe retry-now` to skip a wait, for example after
you log an agent back in.

## `[git]`

| Key | Default | Meaning |
| --- | --- | --- |
| `snapshot` | `true` | commit all changes in every repository after every session |
| `branch` | `"bananavibe/work"` | work branch, created from the current HEAD if needed (`""` = stay on the current branch) |
| `push` | `false` | push the work branch to `remote` after every snapshot |
| `remote` | `"origin"` | |
| `repos` | `[]` | repositories to manage, relative to the workspace. Empty = the workspace itself if it is a repository, plus every direct subfolder that is one |

Snapshot commits use the repository's configured identity, or `BananaVibe <bananavibe@localhost>` if there is none.

## `[[checks]]`

Commands that define "working". They run (with `bash -lc`, `CI=1`) after every session, and all must pass before a
done claim is even shown to the reviewer.

```toml
[[checks]]
name = "backend-tests"
run = "pytest -q"
cwd = "api"                  # relative to the workspace
timeout_minutes = 30
```

Good checks are strict and cheap enough to run often: test suites, type checkers, linters, a production build,
an end-to-end smoke script. A check you write yourself and the agents can't edit (keep it outside the workspace)
is the strongest guard against "it works on my machine" claims.

## `[agents.<name>]`

The built-in names `claude`, `codex` and `opencode` work without a section. Add one to change settings, or define
more agents (for example two Claude profiles with different models).

| Key | Meaning |
| --- | --- |
| `type` | `claude`, `codex`, `opencode` or `custom` (defaults to the section name for built-ins) |
| `command` | the executable and leading arguments, e.g. `["/opt/claude/bin/claude"]` |
| `model` | Claude: `--model` (`opus`, `sonnet`, a full id). Codex: `-m`. OpenCode: `provider/model` |
| `effort` | Claude: `--effort`. Codex: `model_reasoning_effort`. OpenCode: appended as the `#variant` |
| `args` | extra arguments |
| `env` | extra environment variables, e.g. `{ ANTHROPIC_API_KEY = "…" }`, `{ CODEX_HOME = "/root/.codex-2" }` |

What each built-in runs:

- **claude**: `claude -p --dangerously-skip-permissions --output-format stream-json --verbose [--model] [--effort]`,
  prompt on stdin. When running as root, `IS_SANDBOX=1` is set, because Claude Code otherwise refuses to skip
  permissions as root.
- **codex**: `codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check --json -C <workspace>
  [-m] [-c model_reasoning_effort=…] -`, prompt on stdin.
- **opencode**: `opencode run --auto --format json [-m]`, with a message telling it to read the prompt file.

For `bananavibe ask` the agents run read-only: Claude Code without the permission bypass and with only reading
tools allowed (`Read`, `Grep`, `Glob`, `git log/show/diff/status`, `ls`, `cat`…); Codex with `--sandbox
read-only`; OpenCode without `--auto`. Custom agents are only told not to change anything.

### Custom agents

Any CLI that takes a prompt and works in the current directory:

```toml
[agents.aider]
type = "custom"
command = ["aider", "--yes-always", "--no-pretty", "--message-file", "{prompt_file}"]
```

Placeholders: `{prompt}`, `{prompt_file}`, `{model}`, `{effort}`, `{cwd}`. Output is shown line by line; exit code 0
means the session ended normally; otherwise the output is classified (limit / outage / auth / config) like the
built-ins.

## `[prompt]`

| Key | Default | Meaning |
| --- | --- | --- |
| `allow_push` | `false` | tell agents they may push, publish and deploy. Otherwise they are told not to |
| `extra` | `""` | extra rules added to every worker prompt (coding standards, forbidden actions, …) |

## `[notify]`

`command` is run with `bash -c` on `start`, `checkpoint`, `claim`, `done`, `stopped` and `attention` (logged out,
bad configuration, supervisor crash), with `BANANAVIBE_EVENT`, `BANANAVIBE_MESSAGE`, `BANANAVIBE_RUN` and
`BANANAVIBE_WORKSPACE` in the environment. For phone notifications with [ntfy](https://ntfy.sh):

```toml
[notify]
command = 'curl -s -H "Title: BananaVibe $BANANAVIBE_RUN" -d "$BANANAVIBE_EVENT: $BANANAVIBE_MESSAGE" ntfy.sh/your-secret-topic'
```
