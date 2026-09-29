# Contributing to BananaVibe

Thank you for helping. BananaVibe prepares drafts that maintainers review, so
changes must preserve four things: only maintainers can drive it, private
prompts stay private, work is always recoverable, and people decide what
merges.

## Getting started

Read the [development guide](docs/development.md) for the module map and test
commands. In short, with Python 3.11+, Git and age installed:

```sh
python -m pip install pytest ruff
python -m ruff check . && python -m pytest -q
```

Changes to `bananavibe/engine.py`, `sandbox/` or `Dockerfile.sandbox` must also
pass the Docker test: `python -m pytest -q -m docker`. CI runs it on every pull
request.

## Pull requests

- Describe the user-visible behavior, how failures recover, and how you validated it.
- Add or update tests. The suite uses real Git repositories, so most behavior can be tested without mocks.
- Keep BananaVibe dependency-free and compatible with existing configuration and schema-1 task records. Explain any upgrade steps in `UPGRADING.md` and `CHANGELOG.md`.
- Changes to workflows, the Action, credentials handling or the sandbox boundary need a maintainer's direct review.
- `banana_backup/` is shared with BananaWiki and BananaChat. See `scripts/sync_backups.py --help` before touching it.
- A coding agent, including BananaVibe itself, may write a first draft, but a person must review the design and the code. Passing checks never justify merging on their own.

Never put tokens, private prompts, Actions logs or production data in examples,
tests or issues.

## Licensing

Contributions are made under the repository's AGPL-3.0-only license, and
contributors keep copyright in their contributions. Keep OpenCode's MIT notice
and other third-party notices intact. Discussions and reviews follow the
[code of conduct](CODE_OF_CONDUCT.md).
