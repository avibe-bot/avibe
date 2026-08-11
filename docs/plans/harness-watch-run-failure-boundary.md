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
transaction. Every election phase uses the same metadata-writability predicate
as notice persistence, so a malformed legacy row cannot be named as an owner
that is unable to record the notice.

Persistence caused only by `suppress_delivery` is local history, not delivery
evidence. It is tagged as suppressed, rejected by outward receipt lookup, and
promoted under its stable native identity only after a later user-visible send
succeeds. Promotion rebinds the row to that visible target Session. Its local
Message row is also stamped with the visible delivery time, so transcript order
reflects when the user could first see it rather than its earlier hidden creation.
The row id is not written into the Run's transport receipts. It leaves the Turn
notification unacknowledged so the durable fallback can route the failure to a
user-visible target.

A callback delivers the terminal result for its whole Turn. One effective callback
receipt suppresses every linked Run fallback, while one pending callback defers the
Turn fallback until its delivery outcome is known. The aggregate is read from one
SQLite snapshot and includes participants whose Turn attribution is still held in
a deferred terminal intent. Membership comes from the notice's bounded participant
snapshot plus the indexed Delivery-to-Turn ownership relation, not a Run-history
scan. Canceled and cancel-requested parents are excluded only before a callback is
armed; an accepted callback child remains delivery evidence after parent cancellation.

After the shared liveness monitor terminalizes a Turn whose Codex app-server is
definitively dead, the next request may retire that exact dead generation despite
its stale process-local Turn fence. Unknown ownership and active Activities still
block replacement because their native effects can outlive the transport; Delivery,
Turn, and Run rows do not make a dead process reusable.

Terminal output replay is idempotent for status and content but monotonic for
delivery evidence. A later same-Turn notification acknowledgement upgrades the
existing owed notice without resetting its attempts, state, or fallback owner, so
the durable drain cannot repeat a primary notification that succeeded on retry.
The acknowledgement policy follows the routed target platform, not the originating
Session platform, when a delivery override redirects the notification.

Fallback ownership uses the complete notice-writability predicate: malformed
metadata, an owned escalation, and a callback child whose parent already owns the
notice are all ineligible. Eligibility is projected in terminal-write order before
the stable owner tie-breaker, so a parent that acquires its notice in the same batch
removes the callback child that notice suppresses. Callback child insertion and the
parent's armed marker commit in one SQLite transaction. The transaction rejects a
new failure callback after a settled parent is cancel-requested, but still repairs
the armed marker for a child accepted before cancellation. A Run whose terminal
outcome is `canceled` still reports that outcome to its delegator because it never
owns a failure fallback.

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
- `HFR-442`: a visible direct error with bare Turn provenance does not bypass
  definition-level failure-notice suppression.
- `HFR-443`: a deferred participant's pending callback remains part of the Turn
  callback aggregate and blocks an immediate sibling fallback.
- `HFR-444`: a Watch in its first in-flight waiter cycle reports unknown waiter
  health until an outcome exists.
- `HFR-445`: promoting suppressed history rebinds the stable row to the visible
  target Session after delivery.
- `HFR-446`: promotion stamps the visible delivery time so hidden history cannot
  appear before newer target-Session content.
- `HFR-447`: fallback ownership requires metadata that can persist the notice in
  both immediate and deferred settlement.
- `HFR-448`: a cancel-requested deferred callback participant no longer blocks
  the remaining Turn fallback.
- `HFR-449`: cancellation preserves pending and delivered callback evidence once
  the callback child has been accepted.
- `HFR-450`: a same-Turn terminal replay merges newer delivery evidence without
  resetting notice progress.
- `HFR-451`: Turn callback aggregation uses bounded participant ownership and
  indexed Delivery membership instead of scanning Run JSON.
- `HFR-452`: callback children suppressed by their parent's notice cannot become
  the Turn fallback owner.
- `HFR-453`: callback child enqueue and parent arming commit atomically.
- `HFR-454`: transport-only acknowledgement follows the routed target platform.
- `HFR-455`: Turn fallback election excludes callback children suppressed by a
  parent notice created in the same batch.
- `HFR-456`: late-canceled failed parents reject new callbacks while preserving
  accepted children.
- `HFR-457`: stale Turn participant ids are excluded before ownership projection
  and cannot roll back settlement for valid Runs.
- `HFR-458`: a callback accepted into an active Turn can prove its persisted
  receipt through the shared Turn output even when another Run owns the Message.
- `HFR-459`: independent Activity completions reserve the SQLite writer before
  reading deferred participants and electing their shared Turn fallback owner.
- `HFR-472`: an empty failed Harness Turn synthesizes the existing Turn failure
  contract before its immutable snapshot, even before the first participant is
  attached, so every accepted Run shares one fallback owner instead of emitting
  one notice per Run. A primary error already delivered through the shared
  backend-failure path, the message-handler exception path, or auth recovery
  carries its acknowledgement into the same monotonic contract. All visible
  paths resolve the Harness delivery override before sending or persisting, so
  acknowledgement can never be attributed to a different target.
- `HFR-473`: a definitively dead Codex transport can be replaced for the next
  request even while stale Turn ownership from that dead generation remains;
  unknown ownership and active Activities continue to fail closed.
- `HFR-474`: result-less settlement carries the durable Turn failure contract
  into every accepted Run. During an old-to-new upgrade, startup repairs legacy
  per-Run restart notices from the exact accepted `Run -> Delivery -> Turn`
  relation before activating the notice lane. It never groups by Session or
  timestamp. If one legacy notice was already sent, that delivery evidence owns
  the Turn and all pending siblings stand down; otherwise one stable participant
  becomes the only fallback.

Residual manual check: trigger two one-shot Watches into one failing Turn and
confirm that the conversation contains one backend error, both Runs are failed,
both Watches retire normally, and the UI separates waiter health from event
processing health.
