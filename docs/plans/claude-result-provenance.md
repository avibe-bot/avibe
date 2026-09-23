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

## Validation

Consumer tests cover both terminal result orders, notification-before-human-result,
multiple Activity completion aggregation, Assistant buffering, flush-vs-Result
races, buffered replay failure, unknown origin, client replacement and Stop races,
exactly-once output, and durable unsent-input recovery. A hermetic Claude Agent SDK
0.2.158 plus bundled CLI probe verifies the outgoing origin shape and real Result
provenance against the local mock upstream.
