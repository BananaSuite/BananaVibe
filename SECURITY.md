# Security

BananaVibe runs coding agents with all permission prompts disabled. Treat the machine it runs on as fully
controlled by those agents: run it on a dedicated VM, VPS or container without credentials you are not willing to
expose. See [docs/server.md](docs/server.md).

The agents run as the same user as the supervisor and could, if they wanted to, change anything it can. What
BananaVibe does on top (reverting agent edits to `GOAL.md` and `config.toml`, keeping agents on the work branch,
never killing a process it can't prove is its own session's, validating imported archives, never running
anything from a config the operator did not write or import) protects against agent mistakes, not against a
deliberately hostile agent. Review the commands in an imported `config.toml` (`[notify] command`, custom agents)
before starting it: they run on your machine.

A copy of the run's files, `service.env` included (where you may keep API keys for the background service), is kept
outside the workspace in `~/.local/state/bananavibe/` (or `BANANAVIBE_STATE_DIR`), readable only by you. The
supervisor's own git commands run no git hooks.

To report a vulnerability in BananaVibe itself (for example, a way for repository content to escape what the
operator configured), please use GitHub's private vulnerability reporting on this repository instead of a public
issue.
