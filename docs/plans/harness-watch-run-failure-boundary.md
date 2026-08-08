# Harness Watch and Run Failure Boundary

## Background

A Watch waiter and the Agent Run created from a detected event answer different
questions:

- the waiter reports whether observation succeeded;
- the Agent Run reports whether Avibe processed the observed event successfully.

The Run must retain the Watch id as provenance, but provenance does not imply a
shared lifecycle. A successful one-shot waiter may retire normally while its
downstream Agent Turn fails.

One Agent Turn can also accept Runs from several Watch or Task definitions. A
single backend failure currently settles each linked Run and lets every Run own
an independent failure notice. That converts one user-visible failure into
several misleading Harness failures.

## Contract

### Notification ownership

The terminal Agent Turn owns the user-visible execution failure. The backend's
ordinary failure notification is the primary notification and is emitted once
per Turn, including Turns entered through Harness Runs.

Every linked Run still settles independently for audit and retry policy. Its
owed failure notice is a fallback only:

1. a persisted or externally delivered Turn notification suppresses all linked
   Run notices;
2. if the Turn notification was not acknowledged, exactly one linked Run that
   has actually settled failed and owes a notice may deliver the fallback;
3. the remaining linked Runs record that the same Turn was already reported;
4. if an owned Activity delays Run settlement, the Turn notification evidence
   is committed with the deferred terminal intent and survives until settlement.

Fallback election and every linked Run output write share one SQLite write
transaction. The transaction reserves the writer before reading participants,
so a concurrent cancellation either precedes the election and is excluded or
follows the complete settlement. A Run whose Activity defers settlement cannot
own notices that non-deferred siblings owe immediately. If every participant is
deferred, the first Run that actually settles failed elects itself and propagates
that stable owner to the remaining deferred participants in the same locked
transaction.

Persistence caused only by `suppress_delivery` is local history, not delivery
evidence. It is tagged as suppressed, rejected by outward receipt lookup, and
promoted under its stable native identity only after a later user-visible send
succeeds. Promotion rebinds the row to that visible target Session. Its local
Message row id is not written into the Run's transport receipts. It leaves the
Turn notification unacknowledged so the durable fallback can route the failure
to a user-visible target.

A callback delivers the terminal result for its whole Turn. One effective callback
receipt suppresses every linked Run fallback, while one pending callback defers the
Turn fallback until its delivery outcome is known. The aggregate is read from one
SQLite snapshot and includes participants whose Turn attribution is still held in
a deferred terminal intent.

Waiter health is observed only from completed outcome fields. Starting the first
cycle does not prove success, so a Watch with no prior exit, finish, or error remains
unknown while that waiter is in flight.

Turn execution provenance alone does not enter this ownership lane. A terminal
output must carry the explicit `turn_failure_notification` contract; direct error
results without it remain under the existing definition-level notice policy.

The Turn id is the failure identity. Definition ids remain provenance and do
not create additional user-visible failures.

### Health ownership

For Watch definitions, `health` describes only the waiter. Downstream Agent Run
history is exposed separately as `processing_health` with independent counters.
Scheduled Tasks continue to use their existing execution health projection.
An unobserved waiter remains `unknown`; absent downstream history is `healthy`.
An `unknown` downstream verdict means the health projection itself could not be
read and remains visible without a count instead of being presented as healthy.

This is an additive projection change and requires no schema migration. Existing
Run-to-definition and Run-to-Turn links remain intact.

### User-facing copy

A successful Watch followed by a failed Agent Run is described as event
processing failure. It is not described as a Watch failure, and normal one-shot
retirement is not presented as a consequence of the Agent failure.
If the waiter itself failed, the fallback says that failure reporting failed and
does not claim that an event was detected.

## Acceptance

- `HFR-436`: Watch health remains healthy after a successful waiter while its
  event processing health becomes failing. Retry exit codes are healthy only
  while a `forever` Watch remains enabled and can actually retry; the same retry
  outcome remains failing when a lifetime, binding, or operator stop makes it
  terminal. An unreadable processing verdict remains visible as `unknown` in
  both the Watch list and its detail pane.
- `HFR-437`: one acknowledged Turn failure suppresses every linked Run notice.
- `HFR-438`: a missing Turn notification produces exactly one fallback across
  all linked Runs. Canceled Runs are excluded from ownership, and Runs accepted
  after terminal settlement reuse the owner elected from all durable Turn
  participants. Election and participant writes use one locked snapshot. A
  deferred participant becomes eligible only when it actually settles failed,
  so cancellation cannot invalidate an owner that sibling notices depend on.
- `HFR-439`: terminal Turn notification evidence reaches every linked Run in
  the same failed transition, or in the deferred terminal intent when an
  Activity still owns the Run. The immutable terminal Turn snapshot supplies
  the initial election; the locked participant settlement validates it against
  current cancellation and deferral state before writing every Run. Legacy
  Harness contexts without a Turn token still honor explicit delivery evidence.
- `HFR-440`: one effective callback receipt on any linked Run suppresses the
  complete Turn fallback; pending callback delivery defers it.
- `HFR-441`: persistence under `suppress_delivery` cannot satisfy or shadow a
  later outward receipt, including in a foreground callback target Session.
- `HFR-442`: bare Turn provenance without a notification contract does not
  bypass definition-level failure-notice suppression.
- `HFR-443`: a deferred participant's pending callback remains part of the Turn
  callback aggregate and blocks an immediate sibling fallback.
- `HFR-444`: a Watch in its first in-flight waiter cycle reports unknown waiter
  health until an outcome exists.
- `HFR-445`: promoting suppressed history rebinds the stable row to the visible
  target Session after delivery.

Residual manual check: trigger two one-shot Watches into one failing Turn and
confirm that the conversation contains one backend error, both Runs are failed,
both Watches retire normally, and the UI separates waiter health from event
processing health.
