# Vibey continues its own work across Avibe restarts

## Background

A Vibey Turn cut by an Avibe restart ends today. Recovery settles its tool calls
(T2), settles the Turn as interrupted (T4: "the agent never continues it on its
own"), and shows a failure notice. On the Web the notice offers Retry, which
resumes the loop from the committed transcript through a hidden
`agent_initiated` Delivery (`backend-failure-retry.md`). On IM, and whenever T2
handed running commands to Watches, the user has to type a continuation or the
work stays stopped.

Between 2026-10-11 and 2026-10-18 the owner's install had 18 Vibey Turns cut
this way: 11 Harness-owned Turns settled at shutdown (`restarted` /
`service_shutdown`), and 7 conversation Turns found at startup
(`no_terminal_result` / `restart_runtime_missing`). One of the latter came from
a managed `vibe restart` (shutdown intent `restart`), so the evidence kind does
not tell a controlled restart from a crash.

## Owner decisions (2026-10-18)

- Every restart is continued automatically: `vibe restart`, upgrade, stop and
  start, Desktop quit and reopen, host reboot, and a crash alike. Nothing on
  the startup side can tell them apart reliably; the loop bound below covers a
  restart the Turn itself causes.
- Harness-owned Turns (Agent Runs, scheduled Tasks, Watch follow-ups) continue
  too, and their Runs settle from the continued work, so a caller's callback
  reports the real result instead of a restart failure.
- No time limit: a Turn interrupted by a long downtime continues at the next
  start. The user can Stop it like any other Turn.
- No failure notice and no interruption copy for a continued Turn.

This replaces recovery invariant T4 for Vibey (`agent-core-contracts/recovery.md`).

## Mechanism: the cut Turn reruns in place

The first implementation handed a cut Turn's work to a new, hidden
continuation Turn and settled the cut Turn. Five reviewed heads in a row
raised findings at the seams of that handover, so the mechanism changed: the
cut Turn keeps its identity. It stays `active` with its accepted start;
startup recovery rotates it to this process's runtime generation and reruns
its Vibey loop with `Agent.resume` over its own accepted inputs. No Delivery
is created, no Run moves, and no successor Turn starts. A Turn whose final
response was committed was at first completed from that row by recovery,
which rebuilt the end of a live Turn piece by piece; six reviewed heads raised
findings there, so that Turn reruns too, and its own live end finishes it
(item 8).

### Inventory of the seams raised on the successor design

The acceptance list for the in-place rerun. "Removed" means the seam no longer
exists; "handled" names where the in-place design still owns it.

| Seam | In-place rerun |
| --- | --- |
| Runs move to the continuation (one Delivery per Run, `agent_runs.delivery_id` unique, batch claim) | Removed. Runs stay on the Turn's own Deliveries; the rerun's terminal settles them like any Turn's. |
| Steers the dead run accepted but never materialized; a steer still unsettled at startup | Handled. Recovery reconciles the Turn's steer receipts into the same Turn before it reruns, and the rerun pins them with the Turn's inputs. While one is unsettled the Turn waits open and recovery retries; the retry only recovers again, so it never hands a command over early. |
| FIFO tie-break; a requeued continuation overtaken by same-second input | Removed. The Turn stays active, so queued input waits behind it as behind any active Turn. |
| Job-owner ancestry (an open call owned by a settled source; a replacement successor) | Removed. The open call's Turn is the rerunning Turn: T2 defers while it is open and ends the command once a Stop or replacement canceled it. |
| Stop or replacement during the rerun's T2 wait (pre-write cancellation instead of `handle_stop`) | Removed. The Turn is durably active, so a Stop or replacement takes the active-Turn interrupt to `handle_stop`, which ends the wait and the command. |
| A continuation requeued, then retired (backend change) or left unclaimed, after its source settled | Removed. There is no continuation Delivery. A rerun that fails ends its Turn through the ordinary runner, with the failure notice and Retry. |
| A Model Hub refusal at the continuation's preflight | Handled. The rerun is the accepted Turn continuing, so the refusal fails that Turn with its notice and Retry, as a refusal at any later model call of a live Turn does; the work stays in the transcript. |
| Another restart during the T2 wait | Handled. The Turn is still accepted and active, so the next start reruns it again, within the loop bound; its startup T2 leaves the job to it. |
| A committed final not yet delivered to IM; one already delivered | Handled by the live end of the Turn. The rerun ends with the committed row through the ordinary result emit (item 8); the dispatcher sends a final without delivery evidence once and does not resend one with it. |
| The orphaned 👀 after a committed final | Handled by the live end of the Turn: the rerun's indicator retires the 👀 the dead process may have left, as for any rerun. |
| The silent-final promotion (off the controller loop; a failed write) | Handled by the live end of the Turn: the dispatcher promotes as live, reading the Turn's earlier visible text from its rows when the process that showed it died. |
| A committed final across another restart; a Stop arriving meanwhile; the successor's start | Handled by the live end of the Turn. The Turn stays active until its rerun ends it, so another restart reruns it again (within the loop bound), a Stop in the new process loses to the final as live, and the successor starts from the ordinary terminal. |
| A rerun started before its IM transport can deliver | Handled. Recovery holds the rerun until the Turn's surface can deliver, as it holds the restart notice; the transport's ready callback starts it. |
| Retry-loop failures; a Stop while the Turn waits open | Handled. One retry per Turn that survives a failed pass and ends when the Turn reruns or settles; a Stop that settles a waiting Turn has T2 end its command. |
| Continuation admission recheck | Removed with the continuation. |
| Failed silent final | Handled. A failed final keeps its own error and promotes nothing. |
| Query plans of reads on the controller loop | Handled. Every read restart recovery performs is pinned to a bounded index `SEARCH`. |

## Contract

1. **Who is continued.** At startup, recovery continues every Vibey Turn whose
   accepted work a restart cut, whichever writer recorded the cut, when all of
   these hold:
   - the Session is active and not system-visibility, and its Agent still runs
     on the `vibey` backend, as does the Agent that ran the Turn (another
     Agent never continues its work);
   - the Turn's start was accepted (`start_receipt_outcome = accepted`), and no
     stop was asked of it (`stop_requested`: a Stop or replacement control, or
     a cancel of one of its live Runs), and no Run it started for has ended;
   - the loop bound is not reached (item 6).
   A Turn whose final response was committed qualifies by the same rules; its
   rerun ends with that final (item 8).
   Anything else keeps today's behavior: the Turn settles as interrupted, its
   Runs settle without result, and the user gets today's notice.
2. **What continues.** The same Turn. It stays `active` with its accepted
   start; recovery clears its runtime generation (the dead process's runtime
   and native ids) and records the restart streak in one writer transaction,
   fenced on the Turn as the decision read it (a concurrent recovery pass that
   rotated it first leaves this one nothing to release), then reruns the Turn through the ordinary runner with `vibey_resume`, once
   the Turn's surface can deliver (its transport ready: recovery runs before
   external transports connect, so the rerun waits as the restart notice does,
   and that transport's ready callback starts it), with
   the request it was accepted with, while its Vibe Agent still runs on Vibey:
   its native start records that request (the Agent's id and name, the
   effective model and reasoning effort, and the Agent's instructions) on the
   start receipt, and the rerun finds the Agent by id, which survives a rename,
   and runs with the recorded settings, which the message handler puts on the
   rerun's request before the shared model validation. A Session's switch to another Agent, or
   an edit of the Agent's settings, reaches the next Turn, as it does for a
   live one. What a run reads from the machine when it starts (project
   instructions, skills, the environment) is read again, as for any run. A Turn
   an older release accepted names its Agent only as the author of its
   committed responses, and runs with that Agent's current settings; one cut
   before any response names none and keeps the notice. Vibey resumes its committed
   transcript (`Agent.resume`, no input consumed, no visible `continue`, no
   replay of a committed tool call), and the rerun's native start binds this
   process's generation where any run's does, once its Agent exists, so steers
   held for the Turn are dispatched when they can join it. The Turn's accepted inputs,
   steers included, are pinned as for Retry. A steer whose acceptance receipt
   the dead process recorded but did not materialize is reconciled into the
   Turn first; while any steer into a Turn that would rerun is still
   unsettled, the Turn stays open and recovery retries before it reruns. A
   Turn that no longer qualifies settles at once, and the steer then settles
   against it, as before this change.
3. **Tool calls.** A rerun treats a job that survived the restart as its live
   call would. Startup's T2 pass leaves a running job of a Turn still open to
   the Turn owner; the rerun's own T2, before its first projection, waits for
   it until it exits, until the call's foreground window (`bash`'s, 120 s by
   default) passes, counted from the job's launch (`meta.created_at`), or until
   a Stop, which ends the command as in a live Turn: the Turn is durably
   active, so a Stop reaches the adapter's own Stop during the wait. A
   cancellation outside a shutdown (the backend disabled or refreshed) ends the
   command too, also before the adapter took it, and a rerun that fails before
   its adapter has its command handed to a Watch, as a failed run's is; a
   shutdown leaves it for the
   next start's rerun. An exited
   job gives its real output; only a job still running then is
   handed to a Watch, with the ordinary handover result; a window the downtime
   already spent hands over at once. A handover that fails is retried once,
   and with no Watch to take it the job stays in the foreground until it ends,
   as `bash` keeps it in a live Turn. Nothing runs twice: the wait commits and
   starts nothing, and a committed result or handover is reused. A Turn that
   falls back to the notice gets today's T2 first (the Turn owner asks Vibey's
   recovery owner to settle that Session), so its notice still names its
   Watches and offers no Retry. While no Watch can take its command yet, that
   Turn stays open and its owner retries (T2 before T4); a Stop still settles
   it, and then T2 ends the command at once, since settling a Turn open for
   a restart retry (also one held for its transport or a steer) wakes the
   retry (a command whose Turn was canceled is ended, as the Stop would have ended it live, never handed to a Watch, and
   so is one whose Turn holds a stop the dead process recorded and never
   settled, by the same `stop_requested` rule that keeps it from rerunning).
