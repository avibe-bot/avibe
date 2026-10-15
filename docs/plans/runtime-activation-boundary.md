# Runtime activation boundary off the controller loop (#2655)

Status: design note, proposed. No code yet.

## Background

`RuntimeActivationRegistry` (`core/runtime_activation.py`) gives every runtime generation an identity. Each identity has a boundary: a `threading.RLock`, a phase (`live`, `retiring`, `retired`) and a reservation serial. All of it is process memory; nothing is persisted.

The boundary is used in two ways.

- **Short sections.** These touch memory only:
  - `attach`, `current` and `is_current`;
  - `reserve_retirement` and `finish_retirement`;
  - `retire_if_current`, which runs its predicate between the reserve and the finish, without the lock.
- **Commits under the lock.** `commit_if_current(identity, commit)` runs `commit()` while holding the lock, and `hold_if_current` keeps the lock for the caller's whole `with` body. That body is usually a SQLite transaction.

This design exists so that an owner commit for a generation cannot race that generation's retirement. A commit either lands before retirement takes its final ownership snapshot, so the snapshot sees it, or it is refused.

## Problem

Commits under the lock happen on two threads.

**Off the loop.** Only two holders keep the boundary across SQLite I/O on a worker thread. Both are reached through the scheduler's runtime-work door.

- **W1, the scheduler claim.** `_claim_pending_request` → `request_store.claim` → `claim_pending_run`. It runs `BEGIN IMMEDIATE`, then a read, then an update from queued to running.
- **W2, the execution start.** `_start_claimed_execution` → `mark_execution_started`. It sets the pid marker where the run is running and `pid IS NULL`.

**On the loop.** Every other holder that does I/O under the lock runs on the loop thread, and none of them awaits inside the lock:

- the Session Turn admissions in `core/session_turns.py`, including `drain_delivery_queue`'s in-transaction hold;
- native start and agent-initiated turns in `modules/agents/service.py`;
- SessionActivity writes in `core/session_activities.py`.

The loop also takes the boundary for short sections:

- retirement:
  - Codex `_stop_runtime`;
  - OpenCode `_on_generation_stopping`;
  - Claude cleanup, idle eviction and native migration;
- `is_current` before every Claude native write;
- `current` and `attach`.

Because the lock is an `RLock` and the loop never awaits inside it, loop-side holders never make the loop wait on each other. The loop waits on the boundary only while W1 or W2 holds it. That produces two failures.

1. **Retirement waits for a contended commit.** A retirement on the loop that reaches the boundary while W1 or W2 is committing under SQLite contention waits for that commit, up to the 5 s `busy_timeout`. This is the failure #2655 describes.

2. **A lock-order inversion stalls the loop, with no retirement involved.** The two sides take the same two locks in opposite orders:
   - W1 and W2 take the boundary, then the SQLite writer.
   - `drain_delivery_queue` on the loop takes the writer (`reserve_write_lock`), then the boundary (`_claim_fifo_batch_in_transaction` → `owner_factory` → `hold_if_current`).

   When both resolve to the same identity, the loop blocks on the `RLock`, which has no timeout. It stays blocked until the worker's `busy_timeout` expires and the worker gives up its claim.

   This is common in practice. OpenCode resolves every unbound request to its current runtime, and Codex shares one identity per working-directory generation.

   A probe used the real registry and two SQLite connections ordered as above. The loop thread blocked for 5.35 s, and the worker's claim failed with `database is locked`. The scheduler claim has run on the worker since #1173, so this happens on master today and in released versions.

## Goal

The controller loop never waits on a lock that another thread holds across blocking I/O.

Equivalently: **off the loop, the boundary is held only for constant-time in-memory sections.** It may be held across I/O only on the loop thread. There, it is subject to the general controller-loop rule like any other SQLite work on the loop.

## Options

### 1. Persisted generation CAS (from #2655)

Commits would carry a persisted generation, and retirement would bump it transactionally. Rejected, for three reasons.

- **The generation is process-local** (`itertools.count(1)`) and repeats after a restart, so a persisted key needs a restart-unique part and a schema change.
- **The bump at retirement is itself a contended SQLite write.**
  - On the loop, it brings back SQLite-on-loop.
  - Off the loop, it puts an await before the fence lands. That breaks Codex's rule that "the retirement is reserved before anything awaits", unless the in-memory phase flip stays synchronous as well — and then the CAS adds nothing for in-memory owners.
- **Several loop-side owners commit memory as well as SQLite under the boundary**, and a database CAS cannot fence memory:
  - native start mutates the gate and creates loop tasks;
  - the agent-initiated turn must not yield;
  - activities update the identity maps that retirement predicates read.

### 2. Never take the boundary on the loop

This would move every loop-side acquisition to a worker:
- retirement;
- the `is_current` check before each Claude write;
- `current` and `attach`;
- the Session Turn holds inside `drain_delivery_queue`'s transaction;
- native start, the agent-initiated turn and activities.

Several of these are tied to the loop by construction: `get_running_loop().create_task` runs inside the commit, the receiver's activity API is synchronous, and `begin_agent_initiated_turn` is documented as non-yielding. Rejected: the change is very wide, and the risk is high.

### 3. Off the loop, commit outside the boundary and confirm afterwards (recommended)

Change only the two off-loop holders, W1 and W2. Each one becomes:

1. **Admit.** Under the boundary, refuse unless the identity is live. This is unchanged: a retirement already reserved refuses the commit before it runs.
2. **Commit.** Run the SQLite write with no boundary held.
3. **Confirm.** Under the boundary, check that the identity is still current and live.
   - If it is, the commit stands.
   - If it is not, the commit lost to a retirement. Compensate with the existing `requeue`, which moves the run from running back to queued and clears `pid`. Then report "not admitted", so the execution is skipped exactly as it is today.

