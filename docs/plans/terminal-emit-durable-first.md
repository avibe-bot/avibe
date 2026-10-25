# Terminal emit: one synchronous Turn commit after the IM surfaces (#2714)

Status: implemented (#2714). The runner's fallback order is #2735.

**B step 2 amendment (#2706):** the signal, runtime release and initial B attempt
still run without an await. B now tries SQLite's writer without waiting. On
contention, an exact-Turn pending completion fences cleanup, admissions and late
receipts until the evidence and successor decision land; resultless Run settlement
finishes through the runtime-work door before terminal events. See
[terminal-completion synchronization](terminal-completion-synchronization.md) for
the current retry, cancellation and shutdown contract. The rest of this note
records the original #2714 placement decision; its synchronous B-write assumption
is superseded only by that explicit completion fence.

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

**The emit keeps master's rule for which outcomes complete the Turn and changes only the order. Once an emit has passed its durable steps, it ends with one synchronous terminal block after its last IM surface. The block runs from the dispatcher's `finally`, so a cancellation or failure in an IM surface cannot skip it.**

**Durable steps first.** In the delivered branch, the durable steps move up to directly after the primary delivery (the result send and its fallbacks):
- `finalize_scheduled_delivery`, which is persistent routing state (the scheduled base's alias to the delivered message, and the thread marked active), not an IM surface;
- the Run terminal record;
- the persist.

These read only the send's result, including the anchor its fallbacks set, and `finalize_scheduled_delivery` is synchronous. They read nothing produced by the IM surfaces that currently come between them and the send:
- the delivery notice;
- `_upload_file_links`;
- `_retire_status_bubble`;
- `_drop_status_keys`.

So these IM surfaces follow the durable steps, in their current order. In the other three branches the durable steps already precede every IM surface.

**When the block runs.** A local flag is set once the durable steps have passed. It is set in both cases where master goes on to its signal:
- the steps succeeded;
- a step failed in a way master converts and raises only after its signal. The live case is a Run record that fails after a delivered Activity output, which becomes a held `ActivityOutputDeliveryError`.

A durable-step failure that master raises before its signal leaves the flag unset. Examples are an undelivered Activity output, a Workbench output that was not persisted, and a Run record failure with nothing delivered. The `finally` then runs only the release, B does not run, and the Turn owner stays live, as today.

**What the block runs.** The block takes over master's sequence from the signal onwards, and adds the release and B before the return or raise:
1. the signal: `_signal_turn_complete`, or, for a delivered result, the completion half of `_stream_chunk`, with its token check and settlement rule. Its chunk forward stays an IM-side step before the block;
2. what master runs between its signal and its return: the Activity settlement check, or, when a converted error is held, nothing, since master skips the check then. An error from this step is held;
3. `_release_runtime_turn`;
4. `on_terminal_delivery_complete`, called by the dispatcher here;
5. then the held error, if any, is raised, so the caller sees what it sees on master.

The dispatcher's existing `finally` runs the processing-indicator cleanup, as today. Then, in an inner `finally`, it runs the block if the flag is set, or only the release otherwise. The release keeps its place after every IM surface and before B. Nothing in the block awaits, so neither the runner nor a cancellation can get between the signal and B.

The controller keeps its call after the dispatcher returns. That call is a no-op once the pending flag is popped (`_SHOW_CHECKPOINT_TERMINAL_PENDING_KEY`), and it still covers dispatchers that do not reach this block.

`on_terminal_delivery_complete` first ends the Show checkpoint (`_end_show_checkpoint`). For a Session with a Show page, that runs `git` synchronously on the loop through the event bus. Master already makes this call synchronously on the loop, after the dispatcher returns, so the block keeps it where it is, before B's write, and adds no loop cost. Moving that work off the loop is #2734.

**If B's own write fails.** `_finish_durable_terminal_result` logs and returns when `_terminalize_durable_turn` raises, for example a busy database. The runner's release would then commit without evidence. So the block marks the sink beside its B attempt: it copies the evidence `on_terminal_result` latched on the sink (`sink["terminal_evidence"]`) into the sink's `terminal_commit` field (`TERMINAL_COMMIT_KEY`). Only the block sets that field, so a release that never reached B, such as a backend's no-result release, hands over nothing. A bound Stop that won the sink's settlement is covered too (#2735 item 3): if the block ran before `dispatch_turn` pops the sink, the field says so. If the runner got there first, the Stop won outright: the runner commits it, and B then finds the Turn terminal. In its `finally`, before it pops the sink, `dispatch_turn` moves the field, with the sink's settlement, onto the dispatch context the runner holds. So it also survives a cancellation, where `dispatch_turn` returns no outcome. The runner's release commits it on every path, the cancelled one included, exactly as B would have: the same outcome, settlement and `terminal_result` evidence. When B landed, the Turn is already terminal and the runner's write is a no-op.

Not chosen:
- **Running the block before the IM surfaces** (this note's first version). After the release, a successor can start while the previous Turn's surfaces are still running. Status state is keyed by the thread's latest trigger message, and a successor without its own trigger, such as a quick-reply Turn (`message_id=None`), shares that key. The previous Turn's collapse and clear would then act on the successor's bubble and state. Avoiding that needs status state detached from its key at the release, which is a new mechanism. Leaving the surfaces before the release needs none.
- **Running the Activity settlement check before the signal**, and not signalling when it fails. Some failures are swallowed by their caller. For example, the Claude receiver logs a delivered Activity batch that is not durable and keeps it fail-closed; nothing else would then signal the Turn, and its runner would wait forever.
- **Making `runner_release` carry the latched evidence unconditionally**, instead of the block. The latch is set before delivery, so on its own it does not prove the emit got that far. It also does not fix the cancellation case: a Stop that lands before the signal takes the runner's cancelled path. The fallback above therefore depends on the block's own mark.

## What changes, and what does not

- **For IM surfaces and successors: nothing.** The status bubble collapse or retire, the state clear and the processing indicator all run before the runtime release, as on master. A successor still cannot start before them, and nothing new shares state with it.
- **The delivered branch writes its durable steps earlier.** It writes them before its delivery notice, file uploads and bubble retire instead of after them. A failure or cancellation in those surfaces no longer leaves a delivered result without its message row and Run record.
- **The signal comes later on IM.** It moves from before the indicator cleanup to after it. The runner, `turn.end` and the queued successor wake after that cleanup's IM calls instead of before them. A successor already waited for them, because the release follows the cleanup on master. Web Turns have no indicator, and their signal does not move in time.
- **A held Activity error** (a settlement failure, or a Run record failure after a delivered Activity output) now leaves the Turn `terminal_result` with this emit's evidence. On master the runner won the race there and left `runner_release` without it. The emit still raises the same error to its caller, so the Activity's own retry is unchanged: the requeue, the completed-Activity flush and the Registry-owned settlement. A backend's follow-up close for that Turn, such as Claude's silent turn-only emit after an undelivered Activity output, finds the Turn terminal and is a no-op. That is what it already is on master, where the runner wins.
- **Successor resume, the Model Hub fence and event order.** B runs while `in_flight` still holds the Turn, so it does not spawn the resume. The runner does, after B: exactly once, Path B's ordering 2. The Model Hub fence (ordering 3) and `session.status` before `turn.end` (ordering 4) now hold on IM as well whenever B commits. Web Turns already had them. On the fallback path, where B's write failed and the runner's write commits, the runner still publishes `turn.end` before that write, as it does on master; #2735 tracks that order.

## When an IM step fails or is cancelled

- **A failed IM step** is logged by the step itself, as today: a failed edit, delete or reaction call.
- **A cancellation after the durable steps** interrupts the surface it lands in: the notice, a file upload, the bubble retire or collapse, the state clear, the chunk forward or the indicator cleanup. The block still runs from the `finally`, and the cancellation then propagates. The remaining surfaces are skipped, as today, and leave the same things behind:
  - the bubble keeps its last running text, though its heartbeat stops because the gate is released;
  - the 👀 reaction stays;
  - the typing keepalive stops at its next tick (`_typing_turn_is_current`).

  The indicator cleanup in the `finally` still runs while the cancellation propagates, as it does on master. The block adds no IM call, and nothing holds a caught cancellation across the network, the rule A follows.
- **A cancellation during the primary delivery** comes before the durable steps. The flag stays unset and the runner's cancelled path settles the Turn, as on master. A split or fallback send that was cut off part-way may have shown the user part of the result. That is the existing behaviour of a Stop during delivery, and it is not changed here.

## What stays uncancellable

Only the block, which has no await. Its steps are synchronous. B's transaction is the existing loop-side write; moving it off the loop is Path B step 2.

A cancellation caught during the Run terminal record (A, #2715, the `_store_step` rule) is re-armed for the next suspension. The persist is synchronous, so that suspension comes after the flag is set: the first IM surface, or the indicator cleanup. The `finally` then runs the block.

## Test design

- **Regression, failing on master.** Run the real runner with the real dispatcher terminal emit for an IM context whose processing indicator suspends in `finish`. Assert that the Turn's terminal snapshot is `terminal_result` and carries the evidence. One case per branch: silent, duplicate, suppressed, delivered.
- **Cancellation.** Cancel the receiver at each suspension after the durable steps, and assert that the snapshot carries the evidence, the cancellation propagates and the runtime is released. The suspensions are:
  - the delivery notice, the file uploads, the bubble retire and the state drop in the delivered branch, where the scheduled anchor is also already finalized;
  - the collapse and the clear in the other branches;
  - the chunk forward and the indicator cleanup.
- **Held Activity errors.** For a settlement failure, and for a Run record failure after a delivered Activity output, the emit still raises the same error to its caller, and the Turn ends `terminal_result` with the evidence.
- **Durable-step failure raised before the signal.** For an undelivered Activity output and for an unpersisted Workbench output, the Turn owner stays live and B does not run.
- **The runner's evidence fallback.** Three cases:
  - make `_terminalize_durable_turn` raise once for B: the runner's release commits the same evidence B would have;
  - let B succeed: the runner's write is a no-op, and the snapshot keeps B's `terminal_result`;
  - a release that never reached the block: the runner's write commits no evidence;
  - a Stop propagating from an IM surface while B's write fails: the runner's cancelled path commits B's evidence.
- **Existing tests that pin the current order, kept green:**
  - `tests/test_controller_dispatch_loop.py`: the checkpoint ends after delivery, and a delivery failure keeps the Turn owner live;
  - `tests/test_message_dispatcher_result_fallback.py`;
  - the finalized-marker guards in `tests/test_message_dispatcher_status_bubble.py`;
  - `tests/test_agent_run_close_after.py`: close-after waits for the runtime-turn release;
  - in `tests/test_claude_agent_sessions.py`, the terminal result before the gate release: the setup-failure case and the receiver-EOF case;
  - in `tests/test_session_delivery_fsm.py`: the late-accepted steer, the Model Hub fence and the reply-less inbox settlement.

## Plan

- [x] Accept this note.
- [x] One change across the four terminal branches:
  - in the delivered branch, the durable steps, `finalize_scheduled_delivery` included, directly after the primary delivery;
  - a synchronous terminal block that takes over master's signal-to-raise sequence and adds the release and B, run from the dispatcher's `finally` once the durable steps have passed;
  - the runner's release carrying the evidence the block marked beside its B attempt, handed over in `dispatch_turn`'s `finally` so that the cancelled path has it too;
  - the tests above.
