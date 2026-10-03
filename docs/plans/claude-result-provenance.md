# Claude result provenance and background input admission

## Problem

Claude Code keeps one streaming SDK connection per runtime. A detached background
Activity can produce Assistant and Result frames while a newer Avibe human Turn is
waiting to write. The SDK's terminal `ResultMessage.origin` is the reliable
owner signal in this interleaved stream; Assistant frames do not carry that field.
The only earlier signal is the replayed input that starts a human turn.

## Contract

- Stamp every Avibe-originated Claude user query with `origin: {kind: human}`.
- Admit a new human query without waiting for detached Activity output.
- Check the exact Claude client generation immediately before the native write.
- Classify terminal results by `origin` before claiming Activity output or popping a
  pending human request. Human results settle the pending human Turn. Task
  notifications and other injected origins remain detached.
- Buffer Assistant and tool frames while an Activity makes ownership ambiguous, then
  replay them only after the terminal Result identifies the phase. Grace-period
  Activity flushes defer while that phase is buffered, and replay failures do not
  prevent terminal settlement.
- Stream a turn whose first content frame is a replayed human input
  (`UserMessage.origin.kind == "human"`) without waiting for its Result, even
  while an Activity competes. Such a turn ends with a human Result (see Measured
  turn shape), so its frames take the human replay path early: they attach to
  the pending human request and never claim Activity output. Any other first
  frame, a human input drained into a running turn, held frames, and detached
  or claimed Activity output that is not yet delivered keep the Result-owned
  path; a later frame never overtakes earlier held output. A generic receiver
  error keeps the client, so the receiver that the next input starts may join a
  running turn whose start it never sees; that receiver streams early only from
  the turn after its first Result. Completed Activity output that is still
  queued follows the human Result on both paths, because the Activity flush
  defers while a human request is pending; streaming earlier does not change
  that order.
- Treat a missing or unknown origin as foreground only when no competing Activity
  evidence exists; otherwise preserve it as detached output and leave the pending
  human request untouched.
- Keep a synthetic owner for detached output until the durable emit succeeds.
  A failed emit retains the selected text, claimed Activity batch, and
  idempotency identity for retry while the receiver is still open or after an
  EOF/error. The recovery owner, rather than receiver cleanup, releases the
  runtime gate after durable local settlement; a generic EOF failure must not
  replace or release it early.
- Preserve durable prewrite evidence so a write that definitely did not happen can
  be explicitly retried without replaying an attempted or ambiguous native write.

## Ownership transitions

The receiver treats a Claude response phase as provisional until its terminal
`ResultMessage.origin` is classified. A pending Activity, a background tool,
and a pending human request are separate facts; none of them alone transfers
Turn, Run, or delivery ownership.

1. A `ToolUseBlock` and matching `TaskStartedMessage` record provisional phase
   and tool/task identity. `run_in_background` describes execution mode only;
   it is not provenance.
2. While the phase is provisional, Activity flushes may retain completed
   outputs but cannot pop or settle a pending human request. Completed,
   failed, stopped, and killed provisional Activities remain addressable as
   terminal snapshots until classification; uncorrelated task events remain
   unattributed.
3. A human Result resolves only the positively correlated provisional tool/task
   facts to the pending request's captured Turn, Run, and delivery identity.
   This resolution happens before terminal Run settlement. A human-created
   background task can therefore retain its Run lineage without making every
   Activity in the runtime human-owned.
4. A task-notification or other explicitly detached Result resolves only its
   correlated phase as detached, preserves the pending human request, and
   delivers the Activity batch through the existing durable receipt path.
5. Missing or unknown origin remains conservative while competing Activity
   evidence exists. Failed, stopped, and killed task terminals keep the
   provenance barrier until the phase is classified or its request/generation
   is retired; Activity count alone cannot clear it.
6. Stop/retirement wins terminal ownership before buffered replay, Activity
   claim, or unsolicited routing. A late human Result is consumed silently,
   while the existing attempted-versus-definitely-unsent evidence remains
   available for recovery.
7. Provenance classification updates the in-memory Activity ownership before its
   durable snapshot. A persistence failure records retryable recovery evidence
   and cannot cause the already-consumed terminal Result to be skipped.
8. A detached terminal Result selects one existing Activity receipt batch. Its
   exact output text and batch identity remain together until the dispatcher and
   local Activity settlement both succeed. A later Assistant frame cannot
   replace that selected text with a CLI summary, and a retry never claims a
   second batch.
