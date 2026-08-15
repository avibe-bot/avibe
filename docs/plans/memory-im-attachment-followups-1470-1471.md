# Memory IM attachment follow-ups (#1470 / #1471)

## Fixed contracts

Ingress attachment eligibility is the conjunction of:

1. the platform belongs to `IM_ATTACHMENT_CAPTURE_PLATFORMS`;
2. an explicit multimodal configuration is complete; and
3. its configuration generation is a valid non-negative integer.

Platform rollout changes only the first term. The #1470 ingress fix changes only
the latter two terms by removing live provider health from eligibility. Workbench
keeps its one-cycle implicit provider compatibility outside this IM call site.

The call-site invariants remain:

- **Bounded-wait invariant.** No deadline-bound provider or subprocess read may
  occur in a span that blocks any bounded waiter.
- **Text independence.** Eligible text remains independently best effort, and
  that invariant holds at every attachment-path failure or early return.
- Facts sampled before an indeterminate await are revalidated in the O(1),
  no-`await` segment that consumes them; mismatch fails closed.

## PR-D: local IM eligibility (#1470 item 1)

**Selected model.** For IM ingress, a valid explicit configuration generation is
the complete local eligibility proof. Project `ready` without calling
`attachment_capture_status()`, then keep the existing replacement-gate generation
comparison immediately before capture. Provider failure remains downstream and is
absorbed by the existing fail-closed queue/provider outcomes.

**Rejected models.** Moving the health read before or after ticket acquisition is
a position rule: one placement blocks Agent dispatch and the other blocks `/new`.
A cached health value is also rejected as an eligibility input because freshness
cannot be proved without coupling admission correctness to refresh timing.

**Blast radius.** `core/controller.py`, the authoritative generation normalizer in
`core/memory/admission.py`, and focused controller tests only. No persistent shape
changes. This PR does not touch platform allowlists, Workbench readiness, or
Processing Record.

Workbench does not enter the bounded span changed here. The outer
`admits_attachment_turn()` check requires membership in the IM platform allowlist,
while Workbench attachments are converted directly by `CaptureAdmission.decide()`.
It therefore never awaits `attachment_capture_status()` while holding this IM
exact-session ticket, and its `/new` lifecycle budget is not exposed to the probe
removed by PR-D. The one-cycle implicit provider compatibility remains unchanged
in the Workbench/provider path.

**Tests.** A permanently blocked health stub is never called and an eligible
attachment reaches the request; `/new` completes inside its lifecycle budget while
that stub remains blocked; a generation change after selection strips attachments
but preserves caption text; and downstream unavailability does not change ingress
eligibility. Scenarios: `MEMORY-IM-ATTACH-001`, `MEMORY-IM-ATTACH-003`.

## PR-E: lock-free processing action execution (#1470 item 2)

**Selected model.** Producers atomically persist the existing durable processing
action and do nothing else. The existing `MemoryWorker` pass is the single driver:
each active pass drains processing actions as an independent, unconditional step,
before any session-due lookup or early return based on session work. Deliver,
session flush, boot recovery, and future producers never call the runner. Failed
actions retain the existing retry deadline; the worker's one-second tick bounds
classification latency, which is not a prerequisite for user-path correctness.
Generation and `occurred_at` compare-and-set checks continue to reject stale probe
results.

**Rejected models.** Having every producer call a shared drain entry is a
coordination rule that a new producer can omit. Commit-triggered scheduling avoids
that omission but introduces task/lifecycle wake-up machinery when the existing
single periodic owner already supplies a one-second bound. Leaving the probe inside
the exact-session delivery lock violates the bounded-wait invariant.

**Blast radius.** `core/memory/worker.py`, `core/memory/coordinator.py`, and focused
tests. It reuses the existing durable action representation and does not add a
table, component, or migration.

**Tests.** With zero due sessions and one durable processing action, one worker
tick executes the action. A blocked probe never holds an exact-session delivery
lock or delays `/new`; retry deadlines suppress only retry attempts, never the
initial durable action; stale probe completion remains fail-closed.

## PR-A: materializer-wide failure preserves caption (#1471 item 1)

**Selected model.** After PR5 merges, keep the shared materializer's Agent-facing
failure semantics unchanged. At the message-handler boundary, a batch-wide
materializer exception schedules the already-authorized caption through the
text-only Memory path, then preserves the original Agent-path error behavior. The
text-only handoff uses the canonical session anchor and the existing best-effort
capture ownership rules.