4. **Runs follow the work.** Runs never leave the Turn: the rerun settles them
   with its own outcome and output, and callbacks fire once, from that outcome.
   At shutdown, `release_for_service_shutdown` and the scheduled-task teardown
   leave a Vibey Turn and its Runs unsettled, and a Vibey run that fails
   because the service is stopping (in a model call, or in a rerun's preflight
   as the Model Hub stops) leaves its Turn open, so startup finds them as lost
   active Turns like a conversation Turn.
5. **Ordering.** The Turn stays the Session's active Turn, so any input queued
   while the service was down waits behind it, and may be steered into it by
   the existing steer path.
6. **Loop bound.** The Turn's start receipt carries its restart streak: the
   restarts in a row that cut it with no committed model response in between.
   A committed response resets the streak, and a restart counts once however
   often its recovery is retried (a failure setting the rerun up keeps the Turn
   open for a retry). The third restart in a row falls back to item 1's
   notice, which keeps Retry.
7. **Presentation.** No notice, no ⚠️ reaction, no interruption copy, and no
   second Harness prompt echo (the rerun dispatches no prompt). The session
   shows the rerunning Turn as running.
   The fallback notices of item 1 point to Retry where the notice shows it, or
   to sending `continue`, instead of asking for a resend (copy fix in both
   languages). A rerun's processing indicator runs as any Turn's does, and its
   end also retires the 👀 the dead process may have left on its IM message
   (there may be none, so a failed removal withholds no receipt).
