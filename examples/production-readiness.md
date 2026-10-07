# Mission

Make every project in this workspace production ready: correct, secure, tested, documented and pleasant to
operate, without changing what the projects are for.

## Scope

- Every repository in the workspace. Start with a full audit of each: how it builds, runs and is tested, and what
  is broken, missing, insecure or fragile.

## Priorities

1. Bugs, crashes, data loss and security problems (injection, auth, secrets in code, unsafe defaults).
2. Builds and tests: a clean checkout builds and passes with the documented commands; meaningful tests for core
   behaviour; flaky tests fixed, not skipped.
3. Operability: configuration via environment, useful logging, health checks, graceful shutdown, clear errors.
4. Code quality: dead code, duplication and confusing structure in the areas you touch; performance problems
   that matter in practice.
5. Documentation: README with setup, configuration, deployment and troubleshooting that match reality.

## Definition of done

- All `[[checks]]` pass, and every project builds and starts from a clean checkout following its README.
- Each project's main user flows were exercised for real (run the app, call the API, click through the UI).
- No known bugs, TODOs or stubs remain in the touched code; every fix has a test.
- NOTES.md lists the decisions taken and anything the maintainers must know or do (migrations, new env vars).

## Constraints

- Keep public APIs, CLI flags, database schemas and file formats backwards compatible. If a breaking change is
  unavoidable, provide a migration and document it.
- Keep the visual design unless it is broken.
- No new runtime dependencies without a strong reason, recorded in NOTES.md.
- Do not push, publish or deploy.
