# Security reports

Report vulnerabilities privately through the repository's security advisory reporting feature when available. Otherwise contact the owner, [Luca Zani (OverloadedTech)](https://github.com/OverloadedTech), to arrange a private report. Do not publish credentials or private issue contents in a public issue.

Include the affected revision, forge and runner versions, reproduction steps using dummy credentials, and the boundary crossed. Authorization bypasses, leaked prompts or tokens, container escape paths, and publication without successful checks are especially relevant.

Until a fix is deployed, disable the BananaVibe workflow, stop its dedicated runner, and revoke affected credentials. Preserve private state and logs for investigation. Read the [security model](docs/security-model.md) before enabling a workflow.
