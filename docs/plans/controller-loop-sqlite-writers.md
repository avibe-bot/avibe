# SQLite writers on the controller loop: terminal, record and recovery paths (#2706)

Status: paths A, C and B step 1 shipped. B step 2 is implemented by the
[terminal-completion synchronization design](terminal-completion-synchronization.md):
keep its atomic transaction on the loop, but never wait for SQLite's writer there.

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
- **The second stall is a settings read that saved.** `get_user_settings` persisted a default for every key it had never seen, and every emit reads it (`is_message_type_hidden`), as do the routing reads. On the Avibe platform the key is the Session id, so the first emit of each new Web Session saved the whole settings state on the loop. The host holds 923 such Session rows. A read now returns the defaults and saves nothing, on every platform (#2789, #2791). On IM platforms that save used to persist an enabled channel row, which authorized the channel. The owner chose default-deny: only an explicit enable authorizes a channel.
- **Everything else stayed under 700 ms per write:** `upsert_activity` (610 writes), `upsert_connection` (140), `bind_agent_session_by_id`, and `persist_agent_message` (about 3,500 writes).

**Before and after the fixes** (the same host). The rc2 shutdown summary closes Soak 3. It covers about 12 h, from 05:40 to 17:51, and counted 45926 writes at 33 sites. Both sites the fixes target were still there:

| Site | rc2, about 12 h | gh-v3.2.2rc4, about 16 h |
| --- | --- | --- |
| `drain_delivery_queue <- recover_runtime_delivery_observation` (#2781) | 37551 writes, 220 s total, 2981 ms max | 0 writes |
| `settings_service.save_state <- v2_settings.save` (#2789, #2791) | 4 writes, 6.5 s total, 3530 ms max | 0 writes |
| All sites | 45926 writes at 33 sites | 1375 writes at 26 sites, about 9 s total, 388 ms max |

Without those two sites, rc2's loop writes come to about 60 s over 12 h, and no single write took a second. The largest were:
- `on_native_start`: 334 writes, 10 s, 505 ms max;
- `persist_agent_message`: about 4,100 writes, 14 s;
- `upsert_activity`: 887 writes, 7.2 s.

None of these calls for action unless a stall points at it.

The rc4 column is the summary of 2026-10-18 10:07, after about 16 h on the host from 17:52; neither target site was ever logged as a first-seen site. The leading rc4 sites are:
- `on_native_start`: 79 writes, 2.5 s, 388 ms max;
- `_terminalize_durable_turn`: 79 writes, 1.7 s, 211 ms max;
- `persist_agent_message` across its call sites: about 650 writes, 2.9 s, 378 ms max.

In the same window the watchdog reported one 2.3 s stall, at 22:54. Its stack of the event-loop thread sat in `selectors.select`, so the loop was idle and no loop code was running. It coincides with the moment the service's processes lost DNS and user lookups on the host, which lasted until a restart at 11:03 the next day. There was no `database is locked`.

The per-Turn admissions all hold the boundary inside the transaction, so they are loop-side boundary holders and fall under B's split analysis:
- `_admit_p0`, `_admit_p1`, `_admit_p3`, `_promote_fifo_head`;
- `drain_delivery_queue`, `_resume_linked_control_successor`.

`on_native_start` and `register_agent_initiated_turn` commit inside `commit_if_current` and must not yield. The remaining sites hold no boundary and are candidates for the door, one at a time:
- per Turn: `_start_persisted_turn`, `_finish_steer`, `_interrupt_durable_turn`, `reconcile_terminal_run_delivery`, `controller.set_agent_status`, `persist_agent_message`;
- per user input: `_delivery_backend`, which writes only on a session's first binding, and `_observe_active_delivery_turn`, which runs per steer, from `_admit_p1` and `_promote_fifo_head`;
- per pending control item: `_run_pending_interrupt` and `_run_pending_steers`;
- per silent IM terminal: `persist_silent_terminal`, called from `MessageDispatcher.emit_agent_message`.

The Claude receiver calls `SessionActivityRegistry.start`, `progress` and `complete` synchronously, and their store writes run while the Activity lock and the activation boundary are held (`commit_if_current`). That makes them loop-side boundary holders, in the same class as `on_native_start`.

Each move needs the check this note applies to A. Does a loop section rely on the write being synchronous? A Turn can write its running projection before its owner exists, and #2708 showed that moving such a write to a thread can clear a live Turn's status. Where the answer is yes, the alternative is to stay on the loop and never wait for the writer, as `_repair_ownerless_running_projection` does after #2708: a precheck without the writer, then one attempt at the writer with a zero busy timeout.

## Plan

- [x] C: bound the recovery reads (start from live Runs and pending legacy notices, with a pinned plan; read-only transactions; mismatch-only status projection).
- [x] A: record the Turn's Run terminal through the runtime-work door with `land_step`, keeping the cancellation point it had on the loop; make the blocking-Activity check on the loop and re-read the durable Activity rows inside the write's transaction; build the store off the loop. The IM network tail is #2714.
- [x] B, step 1: skip the writer for redundant terminalization, the post-terminal read and an empty drain (#2718).
- [x] B, step 2: [non-waiting terminal completion](terminal-completion-synchronization.md), with exact-Turn cleanup/admission fences and the original atomic successor selection (orderings 1–5).
