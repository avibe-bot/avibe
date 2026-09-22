# Memory zero-impact delivery

## Background

PR #2023 put a Memory authority comparison in the native steer admission path.
PRs #2088 and #2090 narrowed the unavailable-runtime behavior, but the design
still let an optional Memory subsystem refuse a host-owned message delivery.

## Owner decision

Memory has zero influence on Avibe message delivery. Once host-owned routing and
identity checks pass, steer, P0, P1, and P3 delivery proceeds for every Memory
state. If the input would cross the authority of a readable Memory scope, the
Turn remains delivered and Memory access for that Turn/session is denied.

## Invariant

For every Memory state, a delivery has the same `DeliveryResult.state` as the
same admission with Memory disabled. Memory may deny reads or writes at its
boundary, but it cannot change delivery admission, native steering, or queue
ownership.

## Mechanism

`core/session_turns.py` no longer imports `avibe_memory`, calls
`Controller._memory_admission`, or emits Memory-specific refusal reasons. The
Memory boundary evaluates the active Turn's immutable delivery rows at
consumption time. A host-owned storage helper compares the initial delivery's
authority with executing deliveries; a conflicting authority drops the
session's recorded Memory scope, so existing internal Memory endpoints return
403 while the accepted delivery continues.

## Tests and evidence

- Unit: the steer path reaches the native write with `_memory_admission`
  patched to raise; a parametrized delivery invariant covers disabled, missing,
  incompatible, runtime-raising, conflicting, and matching Memory states across
  P0, P1, and P3.
- Contract: delegated lifecycle cases now assert accepted delivery plus denied
  Memory scope for cross-owner steering; matching authority remains readable.
- Scenario: active-poll recovery and delegated continuation exercise the
  consumption-time scope check through `/internal/memory/search`.
- Residual manual check: verify one accepted cross-owner steer in a running
  service and confirm the Turn's `vibe memory` request receives 403.

The cross-owner/cross-scope delivery-fence assertions from PR #2090 were moved
to the Memory-scope layer; the delivery FSM retains only the delivery-state
invariant and native-write structural assertion.
