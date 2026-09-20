# Bound Claude input waits and fence retired clients

## Problem and evidence

An installed 3.1.0 instance accepted input, reused its cached Claude client, and
then made no native write for about an hour. Stop retired the client; the waiting
input subsequently attempted a write and failed with `Not connected`. The adapter
waits indefinitely for background Activity output before writing. Whole-service
watchdog restarts mask the symptom and can interrupt unrelated work.

## Change contract

- A new Claude input waits at most five minutes for previous background Activity
  output. This bounds admission, not execution of an already accepted task.
- A timeout proves that this input was not written. Persist it for explicit retry
  with a localized explanation; do not automatically resend it.
- Retiring or replacing the exact cached client during the wait prevents the old
  input from writing, including when retirement races with Activity settlement.
- Preserve background Activities, native session mappings, conversation history,
  replacement clients and unrelated sessions. Do not restart or disconnect them.
- Keep normal background-output ordering, cancellation propagation, and runtime
  gate release intact. Local admission failures are not provider/auth failures.

## Validation

Regression tests cover timeout with active background work, client removal and
replacement during a wait, output settlement racing with retirement, ordinary
settlement, and cancellation. Adapter tests check the native-write boundary,
explicit-retry evidence, preserved mappings and released gate. Run existing
Claude session/Activity, delivery-state-machine and AgentService tests plus lint.

## Boundaries

This does not put time limits on native inference or repair all possible backend
hangs. It does not change the locally installed watchdog, deploy a package, or
restart the running avibe service. Production fault stacks were not captured;
the regression addresses the confirmed unbounded wait and stale-client-write
failure class, rather than claiming every historical freeze has one cause.
