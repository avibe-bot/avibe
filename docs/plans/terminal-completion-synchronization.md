# Path B step 2: non-waiting terminal completion (#2706)

Status: implemented in this change. The transaction stays on the controller loop;
only waiting for its writer becomes asynchronous. No persisted shape changes.

## Change contract

Remove SQLite's writer wait from the evidence-carrying terminal path while keeping:

1. the delivered terminal evidence ahead of a runner/native/recovery fallback;
2. one atomic predecessor terminalization and linked P0 / start replay / FIFO selection;
3. exactly one post-terminal resume;
4. runtime-generation admission and Model Hub finalization fences;
5. terminal inbox / status publication before the runner's `turn.end`;
6. first-writer-wins Run settlement and existing recovery;
7. a responsive controller loop under writer contention;
8. Stop, backend refresh, shutdown and admission priorities.

Message persistence (#2725), other writer sites, provider behavior and deployment
are not part of this change. Paths A, C and B step 1 already shipped.

## Concrete ordering

Suppose the native result has been delivered, a linked P0 replacement is waiting,
and another process holds SQLite's writer. The dispatcher still signals and releases
the native Turn, records its delivered-evidence marker, and attempts B without
suspending. The attempt now returns immediately on contention. Before control can
yield, the manager owns one pending completion keyed by the exact logical Turn.

The runner may wake, but it joins that completion before Hub finalization, removing
`in_flight`, `turn.end`, or resuming the queue. A new P1 or Stop also joins it before
examining the active owner: the old durable row still says `active`, but its native
Turn has ended, so steering or stopping that native would be wrong.

Once the writer is available, one synchronous transaction commits the evidence,
settles pending-steer dispositions, selects the linked P0 before replay/FIFO, and
updates the projection. No admission can see an ownerless gap. Terminal events
are published before the pending completion releases the runner. The runner then
finishes Hub, releases its projection and resumes the selected successor once.

## Synchronization design

### Keep both existing transaction boundaries

`_terminalize_durable_turn` retains its entire SQLite transaction and its
`_runtime_start_owner` / `hold_if_current` section. Delivered terminal attempts use
the existing `_try_reserve_write_lock`, which temporarily sets a zero busy timeout.
On contention, both contexts exit before any await. A retry resolves and acquires
the runtime identity afresh. No activation RLock is held across an await or on a
worker doing I/O.

This fixes the measured `reserve_write_lock` wait, not all possible slow-disk cost.
The transaction's bounded work remains on the loop. A full off-loop transaction
would require a different generation-admission contract: `commit_then_confirm`
cannot undo a linked P0 activation, FIFO claim or consumed replay budget. Splitting
the terminal CAS from successor selection would instead need a durable admission
fence. Neither mechanism is necessary for the measured writer wait.

### One exact-Turn completion owner

`_PendingDurableTerminal` retains the outcome, settlement, evidence and the strictest
resume decision. The manager retains the retry Task strongly. The uncontended
ordinary-result path still finishes synchronously and removes its temporary record.
The pending registry is controller-local; all runtime terminal callbacks already
reach this controller-owned manager. SQLite, not this registry, still serializes
admissions from other connections.

A competing terminalizer uses the pending evidence at the existing chokepoint,
not its own `runner_release`, absent-start, refresh or shutdown evidence. Its own
`resume_successors=False` can restrict resume; it cannot turn an earlier restriction
back on. Negative start/replay flags cannot reinterpret an already delivered outcome.

If that competitor lands the write, the pending Task still owns completion and
resume. The competitor receives `completion_pending`, not permission to run a
second resume. This matters for synchronous boolean-returning callers that have
no resume tail of their own. If a live runner/agent-initiated holder still owns the
Turn, the completion Task does not resume: the runner/holder is the complementary
owner. The holder now keeps `in_flight` until after the terminal commit, like the
ordinary runner. The ordinary runner also keeps its projection through a fallback
write after Hub finalization, so an expired early retry cannot give both the runner
and completion Task the resume decision.

The existing sink-to-dispatch-context `TERMINAL_COMMIT_KEY` handoff remains. It
covers a B attempt that fails before creating the pending record, and cancellation
where dispatch returns no outcome. It is not replaced by the pre-delivery latch,
which does not prove delivery reached the terminal block.

### Admissions, late receipts and native starts

- `deliver` and `cancel` await the Session's pending completion before observing
  ownership. Their wait is cancellable without canceling the completion owner.
- A late accepted steer receipt joins its exact target Turn's pending write before
  materializing acceptance. Otherwise it could miss the terminal snapshot and leave
  its Run without the already recorded result. Before yielding, the pending owner
  latches the receipt by exact Delivery/attempt. If the join expires, B's eventual
  transaction also stores that accepted recovery fence. A fresh manager can then
  materialize acceptance and settle from the snapshot without asking the ended
  backend. Definitive acceptance can upgrade an earlier UNKNOWN fence, never the
  reverse. Once the terminal snapshot is committed, the receipt needs no further
  wait for B's resultless tail. Negative/UNKNOWN receipts keep their existing path.
  A caught cancellation is rearmed after the join.
- Queue drain, linked-control resume and persisted native start do not enter while
  the Session has pending completion work. In particular, a committed successor
  cannot start while the post-commit Run settlement is still pending. These gates
  re-drive a finished/deferred completion Task before refusing the start, so a
  transient executor/read failure does not strand an already claimed successor.
- Terminal/no-op prechecks from B step 1 remain unchanged when no completion is pending.

### Resultless Run settlement and publication

B's resultless settlement tail is another SQLite writer, after the main transaction.
For a delivered resultless outcome with accepted Runs, the pending owner sends that
tail through `_run_off_loop` (the runtime-work door, with the existing test fallback).
`land_step` keeps the store step owned through cancellation. The activation boundary
has already been released. The resultless settlement branch does not take the
Activity registry lock; the ordinary positive-result/late-Activity behavior is not
moved here. The worker requests `retire_job=False` and returns any one-shot
scheduler ids; the completion owner retires those jobs on the loop, before events
or fence release. Scheduler maps and APScheduler job mutation never move to the worker.

Only after that step lands does the owner publish inbox/status events, clear the
pending fence and permit resume. Existing guarded settlement methods continue to
own first-writer-wins and recovery if a store operation itself fails. The immutable
Turn snapshot and the first committed successor decision are retained across this
tail; retrying it never replaces the committed result with a terminal no-op.

### Failure, refresh and shutdown

One retry Task has a five-second writer-attempt budget, matching the configured
SQLite busy wait without blocking the loop. It retries only contention, with a
short asynchronous backoff. An ordinary error or an exhausted budget is logged
and returned as deferred completion, not success. The pending evidence remains
available to the runner, a subsequent admission retry, or recovery; another
terminalizer cannot erase it. A new admission that still cannot finish that
completion fails closed rather than steering into the ended native Turn.

The runner's existing fallback attempts can retry the retained evidence. Persistent
storage failure remains unresolved, as before; this does not invent an empty result,
pretend that a write landed, or make one retry Task wait forever. A process crash
before B commits leaves the durable owner for ordinary startup recovery, as in the
existing gap between path A and B. The in-memory latch is not a new crash journal.

Backend refresh snapshots the generation's durable and projected owners before
joining pending completions. A successor selected during that join is not silently
added to the old generation's teardown set. A delivered predecessor does not gain
a spurious refresh interruption notice, including when its join exhausts the retry
budget and the durable row has not landed yet. A refresh without a drain also
excludes the successor from its pre-join ownership snapshot.

Shutdown marks pending completions and their projected runners as queue-deferred
before flushing them with a bounded wait. It does not restore a synchronous writer
wait on expiry. Their ordinary cleanup owners still join finite attempts; the
existing shutdown owner retains responsibility for finishing store workers. Neither
the pending Task nor the runner/holder starts a successor during this flush.

## Validation

The regression probe uses a separate spawned process holding `BEGIN IMMEDIATE`.
That process releases its writer only after a queued controller callback reaches
it, or a watchdog bounds the failure. On baseline `d4c37f082`, the callback arrives
only after the watchdog releases SQLite: the probe fails for the writer wait while
the eventual evidence assertions still pass. With this change it passes. There is
no expected-failure marker in the delivered tests.

Owning-boundary evidence:

- `test_terminal_successor_selection_excludes_a_competing_admission`: a second
  connection requests admission between the predecessor CAS and successor selection;
  linked P0, start replay and FIFO each retain their intended owner.
- `test_contended_terminal_keeps_cleanup_fenced_until_evidence_commits`: real
  dispatcher, ordinary runner and agent-initiated holder, with and without backlog;
  repeated cancellation, refresh and shutdown; Hub observes a terminal row, terminal
  events precede end, and successor launch is exactly once or explicitly deferred.
- `test_admission_joins_delivered_terminal_instead_of_the_ended_native`: concurrent
  P1 starts as new input after B; Stop does not target the ended native.
- The existing late-accepted Run/Activity settlement test now also runs under B
  contention, with the receipt arriving before the writer is released. Expiry cases
  let the caller return before B commits, then prove a fresh manager consumes the
  accepted recovery fence without backend reconciliation, including after UNKNOWN.
- `test_pending_terminal_evidence_survives_timeout_and_competing_terminalizer`:
  native terminal, absent-start proof, refresh and shutdown cannot replace B's
  evidence, including after a bounded attempt expires.
- `test_terminal_retry_rechecks_retired_generation_without_holding_its_boundary`:
  retirement between attempts succeeds; the predecessor commits but backlog stays
  queued and no off-loop boundary holder is used.
- `test_resultless_terminal_run_settlement_lands_off_loop_before_completion`: a
  competing writer at the *second* transaction leaves the loop responsive and the
  completion fence in place; an owned worker survives cancellation and settles the Run.
  A real one-shot definition proves its store projection is off-loop but scheduler
  job retirement returns to the loop.
- `test_recovery_redrives_a_failed_post_commit_terminal_tail`: each runtime start,
  drain and linked-resume gate re-arms a failed tail without a new user admission.
- `test_refresh_does_not_relabel_delivered_evidence_after_retry_expiry`: the sink
  is absent and B remains busy through refresh's join, but no interruption replaces
  the delivered result or produces a notice.
- `test_expired_terminal_retries_keep_the_runner_as_the_only_resume_owner`: two
  exhausted pre-Hub attempts followed by a successful fallback still resume once.

Existing FSM, Model Hub finalization, synchronous emit ordering, no-op writer,
unknown-start, restart and delivery-recovery tests remain part of validation.
Manual service / local Incus verification is deliberately not performed in this lane.
