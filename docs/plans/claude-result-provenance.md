# Claude result provenance and background input admission

## Problem

Claude Code keeps one streaming SDK connection per runtime. A detached background
Activity can produce Assistant and Result frames while a newer Avibe human Turn is
waiting to write. The SDK's terminal `ResultMessage.origin` is the only reliable
owner signal in this interleaved stream; Assistant frames do not carry that field.

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

## Validation

Consumer tests cover both terminal result orders, notification-before-human-result,
multiple Activity completion aggregation, Assistant buffering, flush-vs-Result
races, retained Activity text during a long-lived receiver retry, receiver
error recovery ownership, failed/stopped/killed/completed provisional terminal
lineage, buffered replay failure, unknown origin, client replacement and Stop
races, exactly-once output, and durable unsent-input recovery. A hermetic Claude
Agent SDK 0.2.158 plus bundled CLI probe verifies the outgoing origin shape and
real Result provenance against the local mock upstream.

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
| Activity classification | Update lineage and notify the Run owner without deleting awaiting/claimed output receipts. |
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
origin with competing Activity remains conservative; foreground execution mode
and TaskStarted linkage are still not human-provenance evidence.

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