**Rejected models.** Converting every materializer exception into per-file failures
would hide infrastructure defects from ordinary Agent attachment handling. Moving
all Memory capture ahead of materialization is a position rule and would duplicate
capture when materialization later succeeds.

**Blast radius.** `core/handlers/inbound_attachments.py`,
`core/handlers/message_handler.py`, and focused tests. These files overlap PR5, so
this work starts only after PR5 merges and rebases; it never runs in parallel with
PR5. As part of that already-owned edit, PR-A replaces the existing inline
non-negative-generation predicate in `message_handler.py` with
`normalize_attachment_config_generation()`; PR-D deliberately does not widen into
that file. No persistent shape changes.

**Tests.** Root initialization, lease initialization, and gather-level failures
each preserve exactly one caption capture while the Agent path retains its existing
error/result. Cancellation is not converted into degradation, and attachment-only
turns do not create empty captures.

## PR-B: atomic claimed-row text downgrade (#1471 item 2)

**Selected model.** On deterministic bundle decode or revalidation failure before
the provider is called, use one existing-store transaction to change the claimed
row to text-only, clear its bundle reference, return the bundle release id, and make
the same row retryable. The transaction is conditional on the current lease owner,
a non-empty caption, and a still-present bundle. It does not increment attempts;
after downgrade the missing bundle makes a second downgrade impossible. The
coordinator releases the returned bundle outside the transaction.

**Rejected models.** Settling the row and enqueuing a replacement creates a crash
gap and can break ordering/deduplication. Creating a second row duplicates identity.
Keeping text while terminally settling the original row never retries it.

**Blast radius.** `core/memory/store.py`, `core/memory/coordinator.py`, attachment
release handling, and focused transaction/coordinator tests. The default design
changes no persisted schema. If an in-row downgrade cannot be expressed without a
shape change, implementation stops for a persisted-shape decision and legacy-load
fixtures before any code is pushed.

**Tests.** Decode and descriptor revalidation failures atomically retain caption,
release the bundle once, preserve row identity/order and attempt count, and deliver
text on the next claim. Cancellation/crash boundaries leave either the original
claimed row or the committed downgraded row, never a caption-less intermediate.

## PR-C: deterministic provider rejection downgrade (#1471 item 3)

**Selected model.** Reuse PR-B's row downgrade only when the provider returns the
closed capability codes `UNSUPPORTED_FORMAT` or `CAPABILITY_UNAVAILABLE`. Those
results prove no attachment write was accepted, so the row may safely retry as
text-only. Timeouts, transport failures, unknown errors, and every other 4xx remain
ambiguous and terminal/retry according to existing behavior; they are never
replayed as text-only because the remote write may already exist.

**Rejected models.** Treating every 4xx as deterministic guesses provider semantics.
Routing all provider exceptions through one degradation path can duplicate memory
after an ambiguous success. Adapter-side immediate text resubmission bypasses the
durable row's ordering and idempotency.

**Blast radius.** `core/memory/everos.py`, `core/memory/coordinator.py`, the PR-B
store operation, and focused provider/coordinator tests. No new table, component,
or migration.

**Tests.** Parameterize the two allowed capability codes and the full rejected
boundary. Allowed codes produce one attachment call followed by the durable
text-only retry. Timeout, transport error, unknown error, and all other 4xx keep a
single provider invocation and never enqueue or invoke a text-only replay.

## Delivery order and lane boundaries

1. PR-D ships first from `063aebd2`, in parallel with PR5.
2. PR-E follows PR-D in this lane; it is not bundled with the cheapest fix or run
   concurrently in this lane.
3. PR-A starts immediately after PR5 merges and is the first #1471 implementation.
4. PR-B lands alone because it owns the claimed-row transaction model.
5. PR-C follows PR-B and reuses only its proven deterministic downgrade operation.

PR5 exclusively owns the four remaining IM adapters plus
`core/memory/attachments.py` and `core/memory/im_attachments.py`. PR-D exclusively
owns `core/controller.py`. Any change to the frozen eligibility conjunction routes
through the orchestrator rather than lane-to-lane coordination. Five-platform
Incus acceptance remains incomplete until PR-A closes the multiplied caption-loss
exposure after PR5.
