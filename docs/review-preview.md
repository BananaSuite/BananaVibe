# What was wrong with the preview release

Version 1.6 replaces the preview release (commit `7dde06e`). Before rewriting it we read all of the preview and ran it under test. This page records what we found, so you can judge the rewrite and decide how soon to upgrade. The tests in `tests/` reproduce each point.

## Security

The most serious problem was that the preview re-read the issue title and body at the start of every run. After a maintainer typed `/banana start`, the issue's author could change the text, and the agent would follow the new version with the maintainers' model budget and internet access. That is realistic when the issue repository is public, as in the BananaWiki example. In 1.6, starting, resuming or answering records a fingerprint of the approved text, and an edit pauses the task until a maintainer approves it again.

Acceptance checks ran inside the agent's own container, against a `/state` directory the agent could write to. An agent could make a check pass without any trace in the diff, for example by replacing the Python in its virtualenv. Checks now run in a fresh container, on a clean export of the commit, with a fresh `/state`.

Model-written PR descriptions were published as-is. A description saying "Fixes #12" would close an unrelated public issue when merged, and `@name` would ping people. Both are now neutralized.

Smaller points:

- The sandbox could reach any public host. `[network] allow` now restricts that; the default still allows everything, as before.
- Local composite actions (`.github/actions/`, `.forgejo/actions/`) weren't protected, even though workflows run them with their secrets.
- The gateway's unauthenticated proxy sat on Docker's shared bridge, where any other container on the host could use it. Each task now gets its own network.
- Every run pip-installed an unpinned `requests` into the process holding the forge token and model keys. 1.6 has no dependencies.
- The Docker CLI inherited every secret in the environment. It now gets only what it needs.

## Reliability

Three bugs could lose work or instructions:

- The engine downloaded the whole OpenCode transcript on every poll, capped at 8 MiB, so long sessions failed with "The coding engine response is too large". It now asks only for the latest messages.
- A command recorded while a runner was finishing, such as an answer sent just as the agent asked a question, was overwritten when the runner saved its result. The guidance was silently dropped. Each command now bumps a counter that a finishing runner checks.
- When the model provider failed, or a preparation command did, everything since the last checkpoint was thrown away. The workspace is now saved on every way out.

The heartbeat thread and the main thread shared the task record without a lock, so a stale read could overwrite a fresh checkpoint in memory and cause spurious "branch changed" failures. If the engine went idle without replying, the runner waited out the whole 45 minutes, and a hanging test did the same. Touching a protected file failed the entire task instead of undoing that change and telling the agent. Test caches missing from a project's `.gitignore` ended up in PRs, or made checks loop until the iteration budget ran out.

Some everyday annoyances are fixed too. `/banana answer` with its text on the next line was ignored. A finished task couldn't be revised, so review feedback meant a brand-new branch and PR. Private repositories on plans without draft PRs couldn't publish at all. A stop was sometimes reported as a failure. Persistent runners kept using an old sandbox image after an upgrade unless someone renamed its tag by hand.

## Cost

The preview created its state branch from the default branch, so every state write triggered the repository's push workflows. With issues and code in the same repository, as in the BananaWiki example, that meant a full CI run for every heartbeat. New state branches contain only task records, and `bananavibe migrate-state` converts an existing one.

The example watchdog ran every 10 minutes and read each task record with its own API call. It started 144 runners a day, approached API rate limits after a few hundred tasks, and broke at 1,000. It now runs hourly and reads everything in one Git fetch.

Progress was reported as a new issue comment every two minutes; now one comment is edited in place. The PR description needed a second full sandbox; now it takes one request. Provider errors were replaced with a generic message; they now come through with the key removed. On GitHub, any issue opened by anyone started a runner; the example workflow now filters first.

## Housekeeping

A `.DS_Store` file was committed. The lint configuration referred to directories that don't exist. There was no package metadata, and the version lived in two places. The examples pinned `@main` while telling readers to pin a release. Misspelled configuration keys were silently ignored; they now produce warnings. A task waiting for a person made the workflow run fail.

## What 1.6 deliberately leaves alone

`banana_backup/` is unchanged because it is shared, byte for byte, with BananaWiki and BananaChat, and its encrypted format has to stay readable. Only BananaVibe's use of it was rewritten, with the same archive format.

Some limits remain. Task branches live in the target repository, so its CI runs the agent's code with whatever secrets such branches receive. An agent can still weaken a test openly in its diff, which only review catches. Containers share the runner's kernel. The [security model](security-model.md) covers what to do about each.
