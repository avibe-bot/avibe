# SQLite writers on the controller loop: terminal, record and recovery paths (#2706)

Status: design note, proposed. No code yet.

## Background

The rc7 overnight soak recorded controller-loop stalls of 1–3 s. In four of the stall dumps, the loop thread was waiting in SQLite. The general rule is in `AGENTS.md` ("Controller Event Loop"): the loop must not wait for SQLite's writer, which another process (a `vibe` CLI call or the UI server) can hold for up to the 5 s `busy_timeout`.

Two of the stalls already have fixes:
- the watch-store mirror (#2705, a published snapshot);
- the turn-state repair (#2708), which tries the writer without waiting.

This note covers the other three paths. For each one it states what the write does, whether it holds the runtime activation boundary (`docs/plans/runtime-activation-boundary.md`), and which loop-synchronous orderings a move off the loop would have to keep.

The boundary rule decides how far each path can move:
- `commit_if_current` and `hold_if_current` are loop-only.
- An off-loop owner commit uses `commit_then_confirm`: admit, commit, confirm, and compensate if the commit lost.

## Path A: a Turn's Run terminal record

**What it does.** The chain is `MessageDispatcher.emit_agent_message` → `_record_agent_run_terminal_result` → `_record_agent_run_terminal_for_ids` → `SQLiteBackgroundTaskStore.record_turn_run_outputs`. It runs once per terminal result that carries Run ids (Harness, callback and scheduled Turns). One `BEGIN IMMEDIATE` transaction:
1. reads the Turn's participant Runs;
2. writes each Run's output, or defers it while a blocking Activity is open.

The rows are bounded by the Turn's Run ids. The store is also constructed on every call, which opens schema-check connections on the loop.

**Boundary.** None. Its atomicity is SQLite's own: the writer is reserved before the participant reads, and that does not depend on the thread.

**Orderings a move must keep.**
1. **The Run write lands before the Turn's sink is released.** Awaiting an off-loop step at the same point keeps that order. While the step is in flight, a Stop on the loop can land first and settle the Run `canceled`, which is first-writer-wins in the store. That is a legitimate outcome of a Stop racing a result, and the note accepts it.
2. **A cancellation caught during the write is held until the whole terminal-delivery tail has completed**, not just the close-after tail. After `_record_agent_run_terminal_result`, the dispatcher still has to run `_signal_turn_complete` and release the sink. After the dispatcher returns, `Controller.emit_agent_message` calls `on_terminal_delivery_complete`, which is path B's evidence-carrying terminal write. If the cancellation were answered any earlier, the runner's `runner_release` could terminalize the Turn first, without evidence, which breaks B's ordering 1. So the terminal emit runs as one unit that a cancellation cannot split: the Run write (through `land_step`), the close-after tail, `_signal_turn_complete`, the sink release and `on_terminal_delivery_complete`. The caught cancellation is answered only after all of them have run.
3. **The blocking-Activity check is made without the Activity registry's lock while SQLite's writer is held.** Activity start and completion hold `SessionActivityRegistry._lock` while their store write waits for SQLite's writer. A worker that reserved the writer and then called `has_blocking_run_activity` would take the two locks in the opposite order, which is a lock inversion. Two placements look possible, and the choice belongs to the implementation:
   - the check reads the durable Activity rows for the Turn's Runs (session Activity records), inside the same transaction, after the writer is reserved;
   - the in-memory check stays on the loop before the step, and the write re-validates it against the durable rows.

   Either way, the lock order stays single, and an Activity that settles in between cannot make the Turn's status get dropped.

**Recommendation.** Move it to the runtime-work door (`run_sync` through `land_step`), with the three conditions above. Construct the store once rather than per call. The same dispatcher already records Run activity through the door (`_flush_agent_run_activity` → `run_sync`).

## Path B: a durable Turn's terminal write after delivery

**What it does.** The chain is `controller.emit_agent_message` → `SessionTurnManager.on_terminal_delivery_complete` → `_finish_durable_terminal_result` → `_terminalize_durable_turn`. It runs once per durable Turn terminal. One `BEGIN IMMEDIATE` transaction:
1. reconciles the initial batch;
2. writes the terminal snapshot with its evidence (result text, `settles_run`, output provenance);
3. falls back or retires the pending steers;
4. promotes a successor (a linked P0, a start replay, or the FIFO head);
5. writes `agent_status`.

**Boundary.** Yes: `hold_if_current` across the whole transaction. `admitted` gates only the successor promotion ("a non-admitted owner is persisted as queued, never promoted"). The predecessor's terminal write is unconditional. That has two consequences:
- `commit_then_confirm` does not fit as written. B must commit even when it is not admitted, and nothing can undo a promotion.
- B cannot run on a worker as one piece. The off-loop guard from #2662 would report it.

**Orderings a move must keep.**
1. **B is the first terminal writer.** The runner's `finally` terminalizes the same Turn as `runner_release`, without evidence. If the runner got there first, B would become a no-op and the evidence would be lost. A steer accepted late reads that evidence to settle its Run.
2. **Post-terminal resume runs exactly once.** `_finish_durable_terminal_result` spawns the resume only if `in_flight` no longer holds the Turn, and the runner's `_resume_post_terminal` makes the complementary decision. If B ran later, both could resume, or neither.
3. **The Model Hub fence holds.** The runner releases its projection only after Hub finalization, and that relies on the durable row already being terminal.
4. **Event order is preserved.** `session.status` and the inbox update are published before the runner's `turn.end`.

**Recommendation.** Do not move B as a whole. Split the work in two steps:
1. **First, cheap prechecks that remove redundant writer acquisitions on every Turn end** (no boundary change). Today a Turn end takes the writer about four times on the loop, and only B writes in the common case:
   - `_terminalize_durable_turn` returns early when the Turn it reads is already terminal, so the `runner_release` and `on_native_terminal` calls stop taking the writer.
   - `_resume_post_terminal` reads without the writer.
   - `drain_delivery_queue` checks for a claimable head before it takes the writer.
2. **Then, a separate design for B's single remaining write.** That design must keep one more invariant: **5. terminalization and successor selection are atomic with respect to admission.** Once the predecessor is terminal, an admission that sees no live owner may claim or start the FIFO head. So if the terminal CAS ran off the loop and promotion ran later on the loop, a newly admitted Delivery could jump ahead of the linked P0, the start replay or the intended FIFO successor. A runner fence orders only cleanup, not admissions. The design therefore keeps terminalization and successor selection in one transaction, or adds an explicit admission fence that spans both steps, together with the runner fence for orderings 1 and 2. It needs its own review against the tests that pin B's synchronous order. The main ones are in `tests/test_session_delivery_fsm.py`: the late-accepted steer, the Model Hub fence and the reply-less inbox settlement.

## Path C: startup durable-delivery recovery

**What it does.** The chain is `_on_runtime_ready` → `_recover_runtime_owners` → `recover_durable_delivery_state`. It runs once per service start, before producers are admitted: `_delivery_recovery_complete` gates IM inbound, the internal server and restored polls. The 08:09 stall was not a writer wait. It was an unbounded read: the Run attribution for **all** terminal Turns' accepted Deliveries, together with their `metadata_json`, across every session. That read grows with history. On a long-lived install it returns tens of thousands of rows, of which only a handful can still need settlement. In addition:
- two transactions take `BEGIN IMMEDIATE` only to read;
- `project_durable_agent_status` scans all `session_turns` and then updates sessions with `IN (...)`.

**Boundary.** C's own transactions hold none. The functions it calls (terminalize, the admissions, drain) hold it, and they keep B's constraints.

**Recommendation.** Bound the reads; no boundary change is needed:
- Start from the bounded set the recovery actually acts on, then join outward to the terminal Turns that own those Runs. Today it does the reverse: it reads every terminal Turn and filters afterwards. The bounded set is:
  - Runs still `queued` or `running`;
  - Runs whose owed failure notice is still `pending` with no Turn id. This is the legacy case, which the code then matches against the Turn's settlement.

  Terminal Runs without a result are permanent history, so filtering on them would still grow without bound. Pending notices are delivered and leave that state, so the set stays bounded by unresolved work. The query plan is pinned in a test, as `AGENTS.md` requires for growing tables.
- Turn the two read-only `BEGIN IMMEDIATE`s into plain reads.
- Update only the sessions whose projected status differs.

Because producers are gated and the function already yields, moving the remaining reads to the door (`run_sync` works before `activate()`) is optional.

## Other writer sites on the loop (scope only)

The per-Turn admissions all hold the boundary inside the transaction, so they are loop-side boundary holders and fall under B's split analysis:
- `_admit_p0`, `_admit_p1`, `_admit_p3`, `_promote_fifo_head`;
- `drain_delivery_queue`, `_resume_linked_control_successor`.

`on_native_start` and `register_agent_initiated_turn` commit inside `commit_if_current` and must not yield. The remaining sites hold no boundary and are candidates for the door, one at a time:
- per Turn: `_start_persisted_turn`, `_finish_steer`, `_interrupt_durable_turn`, `reconcile_terminal_run_delivery`, `controller.set_agent_status`, `persist_agent_message`;
- per user input: `_delivery_backend`, which writes only on a session's first binding, and `_observe_active_delivery_turn`, which runs per steer, from `_admit_p1` and `_promote_fifo_head`;
- per pending control item: `_run_pending_interrupt` and `_run_pending_steers`.

Each move needs the check this note applies to A. Does a loop section rely on the write being synchronous? A Turn can write its running projection before its owner exists, and #2708 showed that moving such a write to a thread can clear a live Turn's status. Where the answer is yes, the alternative is to stay on the loop and never wait for the writer, as `_repair_ownerless_running_projection` does after #2708: a precheck without the writer, then one attempt at the writer with a zero busy timeout.

## Plan

- [ ] C: bound the recovery reads (start from live Runs and pending legacy notices, with a pinned plan; read-only transactions; mismatch-only status projection).
- [ ] A: record the Turn's Run terminal through the runtime-work door with `land_step`; make the blocking-Activity check without taking the registry lock under the writer; hold a caught cancellation until the whole terminal-delivery tail, including `on_terminal_delivery_complete`, has run; construct the store once.
- [ ] B, step 1: skip the writer for redundant terminalization, the post-terminal read and an empty drain.
- [ ] B, step 2: a design note for B's remaining write that keeps terminalization and successor selection atomic with respect to admission (orderings 1–5).
