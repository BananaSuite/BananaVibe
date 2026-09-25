# Install BananaVibe in an issue repository

BananaVibe runs in Actions to help BananaWiki and BananaChat maintainers prepare quick fixes and routine maintenance. It has no web account system or persistent application server. Each task uses a temporary Linux environment and OpenCode 1.18.18. The bot prepares a branch and draft PR; maintainers review it and decide whether to merge it.

## Repository layout

Choose an **issue repository** and a **target repository** on the same forge instance. They may be the same repository. For private planning, create a private issue repository such as `BananaWiki-prompts` and set `target_repository` to the public BananaWiki repository. Private targets also work.

The issue repository stores `.bananavibe.toml`, the workflow, issues/comments, and a `bananavibe-state` branch. Keep that entire repository private to keep prompts, guidance, state, and workflow history private. The target receives opaque task branches and PRs describing their code changes. Do not put a private issue link or title in a public workflow name.

Protect the target's default branch and require human review and CI. The bot needs write access to task branches, not permission to bypass protected-branch rules. Do not configure auto-merge for bot PRs.

## GitHub Actions

1. Copy [the GitHub workflow](../examples/github.yml) to `.github/workflows/bananavibe.yml` in the issue repository.
2. Copy [the Wiki configuration](../examples/bananawiki.toml) or [the AI configuration](../examples/bananachat.toml) to `.bananavibe.toml`. Set the exact repository names, target base branch, model ID, and preparation/acceptance commands.
3. Replace `BananaSuite/BananaVibe@main` with the reviewed, published commit SHA. The action repository must exist and be accessible to the runner before enabling the workflow.
4. Add the bot credential as the `BANANAVIBE_TOKEN` Actions secret. Add every model credential named in `api_key_env` as a secret and pass it in the workflow's `env` block.
5. Commit both files to the issue repository's default branch and enable Actions. Open an issue as a maintainer, or comment `/banana start` on an existing issue.

A fine-grained bot token needs read/write **Contents**, **Issues**, and **Pull requests**, plus read **Metadata**, on both repositories. A GitHub App installation token with equivalent permissions also works; generate it before this action and pass its output as `BANANAVIBE_TOKEN`. Ensure its lifetime exceeds the configured task allowance. The default `GITHUB_TOKEN` usually cannot write to a separate private repository; the example deliberately uses a separate bot credential.

The author of a command must have write, maintain, or admin access to both repositories. Public contributors can open issues, but their comments cannot directly run a task. A maintainer can review the issue and start it. Bot comments and PR comments do not trigger tasks.

## Forgejo Actions

Copy [the Forgejo workflow](../examples/forgejo.yml) to `.forgejo/workflows/bananavibe.yml`. Set these fields in `.bananavibe.toml`:

```toml
forge = "forgejo"
server_url = "https://forge.example.org"
control_repository = "maintainers/BananaWiki-prompts"
target_repository = "suite/BananaWiki"
```

The API defaults to `/api/v1`. Use a bot access token with repository and issue read/write permissions, scoped to the repositories where the Forgejo version supports that restriction. Both repositories must permit the bot to read collaborator permissions, create branches, update repository contents, comment/close issues, and open PRs. Branch creation uses Forgejo's branch API. PRs use its `WIP:` title convention; GitHub uses native draft PRs.

Enable Actions for the issue repository and register a dedicated Linux host runner with the `bananavibe` label, mapped to host execution, for example `bananavibe:host` in the runner's labels. Install Python 3.11+ with venv support, Git, and Docker Engine. The runner account must be allowed to run Docker. Do not mount a production Docker socket into a shared, untrusted job container. A disposable VM for this runner provides the intended boundary.

The example uses Forgejo's GitHub-compatible workflow context and full action URLs. If the instance cannot fetch actions from GitHub, mirror the checkout action and BananaVibe onto an accessible forge, retain their licenses, and pin the mirrors to reviewed commits. Enable enough runner capacity for a command job to run while a task job is active; two concurrent jobs are the minimum for responsive stop/model commands. Set this deliberately on a self-hosted runner.

