# CI Model Hub Completion Boundaries

## Scope and Diagnosis

The owner's order is stability, build/install preparation, then repeated work
inside test calls. This PR addresses only the first phase. Source baseline is
`85fe96897248c04bcf3d707b9f5872907597f377`; do not stack the following phases.

Master run 34007676888 at `d802675cf4` failed the exact-request terminal-result
case in `test_out_of_window_request_releases_finalization_ownership`: history
was `failed_terminal`, not `served`. The gateway logged transport-deadline
abandonment. Subsequent master run 34008143484 passed without changing this test
or the gateway, which does not resolve the failure.

The test selected a 20ms transport cleanup budget while asserting successful
completion. A disposable probe delegated to the original close method after
300ms of asynchronous delay and reproduced the exact assertion. An independent
100ms delay before the original request JSON method reproduced premature drain
cancellation in the adjacent body-attribution test, which selected 50ms. The
same close probe reproduced lost metering in an L3 cancellation test selecting
200ms. All three failures are expected consequences of their artificially short
test budgets, not evidence that production should ignore its deadline.

The unmodified retention module passed all 30 cases normally. Under the probe,
the six selected cases produced three failures and three passes. The original
CI's precise scheduling delay is not reconstructible; the controlled failures
establish the test-contract problem without attributing it to runner resources.

## Smallest Complete Change

Remove transport-timeout overrides from the three non-deadline tests, retaining
the real production default. The existing outer one/five-second waits still
bound these tests. Keep every original assertion, input, real gateway method,
ownership event, cancellation and final cleanup. No production code, workflow,
CI timeout, dependency, runner count, test selection, sleeps or retries change.

Whole-class inventory of short gateway budgets found four other cases that
deliberately block parsing, teardown or fallback settlement to test deadline
expiry. Their 20ms budgets and bounded-abandonment assertions remain unchanged.

## Verification and Delivery

Run the original full retention and L3 modules, the six delayed probe cases,
transport/settlement consumers and lint. Compare the original functions after
normalizing only these three constructor keyword removals: business bodies and
assertions must be identical. Actual deadline regressions must still pass.

Require head-bound Codex review, all 17 expected checks and every exact-head
lint run successful, zero unresolved whole-PR threads, and independently clean
scope/integration before guarded squash. Verify the exact merged-source CI
before proceeding to build/install optimization. Initial findings-bearing
review-head inventory is zero. No real model, production installation or service
operation is involved.

## Local Evidence

- The six controlled-delay cases now pass (the same probe originally failed
  three). The probe delegates to original methods and is not committed.
- Separate-process complete suites: retention 30 passed; L3 312 passed and its
  existing AC-50 expected failure remains unchanged.
- Transport lease, usage and workflow consumers: 143 passed.
- AST comparison of both complete modules preserves all 1,047 original
  assertions and all business bodies after normalizing only the three keyword
  removals. Four explicit deadline tests remain unchanged.
- Ruff 0.4.9, all 83 installed package requirements and whitespace checks pass.

Only private local test tooling was installed. Build/install preparation and
test-call cost analysis remain subsequent stages, not claimed outcomes here.
