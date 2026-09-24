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
   outputs but cannot pop or settle a pending human request. Uncorrelated task
   events remain unattributed.
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

The registry uses one FIFO candidate-selection and receipt-binding algorithm
for metadata eligibility, Turn constraints, retries, and persisted local-only
output batches. A metadata predicate can select a candidate, but it cannot
create a second batching path or absorb a previously bound receipt.

## Validation

Consumer tests cover both terminal result orders, notification-before-human-result,
multiple Activity completion aggregation, Assistant buffering, flush-vs-Result
races, buffered replay failure, unknown origin, client replacement and Stop races,
exactly-once output, and durable unsent-input recovery. A hermetic Claude Agent SDK
0.2.158 plus bundled CLI probe verifies the outgoing origin shape and real Result
provenance against the local mock upstream.

The current implementation scope is the Claude receiver and existing Activity,
dispatcher, receipt, steering, and generation owners only. It does not add a
cross-backend event abstraction or redesign the Claude SDK.
