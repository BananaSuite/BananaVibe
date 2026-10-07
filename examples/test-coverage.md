# Mission

Raise confidence in <project> by testing what matters: line coverage of the core packages to at least 85%, with
tests that would catch real regressions (behaviour and edge cases, not implementation details).

## Definition of done

- `<coverage command>` reports ≥ 85% for `<core paths>`; the check enforces it.
- Every bug the new tests uncover is fixed, with the test kept as a regression test.
- No test is skipped, marked flaky or made to pass by weakening assertions.
- The suite stays under <N> minutes.
