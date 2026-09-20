# Model Picker CI Readiness

## Cause and Scope

Master `616aa5df5` failed UI job `101333936578` in lint run `33976469074`:
the picker title was present while the model request was still loading, but the
test immediately queried a group that only exists after that request completes.
The loading DOM contained a disabled search field and no candidate groups.

The same test file had three instances of this class: waiting for the static
title, an unchanged confirmation label, or a search field that exists while
disabled. They originated in #1839 (`77c1fd8a4`), not the unrelated #1890 change
whose master run exposed the race.

The initial scope was the picker test and this record. During full local UI
validation, the unchanged catalog test also failed at its title-to-row wait.
Its loading DOM showed the same class: title present, no rows, disabled inputs.
The orchestrator inspected the catalog consumer and all its asynchronous read
boundaries before extending scope to `BackendModelCatalogDialog.test.tsx`.
Product behavior, workflow gates, timeouts, dependencies, and test selection
remain unchanged.

A controlled 250 ms delay on the catalog and candidate reads reproduced twelve
catalog failures: one title-to-row assertion, nine clicks on the still-disabled
Add models button, and two attempts to type into still-disabled search inputs.
The complete class is addressed with a local enabled-button query and explicit
row/input readiness waits, not test reruns or implicit synchronization in render.
Error and legacy-read tests retain their own readiness boundaries.

## Invariant

Assertions and interactions that consume candidate data must wait for its
observable readiness, not for the dialog shell. The group test awaits its real
group, the selection test awaits its candidate, and the search test awaits an
enabled input. All original assertions remain.

A deferred-response regression holds the real component in loading state,
checks that the title and confirmation label already exist while search is
disabled, then completes the request explicitly. It verifies the actual group
and enabled search after completion. It uses no sleeps, retries, or fake timers.

## Evidence

- Before editing, the original eight tests passed with immediate responses.
- An isolated in-memory mock with a 250 ms response delay reproduced exactly
  the three premature waits; the group failure matched the archived CI failure.
- Under that same controlled delay, readiness waits made all eight pass.
- The final picker suite passed all nine cases normally and with that delay.
- The catalog suite passed all 35 cases with controlled delayed reads, after
  the original source failed twelve of its 34 cases under identical stimulus.
- Both focused suites passed 44 cases. Full UI validation passed all 287 files
  and 3816 cases; test typecheck, theme validation, and production build passed.
- An AST assertion inventory retained all 207 original assertions across 39
  test definitions (42 expanded cases). The locked-row query alone changes from
  synchronous `getByText` to awaited `findByText`; no expected value changes.
- A lint run concurrent with full UI tests observed the lint-integrity test's
  deliberately invalid temporary source. That test removes it in `finally`.
  Lint must run after that suite, as CI already does, not concurrently with it.
- Exact-head review and CI results are recorded in the PR.

The controlled delay is diagnostic stimulus, not a production or committed-test
sleep. No model API calls or live user state are used. This fixes test ordering;
it does not claim a production model-loading defect or a CI speedup.
