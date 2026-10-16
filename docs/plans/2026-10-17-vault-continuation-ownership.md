# Vault approval continuation ownership

## Problem and cause

A protected `vault run` or `vault fetch` can wait synchronously for browser
approval while the same request retains an asynchronous Session callback.
The callback currently treats `waiter.status == active` plus a future deadline
as proof that the CLI still owns continuation. A command killed before that
deadline leaves a durable but ownerless waiter, delaying a ready approval.

The synchronous wait/gate shipped in #826 (2026-07-07). The later agent-core
Bash/job lifecycle (#2345, 2026-10-04) supplies another concrete interruption
path: a caller-selected command timeout can precede the approval timeout.
The foreground handover window is not itself a command-kill timeout.

A second boundary must be fixed with the first: callback processing uses a
previously scanned row, while CLI waiter arm/completion happens independently.
Liveness without serialized, fresh ownership decisions still permits duplicate
continuations.

## Change contract

Preserve the shipped synchronous `run`/`fetch` default and explicit wait flags.
Do not remove real terminal/CLI behavior, special-case an Agent backend, or
change job/Watch timeouts to hide the defect. The shared Vault boundary owns
continuation arbitration; the job layer only owns command execution.

1. A live synchronous owner suppresses an asynchronous Agent callback. A date
   or an unverified PID alone is not proof of ownership.
2. A process-bound ownership primitive is released by the OS on process death,
   including forced kill. Signal handlers, best-effort cleanup and heartbeat
   timing must not be the correctness foundation. Prefer the existing portable
   cross-process lock implementation over a new lease subsystem.
3. CLI arm/completion and callback takeover are serialized and re-read durable
   state at the decision point. Once callback continuation is durably selected,
   a late or resumed CLI must not execute the approved operation. Daemon recovery
   must not reopen that decision after enqueue-before-mark interruption.
4. A successfully consumed synchronous approval remains transcript-only; it
   must not start a second Agent turn. Approval consumption is not a claim that
   the downstream command has completed.
5. A dead owner's ready request becomes eligible on the next callback processing
   pass, not its old approval deadline. A still-live owner survives service
   restart without being mistaken for dead. Holding a lock must not block the
   controller event loop or keep a SQLite transaction open while awaiting a user.
6. Grant delivery readiness, authorization snapshots and one-shot grant rules
   remain authoritative. Callback enqueue is idempotent across the enqueue/mark
   crash window: `source_actor` alone is currently only a label for Vault
   callbacks, not a deduplication guarantee. Reuse durable Run identity and the
   existing request store rather than add a parallel queue. Covered sibling
   requests must follow the same ownership decision without duplicate Agent
   turns for the same resolved approval.
7. Released waiter rows without ownership evidence need an explicit bounded
   compatibility policy. Missing historical proof is not evidence of death;
   do not release possibly live old waiters by guessing.
8. Do not automatically replay a command whose effects may already have started.
   This is approval continuation arbitration, not an exactly-once guarantee for
   arbitrary external side effects.

## Validation

Existing tests exercise graceful completion and timeout, not an abruptly dead
CLI with a future deadline. The primary regression must kill a real test-owned
waiting process, approve in isolated state, and prove prompt callback eligibility.
It must fail on the base revision for the stale-waiter gate, not setup failure.

Additional distinct risks:

- A live waiter, including a handed-over command, still suppresses callback.
- Explicit wait and no-wait CLI behavior remain compatible for run and fetch.
- Completion versus callback takeover, and approval before waiter arming, cannot
  select both continuations, including use of a stale scheduler scan row.
- Callback enqueue followed by daemon interruption cannot hand execution back
  to a new CLI waiter.
- Dead-owner recovery and live-owner preservation across a service instance
  change are distinguished.
- Approval without a delivery-ready grant still waits.
- Covered siblings and repeated scheduler processing do not create duplicate
  Agent turns.
- Legacy records follow the documented bounded rollout policy.

Tests use temporary homes/databases and only processes they start; no real
secrets, live approvals, installed runtime changes or service restarts.
Focused Python tests and changed-file Ruff precede CI/exact-head review.

## Implementation constraints

- Use a kernel-held lock as new waiter liveness evidence and conditional database
  updates (checking the affected-row count) as the continuation fence. These have
  different responsibilities; neither replaces the other.
- The callback must durably claim continuation even when no waiter has yet been
  armed. Merely testing `callback_status != sent` leaves a create/arm versus
  enqueue/mark window. A CLI losing either arm or completion must not deliver.
- Read the current row at the ownership decision, not a scan snapshot. Preserve
  current provision-field projection when refreshing callback data.
- Lock paths must be derived from the database/instance being protected, not
  an unrelated ambient home; validate identifiers before path construction.
  Do not unlink a lock inode while another claimant could hold or acquire it.
- Legacy unproven waiters retain their existing bounded deadline behavior for
  rollout; new waiters must not use that deadline as liveness evidence.
- Align the bundled Vault Skill with real synchronous success versus deferred
  continuation behavior; do not tell the Agent to replay a successful command.

## Status

Root-cause investigation complete; ownership contract recorded before
implementation. No runtime deployment or restart is authorized by this change.
