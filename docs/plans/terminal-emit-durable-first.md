# Terminal emit: the Turn's terminal commit before the IM surfaces (#2714)

Status: design note, proposed. No code yet.

## Background

A terminal result reaches its durable Turn through two writers:
- **The evidence-carrying write ("B").** `Controller.emit_agent_message` calls `SessionTurnManager.on_terminal_delivery_complete` after the dispatcher returns. It terminalizes the Turn with the result's evidence (`result_text`, `settles_run`, output provenance), which `on_terminal_result` latched before delivery.
- **The runner's release.** The runner's `finally` in `SessionTurnManager._run` wakes when the Turn's sink is signalled (`done_event`). It terminalizes the same Turn as `runner_release`, without evidence.

Whichever commits first wins; the other is a no-op. Path B in `docs/plans/controller-loop-sqlite-writers.md` states the ordering this needs: **B is the first terminal writer.** A steer accepted after the Turn settles reads that evidence to settle its Run. `test_terminal_turn_without_positive_result_evidence_keeps_late_run_unsettled` pins what happens without it: the Run stays `running`.

## What happens today

Each terminal branch of `MessageDispatcher.emit_agent_message` runs its durable steps (delivery or persist, and the Run terminal record), then IM surface work, the signal and the runtime release:

| Branch | After the durable steps |
| --- | --- |
| silent or empty result | `_collapse_status_bubble`, `_clear_consolidated_state`, `_signal_turn_complete`, the Activity settlement check; `finally`: `_finish_processing_indicator_turn`, `_release_runtime_turn` |
| duplicate (output already accepted) | the same |
| suppressed outward delivery | the same |
| delivered result | `_stream_chunk` (its completion is the signal), the Activity settlement check; `finally`: `_finish_processing_indicator_turn`, `_release_runtime_turn` |

B runs only after the dispatcher returns. Two things can put the runner first:

1. **Any suspension between the signal and B. This happens on the normal path, not only on cancellation.** The signal resolves the runner's `done.wait()`, and the runner runs at the receiver's next suspension. A probe with the real runner (`SessionTurnManager._run`) confirms it:
   - with no suspension between the signal and B, the Turn ends with `terminal_result` and the evidence;
   - with one `await asyncio.sleep(0)`, it ends with `runner_release` and `{}`.

   On master, `_finish_processing_indicator_turn` always sits in that window. The indicator is tracked at the gate hand-off (`track_turn`). This call suspends whenever that indicator still has anything to clean up: `ProcessingIndicator.finish` cancels and awaits the typing keepalive (the default `ack_mode`), deletes the ack message and removes the reaction. For example, the Claude receiver removes its own reaction only after the result emit returns, so on IM the dispatcher's cleanup is the first one and it suspends. IM Turns therefore lose their evidence routinely. Web Turns track no indicator, do not suspend there, and are not affected.
