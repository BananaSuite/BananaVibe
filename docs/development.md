# Developing BananaVibe

## Layout

| Path | Responsibility |
| --- | --- |
| `bananavibe/cli.py` | Entry point used by the Action; `check-config`, `migrate-state`, backups. |
| `bananavibe/controller.py` | Events → authorized commands → recorded intent. Does no task work. |
| `bananavibe/runner.py` | One run: lease, heartbeat, work loop, checkpoints, checks, publication. |
| `bananavibe/engine.py` | Docker lifecycle, the OpenCode API client, isolated checks, image build. |
| `bananavibe/workspace.py` | The task's Git working tree: snapshot, protected paths, leased push, export. |
| `bananavibe/state.py` | Task records on the state branch (compare-and-swap), listing, orphan branch. |
| `bananavibe/forge.py` | GitHub and Forgejo REST calls. |
| `bananavibe/gitutil.py` | Hardened Git invocation with credentials kept out of argv and errors. |
| `bananavibe/httpclient.py` | Bounded, no-redirect, no-proxy HTTP client. |
| `bananavibe/summary.py` | Diff-only PR description and its sanitizer. |
| `bananavibe/commands.py`, `config.py` | Command parsing and configuration. |
| `bananavibe/backups.py` | BananaVibe's backup package; encryption and upload are in `banana_backup/`. |
| `sandbox/gateway.py` | Runs inside the sandbox image: inference, egress and control relays. |
| `Dockerfile.sandbox` | The sandbox image. Local builds are tagged with a hash of their inputs. |

Everything is standard-library Python 3.11+. Please keep it that way: the Action runs without a virtualenv or network installs.

## Tests

```sh
python -m pip install pytest ruff     # plus: apt install age   (backup tests)
python -m ruff check .
python -m pytest -q                   # unit and integration tests, no network or Docker
python -m pytest -q -m docker         # full task in the real sandbox (builds the image)
python scripts/sync_backups.py --check
```

The tests use real Git repositories. `tests/fakes.py` provides a forge double whose branches and state files are real Git objects, so leases, compare-and-swap, checkpoints and pushes behave as they do in production. `tests/test_e2e_docker.py` runs a whole task in the real image against a scripted model and asserts the network isolation.

## Rules of thumb

- Credentials stay on the controller side. Nothing in `sandbox/` or a container may receive the forge token or a real model key.
- Every loop that can take time calls `pulse()`, so stop commands, deadlines and cancellation are observed.
- Messages posted to issues say what happened and what to do next. Diagnostics go to the job log.
- Keep the state record at schema 1. Add fields with defaults in `state.normalize()`, so running tasks survive upgrades in both directions.
- `banana_backup/` is shared with BananaWiki and BananaChat. Change it only together with them, via `scripts/sync_backups.py --record` and `--write`.
- Changing `Dockerfile.sandbox` or `sandbox/` changes the image tag automatically. Bump the base image digest and OpenCode version deliberately, and run the Docker test.
