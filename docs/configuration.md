# Configuration

A run lives in `.bananavibe/` inside the workspace:

| File | Who writes it | Purpose |
| --- | --- | --- |
| `config.toml` | you | this file's subject |
| `GOAL.md` | you | the mission and its definition of done; agents must not edit it |
| `PLAN.md` | agents | master checklist: `- [ ]` open, `- [x]` done, `- [!]` blocked, `- [~]` dropped |
| `NOTES.md` | agents | durable knowledge: how to build and test, decisions, gotchas |
| `JOURNAL.md` | supervisor | every session's handoff, in order (older entries move to `JOURNAL.archive.md` past 4 MB) |
| `HANDOFF.md`, `DONE.md`, `REVIEW.md` | agents | per-session handoff, done claim, reviewer verdict (consumed by the supervisor) |
| `FEEDBACK.md` | supervisor | what the next session must address first |
| `inbox/` | `bananavibe say` | your messages, one file each, until a session has completed with them (then `inbox/delivered/` and `INBOX.archive.md`). A legacy `INBOX.md` is still picked up |
| `ANSWERS.md` | `bananavibe ask` / `chat` | your questions and the answers you got |
| `state.json` (+ `state.json.bak`), `control.json` | supervisor / CLI | run state (every session and its agent session id, the session in progress, post-session steps still to do, feedback) and pending commands |
| `heartbeat.json`, `guardian.json` | supervisor / guardian | liveness, used by the watchdog and `status` |
| `reverted/` | supervisor | versions of `GOAL.md` / `config.toml` changed without `bananavibe edit`/`reload`, after being restored |
| `service.json`, `service.env` | `bananavibe start` / `service` | the autostart set up on this machine; `service.env` holds extra environment for it (API keys...). For cron, the script it runs is `autostart.sh` in the backup directory described below |
| `logs/` | supervisor | `supervisor.log` (rotated at 64 MB), `supervisor.out` (background process output), one raw log per session (older ones gzip-compressed), check output, `ask/`, `autostart.log` |
| `reports/` | supervisor | checkpoint, verification and final reports (`latest.md`), periodic updates (`update-latest.md`), archived reviews and done claims |
| `prompts/` | supervisor | the last prompt sent for each session type |

When the workspace is itself a git repository, `.bananavibe/` is added to `.git/info/exclude`, so it never
shows up in the project's history. `.bananavibe/` also contains an empty git repository of its own (`.git`, a few
KB), which makes `git clean -fdx` and `git stash --all` in the project leave it alone. If an agent deletes it anyway,
the supervisor restores it: state, mission and config from memory, and plan, notes, journal, queued messages and
pending commands (`control.json`) from a backup kept in `~/.local/state/bananavibe/<id>/` (or `$XDG_STATE_HOME`, or
`BANANAVIBE_STATE_DIR`; autostart pins the directory it was set up with). Operator messages the deleting session
received are given again to the next one. The backup also holds the state, `GOAL.md`, `config.toml` (as last
approved) and `service.env`, so if the supervisor died too, its automatic restart (guardian, systemd or cron)
rebuilds the run directory and continues; a run you stopped on purpose, or a finished one, is not brought back.
`bananavibe init` in a workspace without a run starts a fresh backup (the previous one is kept once, as
`backup.previous`). The locks that stop two supervisors from running the same workspace are held on a file in
`.bananavibe/` and one in that directory.

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
| `resume_sessions` | `true` | continue an interrupted session (`pause --now`, `stop --now`, `say --now`, a supervisor restart, or a limit/outage after real work) in the agent's own conversation (Claude Code, same machine, or moved with `export`). Otherwise a fresh session is told what the interrupted one was doing |
| `asker` | the reviewer | agent that answers `bananavibe ask` and `chat` |
| `ask_timeout_minutes` | `15` | how long an answer may take |
| `after_done` | `"wait"` | when the mission is approved: `wait` keeps the supervisor up and continues as soon as you send new instructions with `say`; `exit` ends it |
| `autostart` | `true` | `bananavibe start` sets up starting the run at boot and whenever it is down: a systemd service (as root, or a user service with lingering) or cron entries. See [server.md](server.md) |
| `watchdog_minutes` | `60` | the guardian restarts a supervisor whose main loop made no progress this long, or whose heartbeat stopped (`0` = off) |
| `min_free_disk_mb` | `1024` | below this, old logs are pruned and the next session waits (up to 30 minutes at a time) |
| `min_free_memory_mb` | `256` | below this (Linux `MemAvailable`), the next session waits (up to 30 minutes at a time) |
| `agent_oom_score_adj` | `500` | Linux: agents get this OOM score adjustment, so under memory pressure the kernel kills an agent (which is retried) rather than the supervisor (`0` = leave it) |

## `[retry]`