8. **A committed final.** A rerun whose Turn already holds its final response
   (the loop's own finality rule, read after the rerun's T2 and T3) makes no
   model call: it ends where the run that died would have, through `_settle`
   and the ordinary result emit with that committed row. Delivery, a silent
   final's promotion, the processing indicator, Run settlement, and the
   successor all run in the live code. A final that carries delivery evidence
   for its target is not sent again: the dispatcher writes a committed row's
   display only as its delivery (on the Web that write is the delivery; on IM
   it follows a send that returned an id), so a row written for this platform
   is its receipt, as a native id is for an inserted row. A final without it is
   sent once, with the images the dead run generated, which its committed
   display carries. A silent final promotes the Turn's earlier visible text,
   read from the Turn's rows because the process that showed it died. A Stop
   arriving in the new process loses to the final, as it does live.

## Ownership and touch points

- `core/session_turns.py`: `recover_durable_delivery_state` reconciles a lost
  Vibey Turn's steer receipts (`_recover_steer_attempts`), then
  `_recover_restart_cut_vibey_turn` reruns it in place (`_rerun_turn`, which
  shares the dispatch preparation of `_start_persisted_turn`), holds it until
  its transport can deliver (`notify_transport_ready` starts it), keeps it open
  for a retry, or leaves it to today's settlement. `_retry_restart_recovery`
  recovers a waiting Turn again. `release_for_service_shutdown` skips a Vibey
  Turn nobody asked to stop.
- `core/message_dispatcher.py` with `core/message_mirror.py`: item 8's delivery
  evidence (`delivered_committed_row`) and a rerun's silent-final result
  (`resumed_turn_text`), both bounded reads on the controller loop.