9. A synthetic agent-initiated request is an explicit terminal owner even when
   its output is detached. Receiver EOF/error and generation cleanup may close
   native admission, but they retain the synthetic request and its gate while
   durable recovery is pending. Only the successful recovery path retires that
   owner and admits the next human Turn. Detached output that arrives while a
   real human request is pending retains its own retry payload, but does not
   become a synthetic owner or hold the human request's runtime gate.
10. Retained detached output is keyed by its response phase and output identity.
    A later Assistant frame is provisional until its own Result is classified; it
    cannot overwrite the selected text or idempotency identity of an earlier
    retained phase. Once the earlier payload settles, the later phase may create
    its own durable output identity.
11. `awaiting_output` receipts and terminal snapshots have separate acknowledgers.
    Activity Run classification may notify the Run owner, but only completed
    output settlement releases a claimed receipt, and only terminal-snapshot
    acknowledgement deletes a terminal snapshot. This keeps queued output
    recoverable across a crash or restart.
12. Receiver EOF/error/replacement retires the exact dead Claude client
    generation even when a detached recovery owner remains. Generation end also
    closes any still-provisional terminal Activity conservatively as unresolved
    detached state, so no future Result is required and backend work cannot stay
    permanently blocked by an owner that cannot arrive.

The registry uses one FIFO candidate-selection and receipt-binding algorithm
for metadata eligibility, Turn constraints, retries, and persisted local-only
output batches. A metadata predicate can select a candidate, but it cannot
create a second batching path or absorb a previously bound receipt.

## Measured turn shape (Claude CLI 2.1.280, 2026-09-28)

A hermetic probe (Claude Agent SDK 0.2.158 and its bundled CLI, with
`--replay-user-messages`, against a scripted local Messages mock) established:

1. Turns are serialized. Each turn is `system init`, an optional replayed input,
   its frames, and one Result; turns never interleave.
2. A human turn's first content frame is its replayed input (`isReplay`,
   `origin: human`), always before any Assistant frame. A task-notification turn
   has no replay: it starts with Assistant output, and its Result carries
   `origin: task-notification`.
3. A human input sent while a notification turn runs without a tool becomes its
   own human turn after that Result. A notification and a human input both
   queued behind a turn run as separate FIFO turns.
4. A notification drained into a running human turn appears mid-turn as a
   replayed input with `origin: task-notification`; the Result stays human.
5. A human input sent while a notification turn runs a tool is drained into that
   turn: its replay appears mid-turn, the model answers it inside the
   notification turn, and no human Result follows.
6. Human inputs queued before a turn starts are merged into one replayed input.

Facts 1 and 2 make a turn's first content frame the earliest proof of its owner;
facts 4 and 5 are why only the first frame counts. Turns started by a scheduled
wakeup were not measured; the rule assumes that, like notifications, they do not
replay a human-origin input.

Open follow-up outside this rule: under fact 5 the answered human request never
receives a human Result. It predates the rule and needs its own decision.

Fact 6 is resolved in the receipt owner (HFR-487). The merged replay is the
queued inputs joined by newlines, so one echo consumes the matching contiguous
FIFO run of receipts. Echoes with another explicit origin never consume receipts,
even when their text matches. A human-origin or origin-less echo that matches no receipt is
Avibe input in a shape the receipts do not model; it logs a warning and leaves
the receipts pending. The stream exposes no sound boundary for which receipts
such an echo covers, so a release heuristic would trade a loud wedge for silent
early settlement.

## Validation

Consumer tests cover both terminal result orders, notification-before-human-result,
multiple Activity completion aggregation, Assistant buffering, flush-vs-Result
races, retained Activity text during a long-lived receiver retry, receiver
error recovery ownership, failed/stopped/killed/completed provisional terminal
lineage, buffered replay failure, unknown origin, client replacement and Stop
races, exactly-once output, durable unsent-input recovery, and a replay-proven
human turn streaming past a lingering Activity, including one that finishes
mid-turn, without claiming its output, while notification-started turns, human
inputs drained after Assistant output, and turns the receiver joined after their
start, including a replacement receiver that first sees a task event, stay held.
A hermetic Claude Agent SDK 0.2.158 plus bundled CLI probe verifies the outgoing
origin shape and real Result provenance against the local mock upstream.

The current implementation scope is the Claude receiver and existing Activity,
dispatcher, receipt, steering, and generation owners only. It does not add a
cross-backend event abstraction or redesign the Claude SDK.