## Configuration keys

`.bananavibe.toml` lives in the issue repository's default branch. The
examples in `examples/` are complete; these are what each key does.

| Key | Purpose |
| --- | --- |
| `forge` | `github` or `forgejo` |
| `server_url` | Forge base URL. `https://github.com`, or your Forgejo instance |
| `control_repository` | Where issues, prompts and task state live |
| `target_repository` | Where branches and pull requests are created |
| `base_branch` | Branch a task starts from, usually `main` |
| `state_branch` | Branch holding task state, separate from the work |
| `default_model` | Which `[models.*]` alias a task starts with |
| `open_issues` | Whether opening an issue starts a task by itself, instead of waiting for `/banana start`. Maintainer access is still required |
| `allow_http` | Permit a plain HTTP `server_url`. Off, and only for a local Forgejo |
| `prepare` | Argument arrays run once before the task, to install dependencies |
| `checks` | Argument arrays that must pass before a pull request is opened |
| `[models.<alias>]` | `provider`, `endpoint`, `model` and `api_key_env` for one allowed model |
| `limits.max_minutes` | Wall clock allowance, default 45 |
| `limits.max_iterations` | Completion loop iterations, default 12 |
| `limits.max_calls` | Gateway model calls per engine, default 1000 |
| `limits.memory_mb` | Container memory ceiling, default 4096 |
| `limits.cpus` | Container CPU allowance, default 2 |

`api_key_env` names an environment variable, never the key itself. The key
comes from an Actions secret of that name.

## Checks, resources, and credentials

Preparation and acceptance commands are argument arrays executed inside the task container. They are maintained in the issue repository's trusted default branch. The default examples install dependencies into `/state/venv` and run the application's tests. Choose checks that establish the requested behavior. Every configured acceptance check and `git diff --check` must pass on a stable source tree before publication.

Run `python3 -m bananavibe --config .bananavibe.toml --check-config` from the BananaVibe checkout to validate configuration without contacting a forge. Install its requirements first. Model aliases are an explicit allowlist; `/banana models` lists them and `/banana model ALIAS` selects one. Adding a provider/key/endpoint requires a configuration change, not an issue command. Supported adapters are OpenAI, OpenAI-compatible, Anthropic, Google, and Azure. Use the endpoint and model ID required by that account.

The default allowance is 45 minutes, 12 completion-loop iterations, and 1,000 gateway model calls per engine. The workflow allows 60 minutes so shutdown and checkpointing have time to finish. Raise the workflow timeout when raising `limits.max_minutes`. Apply provider-side spending limits. Docker limits memory, CPU, process count, and individual file sizes; use a disposable runner disk or filesystem quota for a hard total storage limit. The workspace limit is checked at checkpoints.

Keep workflow jobs concurrent: do not add one canceling concurrency group for all comments on an issue. A stop command must be able to reach the active task. Durable leases prevent two task runners from owning the same issue at once.

The state branch receives commits. On other CI workflows that run on every push, ignore `.bananavibe-state/**` or exclude `bananavibe-state`. BananaWiki and BananaChat's example CI already ignore state-only changes. Avoid deleting the state branch while tasks are active.

## Recovery and removal

For encrypted off-forge copies, see [repository backups](backups.md). Install age on the operator/backup runner. The optional `examples/backup-github.yml` and `examples/backup-forgejo.yml` workflows back up configuration, transcripts, state, and Git checkpoints without starting a model task. Keep the recovery identity offline and give the backup job separate repository credentials.

Use `/banana status`, `/banana stop`, `/banana resume`, and `/banana restart` as described in [task recovery](task-lifecycle.md). The scheduled watchdog marks expired runner leases as interrupted; it does not silently retry a failed task or consume an unlimited budget. GitHub may delay schedules or disable inactive-repository schedules; a later command also reconciles an expired lease.

To remove the integration, stop active tasks, disable/remove the workflow, then revoke its credentials. Keep task branches and state until their PRs and recovery needs have been reviewed. Removing the integration does not merge or delete contributions.