- `core/restart_resume.py`: `restart_resume_plan` (item 1, the steer wait, and
  item 6's streak).
- `storage/message_deliveries.py`: `rotate_restart_generation` (the writer
  transaction of item 2) and `turn_has_unresolved_steer`; `bind_native_start`
  keeps the restart streak when the rerun binds its generation.
- `storage/agent_transcript.py`: `last_response` and `committed_final`, bounded
  reads of the context tail, and `last_turn_text`, the silent final's result,
  bounded by the Turn's start. A final follows the loop's own rule: no tool
  calls, not the `error`/`aborted` partial a failed stream commits, and no
  input after it.
- `modules/agents/vibey/agent.py`: a rerun takes a committed final
  (`_take_committed_final`) and settles from it.
- `modules/agents/vibey/agent.py`, `recovery.py`, `tools.py` and agent-core
  `settle_open_calls` / `bash.settle_in_foreground`: item 3's T2 split
  (startup defers a running job of an open Turn; the rerun's T2 waits; T2 ends
  the command of a canceled Turn; `VibeyRecovery.settle_session` hands over
  before a fallback notice). Stopping the service stops the Model Hub gateway
  before Turns end, so a run in a model call fails during shutdown; a run that
  ends without completing while the service is stopping leaves its Turn open
  instead of settling it.
- `core/scheduled_tasks.py`: no change needed. Its shutdown cancellation only
  covers executions still in flight, and a Turn-owned Run has already handed
  its execution to the Turn; `recover_processing` skips Delivery-linked Runs.
- Docs: `agent-core-contracts/recovery.md` T4, `backend-failure-retry.md`.
  Public docs (`avibe-docs` `agents/vibey.mdx` Restart bullet, both languages)
  change in a follow-up in that repository.

## Validation

Scenario IDs in `tests/scenarios/message_delivery/catalog.yaml`:

- `MESSAGE-DELIVERY-327`: startup recovery with a cut conversation Turn: the
  same Turn reruns as a transcript resume, no Delivery, no notice, once its
  transport can deliver.
- `MESSAGE-DELIVERY-328`: cut Harness Turn: shutdown leaves the Turn and its
  Run; the rerun settles the Run from its own result, no owed restart notice;
  a Run steered into the Turn whose receipt was never materialized settles the
  same way, after a retry when startup could not materialize it.
- `MESSAGE-DELIVERY-329`: ineligible cases each fall back to today's settlement
  and notice: Stop requested, archived Session, Agent switched off Vibey, the
  third restart in a row without a model response (a committed response
  resets the streak). A start never accepted stays on the unknown-start path
  it takes today.
- `MESSAGE-DELIVERY-330`: a command running at the restart: startup leaves it
  to the rerun, also across another restart, which gives a job exiting inside
  its window (from launch) its real output, hands over one whose window the
  downtime spent, and ends it on a Stop; a committed handover is reused; the
  command never starts again.
- `MESSAGE-DELIVERY-331`: input queued during downtime runs after the rerun.
- `MESSAGE-DELIVERY-332`: a run that fails because the service is stopping
  leaves its Turn open.
- `MESSAGE-DELIVERY-333`: a committed final: the Turn reruns, also across
  another restart, and the rerun ends with it and no model call; a final not
  yet delivered to IM is sent once, with its images; one already delivered is
  not resent; a silent final promotes the narration the dead process showed.
- `MESSAGE-DELIVERY-322` keeps the Watch-naming notice without Retry for the
  fallback, and a Stop while it waits for that Watch ends the command.
- Local Incus: restart the master environment during a long Vibey conversation
  Turn and during a Vibey Agent Run with a callback; both continue without a
  notice, and the callback reports the continued result.

## Known by design

- The response being generated at the restart is generated again: its partial
  output was never committed.
- A `write` or `edit` cut mid-call gets the interrupted result and the model is
  told to re-read the file, as after any interruption.
- While the rerun waits for a surviving command it shows as running without
  new activity, as a live foreground command does; it starts its model call
  after the wait. A Stop then ends the command and the Turn before any model
  call.
- The restart path is at-least-once for a committed final: one the dead process
  sent to IM just before the dispatcher recorded its delivery is sent again by
  the rerun. A rare duplicate is the accepted cost; a silently lost answer is
  not.
- A Stop requested before the restart keeps the notice even when the final was
  committed (item 1 applies to every Turn); the answer stays in the
  transcript.
- A rerun has a live Turn's reliability, and no durable retry beyond it. A
  transient failure in its run or its end is handled as in a live Turn:
  - removing the 👀 the dead process left is one attempt, like a live Turn's
    own removal;
  - a failed read or write of a silent final's promotion is logged and the
    Turn settles, as a live promotion's failure is (a silent final's Run
    result is empty either way);
  - a failure in the rerun's own T2 or T3 before its run fails the Turn with
    the notice and Retry, as at the start of any live run. A command whose
    result was not committed keeps its job's output for the next resume.
- A Turn whose initial Run already settled from its result, in the moment
  before the Turn's own terminal write, cannot rerun (the runner refuses a
  Turn whose initial Run ended). It keeps today's restart settlement, as on
  every backend: the Run keeps its result and owes no notice, and the Turn is
  recorded as interrupted.
- A rerun whose transport never becomes ready stays open, as a scheduled Run
  waits for its transport; a Stop ends it.
- A rerun's final that cites sources is displayed again from its message, so
  images the dead run generated are not attached to it; they stay in the
  Turn's tool results.
- A Stop is checked before each handover of a surviving command, as the live
  `bash` call checks it; one that arrives while a handover succeeds leaves the
  command with its Watch, as it does live, and the Watch can be stopped.
- Native backends (Claude Code, Codex, OpenCode) keep today's behavior.
