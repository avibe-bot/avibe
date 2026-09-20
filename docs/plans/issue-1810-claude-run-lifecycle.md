# Issue 1810: Claude Agent Run Lifecycle

## Background

Issue #1810 reports a Claude SDK client teardown during an active turn. The
reported cache-invalidation asymmetry exists, but the incident timeline first
shows a different invariant violation: an Agent Run became terminal while its
native Claude turn and receiver continued producing output. The later
caller-environment invalidation happened after that output completed and does
not establish the teardown as the initiating cause.

The historical session (`sesanmf2yy8nc`) and runs (`d83ef3101a01` and
`ddb91779cbda`) are no longer present in the local Avibe state exposed by the
CLI, so the investigation must rebuild the state transition in test-owned
storage.

## Goal

Find and fix the lifecycle boundaries that can terminalize a P1 Agent Run or
its durable Turn while the accepted Claude steer is still owned by the native
receiver. Preserve one Claude client/receiver generation, prevent synthetic
agent-initiated ownership, and keep the primary result and steered successor
attached to their durable turn.

## Investigation

1. Trace Agent Run creation and its binding to Delivery and durable Turn rows.
2. Trace P1 admission, native input receipt, durable materialization, and the
   exact event that terminalizes the Run.
3. Trace Claude primary-phase retirement, receiver generation, native EOF, and
   agent-initiated fallback ownership.
4. Enumerate cancellation and teardown paths that can leave the native receiver
   live after local ownership is released.
5. Reproduce the earliest invariant violation before changing production code.

## Required Invariants

- An accepted P1 Agent Run remains non-terminal until its owning durable turn
  reaches native terminal evidence.
- The accepted steer does not recreate or disconnect the Claude client or
  receiver generation.
- Pre-steer primary output and post-steer output remain owned by their respective
  durable turns; no synthetic agent-initiated turn is opened.
- Stop, P3 delivery, backend refresh, and service-restart recovery keep their
  existing terminal semantics.
- Every Claude teardown records a reason, busy state, and exact runtime
  generation.

## Causal Model

Two independently reproducible cancellation boundaries could split durable and
native ownership:

1. The internal Session gate commits `agent_runs.delivery_id` before awaiting
   delivery. Claude steering shields the native write so it can reconcile an
   accepted receipt before propagating caller cancellation. The gate caught
   ordinary exceptions after ownership transfer but not `CancelledError`, so
   the canceled executor wrapper could settle the Run before Delivery recovery
   or the native terminal result.
2. Scheduled-service lease loss called the service-local stop path. That path
   canceled executor wrappers and released active durable Turns, but did not
   stop backend clients or receiver tasks. Lease loss is process-wide and was
   already owned by the controller runtime supervisor; the second local owner
   could therefore terminalize the Run/Turn while Claude kept producing.

The caller-environment cache invalidation observed later in the incident is a
consequence after the first result, not the initiating transition. No test
evidence justifies adding an idle wait to cache invalidation, and doing so under
the generation lock could deadlock receiver cleanup.

## Fix

- After a Run has transferred to Delivery, the internal gate converts caller
  cancellation into the existing durable recovery result. Pre-transfer
  cancellation still propagates normally.
- Scheduled-service lease loss stops local admission and asks the controller to
  shut down the complete runtime generation. Explicit service stop retains its
  existing restart settlement behavior. Lightweight controllers without a
  shutdown owner retain the previous local fallback.
- Claude teardown now emits stable reason, busy, runtime generation,
  client/receiver identity, receiver state, and PID evidence. Cache retirement
  timing is otherwise unchanged.

## Delivery

- Add a deterministic failing regression before the fix.
- Implement the smallest complete lifecycle correction at the authoritative
  owner; harden cache retirement only if the reproducer proves it is needed.
- Run focused and adjacent tests plus Ruff, then complete exact-head review and
  CI before handoff.