The registry owns this rule as one new method, `commit_then_confirm(identity, commit, compensate)`. `commit_if_current` and `hold_if_current` keep their meaning and are documented as loop-only, because their commit holds the boundary.

**Why this is correct**, case by case:

- **The retirement reserves before admission.** The commit is refused before it runs. Unchanged.
- **The commit lands before the retirement's final snapshot.**
  - Codex `_generation_drained` and Claude idle eviction read durable ownership through `RuntimeOwnershipProvider`. If that predicate counts the run as an owner, the retirement declines, as it does today. The confirm step then sees `live`, or sees `retiring` and compensates; either is safe.
  - If the predicate does not count the run, the retirement completes, and the next case applies.
  - Correctness therefore does not depend on what any predicate sees.
- **The commit lands after the snapshot, or the predicate is in memory only** (OpenCode `lambda: True`, Claude cleanup). The retirement completes, and the confirm step sees the identity retired and compensates.
  - Today the retirement would have waited for the commit and retired anyway, leaving a claimed run whose start W2 then refuses.
  - Both designs end with the run requeued and nothing executing on the retired generation; the new one gets there a step earlier.
- **A retirement is reserved and then aborted while the commit is in flight.** The confirm step either compensates when it did not need to (safe: the run is reclaimed later) or sees `live`.

**What changes:**
- A retirement no longer waits for an in-flight W1 or W2 commit.
- One test pins that wait: `test_hfr_137_fallback_pid_marker_first_blocks_retirement_until_commit` (`tests/test_runtime_activation.py:1058`, order asserted at :1117). It is restated as an outcome assertion: no execution proceeds on a generation retired before its start was confirmed. The cleanup-first and claim-first tests (:885, :932) keep their outcomes.
- A run may be `running` for a moment before it is compensated. Graceful retirement treats that as an owner and declines, which is the conservative choice.

**If the compensating write fails**, the step still reports "not admitted", so nothing executes on the retired generation. The row is left running and is recovered by the stale-run sweep or `recover_processing`, like any other failed store write.

**What it removes:**
- Failure 1: off the loop, the boundary is held only for constant-time sections, so a retirement on the loop never waits on I/O.
- Failure 2: W1 and W2 no longer hold the boundary while they wait for the writer, so the inversion cannot form.

**What it leaves (not in scope here):**
- **Loop-side holders still do SQLite I/O under the boundary.** They cannot make the loop wait on another thread, but they are SQLite-on-loop under the controller-loop rule. That belongs to the #2630 family.
- **W1 and W2 resolve their identity on the worker by reading loop-owned adapter state.** For example, Claude iterates `claude_sessions.values()`. A concurrent mutation can raise `dictionary changed size during iteration`. `AgentService` catches the error and fails closed: the resolution is treated as inconclusive, and the claim stays queued and is retried. That is correct but noisy. Moving the in-memory part of the resolution to the loop is an optional follow-up.

## Invariants each caller keeps

| Caller | Thread | Invariant | After this change |
| --- | --- | --- | --- |
| W1 scheduler claim | worker | A queued run does not become running against a retiring or retired generation | Admit, commit, confirm; compensate with `requeue` |
| W2 execution start | worker | The pid marker never stands after its generation retired; the execution is skipped | Admit, commit, confirm; compensate with `requeue` |
| Session Turn admissions | loop | A non-admitted owner is persisted as queued, never promoted | Unchanged: `hold_if_current` |
| Native start / agent-initiated turn | loop | A retired generation does not bind the gate or the durable Turn | Unchanged: `commit_if_current` |
| SessionActivity writes | loop | A stale generation does not rebuild or complete an owner | Unchanged: `commit_if_current` |
| Codex retirement | loop | Reserved before anything awaits; drained check made inside the reservation; aborts on failure | Unchanged |
| Claude cleanup / idle eviction / migration | loop | Durable Turn and Activity ownership vetoes reclamation | Unchanged |
| OpenCode retirement | loop | No fenced commit is lost when an unbound generation retires | Unchanged |

## Test design

1. **Inversion.** A worker in W1 waits for the SQLite writer, which a transaction on the loop thread holds before that transaction takes the boundary (`drain_delivery_queue`'s order). The loop thread must acquire the boundary at once (under 50 ms). The claim then commits after the loop's transaction, or is retried. Neither side waits 5 s.
2. **Retirement during a contended commit.** W1's or W2's commit waits on a writer that another connection holds. Meanwhile the loop runs `reserve_retirement` or `retire_if_current`, which must return at once. When the commit lands, the confirm step compensates: the run is queued and nothing starts.
3. **Admission first, graceful retirement.** The commit lands before Codex's drained snapshot. The retirement declines, and the run proceeds (unchanged behavior).
4. **Compensation fails.** The compensating `requeue` raises. The execution is still skipped.

Test 1 fails on master today, with the 5.35 s stall above.

## Plan

- [ ] Registry: add `commit_then_confirm`, and document `commit_if_current` and `hold_if_current` as loop-only.
- [ ] Scheduler: move W1 (`_claim_pending_request`) and W2 (`_start_claimed_execution`) to it, with `requeue` as the compensation.
- [ ] Tests: restate the pinned wait-order tests as outcome tests, and add test designs 1–4.
- [ ] Optional: log an error when `commit_if_current` or `hold_if_current` runs off the loop thread, so a new off-loop holder is caught early.
