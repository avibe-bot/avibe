# Cancellation Completion Evidence in Internal API Tests

## Scope and Decision

Change only the two cancellation scenarios in `tests/test_internal_server.py`
and this plan. The owner approved this correctness repair, followed by a
separate review of Model Hub authority scan boundaries and repeated parsing.
Further dedicated CI optimization stops after those two items. Historical
migrations, locking tests, installation isolation, UI coverage, runner counts,
and shard allocation remain unchanged.

At base `47300bfb8`, the durable cancellation path already distinguishes an
accepted Stop receipt from terminal evidence. No production cancellation bug
was established, so no production change is warranted.

## Observed False Positives

A temporary probe around the original tests recorded the state immediately
before `asyncio.run` began loop cleanup:

- The queued-message test received a successful Stop receipt at 0.108 seconds.
  Its synthetic work returned naturally at 5.108 seconds, but the streaming
  dispatch was still alive at 6.680 seconds. Loop cleanup cancelled that
  dispatch, which then resumed the queued message. The test passed.
- The scheduled test received its receipt at 0.087 seconds. Its actual dispatch
  remained alive at 4.462 seconds, even after cancelling the separate submission
  task. Loop cleanup cancelled the dispatch at 4.463 seconds. The test passed.

Thus both tests could pass without proving completion before teardown. These
local observations establish a test-oracle defect, not a hosted-runner cause or
a production cancellation failure.

## Completion Contract

The synthetic backend exposes separate Stop-received and terminal-ready events.
Only the original cancellation route may request Stop. Before releasing terminal
evidence, each test verifies the exact stopped context, successful receipt, and
continued ownership of the live task; queued work must not have started yet.

The human test releases the real streaming sink through
`Controller.mark_turn_complete` with the existing `stopped` settlement. The
scheduled dispatch double returns that same settlement. The tests then join the
actual dispatch tasks, verify durable `canceled` outcomes and released ownership,
and verify queued work has finished, all before cleanup can cancel any task.
Submission admission is not treated as dispatch completion.

Event/task waits are bounded at three seconds, matching the existing startup
wait convention. Shielding the dispatch prevents timeout cancellation from
creating the evidence under test. `finally` cancels and joins remaining tasks
only for cleanup. No sleep, polling retry, fake clock, production timeout,
workflow timeout, test-selection, or dependency change is introduced.

## Evidence

- All 175 original top-level definitions and 844 assertions remain; only the two
  named test bodies change. Every unrelated definition is AST-identical.
- Both focused tests pass, followed by all 176 internal-server cases in 12.01
  seconds and 109 dispatch, stream-sink, priority-cutover, and workflow cases.
- A diagnostic mutation suppressing terminal-ready delivery makes both tests
  fail at the shielded completion wait. Cleanup still joins the pending work.
  Suppressing the Stop-received signal also makes both fail before that wait.
- Ruff 0.4.9, dependency compatibility, and whitespace validation pass.
- The temporary probes are not committed. Local timing is not a CI speedup
  claim; the main benefit is removing false-positive completion evidence.

## Delivery Gates and Residuals

Require a current-head Codex PASS, complete review/comment/thread inventory,
zero unresolved whole-PR threads, all 17 expected checks, every exact-head lint
run successful, and clean scope/integration before guarded exact-head squash.
Then verify the exact merged source with its own master Actions run, including
the complete unit selection and real distribution/installation/Windows gates.
The new model-catalog browser checks from base PR #1919 remain in the UI job.
The current findings-bearing review-head count is zero before PR creation.

No real backend or production service is invoked locally. Backend stop transport
behavior remains covered by its existing tests; these scenarios validate the
internal API's receipt-to-completion and queue-resumption contracts.
