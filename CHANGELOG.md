# Changelog

## 3.0.0

A rewrite for production use and open-source release. Configuration, state and
backups are compatible with 2.x; see [UPGRADING.md](UPGRADING.md). The review
that motivated each change is in [docs/review-2.0.md](docs/review-2.0.md).

### Security
- Issue text is fingerprinted when a maintainer approves it. An edit afterwards blocks the task until it is re-approved.
- Acceptance checks run in a fresh container on a clean export of the committed tree, out of the agent's reach.
- `[network] allow` restricts the sandbox's internet access to listed hosts.
- PR text cannot mention people or auto-close issues.
- Local composite actions (`.github/actions/`, `.forgejo/actions/`) are protected by default.
- Each task's gateway gets its own isolated bridge network.
- No runtime dependencies or installs. Docker gets a minimal environment.

### Reliability
- Commands that arrive while a run is finishing are no longer lost.
- Work is checkpointed on every exit path, including model and preparation failures.
- OpenCode is polled with bounded message reads. Long sessions no longer fail at 8 MiB.
- An engine that goes idle without replying is detected within two minutes.
- Per-command timeouts (`limits.command_minutes`).
- Pushes are leased against the last checkpoint and never overwrite outside commits.
- Draft PRs fall back gracefully where drafts are unavailable.
- Heartbeat and worker threads share state safely.

### Features
- `/banana answer` on a finished task revises the same branch and PR.
- Multi-line `/banana answer`, plus `revise`, `cancel` and `continue` aliases.
- A single status comment, edited in place.
- The sandbox image is tagged by content, so upgrades rebuild automatically. Release builds are published to GHCR.
- `bananavibe check-config`, `bananavibe migrate-state`, and Action outputs `outcome` and `issue`.
- Protected-path edits are reverted and explained to the agent instead of failing the task.

### Operations
- The state branch is an orphan, so its writes don't trigger push workflows. All records are listed with one Git fetch, with no 1,000-task limit.
- The PR description comes from one diff-only request instead of a second sandbox.
- Hourly watchdog and cheap `author_association` filtering in the example workflows.
- Provider errors reach OpenCode and the logs, with keys redacted.
- A test suite built on real Git, plus a Docker end-to-end test in CI.

## 2.0.0

First public release (September 2026).