## Bounded recovery-ledger decision (2026-09-25)

The orchestrator authorized one ownership-model correction after ten
findings-bearing reviewed heads, through `16c1ed184b12`. The circuit breaker
remains cumulative; this decision does not reset it or authorize independent
comment-by-comment pushes. Existing threads remain unchanged during this pass.

The Claude adapter now has one phase/output recovery ledger instead of the
parallel runtime-keyed detached text, Activity, MessageOutput, and provisional
payload maps. A record captures its immutable identity, exact client activation,
provenance, selected text, claimed batch, MessageOutput, context, and synthetic
request owner. A runtime key locates records; it does not determine which request
may be retired. Human request settlement still uses the existing pending Request
and native-input receipts, not a second human terminal state machine.

| Boundary | Owning transition |
| --- | --- |
| Assistant or claimed Activity | Create/update only the current generation's provisional record. |
| Detached Result | Freeze selected text, batch, output identity, and context before any delivery await. A later failure Result gets another record. |
| Delivery failure before acceptance | Keep the same record and retry through the existing managed Activity flush worker. |
| External acceptance but failed local settlement without durable Message evidence | Keep the claim and original payload; refine only the record's retry policy to the dispatcher's existing local-settlement-only path. Never resend externally. |
| Durable delivery and local settlement | Retire that record and only its captured synthetic Request/token. An unrelated current synthetic or human owner is untouched. |
| EOF/error/Stop/replacement | Retire exactly the dead client. Frozen records survive; provisional detached records from that generation are conservatively frozen without borrowing replacement provenance. |
| Activity classification | Update lineage and notify the Run owner of background terminals without deleting awaiting/claimed output receipts; a foreground terminal only acknowledges its snapshot (2026-09-28). |
| Terminal-snapshot acknowledgement | Delete only an indexed terminal snapshot, after Run-owner acceptance. Force-ended snapshots remain indexed until acknowledgement. |

The existing admission fence is consulted before Result classification/replay,
including while Stop is awaiting its native interrupt. A receiver revalidates its
exact activation after waiting for a native frame; replacement cannot let the old
receiver pop the new FIFO head or clear the new generation's phase state.
Cancellation preserves a terminal recovery owner's pending request and gate.
Recovery completion uses the captured owner/token rather than looking up whoever
currently owns that runtime.
An unclassified task that finishes after its pending human request has retired
keeps its original Activity phase. Its late notification cannot become positive
correlation for a newer human phase, and it remains classifiable without requiring
another pending request to exist.

Terminal snapshots retain their opaque activation identity in memory. A numeric
generation is audit metadata only: a process restart can reuse the counter.
Generation end finalizes only its own unresolved snapshots. Restart conservatively
finalizes recovered provisional failed/stopped/killed/disconnected snapshots as
generation-ended and unresolved, without assigning a new human Run. They then
have a reachable drain/ack path.

The managed worker rechecks the ledger after awaited delivery: a Result appended
during a retry remains its responsibility even if the initial list was drained.
There is no new generic queue, service, or per-output timer. Missing/unknown
origin with competing Activity remains conservative; while ownership is
contested, foreground execution mode and TaskStarted linkage are still not
human-provenance evidence. The uncontested live case is covered in the
2026-09-28 section.

Consumer coverage includes event-held native streams; old payload plus later
failure and human phases; human and synthetic successor admission; Stop-first
and Result-first ordering; EOF/error/replacement with persistent delivery failure;
new Result during an in-flight retry; both generation-ending snapshot orders;
SQLite classification/receipt restart; force-end service acknowledgement; and
real-dispatcher external acceptance followed by failing local receipt storage.
These are hermetic adapter/service/dispatcher consumers, not real Web/IM or SDK
end-to-end tests. The ledger is process-local recovery across client generations;
restart tests cover the existing durable Activity receipt/snapshot store, not a
new durable outbox for never-accepted unsolicited text.

## Bounded persistence/EOF correction and ancestry integration (2026-09-26)

The orchestrator authorized one combined pass after the terminal review of
`d0b3f86e78c6`: integrate master `6d464094b2a7`, retain its behavioral
cross-backend Stop test with the Claude synthetic-owner fixture initialized,
and correct two existing boundaries. This does not extend the recovery ledger
or reset the cumulative breaker (eleven findings-bearing reviewed heads).

