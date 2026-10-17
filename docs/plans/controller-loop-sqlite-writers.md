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
2. **A cancellation caught during the write keeps the cancellation point it had on the loop** (the `_store_step` rule from #2654). It waits for the write to land, the synchronous tail runs (held Activity settlements, close-after arming), and it is delivered at the emitter's next await -- where it landed when the write ran on the loop. The uncancellable unit is the write and that tail, with no network inside.

   The rule first proposed here held the cancellation through the whole terminal emit, up to `on_terminal_delivery_complete`. In every terminal branch, though, IM network awaits (`_collapse_status_bubble`, `_clear_consolidated_state`, `_stream_chunk`, `_finish_processing_indicator_turn`) sit between the Run write and `_signal_turn_complete`, the sink release and that evidence-carrying write. That rule would hold a cancellation across network calls, so the orchestrator chose this one instead. A cancellation that lands at those network awaits can skip the evidence write and let `runner_release` terminalize first. That is the case on master too; it is unchanged here and tracked in #2714.
3. **The blocking-Activity check is made without the Activity registry's lock while SQLite's writer is held.** Activity start and completion hold `SessionActivityRegistry._lock` while their store write waits for SQLite's writer. A worker that reserved the writer and then called `has_blocking_run_activity` would take the two locks in the opposite order, which is a lock inversion. Two placements look possible, and the choice belongs to the implementation:
   - the check reads the durable Activity rows for the Turn's Runs (session Activity records), inside the same transaction, after the writer is reserved;
   - the in-memory check stays on the loop before the step, and the write re-validates it against the durable rows.

   As implemented: the in-memory check stays on the loop, and the transaction adds the Runs that its durable Activity rows still block (`record_turn_terminal_outputs`). The registry persists an Activity before it changes its own state, so its `active` rows are exactly the Activities it holds open. The record therefore follows the order in which the loop committed the Activity events:
   - an Activity that started while the record waited for the writer still defers the Run, and close-after waits for it;
   - an Activity that ended and settled the Run first stays first, with its own status, exactly as when it ended before the result on the loop.

   No settlement is held or replayed. A first version held Activity terminals until the record landed; every round of review then found a new interleaving, which this rule removes at its root.

**Recommendation.** Move it to the runtime-work door (`run_sync` through `land_step`), with the three conditions above. Construct the store once rather than per call. The same dispatcher already records Run activity through the door (`_flush_agent_run_activity` → `run_sync`).

## Path A2: agent message persistence (#2725)

**What it does.** `message_mirror.persist_agent_message` writes every persisted output in one deferred SQLite transaction: results, notices, interim rows, and hosted and tool-call trace rows. It runs on the loop, and so do `persist_silent_terminal` and the few direct callers outside the dispatcher (the message handler, the agent auth service, the Claude receiver). The v3.2.1 upgrade run on a slow disk stalled the loop for about 3 s on three consecutive commits. The wait is the commit itself (`fsync`), not a writer wait: the database was 840 KB.

**Boundary.** None.

**Orderings a move must keep.** These are the #2708 check.
1. **Within one emit**, the emit uses the persisted row: its id feeds delivery evidence and the Run terminal record (`message_id`). Awaiting the persist in place keeps that.
2. **Transcript order is commit order.** A row's `created_at`, the transcript's sort key, is stamped inside the transaction (`messages_service`), and so is the Session's activity rank. On the loop, persists commit in the order the loop issued them. A multi-thread pool would stamp them in whatever order its threads reach SQLite, so two emits for the same Session can swap. That can happen with a receiver's result and a concurrent Activity-output flush, or with an interim row and a notice.
3. **An emit's Run terminal record (A) and its persist commit in the emit's order.** On master both run on the loop with no await between them, so a Run never records an output whose message row is not yet there. Awaiting each step in turn keeps that. What a move gives up is adjacency: another emit's write can commit between one emit's persist and its record, as in A persist → B persist → A record. That does not change what either one commits. The record reads and writes Run rows and reads Activity rows. A persist writes message, Session and event rows. The only Run fields it can read are a harness row's source provenance, which the record never writes. Between the two commits a reader in another process can already see, on master, a message row whose Run is not yet terminal; the move only lets newer rows appear in that window.

**Recommendation: one global ordered writer for agent-message persistence and the Run terminal record.** One thread commits the work in the order the loop submitted it. A's Run terminal record moves from the runtime-work door to this writer. Each caller awaits its own step under the `_store_step` rule, so cancellation points stay where master has them. A cancellation that lands during a terminal persist is answered at the next await, which is the same `_stream_chunk` or IM call that would answer it on master, where the persist cannot be interrupted. The tail that may then be skipped, including `on_terminal_delivery_complete`, is a pre-existing gap of the terminal emit. #2714 tracks it, and its fix covers A and A2 together. The post-commit publishes (`message.new`, `inbox.session.updated`, the activity rank) go out from the loop after the commit lands, in the same order. The writer takes its shape from `_TurnModeWriter` in `modules/agents/model_hub.py`: one daemon thread, one FIFO, one future per write.

- **Why one global thread rather than per-Session ordering on a shared pool.** SQLite admits one writer at a time, so a pool cannot commit two Sessions' rows in parallel. Its threads would queue on the write lock, and one slow `fsync` delays every Session either way. A global writer costs no latency the database does not already impose, and the loop no longer pays it. The global writer also gives a single total order across both write kinds, which per-Session keys cannot: Run records and inbox rows are not cleanly keyed by Session. It also leaves room for group commit later, where several queued writes share one transaction and one `fsync`. That would directly help a slow disk; it is not part of this change.
- **Why A joins it.** With both on one FIFO writer, every write commits in the order it was submitted, and the door's threads do not compete with the writer for SQLite's write lock on every Turn end. Adjacency is not needed (ordering 3), so the persist and the record stay two jobs. A's own guarantees are unchanged: the record re-reads open Activities in its transaction, and the cancellation point is the same.
- **Every agent-message write goes through it.** That is every caller of `persist_agent_message`:
  - the dispatcher's emits;
  - the message handler's terminal error row (`_stream_terminal_error`);
  - the auth service's recovery notice (`maybe_emit_auth_recovery_message`);
  - the Claude receiver's rows in `handle_message`, `_handle_receiver_eof` and `_handle_receiver_exception`.

  `persist_silent_terminal` goes through it too. All of these already run in coroutines, so each awaits its step instead of calling the helper. A caller left on the loop would bring the stall back. It could also commit ahead of an older write for the same Session that is still in the FIFO, such as an Activity-output flush, and swap the transcript. So the helpers become writer-only. A call on the controller's loop thread logs an error that names the caller, as the #2662 guard does for an activation commit off the loop. A test pins it.
- **Human rows stay where they are.** `mirror_inbound` and the `link_inbound_message_session` that follows it still run together on the loop, on the legacy direct path. Durable Deliveries still write theirs when native acceptance is recorded, as `on_native_start` does, inside transactions that must not yield. A human row can therefore commit ahead of a queued agent row. That happens only for a message the user sent before that agent row reached them: an agent row is delivered only after its commit. The two were concurrent, and either order matches what the user saw.
- **What it does not change.** Other writes (the scheduler, Session Turns, Activities) keep their current owners.
- **The loop takes precedence over the writer.** SQLite's write lock has no queue. A write that stays on the loop and arrives during a writer commit waits in SQLite's busy handler. The writer starts its next job microseconds after each commit, well inside the handler's sleep. While the loop waits, nothing new is submitted, so the loop would wait for every write already queued. With two slow commits queued, that passes the 5 s `busy_timeout`, and the write fails with `database is locked`. On master the same write would have run after a frozen loop and succeeded.

  That would hit every write that stays on the loop: the boundary holders (the admissions, `on_native_start`, Activity starts), the door candidates listed below, and the legacy inbound mirror. So precedence is set once, where every engine is built (`storage/db.py`). An engine counts the transactions open on the controller's loop thread, and the writer starts a job only when that count is zero. Loop reads count too; they are short by rule. A loop-side write therefore waits at most for the one commit already in flight, which master made the loop pay on every persist.

  The writer's wait is bounded by the watchdog's 2 s threshold. Past it, the writer proceeds and logs the loop transaction that held it, so a loop transaction held open across an await of the writer delays persistence instead of deadlocking. Writers in other threads and processes get no precedence; they compete with the writer as they compete with the loop's commits today.
- **Failures keep today's contract.** The writer runs the same functions, so a caller gets what a direct call gives it today:
  - `persist_agent_message` still catches a database failure, fills `error_sink` and returns `None`. Its awaiter gets `None` and the filled sink. The interim and process rows have no handler around them, and they stay best-effort.
  - A's record keeps the handling it has after #2715.

  The writer adds isolation on top of that contract. Anything that still escapes a function is set on that write's own future and raised to its awaiter. The thread catches everything a write raises and goes on to the next, so one bad write never stops persistence for anyone else. A test submits a persist whose database write fails, then one that raises past the helper, then one that succeeds. The first awaiter gets `None` with the error in its sink, the second gets the exception, the third gets its row, and the thread is still running.
- **When the queue backs up behind a slow `fsync`.** Every write is submitted by a coroutine that awaits it before doing anything else, and no caller fires and forgets. So the queue holds exactly one write per coroutine that is waiting for one. The writer creates no coroutines of its own. The waiting coroutines are receivers, Activity flushes and notice paths that exist today. On master each of them would be waiting on the same commit, with the loop frozen.

  What changes is that the loop keeps running during the stall. Work that starts during it can add waiters: one per new emitting coroutine, at the rate new Turns, Activities and notices start. The backlog therefore grows with the work admitted during the stall, not with output volume, and it empties as commits land.

  There is no capacity limit. A cap would hold the same coroutines at a second wait point, so it would bound the queue's length but neither the waiters nor their memory. Its wait would also be a new cancellation point before the step is submitted, which the `_store_step` rule would then have to cover. A real bound on the waiters means admitting less work during a stall. That is a product decision outside this change.
- **Evidence when it happens.** The loop watchdog stays quiet, because the loop is free; that is the point of the change. If a watchdog dump fires for another reason, it shows the writer thread in `commit`, not the event loop. To keep a backed-up queue visible, the writer logs one rate-limited WARNING when a write has spent longer than the watchdog's 2 s threshold between submission and commit. The warning gives the queue depth and the oldest wait.
- **Shutdown.** The writer has a close step. After close, it refuses any submission, and the caller gets what the contract already gives a failed write: `None` with the reason in `error_sink` for a persist, and for A's record the failure its caller already handles. So nothing can be queued behind the final drain.

  Close and the final drain must run after the last producer can submit. That rules out the point where `drain_agent_run_activity` runs today, in `_stop_runtime_work_stack`. It comes before the scheduled-task, watch and Model Hub services stop, and the receiver tasks are cancelled only later, in `cleanup_sync`. So the final drain is the last loop step of `cleanup_sync`, after the receivers are cancelled and the IM thread has joined. A cancelled receiver that tries to write after close is refused and logged.

  The drain waits for every accepted write, so their awaiters resolve before the loop closes. Like `drain_turn_mode_stamps`, it waits with a bound. The thread is a daemon, so a commit wedged on a dead disk cannot hold exit, and SQLite rolls back the write it cut off. A Run record lost that way is left for the next start's recovery of interrupted Runs, as a crash leaves it today.

**Test design.**
- A held writer (an `fsync` that does not return) must not hold the loop.
- Two concurrent emits for one Session must keep their transcript order.
- A receiver's direct row (EOF or exception) submitted after a queued emit must commit after it.
- A Turn's Run record must commit after its persisted row.
- Two emits interleaved as A persist → B persist → A record must leave the same rows as the adjacent order.
- With one writer commit held and two more writes queued, a loop-side write must wait only for the held commit.
- A loop transaction held open past the bound must not deadlock the writer, which logs it and proceeds.
- A cancellation during a persist must be answered at the next await, with the row committed.
- A persist whose database write fails must return `None` and fill its sink. An exception that escapes must fail only its own awaiter. The next write must commit.
- A direct call of the helper on the loop thread must log the caller.
- A write held past 2 s must log the queue warning while the loop stays free.
- A submission after close must be refused through the contract. Writes accepted before close must commit before the final drain returns.

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

The inventory is measured, not reviewed by hand: writers are reached through many indirections, and two review rounds each found sites a manual read had missed (#2710). `storage/loop_writers.py` counts every write transaction the controller loop runs, by code location, and logs the ranked list ("Controller-loop SQLite writers") every five minutes while writes arrive and at shutdown. It times each transaction from connection checkout to commit or rollback. That log is the inventory of a run's **transactional** loop writes. It does not see SQLite writes that run outside a transaction: DDL such as a store's table or index setup, and the `PRAGMA journal_mode` a new pooled connection runs before its checkout (#2747). This section records what a soak measured.

**Soak 1** (2026-10-17, master 10f6b8d38). The run used a hermetic home with its state on a USB disk, for about ten minutes, with Vibey Turns, Source adds and a Model Hub engine restart running alongside. At shutdown the census reported 83 writes at 19 sites. The leading sites, by total loop time:

| Site (caller) | Writes | Total | Max |
| --- | --- | --- | --- |
| `_start_persisted_turn` `:4409` (`recover_runtime_delivery_observation`) | 2 | 5867 ms | 3049 ms |
| `backfill_agent_session_title` (`_backfill_session_title_once`) | 5 | 234 ms | 135 ms |
| `_admit_p1` (`deliver`) | 8 | 222 ms | 118 ms |
| `on_native_start` (`commit_native_start`) | 5 | 211 ms | 123 ms |
| `persist_agent_message` (`emit_agent_message` `:3286`) | 5 | 184 ms | 120 ms |
| `persist_agent_message` (`emit_agent_message` `:3387`) | 5 | 77 ms | 19 ms |
| `_terminalize_durable_turn` (`_finish_durable_terminal_result`, path B) | 8 | 72 ms | 35 ms |
| `controller.set_agent_status` (the Turn runner) | 8 | 70 ms | 51 ms |

The remaining eleven sites each took under 60 ms of loop time in total and under 50 ms per write.

What it shows:
- **The only multi-second writes** were `_start_persisted_turn` re-entered by the runtime-work lane's recovery. Each took about 3 s: 2818 ms when first seen, and 3049 ms at its maximum. The loop watchdog's 3.2 s and 4.0 s stalls match them. The census times the whole transaction, so these samples cover reserving the writer, the transaction's reads and its commit together; they do not say which phase took the time. In this run the disk itself was slow: 8 s before the first of these writes, the loop stalled 2.6 s listing the runtime-command directory (#2768), and the same call's next transaction (`:4469`) took 2.9 ms, so the delay was transient. `_start_persisted_turn` holds no boundary, and it is the next candidate in the list below. Only moving the whole transaction off the loop (the door) answers every phase. A never-waiting attempt at the writer answers only a wait for the writer, so choosing it needs the phases measured first.
- **Every other site stayed under 135 ms per write**, on the same disk and under the same load. That includes `persist_agent_message` (#2725, parked until evidence shows a need) and path B's terminal write.
- **Coverage.** The soak exercised the Vibey and Model Hub paths. It did not exercise IM transports, the Claude or Codex receivers, or Harness Runs. Sites that review found and that this soak did not reach stay listed below until a soak measures them.

**Soak 2** (2026-10-17, the owner's host on gh-v3.2.2rc2, about 3 h 10 min of everyday use: Claude, Codex and Vibey Turns, Harness and IM). The 08:50 summary counted 12221 writes at 23 sites. There were no loop stalls of 2 s or more and no `database is locked`. The leading sites, by total loop time:

| Site (caller) | Writes | Total | Max |
| --- | --- | --- | --- |
| `drain_delivery_queue` (`recover_runtime_delivery_observation`) | 9967 | 91.5 s | 506 ms |
| `persist_agent_message` (`emit_agent_message` `:3934`) | 841 | 2.7 s | 403 ms |
| `persist_agent_message` (`emit_agent_message` `:3286`) | 699 | 2.6 s | 482 ms |
| `on_native_start` (`commit_native_start`) | 72 | 1.7 s | 475 ms |
| `upsert_activity` (`_persist_activity`) | 78 | 1.3 s | 489 ms |
| `bind_agent_session_by_id` (`v2_sessions.bind_agent_session_by_id`) | 74 | 1.3 s | 598 ms |
| `_admit_p1` (`deliver`) | 112 | 1.0 s | 396 ms |

The other sixteen sites each took under 1 s of loop time in total.

What it shows:
- **One queued head took 82% of all loop writes.** It had failed to start three times and was held until its owner retried it explicitly. No automatic start can claim such a head. Even so, every runtime-work scan reported it as an open head, and the drain's read-only precheck counted it as claimable. Each scan therefore took SQLite's writer on the loop, the claim refused the head, and the lane backed off for a second and scanned it again, at about 0.9 writes a second. A held head now only moves the scan's cursor, and the drain's precheck no longer counts it as claimable.
- **Every other site averaged under 30 ms per write.** Single writes reached 400–600 ms on the host's disk. `persist_agent_message` (#2725) is the largest of them, at 5.3 s over 1540 writes.

**Soak 3** (2026-10-17, the same host, gh-v3.2.2rc2 from 05:40 to 11:31). The 11:31 summary counted 24199 writes at 30 sites, with no `database is locked`. Two loop stalls, attributed from the watchdog's stack of the event-loop thread:

| Stall | Event-loop thread | Census site |
| --- | --- | --- |
| 10:16:52, 3.5 s | `drain_delivery_queue` -> `reserve_write_lock` (`BEGIN IMMEDIATE`) | 18066 writes, 140 s total, 2981 ms max |
| 10:55:19, 4.2 s | `emit_agent_message` -> `is_message_type_hidden` -> `get_user_settings` -> `SettingsStore.save` | `settings_service.save_state`: 4 writes, 6.5 s total, 3530 ms max |

What it shows:
- **The first stall is the held head** of Soak 2. #2781 fixed it after this build.
- **The second stall is a settings read that saved.** `get_user_settings` persisted a default for every key it had never seen, and every emit reads it (`is_message_type_hidden`), as do the routing reads. On the Avibe platform the key is the Session id, so the first emit of each new Web Session saved the whole settings state on the loop. The host holds 923 such Session rows. An Avibe read now returns the defaults and saves nothing. On IM platforms the same save persists an enabled channel row, which is what authorizes the channel. That is left as it is until the owner decides the rule for channels nobody enabled explicitly.
- **Everything else stayed under 700 ms per write:** `upsert_activity` (610 writes), `upsert_connection` (140), `bind_agent_session_by_id`, and `persist_agent_message` (about 3,500 writes).

The per-Turn admissions all hold the boundary inside the transaction, so they are loop-side boundary holders and fall under B's split analysis:
- `_admit_p0`, `_admit_p1`, `_admit_p3`, `_promote_fifo_head`;
- `drain_delivery_queue`, `_resume_linked_control_successor`.

`on_native_start` and `register_agent_initiated_turn` commit inside `commit_if_current` and must not yield. The remaining sites hold no boundary and are candidates for the door, one at a time:
- per Turn: `_start_persisted_turn`, `_finish_steer`, `_interrupt_durable_turn`, `reconcile_terminal_run_delivery`, `controller.set_agent_status`;
- per user input: `_delivery_backend`, which writes only on a session's first binding, and `_observe_active_delivery_turn`, which runs per steer, from `_admit_p1` and `_promote_fifo_head`;
- per pending control item: `_run_pending_interrupt` and `_run_pending_steers`;
- per legacy IM inbound: `mirror_inbound` and `link_inbound_message_session`, in `MessageHandler._handle_turn`.

The message writers `persist_agent_message` and `persist_silent_terminal` are path A2's (#2725).

The Claude receiver calls `SessionActivityRegistry.start`, `progress` and `complete` synchronously, and their store writes run while the Activity lock and the activation boundary are held (`commit_if_current`). That makes them loop-side boundary holders, in the same class as `on_native_start`.

Each move needs the check this note applies to A. Does a loop section rely on the write being synchronous? A Turn can write its running projection before its owner exists, and #2708 showed that moving such a write to a thread can clear a live Turn's status. Where the answer is yes, the alternative is to stay on the loop and never wait for the writer, as `_repair_ownerless_running_projection` does after #2708: a precheck without the writer, then one attempt at the writer with a zero busy timeout.

## Plan

- [x] C: bound the recovery reads (start from live Runs and pending legacy notices, with a pinned plan; read-only transactions; mismatch-only status projection).
- [x] A: record the Turn's Run terminal through the runtime-work door with `land_step`, keeping the cancellation point it had on the loop; make the blocking-Activity check on the loop and re-read the durable Activity rows inside the write's transaction; build the store off the loop. The IM network tail is #2714.
- [ ] B, step 1: skip the writer for redundant terminalization, the post-terminal read and an empty drain.
- [ ] B, step 2: a design note for B's remaining write that keeps terminalization and successor selection atomic with respect to admission (orderings 1–5).
- [ ] A2: one global ordered writer for every agent-message write and A's Run terminal record, with the contract-preserving failures, the loop-thread guard, loop precedence over the writer and the final drain after the last producer (#2725).
