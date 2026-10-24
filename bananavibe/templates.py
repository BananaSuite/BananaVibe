"""Files written by `bananavibe init`."""

CONFIG = """\
# BananaVibe run configuration. Every key is optional; see docs/configuration.md.

[run]
name = "{name}"
# Agents that do the work, in order of preference. When one hits a usage limit or an outage,
# the next available one takes over. Built-in agents: claude, codex, opencode (or define your own below).
workers = {workers}
# Agent that audits the work at checkpoints and when a worker claims the mission is done.
# A different model than the worker catches more.
reviewer = "{reviewer}"
strategy = "failover"            # or "round-robin" to alternate workers every session
session_timeout_minutes = 120    # a single session is stopped after this long (work is kept)
idle_timeout_minutes = 30        # ...or after this long without any output (hung)
max_iterations = 0               # 0 = no limit
max_hours = 0                    # 0 = no limit
checkpoint_every = 5             # review + report every N sessions (0 = never)
review_at_checkpoint = true
pause_at_checkpoint = false      # true = wait for `bananavibe resume` after each checkpoint report
done_approvals = 2               # consecutive reviewer approvals needed to finish
stall_limit = 3                  # sessions without progress before switching agent
resume_sessions = true           # continue an interrupted or cut-off session in the same conversation (Claude Code)
# asker = ""                     # agent that answers `bananavibe ask` / `chat` ("" = the reviewer)
after_done = "wait"              # when the mission is approved: "wait" for new instructions (`say`), or "exit"
autostart = true                 # `start` sets up starting at boot and whenever the run is down (systemd or cron)
watchdog_minutes = 60            # restart a supervisor whose main loop has been stuck this long (0 = off)
min_free_disk_mb = 1024          # below this, clean old logs and wait before starting a session
min_free_memory_mb = 256         # below this, wait before starting a session
agent_oom_score_adj = 500        # Linux: the OOM killer takes an agent (retried) before the supervisor

[retry]
initial_seconds = 30             # backoff for outages and crashes, doubling up to max_seconds
max_seconds = 1800
limit_fallback_seconds = 900     # how often to retry a usage limit whose reset time is unknown
auth_seconds = 600               # how often to retry while an agent is logged out
limit_recheck_seconds = 3600     # re-check a limit at least this often even if its reset time looks later

[git]
snapshot = true                  # commit the work after every session
branch = "bananavibe/work"       # work branch in every repository ("" = stay on the current branch)
push = false                     # push the branch after every snapshot
remote = "origin"
repos = []                       # repositories to snapshot, relative to the workspace ([] = auto-detect)
max_file_mb = 50                 # never commit files larger than this (build output, datasets...); 0 = no limit

[prompt]
allow_push = false               # whether agents may push, publish or deploy themselves
extra = \"\"\"
\"\"\"

# Verification commands. They run after every session and must pass before the run can finish.
# Make them strict: they are the objective definition of "working".
{checks}
[agents.claude]
model = "{claude_model}"         # e.g. "opus", "sonnet" or a full model id; "" = Claude Code's default
effort = "{claude_effort}"       # low | medium | high | xhigh | max; "" = default
# args = []                      # extra command-line arguments
# env = {{}}                       # extra environment variables

[agents.codex]
model = "{codex_model}"          # "" = Codex's configured default
effort = "{codex_effort}"        # model_reasoning_effort, e.g. high | xhigh | max | ultra

[agents.opencode]
model = "{opencode_model}"       # provider/model, e.g. "anthropic/claude-opus-5-5"; "" = OpenCode's default

# Any other CLI agent. {{prompt}} or {{prompt_file}} is replaced with the session prompt.
# [agents.aider]
# type = "custom"
# command = ["aider", "--yes-always", "--message-file", "{{prompt_file}}"]

[notify]
# Optional. Events: start, checkpoint, claim, done, stopped, attention, update, restart.
# A shell command; it receives BANANAVIBE_EVENT, BANANAVIBE_MESSAGE, BANANAVIBE_RUN, BANANAVIBE_WORKSPACE,
# BANANAVIBE_HOST (and BANANAVIBE_REPORT for reports).
# command = 'curl -s -d "$BANANAVIBE_RUN: $BANANAVIBE_MESSAGE" ntfy.sh/my-secret-topic'
command = ""
# ...and/or a webhook: format "text" (ntfy.sh and friends), "json", "slack" or "discord".
# url = "https://ntfy.sh/my-secret-topic"
# format = "text"
# headers = {{ Authorization = "Bearer ..." }}
# events = []                    # [] = all events
update_every_hours = 0           # a short progress update every N hours (0 = off)
# ping_url = "https://hc-ping.com/<uuid>"   # pinged every ping_every_minutes while the run is alive
# ping_every_minutes = 5

[logs]
max_mb = 2048                    # old logs are deleted beyond this total size (0 = keep everything)
compress_after = 20              # session logs older than the newest N are gzip-compressed
"""

CHECK = """[[checks]]
name = {name}
run = {run}
cwd = "."
timeout_minutes = 30

"""

NO_CHECKS = """# [[checks]]
# name = "tests"
# run = "pytest -q"
# cwd = "."
# timeout_minutes = 30

"""

GOAL = """\
# Mission

{goal}

## Definition of done

<!-- Make it concrete and verifiable. For example:
- Every project builds from a clean checkout with the documented commands.
- All tests pass, and new tests cover every change.
- The main user flows were exercised for real (started the app, used it) and work.
- No TODOs, stubs, dead code or known bugs remain in the touched areas.
- Documentation matches the behavior.
-->

## Constraints

<!-- What must not change: public APIs, data formats, look and feel, dependencies to avoid, and so on. -->
"""

PLAN = """\
# Plan

<!-- Maintained by the agents. `- [ ]` open, `- [x]` done, `- [!]` blocked (why), `- [~]` dropped (why). -->
"""

NOTES = """\
# Notes

<!-- Maintained by the agents: how to build, run and test things; decisions taken; gotchas. -->
"""
