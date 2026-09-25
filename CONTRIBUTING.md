# Contributing to BananaVibe

See the [development guide](docs/development.md) for the controller, runner,
engine, gateway, and backup module map.

BananaVibe prepares feature trials and maintenance drafts for BananaWiki and BananaChat. Changes must preserve maintainer authorization, private prompt separation, recoverable tasks, and human control over merging.

Use Python 3.11 or newer. Install `requirements.txt` and `pytest`, then run `python -m pytest -q`. The tests use local Git repositories and HTTP fixtures; no forge account or paid model is needed. Test the real Docker/OpenCode path on a disposable runner when changing the engine or gateway.

Encrypted backup tests require Git and age (`apt install age` on Debian/Ubuntu); they use disposable local repositories and dummy credentials. Run `python scripts/sync_backups.py --check` for the backup code shared by all three applications. After reviewing a shared change, use `--record` and `--write ../OTHER_CHECKOUT`, review each diff, and commit the manifests with the implementation. The copy refuses unrecorded changes in the destination.

The task image provides Python 3.13 for the applications' acceptance checks. Bump its default image tag when changing the Dockerfile or bundled gateway so persistent runners build the new image instead of reusing an older cached tag.

Describe the user-visible behavior, failure recovery, and validation in each PR. A coding agent may write the first draft, but a maintainer must review the design and the resulting code. Passing checks are evidence for review and do not permit an automatic merge.

Never place tokens, private prompts, Actions logs, or production data in examples or tests. Workflow/configuration changes are privileged changes and require direct maintainer review. See [security boundaries](docs/security-model.md) and [task lifecycle](docs/task-lifecycle.md).

Contributions use the repository's AGPL-3.0-only license. Contributors retain copyright in their contributions. Preserve OpenCode's MIT notice and other third-party notices.

Discussions and reviews follow the [code of conduct](CODE_OF_CONDUCT.md).
