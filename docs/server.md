# Running on a server for days

## 1. A machine you can give away

BananaVibe disables every permission prompt, so the agents can run any command. Use a dedicated VPS or VM, or a
container, with nothing on it you can't afford to lose:

- No production credentials, SSH keys to other servers, or cloud admin tokens.
- If agents need to push, use a fine-grained token or a deploy key limited to the repositories involved (and keep
  `git.push` / `allow_push` off until you trust the setup). Without pushing, everything stays local until you
  review it.
- Take a snapshot of the VM before a long run; it's the cheapest undo there is.

Running as root works. Claude Code needs `IS_SANDBOX=1` to skip permissions as root, and BananaVibe sets it for
you. A normal user is still the better default.

## 2. Install the tools

```bash
apt update && apt install -y git python3 curl
# Agents (pick the ones you use), then log each one in once, interactively:
curl -fsSL https://claude.ai/install.sh | bash && claude          # /login
npm install -g @openai/codex && codex login                         # or: codex login --device-auth over SSH
curl -fsSL https://opencode.ai/install | bash && opencode auth login
# BananaVibe
pipx install git+https://github.com/BananaSuite/BananaVibe          # or ./scripts/install.sh from a checkout
```

Plus whatever your projects need to build and test (Node, Docker, databases, browsers for screenshots…). Agents can
install missing tools themselves when they run as root, but it is faster and more predictable to do it up front.

## 3. Set up the workspace

```bash
mkdir -p ~/work && cd ~/work
git clone https://github.com/you/project-a && git clone https://github.com/you/project-b
bananavibe init --workers claude,codex --reviewer codex --check "a-tests=cd project-a && npm test"
$EDITOR .bananavibe/GOAL.md .bananavibe/config.toml
bananavibe doctor --live
```

## 4. Start, leave, come back

```bash
bananavibe start            # in the background, guarded, and set up to start again after a reboot
exit                        # log out; it keeps going
```

`start` runs a small guardian process that runs the supervisor and restarts it whenever it crashes, is killed
(`kill -9`, the OOM killer) or hangs (it writes a heartbeat; `watchdog_minutes`). The first `start` in a workspace
also installs autostart, so the run comes back after a reboot or if the guardian itself dies:

- **as root on a systemd machine**: a system service `bananavibe-<name>-<id>` (see section 6);
- **as a normal user on a systemd machine with lingering** (`sudo loginctl enable-linger $USER`, which also keeps
  your processes alive when you log out): the same as a user service;
- **otherwise**: two crontab entries, `@reboot` and every 10 minutes, that start the run if it is down.

`bananavibe service` shows which one is in use (`service show` prints the unit, `service uninstall` removes it);
`[run] autostart = false` or `start --no-autostart` skips it. A run you stopped with `bananavibe stop` is never
started automatically, not even after a reboot, until you `bananavibe start` it again.

Hours later:

```bash
bananavibe status           # one screen: state, current agent, plan progress, checks, cooldowns, events
bananavibe attach           # live view of the agent's transcript; Ctrl-C stops watching, not the run
bananavibe ask "how far along is it, and is anything stuck?"
less .bananavibe/reports/latest.md
git -C project-a log --oneline main..bananavibe/work
```

Steering:

