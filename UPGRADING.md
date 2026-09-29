# Upgrading

## From 2.x to 3.0

3.0 is a drop-in replacement. Configuration files, task records, task
branches, the state branch and backup archives are all compatible, and a task
that is running when you upgrade continues under the new version.

### Required: one line

In `.github/workflows/bananavibe.yml` (or `.forgejo/workflows/…`), change the
Action reference:

```yaml
- uses: BananaSuite/BananaVibe@v3.0.0   # or the release commit SHA
```

That's it. The first task after the upgrade builds the new sandbox image
(3–5 minutes on a fresh runner). If your configuration pinned the old default
`image = "bananavibe-sandbox:2.0.0"`, it is ignored with a warning and the
matching image is built instead.

### Recommended

1. Raise the job timeout to `timeout-minutes: 75`, because image builds no longer count against `limits.max_minutes`.
2. Make the watchdog hourly (`cron: '17 * * * *'`) instead of every 10 minutes. It now reads all records in one request.
3. On GitHub, copy the job-level `if:` from [`examples/github.yml`](examples/github.yml) so non-maintainers' issues don't start runners.
4. Convert the state branch to an orphan so state writes stop triggering your push workflows. With no tasks running:

   ```sh
   git clone https://github.com/BananaSuite/BananaVibe && cd BananaVibe && git checkout v3.0.0
   BANANAVIBE_TOKEN=… GITHUB_REPOSITORY=owner/control-repo \
     python3 -m bananavibe migrate-state --config /path/to/.bananavibe.toml
   ```

   It keeps every task record and refuses to run while a lease is live. If the branch is protected against force pushes, allow it once.
5. Restrict egress with `[network] allow = [...]`, listing the package registries your `prepare` step needs. See [configuration](docs/configuration.md#network).
6. Make sure `prepare` works on a clean checkout. Checks now run in a fresh container that repeats `prepare`, so it must install everything the checks need. It usually does already.
7. Backup workflows keep working as they are. The new examples drop the virtualenv step.

### Behavior changes to know about

- Progress is one status comment, edited in place, instead of a new comment every two minutes.
- Editing an issue's title or body after starting it pauses the task (**blocked**) until a maintainer comments `/banana resume`.
- Agent edits to protected paths are reverted and explained to the agent, instead of failing the task. `.github/actions/` and `.forgejo/actions/` are now protected by default. If you set `forbidden_paths` yourself, consider adding them.
- `/banana answer` on a finished task now revises its open PR, where it used to be ignored.
- Blocked and paused tasks no longer fail the workflow job (exit status 0). Only failed tasks do.
- PR descriptions have `@mentions` and `Fixes #N` keywords neutralized.

### Rolling back

Point the workflow back at the 2.x commit. Records written by 3.0 contain a few extra fields, which 2.x ignores. An orphaned state branch also works with 2.x.
