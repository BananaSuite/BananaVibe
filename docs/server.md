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
apt update && apt install -y git tmux python3 curl
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
bananavibe start            # detached tmux session "bananavibe-<name>"
exit                        # log out; it keeps going
```

Hours later:

```bash
bananavibe status           # one screen: state, current agent, plan progress, checks, cooldowns, events
bananavibe attach           # live view; Ctrl-b d to detach again
less .bananavibe/reports/latest.md
git -C project-a log --oneline main..bananavibe/work
```

Steering:

| You want to… | Do |
| --- | --- |
| change direction | `bananavibe say "…"` (read by the next session, before the plan) |
| read every checkpoint before it continues | `pause_at_checkpoint = true`, then `bananavibe resume` |
| stop for now | `bananavibe stop` (after the current session) or `bananavibe stop --now` (interrupts it; work is committed) |
| continue later | `bananavibe start` again; it picks up where it stopped |
| retry right away after fixing a login | `bananavibe retry-now` |
| continue a finished run with new work | `bananavibe say "…"` then `bananavibe start` |

Config changes take effect on the next `bananavibe start`.

## 5. Survive reboots (optional)

A systemd unit restarts the supervisor after crashes and reboots:

```ini
# /etc/systemd/system/bananavibe.service
[Unit]
Description=BananaVibe run
After=network-online.target

[Service]
User=root
WorkingDirectory=/root/work
ExecStart=/root/.local/bin/bananavibe run
Restart=always
RestartSec=60
KillSignal=SIGTERM
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
```

`systemctl enable --now bananavibe`, then follow it with `journalctl -fu bananavibe` or `bananavibe logs -f`.
Use either systemd or `bananavibe start`, not both. A finished run doesn't restart: `run` exits immediately
until you give new instructions.

## Costs and limits

Long runs consume a lot. Subscriptions cap usage per window, so BananaVibe waits out the limits; with API keys
there is no cap except your budget, so set `max_hours` or `max_iterations`, and watch the provider's dashboard.
Configuring two different agents as workers keeps work going while one of them is limited.
