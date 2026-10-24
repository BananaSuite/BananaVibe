# Writing a good mission

The agents read `GOAL.md` at the start of every session, and the reviewer judges the work against it. It is the
only fixed point in a run that can last days, so it pays to be precise.

**State the outcome, not the steps.** "Every API endpoint validates its input and returns documented errors" beats
"add validation to the handlers". The first session turns the outcome into a plan; reviewers check against the
outcome.

**Define done in checkable terms.** Builds from a clean checkout with documented commands; tests pass; new behaviour
has tests; main flows were exercised for real; docs match behaviour; no TODOs in touched code. Back each item with a
`[[checks]]` command when you can.

**Say what must not change.** Public APIs, database schemas and data formats, look and feel, licences, dependencies
to avoid, files not to touch. Unbounded "optimize everything" runs drift into rewriting things you liked.

**Name priorities.** If time or budget runs out, what matters most? Agents work the plan in priority order.

**Give context agents can't discover.** Deployment targets, expected load, who the users are, known bugs, links to
specs (paste them; agents may not have network access to private docs).

**Keep it stable.** For course corrections during a run use `bananavibe say` (or `say --now`), which reaches the
agents and is recorded. If the mission itself must change, use `bananavibe edit goal`: it applies from the next
session without interrupting the current one.

## Template

```markdown
# Mission

<One paragraph: what should be true when this is finished, and why.>

## Scope

- <project or area>: <what to achieve there>
- ...

## Priorities

1. <most important>
2. ...

## Definition of done

- Builds and starts from a clean checkout using the commands in the README.
- All tests and checks pass; every change is covered by tests.
- <feature-specific acceptance criteria>
- Docs updated.

## Constraints

- Do not change <public API / schema / look and feel / …>.
- Do not add dependencies without a strong reason (record it in NOTES.md).
- Do not push or deploy.
```

See [examples/](../examples/) for complete missions.
