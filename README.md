<img src="banana-logo.png" alt="BananaVibe logo" width="64">

# BananaVibe

**BananaVibe** lets BananaWiki and BananaChat maintainers try out features quickly and keep up with routine maintenance. Start it from a GitHub or Forgejo issue; it runs OpenCode in Actions and opens a draft pull request for review.

The workflow can live in the contribution repository or in a separate private issue repository. The bot checks maintainer access, works on its own branch, reports progress, runs the configured checks, and links the draft PR before closing the issue. Merging stays with the maintainer.

Typical tasks include a first version of a small feature to try out, small bug fixes, focused regression tests, documentation updates, and dependency maintenance. Maintainers remain responsible for design, code review, testing, and releases.

## Install the workflow

1. Choose the repository where issues and prompts will live. Keep it private if the discussion should be private.
2. Copy [examples/github.yml](examples/github.yml) to `.github/workflows/bananavibe.yml`, or [examples/forgejo.yml](examples/forgejo.yml) to `.forgejo/workflows/bananavibe.yml`.
3. Copy [examples/bananawiki.toml](examples/bananawiki.toml) or [examples/bananachat.toml](examples/bananachat.toml) to `.bananavibe.toml`. Set the issue repository, target repository, branch, model, and checks. For Forgejo, set `forge = "forgejo"` and your instance's HTTPS `server_url`.
4. Add `BANANAVIBE_TOKEN` and the named model keys as Actions secrets. Grant the bot access to both repositories, including branch creation, state-file updates, issue comments/closing, and pull-request creation. Use a dedicated bot account or a scoped GitHub App token.
5. Pin the BananaVibe action to the reviewed release commit, then commit the workflow and configuration to the issue repository's default branch.

The runner needs Linux, Python 3.11+, Git, and Docker. GitHub's Ubuntu runners provide these. Forgejo uses a dedicated Linux host runner labeled `bananavibe`; [setup details](docs/deployment.md) include runner configuration and permissions. The target may be private. Git credentials remain with the controller.

Validate configuration locally without contacting a forge:

```sh
python3 -m bananavibe --config examples/bananawiki.toml --check-config
```

## Issue commands

Only maintainers with write access to both repositories can start or control a task. Commands occupy their own comment.

| Command | Effect |
| --- | --- |
| `/banana help` | List commands. |
| `/banana start` | Start work on an existing issue. |
| `/banana status` | Show status and checkpoint. |
| `/banana stop` | Stop and retain the working branch. |
| `/banana resume` | Continue saved work. |
| `/banana restart` | Start from the base on a new branch. |
| `/banana models` | List configured model aliases. |
| `/banana model ALIAS` | Change model, including during a running task. |
| `/banana answer TEXT` | Answer a request for guidance and resume. |
| `/banana fail REASON` | End the task as failed. |

Tasks have time and iteration limits. Work stops when its checks pass, it needs guidance, or a limit is reached. Use `resume` to continue from a saved checkpoint. A scheduled watchdog records interrupted runners; [task recovery](docs/task-lifecycle.md) covers cancellation and retries.

## Prompts and review

Private prompts, guidance, and task metadata stay in the issue repository. Working branches contain proposed code. PR descriptions are generated in a separate session that sees only the public diff; issue titles, private links, conversation logs, and OpenCode share links are not copied into PRs. Generated code still needs review, including for unintended disclosure of information provided in a task.

OpenCode runs in an unprivileged Docker container with only its project and temporary state mounted. Its gateway supplies narrowly scoped model access; the container receives neither the forge token nor the real model key. See [security boundaries](docs/security-model.md).

## Backups

[Optional encrypted backups](docs/backups.md) save configuration, task state, issue transcripts, and Git checkpoints in a dedicated private GitHub or Forgejo repository. Restore first creates a private review directory. Explicit state recovery keeps unfinished tasks paused and preserves newer branches for human review. Save the recovery key offline; runner secrets and the forge's own issue/PR database need separate recovery.

## Why it exists

BananaWiki and BananaChat are large enough that trying an idea, bumping a dependency or fixing stale documentation adds up to steady work. BananaVibe was written to take that load, so a feature can be tried quickly and the routine upkeep does not pile up. Nothing it writes reaches a default branch until a person has read it.

It was an internal tool until September 2026, and the repository begins at a single commit because its development history is private and contains forge tokens and runner configuration. See [NOTICE](NOTICE) for the original dates.

## Ownership and license

Originally started by Luca Zani ([OverloadedTech](https://github.com/OverloadedTech)) on 13 August 2026, the date of its first recorded project commit. It was first called BananaAgent. Developed at Officina Tecnologica.

Copyright © 2026 Luca Zani and all contributors. Each contributor retains copyright in their contributions.

BananaVibe uses **AGPL-3.0-only**. Personal and commercial use are permitted. OpenCode remains MIT licensed; its notice is retained. See [LICENSE](LICENSE), [third-party notices](THIRD_PARTY_NOTICES.md), [contributing](CONTRIBUTING.md), [code of conduct](CODE_OF_CONDUCT.md), and [security reports](SECURITY.md).
