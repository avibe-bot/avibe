# Terminal emit: one synchronous Turn commit after the IM surfaces (#2714)

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

The delivered branch retires its status bubble and drops its status state as part of delivery, before its durable steps. `_retire_status_bubble` deletes the bubble where the platform can, and collapses it otherwise.

B runs only after the dispatcher returns. Two things can put the runner first:

1. **Any suspension between the signal and B. This happens on the normal path, not only on cancellation.** The signal resolves the runner's `done.wait()`, and the runner runs at the receiver's next suspension. A probe with the real runner (`SessionTurnManager._run`) confirms it:
   - with no suspension between the signal and B, the Turn ends with `terminal_result` and the evidence;
   - with one `await asyncio.sleep(0)`, it ends with `runner_release` and `{}`.

   On master, `_finish_processing_indicator_turn` always sits in that window. The indicator is tracked at the gate hand-off (`track_turn`). This call suspends whenever that indicator still has anything to clean up: `ProcessingIndicator.finish` cancels and awaits the typing keepalive (the default `ack_mode`), deletes the ack message and removes the reaction. For example, the Claude receiver removes its own reaction only after the result emit returns, so on IM the dispatcher's cleanup is the first one and it suspends. IM Turns therefore lose their evidence routinely. Web Turns track no indicator, do not suspend there, and are not affected.
