# Working on BananaVibe

Run `python -m pytest -q` with Python 3.11 or newer after installing
`requirements.txt`, `pytest`, and `pyyaml`. Git and age are needed for backup
tests. The suite uses local repositories and HTTP fixtures.

| Area | Module |
| --- | --- |
| Issue commands and authorization | `bananavibe/controller.py` |
| Task leases, checkpoints, and recovery | `bananavibe/runner.py`, `state.py` |
| OpenCode process and acceptance checks | `bananavibe/engine.py` |
| GitHub and Forgejo API calls | `bananavibe/forge.py` |
| Git branches and worktrees | `bananavibe/workspace.py` |
| Model access inside the container | `sandbox/gateway.py` |
| Encrypted repository backups | `bananavibe/backups.py`, `banana_backup/` |

Keep forge credentials on the controller side of the container boundary.
Model tasks receive the workspace, task instructions, and scoped gateway
access. Changes to the engine or gateway also need a Docker/OpenCode run
on a disposable runner; see [security-model.md](security-model.md).

Write status messages for the maintainer deciding what to do next: state the
task outcome, the failed check, or the command that resumes it. Keep internal
diagnostics in the runner log. Document new commands in both the CLI help and
[README.md](../README.md).