| Key | Default | Meaning |
| --- | --- | --- |
| `initial_seconds` | `30` | first backoff after an outage, overload, network error or crash; doubles per failure |
| `max_seconds` | `1800` | backoff cap; also the retry interval for configuration errors such as a bad model name |
| `limit_fallback_seconds` | `900` | retry interval for usage limits whose reset time could not be determined |
| `auth_seconds` | `600` | retry interval while an agent is logged out |
| `limit_recheck_seconds` | `3600` | a reset time read from an error message may be wrong: the agent is retried at least this often. Claude Code's own reset timestamps are trusted (`0` = always trust) |

Nothing is ever retried a limited number of times: a run only ends when you stop it, when `max_iterations` or
`max_hours` is reached, or (with `after_done = "exit"`) when the mission is approved. Use `bananavibe retry-now`
to skip a wait, for example after you log an agent back in; it also overrides a low disk or memory wait for 30
minutes.

## `[git]`

| Key | Default | Meaning |
| --- | --- | --- |
| `snapshot` | `true` | commit all changes in every repository after every session |
| `branch` | `"bananavibe/work"` | work branch, created from the current HEAD if needed (`""` = stay on the current branch) |
| `push` | `false` | push the work branch to `remote` after every snapshot |
| `remote` | `"origin"` | |
| `repos` | `[]` | repositories to manage, relative to the workspace. Empty = the workspace itself if it is a repository, plus every direct subfolder that is one |
| `max_file_mb` | `50` | files larger than this are never committed by a snapshot (the agents are told to ignore or avoid them); `0` = no limit |

Snapshot commits use the repository's configured identity, or `BananaVibe <bananavibe@localhost>` if there is none.
If an agent leaves a repository on another branch, the supervisor switches it back to `branch` (carrying
uncommitted changes) and tells the agents to merge their work. A merge or rebase left unfinished is reported to
the agents, since nothing can be committed until it is resolved.

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
is the strongest guard against "it works on my machine" claims. Checks run the project's code, which the agents
wrote, so the same rules apply as after a session: changes to `GOAL.md` or `config.toml` made while they run are
reverted, and a deleted `.bananavibe/` is restored.

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

Optional. Events: `start`, `checkpoint`, `claim`, `done`, `stopped`, `attention` (logged out, bad configuration,
low disk or memory, agents stalling, supervisor crash), `restart` (the guardian restarted the supervisor) and
`update` (periodic progress). Notifications run in the background with timeouts, so a slow or broken channel never
holds the run up.

| Key | Default | Meaning |
| --- | --- | --- |
| `command` | `""` | run with `bash -c`, with `BANANAVIBE_EVENT`, `BANANAVIBE_MESSAGE`, `BANANAVIBE_RUN`, `BANANAVIBE_WORKSPACE`, `BANANAVIBE_HOST` (and `BANANAVIBE_REPORT`, a report's path) in the environment; killed after 60 s |
| `url` | `""` | a webhook, POSTed to |
| `format` | `"text"` | `text` (the message as the body and a `Title` header: [ntfy](https://ntfy.sh) and most bridges), `json` (`event`, `message`, `run`, `host`, `workspace`, `at`), `slack` or `discord` |
| `headers` | `{}` | extra HTTP headers, e.g. `{ Authorization = "Bearer ..." }` |
| `events` | `[]` | send only these events (`[]` = all) |
| `repeat_minutes` | `360` | the same alert (e.g. "logged out") is repeated at most this often |
| `update_every_hours` | `0` | send a short progress update (status, plan, checks, recent sessions and commits, the latest handoff) every N hours; also written to `reports/update-latest.md` |
| `ping_url` | `""` | a "dead man's switch" ([healthchecks.io](https://healthchecks.io), Uptime Kuma, ...): POSTed every `ping_every_minutes` while the supervisor is alive and its loop healthy, so the service alerts you if the machine goes down |
| `ping_every_minutes` | `5` | |

```toml
[notify]
url = "https://ntfy.sh/your-secret-topic"
update_every_hours = 6
# or a command:
# command = 'curl -s -H "Title: BananaVibe $BANANAVIBE_RUN" -d "$BANANAVIBE_EVENT: $BANANAVIBE_MESSAGE" ntfy.sh/your-secret-topic'
```

## `[logs]`

| Key | Default | Meaning |
| --- | --- | --- |
| `max_mb` | `2048` | total size of `logs/`; past it, the oldest session logs, check logs and `ask` logs are deleted (`0` = keep everything) |
| `compress_after` | `20` | session logs older than the newest N are gzip-compressed (`show`, `attach` and `ask` read them transparently) |

## Changing the configuration while it runs

`bananavibe edit config` (or `edit goal`) opens the file in your editor, validates it and applies it before the
next session; the running session is not interrupted. During a session it opens the last approved version
instead of the file on disk: if an agent changed the file, editing that would approve the agent's lines along
with yours.

Editing the files directly also works: changes made while no session (or the checks after it) is running are
picked up before the next one. During a session or its checks, run `bananavibe reload` afterwards (`reload goal` or `reload config` for just one): it shows
the diff against the approved version and asks before approving it (`--yes` skips the question; without a
terminal it is required),
since a change found right after a session is otherwise treated as the agent's and reverted (your version is kept
in `.bananavibe/reverted/`). An invalid `config.toml` is never applied: the previous configuration stays in force.