| You want to… | Do |
| --- | --- |
| watch and act from one screen | `bananavibe dashboard` (keys: pause, resume, say, say now, ask, retry, stop) |
| talk to it | `bananavibe chat` (questions are answered, `/say` and `/now` give instructions) |
| change direction | `bananavibe say "…"` (the running agent sees it between tasks; every next session gets it until one completes) |
| change direction right now | `bananavibe say --now "…"` (interrupts the work session, which continues at once with your message) |
| change the mission or settings | `bananavibe edit goal` / `edit config` (applied before the next session) |
| read every checkpoint before it continues | `pause_at_checkpoint = true`, then `bananavibe resume` |
| know something | `bananavibe ask "…"` (a read-only agent answers from the run's state, live or after it ended) |
| pause | `bananavibe pause` (after the current session) or `pause --now` (interrupts it; `resume` continues the same session) |
| stop for now | `bananavibe stop` (after the current session) or `bananavibe stop --now [--wait]` (interrupts it; work is committed) |
| continue later | `bananavibe start` again; it picks up where it stopped, continuing an interrupted session |
| move to another server | `bananavibe export --stop -o run.tar.gz`, copy it, `bananavibe -w ~/work import run.tar.gz`, `start` |
| look inside a session | `bananavibe sessions`, `bananavibe show <#>`, `bananavibe open <#>` (the agent's own UI) |
| retry right away after fixing a login | `bananavibe retry-now` |
| continue a finished run with new work | `bananavibe say "…"` then `bananavibe start` |

Everything the background supervisor prints goes to `.bananavibe/logs/supervisor.log` (`bananavibe logs -f`);
crashes of the supervisor itself also land in `logs/supervisor.out`, and restarts are listed in `status`.

## 5. Moving a run to another machine

```bash
# old server
bananavibe export --stop -o /tmp/run.tar.gz      # stops now; the interrupted session will be continued
scp /tmp/run.tar.gz new-server:
# new server (agents installed and logged in)
bananavibe -w ~/work import ~/run.tar.gz
bananavibe -w ~/work doctor --live && bananavibe -w ~/work start
```

The archive has the run directory and a `git bundle` of every repository with all branches and tags, so no
shared remote is needed, plus checksums that are verified on import. The import recreates the repositories (or
fast-forwards existing clones; it refuses to overwrite diverged work unless you pass `--force`), sets the original
remote URL as `origin`, and clears cooldowns, since limits belong to the old machine's logins. Ignored files
(dependencies, build output, local databases) don't travel: have the agents or a setup script recreate them.

If a Claude Code session was interrupted, its conversation travels too and is resumed natively on the new machine
(when that fails, the next session starts fresh with a summary of what the interrupted one was doing). The old
machine treats the run as stopped by you, so its autostart won't start it there again. Review the imported
`config.toml` before starting: its `[notify] command` and custom agent commands will run on the new machine.

## 6. The systemd service

`bananavibe start` writes and enables the unit for you; `bananavibe service show` prints it. The parts that
matter for a run that must never stop:

```ini
[Unit]
StartLimitIntervalSec=0          # never give up restarting

[Service]
ExecStart=/usr/bin/python3 -P -m bananavibe -w /root/work run --service --daemon
EnvironmentFile=-/root/work/.bananavibe/service.env   # API keys etc. your shell profile would normally set
Restart=on-failure               # a clean exit (finished, or `bananavibe stop`) is final; everything else restarts
RestartSec=20
KillMode=mixed                   # on shutdown, the supervisor interrupts the agent itself: continued after boot
TimeoutStopSec=180
OOMPolicy=continue               # the OOM killer taking an agent must not stop the whole service
OOMScoreAdjust=-800              # (system service) the supervisor is the last thing the OOM killer picks

[Install]
WantedBy=multi-user.target
```

`run --service` respects an operator stop: after `bananavibe stop`, neither a reboot nor `systemctl start` starts
it; `bananavibe start` does (it goes through systemd). `systemctl stop <unit>` stops it until the next boot.
Follow it with `bananavibe logs -f` or `journalctl -fu <unit>`; `systemctl status <unit>` shows the run's current
state. `attach`, `chat`, `pause` and the rest work the same way.

Agents run with your PATH as it was when you ran `start`. If they need environment variables your shell profile
sets (API keys, `CODEX_HOME`...), put them in `.bananavibe/service.env`.

## 7. Never stopping: what is covered

| Event | What happens |
| --- | --- |
| Agent crashes, hangs, hits a limit, is logged out | retried with backoff / at the reset time / every `auth_seconds`, failing over to other workers |
| Agent killed by the OOM killer | agents carry `oom_score_adj = 500`, so they go before the supervisor; the session is retried |
| Supervisor crashes, is killed or hangs | the guardian restarts it in seconds; the interrupted session is continued |
| Guardian killed, or the machine reboots | systemd restarts it / starts it at boot, or cron does within 10 minutes |
| Disk full or nearly full, memory low | logs are pruned; it waits and retries instead of failing (`retry-now` overrides) |
| You log out | nothing (with a systemd service or lingering; see `bananavibe doctor` for `KillUserProcesses`) |
| The machine itself dies | nothing software can do: `[notify] ping_url` lets an external monitor tell you |

`bananavibe doctor` reports what could still stop a long run on this machine.

## Costs and limits

Long runs consume a lot. Subscriptions cap usage per window, so BananaVibe waits out the limits; with API keys
there is no cap except your budget, so set `max_hours` or `max_iterations`, and watch the provider's dashboard.
With `after_done = "wait"` (the default) a finished run uses nothing while it waits for new instructions.
Configuring two different agents as workers keeps work going while one of them is limited.