2. **A cancellation in the IM work after the record** (the case #2714 was opened for). In the first three branches, the collapse and clear awaits come before the signal. A Stop that lands there skips the signal and B, while the Run terminal record has already settled the Run. The runner's cancelled path then terminalizes the Turn as `runner_release`, with no evidence.

Both paths leave a Turn whose snapshot disagrees with its Run. The first also publishes `turn.end` before the terminal `session.status`, which is Path B's ordering 4.

## Decision

**Once a terminal emit's durable steps have succeeded, its terminal commit runs as one synchronous block, before any IM surface work.** The block, in order:
1. the signal: `_signal_turn_complete`, or `_stream_chunk`'s completion for a delivered result;
2. the Activity settlement check, unchanged: if it raises, the rest of the block is skipped, as B is skipped today;
3. `_release_runtime_turn`;
4. `on_terminal_delivery_complete`, called by the dispatcher here.

The controller keeps its call after the dispatcher returns. That call is a no-op once the pending flag is popped (`_SHOW_CHECKPOINT_TERMINAL_PENDING_KEY`), and it still covers dispatchers that do not reach this block.

Then the IM surfaces run in the `finally`, each best-effort as today: the status bubble collapse, the consolidated-state clear and the processing indicator. They use the status key captured before the release (`status_key_for_context`), as `finish_prewrite_stop_surfaces` already does. If the emit raised before the block, the `finally` also performs the release it performs today.

Nothing in the block awaits, so neither the runner nor a cancellation can get between the signal and B.

Not chosen: making `runner_release` carry the latched evidence. The latch is set before delivery, so it does not prove the result was delivered. The runner would need a second "delivered" marker, which would make it a second owner of a decision the emit already makes.

## What the IM side relies on in the current order

After the block, a successor Turn may start while the previous Turn's surfaces are still running. Each dependency was checked:
- **Status key.** `_get_consolidated_message_key` reads the thread's current trigger message id, and the next Turn overwrites it (`update_thread_message_id`). A key computed after the release could collapse or clear the successor's bubble. So the key is captured before the release.
- **The finalized marker and the gate.** `_drop_status_keys` keeps the finalized marker because the gate used to be released after the teardown. After the release, a late same-token process emit fails `_is_current_runtime_turn` and is dropped ("Dropping stale ... emit"). The status heartbeat stops itself on the same check. Releasing first is at least as safe.
- **The next Turn's bubble and indicator.** Both are keyed per Turn: the bubble by its own trigger message, the indicator handle by turn token. Neither reads the previous Turn's state. What the user can see change is ordering on screen: a successor's first output can appear before the previous bubble collapses to ✅.
- **WeChat typing** is the one shared resource. It needs an explicit clear per DM (`typing_indicator_requires_clear`). The previous Turn's clear can cancel a successor's typing that has already started. The successor's keepalive re-sends it within `TYPING_KEEPALIVE_INTERVAL_SECONDS` (5 s). On every other platform, typing expires on its own and the clear is a no-op.
- **Successor resume, the Model Hub fence and event order.** B runs while `in_flight` still holds the Turn, so it does not spawn the resume. The runner does, after B: exactly once, Path B's ordering 2. The Model Hub fence (ordering 3) and `session.status` before `turn.end` (ordering 4) now hold on IM as well. Web Turns already had them.

## When an IM step fails or is cancelled after the block

The durable state is complete by then.
- **A failed IM step** is logged by the step itself, as today: a failed edit, delete or reaction call.
- **A cancellation** lands at the first suspension in the surfaces, and the remaining surfaces are skipped, as they are today. What it can leave behind, and the existing fallbacks:
  - the bubble keeps its last running text, though its heartbeat stops because the gate is released;
  - the 👀 reaction stays;
  - the typing keepalive stops at its next tick (`_typing_turn_is_current`).

  Running the surfaces through a cancellation would mean holding a Stop behind IM network calls, which A rejected. These leftovers are cosmetic and the same as today.

## What stays uncancellable

Only the block, which has no await. Its four steps are synchronous. B's transaction is the existing loop-side write; moving it off the loop is Path B step 2.

A cancellation caught during the Run terminal record (A, #2715, the `_store_step` rule) is re-armed for the next suspension, and that is now after the block. Between the record and the block, a delivered result runs `_stream_chunk`'s forward. Every production sink is `_noop_chunk`, which never suspends. A sink that suspends would reopen the window, and a test pins this.

## Test design

- **Regression, failing on master.** Run the real runner with the real dispatcher terminal emit for an IM context whose processing indicator suspends in `finish`. Assert that the Turn's terminal snapshot is `terminal_result` and carries the evidence. One case per branch: silent, duplicate, suppressed, delivered.
- **Cancellation.** Cancel the receiver at each suspension in the surfaces, and assert:
  - the snapshot carries the evidence;
  - the cancellation propagates;
  - the runtime is released.
- **Key capture.** A successor that updates the thread's trigger id before the surfaces run keeps its own bubble and state.
- **Sinks.** The production sinks used for durable Turns do not suspend in `_stream_chunk`'s forward.
- **Existing tests that pin the current order, kept green:**
  - `tests/test_controller_dispatch_loop.py`: the checkpoint ends after delivery, and a delivery failure keeps the Turn owner live;
  - `tests/test_message_dispatcher_result_fallback.py`;
  - the finalized-marker guards in `tests/test_message_dispatcher_status_bubble.py`;
  - `tests/test_agent_run_close_after.py`: close-after waits for the runtime-turn release;
  - in `tests/test_claude_agent_sessions.py`, the terminal result before the gate release: the setup-failure case and the receiver-EOF case;
  - in `tests/test_session_delivery_fsm.py`: the late-accepted steer, the Model Hub fence and the reply-less inbox settlement.

## Plan

- [ ] Accept this note.
- [ ] One change across the four terminal branches: the synchronous terminal commit after the durable steps, with the surfaces moved to the `finally` behind a captured status key; the regression and cancellation tests above.