- A provenance retry describes a failed write, not lifecycle authority. At
  retry time the registry derives the phase from the current owner: active,
  queued/claimed output, or terminal snapshot. A later successful write or
  deletion supersedes that retry evidence, including atomic multi-Activity
  batch binding. Failed writes retain evidence. Neither stale diagnostics nor
  retries may downgrade an output receipt to active or resurrect a deleted row.
- Buffered failure replay distinguishes a diagnostic replay exception from an
  accepted terminal failure. At EOF, a request still owned after failed replay
  follows the existing no-result settlement before releasing its service gate.
  If replay already consumed the owner, EOF cannot emit another terminal.
  Receiver errors use the same ownership rule; a retired receiver cannot
  replay against a successor's client, request, or token.

Consumer coverage uses a real SQLite Activity store with injected single/batch
write failures, transitions from active to completed/failed/stopped/killed,
queued and claimed receipts, terminal acknowledgement, and registry restart.
The receiver/service matrix covers EOF/error, failure before emission and after
owner consumption, successful authoritative replay, repeated cleanup, and
Stop/replacement with an admitted successor. Gate acquisition/release is real
AgentService behavior; outbound acceptance is recorded by a test double, not
claimed as native SDK, durable Run, or Web/IM end-to-end verification. Existing
dispatcher/durable-receipt consumer suites remain part of the focused gate.

No review-thread operations, manual review trigger, PR merge, deployment,
service restart, or Watch/cursor changes are part of this pass. Fresh exact-head
automatic review and repository CI remain required after the single push;
local green tests are not acceptance.

## Bounded terminal ownership transfer correction (2026-09-26)

The orchestrator authorized one coherent correction after `61751dee2389`.
The cumulative breaker remains at twelve findings-bearing reviewed heads.
This pass corrects the existing ownership-transfer boundaries, without a new
queue, service, lock, provenance policy, or thread operation.

- EOF/error replay carries the receiver's exact client and activation identity.
  It validates that identity under the existing steering fence before consuming
  buffered state or a pending request. The caller validates again after replay
  and before fallback settlement or Activity flushing; a replacement during an
  await cannot transfer a failure to the successor. The empty-buffer fast path
  keeps the existing lock order and does not wait unnecessarily on steering.
- A detached Result freezes selected text, phase identity, claim eligibility,
  and output ownership before the first fallible receipt operation. The existing
  FIFO selector can return its raw claim to that ledger owner. The owner then
  constructs the MessageOutput with the existing receipt ID or its immutable
  phase ID before binding. Binding and delivery retries retain exactly those
  members, text, and idempotency identity; they neither reconstruct a CLI
  summary nor absorb later completions. The existing managed worker retries
  that record before claiming other completed outputs.
- Completed-output settlement indexes its terminal snapshot before persistence
  and callback. The terminal callback can therefore acknowledge the durable
  evidence once, including when output-local settlement reports an error.
  Classification alone still cannot acknowledge an awaiting/claimed receipt.

Consumer tests cover EOF/error replacement while replay waits on the steering
fence, replacement after replay returns, pre-emission/post-consumption replay
failures, and Stop refusing a phase already owned by EOF. Event-held streams
cover repeated receipt-storage failure through live delivery, EOF/error,
Stop, and replacement, followed by recovery and successor admission. Real
dispatcher/SQLite consumers cover pre-bound and new batches, delivery failure,
later queued completions, stable output identity, and terminal acknowledgement
with and without accepted Message evidence and local settlement errors.
These are hermetic adapter/service/dispatcher consumers, not native SDK or
Web/IM end-to-end tests. The ledger remains process-local; restart coverage
concerns the existing durable Activity receipt store, not a new unsolicited
output outbox. Fresh automatic exact-head review and CI remain required after
the single push; local tests do not establish acceptance.

## Agent-initiated Turn settlement on retirement (2026-09-27)

A task-notification Result is detached output, so its delivery never completes
a Turn. When that output belonged to a synthetic agent-initiated owner, the
retirement in transition 9 released only the runtime gate. The agent-initiated
Turn's waiter was never signalled. The Session therefore stayed in flight
("delivering"), Stop waited for a terminal that could not arrive, and later
human input queued behind the Turn indefinitely.

- Retiring a synthetic owner is that owner's terminal boundary. It ends the
  agent-initiated Turn through the canonical empty terminal result
  (`completes_turn=True`, `completes_run=False`) before the gate is released,
  matching the forced-cancel settle path. The Run, if any, stays with the
  detached output that already settled it. The output record is already gone,
  so nothing retries this settle: if it fails or is cancelled, the Turn waiter
  is still released (cancellation then propagates), so the Session cannot wedge.
  That fallback first latches an error outcome through the same
  `on_terminal_result` chokepoint. Otherwise a failed error settle would
  terminalize the Turn as completed.
