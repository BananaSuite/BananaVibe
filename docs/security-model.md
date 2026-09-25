# Security boundaries

The trusted controller reads configuration only from the issue repository's checked-out default branch. It accepts commands only from maintainers with write access to both the issue and target repositories. Forge credentials remain in the controller's environment and its Git child processes. Git metadata, credential helpers, and the Docker socket are outside the coding workspace. The model cannot merge a PR or issue forge API requests with the bot token.

OpenCode runs as an unprivileged user in a disposable Docker container with a read-only root filesystem, dropped capabilities, no privilege escalation, resource limits, and only the project and temporary state mounted. A private network and an authenticated gateway separate the task from the host and internet. The gateway supplies a scoped, expiring model capability; the real provider key is mounted only in the gateway. Its separate control secret protects the engine's control API. Project OpenCode configuration and share links are disabled.

The task's network proxy rejects private, loopback, link-local, and metadata destinations. Provider endpoints are trusted operator configuration. Dependencies and source code are untrusted workload inputs; they execute inside this same task boundary. Containers share a kernel with the runner, so use a dedicated disposable VM and keep Docker and the host patched. A Docker limit on individual files is not a total filesystem quota.

The controller rejects changes to workflow/configuration paths by default, unsafe workspace entries, excessive checkpoint size, and Git metadata writes. Do not remove protected paths to make a task easier; review privileged changes directly. It checkpoints with constant commit messages and independently executes the configured acceptance checks. Human branch-protection and CI policies remain necessary.

Task result and public-summary files are read through directory descriptors without following links, with regular-file and size limits. Clearing a previous result also stays within the task directory; a linked report directory cannot redirect these operations into controller files.

For private prompts, keep the issue repository, its Actions history, and its state branch private. Status comments and requests for help are posted there. Only code changes enter the target branch. The public PR summary is generated in a separate workspace/session that receives the already-pushed diff without the issue conversation. Provider requests still contain the task and source needed to implement it; choose a provider whose data handling meets your requirements.

Generated source can accidentally repeat information from a prompt. Review task branches and proposed changes for disclosure. Do not include secrets in a prompt. Private Git access for the target and private prompt storage are separate settings; one does not imply the other.

Task state uses content-version checks and expiring leases. External changes to the branch block final publication. Abrupt runner failure leaves a last-known checkpoint and an interrupted state, not a successful completion. Branches and PRs are never automatically merged or deleted.

See [installation](deployment.md) for token scopes, runner setup, resource budgets, and removal. Report problems using [SECURITY.md](../SECURITY.md).
