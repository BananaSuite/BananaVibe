# Mission

Refactor, harden and optimise BananaWiki and BananaChat until both are production ready, then refresh their landing
pages and screenshots to match the finished products.

## Scope

- `BananaWiki/` and `BananaChat/` (the applications).
- The landing pages of BananaWiki and BananaChat (<path or repository of each landing site>).

## Priorities

1. Correctness and security: bugs, crashes, data loss, auth and permission holes, injection, unsafe defaults,
   secrets in code.
2. Reliability: clean builds from a fresh checkout, a meaningful automated test suite that passes, no flaky tests,
   migrations that work on existing data.
3. Refactoring: remove dead code and duplication, simplify confusing modules, consistent structure and naming,
   where it makes the code easier to maintain. No rewrites for their own sake.
4. Performance: measure first (slow pages, heavy queries, large bundles), fix what matters, record numbers in
   NOTES.md.
5. Operability and docs: configuration, logging, health checks, backups, a README and deployment guide that match
   reality.
6. Last, when 1–5 are done: update both landing pages so features, wording and screenshots match the current
   products. Regenerate every screenshot from the real running apps (seeded with realistic demo data, consistent
   window size, light theme unless the page uses dark) with a scripted, repeatable tool (e.g. Playwright), and
   commit the script so screenshots can be regenerated later.

## Definition of done

- Both apps build, start and pass all their tests from a clean checkout following their READMEs; all `[[checks]]`
  pass.
- The main flows of each app were exercised for real in a browser (sign-up/login, the core features, admin pages).
- No known bugs, TODOs or stubs remain in touched code; each fix has a regression test.
- Landing pages are accurate, build cleanly, and use freshly generated screenshots.
- NOTES.md summarises what changed, decisions taken, and anything maintainers must do (new env vars, migrations).

## Constraints

- Keep existing data, URLs and public APIs compatible; any unavoidable breaking change comes with a migration and a
  note.
- Keep the visual identity of the apps and landing pages; improve, don't redesign.
- Don't add heavy dependencies without a strong reason (record it).
- Don't push, publish or deploy: the maintainers review the `bananavibe/work` branches and ship.