- The settle carries the outcome frozen on the record when it is classified.
  That outcome uses `_terminal_backend_failure`, the predicate that already
  selects detached result text. A Result that fails only through `is_error`,
  `error`, `errors`, `api_error_status`, or a `failed` subtype therefore ends
  its Turn as failed, not completed.
- The settle awaits delivery while the owner is already retired but the gate
  is still held. Output reaching the receiver in that window belongs to the
  next Turn. It waits for the settlement, then opens that Turn normally.
  Classifying the output against the held gate would instead make it a
  detached record that is delivered outside any Turn.
- A silent-only detached reply settles its Activity claim without creating a
  Message. A missing receipt for such a reply is success, not a delivery
  failure, so it no longer retries forever ahead of every later record.

## Uncontested live phases and steer boundaries (2026-09-28)

A long foreground command made a human Turn look stuck. Take this stream:
`Bash(sleep 600)` is emitted live, its `TaskStarted` arrives, and the model
keeps working. Every `TaskStarted` opened the provenance barrier, so each later
Assistant and tool frame of that Turn was buffered until the terminal Result.
The Web UI showed nothing for the rest of the Turn. If a steer receipt arrived
while frames were buffered, the receipt boundary cleared the buffer and its
provisional facts without replaying them. Those frames were lost, and the
pre-steer Activities stayed provisional snapshots that kept competing.

- A live frame is emitted only when nothing competes with the pending human
  request, so the receiver has already attributed it to that request. A
  foreground task whose parent tool is such a frame inherits the frame's owner
  when all of these hold: the tool is still recorded as a live foreground tool,
  no frame is buffered, no competing Activity or output record exists, and the
  pending request is a real human request. The Activity starts with that Turn,
  Run, and delivery identity and `provenance_human`, so it does not compete, and
  later frames of the phase stay live. If any condition fails, the task is
  provisional as before.
- Only foreground tools qualify. A foreground completion has no Activity output
  that a Result could claim, and its terminal follows the existing
  non-provisional foreground owner: the Turn's Result settles the Run. A
  background tool's completion creates queued output, and the Activity flush can
  complete a Turn from it. Background tools therefore stay provisional until the
  Result classifies their phase, even when their frame was live. Foreground
  follows each tool's real default through the shared tool policy
  (`runs_in_background`): an `Agent` call without `run_in_background` is a
  background subagent, while a `Bash` call without it is foreground.
- A foreground task is one step of its Turn, and the agent can recover from a
  failed step. Its terminal never settles the Run on either path: the Turn's
  Result owns the Run outcome. Previously a foreground task that stayed
  provisional behind competing output was handed to the Run owner once the
  human Result classified it, so a failed, stopped, or killed step made the Run
  fail or cancel immediately and stickily, even when the Turn then succeeded.
  The same stream settled differently depending on whether unrelated output
  happened to compete. Classification now only acknowledges such a foreground
  terminal snapshot; classified background terminals still notify the Run
  owner, which `Activity classification` below describes.
- When a steer receipt arrives while frames are buffered, no Result separates
  those frames from the steer, so Claude consumed the steer within the same
  turn. The receiver appends a steer-boundary marker and keeps the frames and
  their provisional facts for the one terminal Result that classifies both
  sides. A human Result replays the frames and emits the pre-steer text at the
  marker as non-terminal primary output, the same shape as a live steer
  boundary. Foreground-tool evidence is phase-local there too: the marker
  retires the pre-steer evidence, so a post-steer Result without its own
  Assistant text keeps its result text instead of the silent tool-only
  sentinel. A detached Result ignores the marker and keeps the pending request.
  Without buffered frames, the existing live boundary behavior is unchanged.
- Replay is ordered. Once a phase has a buffered frame, every later frame of
  that phase is buffered too, even if the competing output finishes first. A
  later frame therefore cannot overtake earlier held frames, and replay after
  the marker re-derives exactly the evidence that belongs after the boundary.

Consumer tests cover the uncontested live task, a competing Activity that
appears before `TaskStarted`, a default-background `Agent` task, and human or
detached classification of buffered frames across a steer boundary, including a
post-steer Result with no Assistant frame and competition that ends after the
steer. They are hermetic receiver tests, not native SDK or Web/IM end-to-end
tests.