2. **A cancellation in the IM work after the record** (the case #2714 was opened for). In the first three branches, the collapse and clear awaits come before the signal. A Stop that lands there skips the signal and B, while the Run terminal record has already settled the Run. The runner's cancelled path then terminalizes the Turn as `runner_release`, with no evidence.

Both paths leave a Turn whose snapshot disagrees with its Run. The first also publishes `turn.end` before the terminal `session.status`, which is Path B's ordering 4.

## Decision

**Every IM surface stays where it is. The signal moves out of the IM work. Once the durable steps have succeeded, the emit ends with one synchronous terminal block, after its last IM surface, and the block runs in a `finally`.** The block, in order:
1. the signal: `_signal_turn_complete`, or, for a delivered result, the completion half of `_stream_chunk`, with its token check and settlement rule. Its chunk forward stays where it is;
2. the Activity settlement check, as today. A failure no longer skips the rest of the block: its error is held and raised once the block has run;
3. `_release_runtime_turn`;
4. `on_terminal_delivery_complete`, called by the dispatcher here.

A local flag records that the durable steps succeeded. The dispatcher's existing `finally` runs the processing-indicator cleanup, as today. Then, in an inner `finally`, it runs the block if the flag is set, or else only the release, as today. So the block runs after a normal ending, and also after a cancellation or failure in any IM surface, before the cancellation propagates. The release keeps its place after every IM surface and before B.

The controller keeps its call after the dispatcher returns. That call is a no-op once the pending flag is popped (`_SHOW_CHECKPOINT_TERMINAL_PENDING_KEY`), and it still covers dispatchers that do not reach this block.

Nothing in the block awaits, so neither the runner nor a cancellation can get between the signal and B.

Not chosen:
- **Running the block before the IM surfaces** (this note's first version). After the release, a successor can start while the previous Turn's surfaces are still running. Status state is keyed by the thread's latest trigger message, and a successor without its own trigger, such as a quick-reply Turn (`message_id=None`), shares that key. The previous Turn's collapse and clear would then act on the successor's bubble and state. Avoiding that needs status state detached from its key at the release, which is a new mechanism. Leaving the surfaces before the release needs none.
- **Running the Activity settlement check before the signal**, and not signalling when it fails. Some failures are swallowed by their caller. For example, the Claude receiver logs a delivered Activity batch that is not durable and keeps it fail-closed; nothing else would then signal the Turn, and its runner would wait forever.
- **Making `runner_release` carry the latched evidence.** The latch is set before delivery, so it does not prove the result was delivered. The runner would need a second "delivered" marker, which would make it a second owner of a decision the emit already makes.

## What changes, and what does not

- **For IM surfaces and successors: nothing.** The status bubble collapse or retire, the state clear and the processing indicator all run before the runtime release, as on master. A successor still cannot start before them, and nothing new shares state with it.
- **The signal comes later on IM.** It moves from before the indicator cleanup to after it. The runner, `turn.end` and the queued successor wake after that cleanup's IM calls instead of before them. A successor already waited for them, because the release follows the cleanup on master. Web Turns have no indicator, and their signal does not move in time.
- **A held Activity settlement failure** now leaves the Turn `terminal_result` with this emit's evidence. On master the runner won the race there and left `runner_release` without it. The emit still raises the same error to its caller, so the Activity's own retry is unchanged: the requeue, the completed-Activity flush and the Registry-owned settlement. A backend's follow-up close for that Turn, such as Claude's silent turn-only emit after an undelivered Activity output, finds the Turn terminal and is a no-op. That is what it already is on master, where the runner wins.
- **Successor resume, the Model Hub fence and event order.** B runs while `in_flight` still holds the Turn, so it does not spawn the resume. The runner does, after B: exactly once, Path B's ordering 2. The Model Hub fence (ordering 3) and `session.status` before `turn.end` (ordering 4) now hold on IM as well. Web Turns already had them.

## When an IM step fails or is cancelled

- **A failed IM step** is logged by the step itself, as today: a failed edit, delete or reaction call.
- **A cancellation** interrupts the surface it lands in. The block still runs from the `finally`, and the cancellation then propagates. The remaining surfaces are skipped, as today, and leave the same things behind:
  - the bubble keeps its last running text, though its heartbeat stops because the gate is released;
  - the 👀 reaction stays;
  - the typing keepalive stops at its next tick (`_typing_turn_is_current`).

  The indicator cleanup in the `finally` still runs while the cancellation propagates, as it does on master. The block adds no IM call, and nothing holds a caught cancellation across the network, the rule A follows.
- **A failure in a durable step** (delivery, persist, the Run record) happens before the flag is set. The `finally` then runs only the release, B does not run, and the Turn owner stays live, as today.

## What stays uncancellable

Only the block, which has no await. Its four steps are synchronous. B's transaction is the existing loop-side write; moving it off the loop is Path B step 2.

A cancellation caught during the Run terminal record (A, #2715, the `_store_step` rule) is re-armed for the next suspension. That is the first IM surface, or the indicator cleanup, and the `finally` runs the block after it.

## Test design

- **Regression, failing on master.** Run the real runner with the real dispatcher terminal emit for an IM context whose processing indicator suspends in `finish`. Assert that the Turn's terminal snapshot is `terminal_result` and carries the evidence. One case per branch: silent, duplicate, suppressed, delivered.
- **Cancellation.** Cancel the receiver at each suspension after the durable steps: the collapse, the clear, the chunk forward and the indicator cleanup. Assert that:
  - the snapshot carries the evidence;
  - the cancellation propagates;
  - the runtime is released.
- **Activity settlement failure.** A held settlement failure still raises to the caller, and the Turn ends `terminal_result` with the evidence.
- **Durable-step failure.** The Turn owner stays live, and B does not run.
- **Existing tests that pin the current order, kept green:**
  - `tests/test_controller_dispatch_loop.py`: the checkpoint ends after delivery, and a delivery failure keeps the Turn owner live;
  - `tests/test_message_dispatcher_result_fallback.py`;
  - the finalized-marker guards in `tests/test_message_dispatcher_status_bubble.py`;
  - `tests/test_agent_run_close_after.py`: close-after waits for the runtime-turn release;
  - in `tests/test_claude_agent_sessions.py`, the terminal result before the gate release: the setup-failure case and the receiver-EOF case;
  - in `tests/test_session_delivery_fsm.py`: the late-accepted steer, the Model Hub fence and the reply-less inbox settlement.

## Plan

- [ ] Accept this note.
- [ ] One change across the four terminal branches: move the signal into a synchronous terminal block that runs after the last IM surface, in the dispatcher's `finally`, once the durable steps have succeeded. Hold the Activity settlement error until the block has run. Add the tests above.
