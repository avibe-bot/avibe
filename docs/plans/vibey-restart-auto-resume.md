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

## Contract

1. **Who is continued.** At startup, recovery continues every Vibey Turn whose
   accepted work a restart cut, whichever writer recorded the cut, when all of
   these hold:
   - the Session is active and not system-visibility, and its Agent still runs
     on the `vibey` backend;
   - the Turn's start was accepted (`start_receipt_outcome = accepted`), and no
     Stop or replacement control was requested for it;
   - the Turn's transcript has work left: its last committed response has tool
     calls (T2 has just given each one a result), or the run had not committed
     a final response. A Turn whose final response was committed is settled as
     completed (`terminal_result`), with no continuation and no notice, before
     any of the other conditions apply: a Stop or a Session change after the
     final loses the race, as it does in a live Turn. Its result is that final,
     or, for a successful silent final, the Turn's last visible text, promoted
     to the result as the live dispatcher does; a failed final keeps its error;
   - the continuation bound is not exhausted (item 6).
   Anything else keeps today's behavior: the Turn settles as interrupted, its
   Runs settle without result, and the user gets today's notice.
2. **What continues.** The same hidden `agent_initiated` Delivery the Web Retry
   creates (`vibey_resume`, `vibey_source_turn_id` = the cut Turn), admitted
   through the ordinary P3 path, with server-owned history binding kind
   `restart_resume` instead of a notice. Vibey resumes its committed transcript;
   no input row, no visible `continue`, no replay of a committed tool call.
   Accepted steers of the cut Turn and its retry ancestry are pinned as for
   Retry. A steer whose acceptance receipt the dead process recorded but did
   not materialize is reconciled into the cut Turn first, so it joins the Turn
   and its Run moves with the rest (item 4). While any steer into the Turn is
   still unsettled (its receipt could not be materialized yet), the Turn stays
   open and its owner retries, as for item 3's handover, before it completes
   or continues.
3. **Tool calls.** A continued Turn treats a job that survived the restart as
   its live call would. Startup's T2 pass leaves a running job of a Turn still
   unsettled to the Turn owner; the continuation's own T2, before its first
   projection, waits for it until it exits, until the call's foreground window
   (`bash`'s, 120 s by default) passes, counted from the job's launch
   (`meta.created_at`), or until a Stop, which ends the command as in a live
   Turn. The continuation writes no input, so it is accepted before it waits:
   a Stop reaches the adapter's own Stop, and another restart during the wait
   finds an accepted Turn to continue (within item 6's bound). Startup leaves
   the job to the continuation even then: an open call whose Turn was
   `restart_resumed` belongs to the Session's newest Turn while that is
   unsettled. An exited job gives its real output; only a job still running then is
   handed to a Watch, with the ordinary handover result; a window the downtime
   already spent hands over at once. A handover that fails is retried once, and
   with no Watch to take it the job stays in the foreground until it ends, as
   `bash` keeps it in a live Turn. So within the window the continuation, and
   any Run it settles, see the command's real result, exactly as without a
   restart. Nothing runs twice: the wait commits and starts nothing, and a
   committed result or handover is reused. A Turn that falls back to the notice
   gets today's T2 first (the Turn owner asks Vibey's recovery owner to settle
   that Session), so its notice still names its Watches and offers no Retry.
   That rule stays for the manual notice only. While no Watch can take its
   command yet, that Turn stays open and its owner retries (T2 before T4): no
   notice without the Watch, and no Run failed under a running command; a Stop
   still settles it, and then T2 ends the command (a command whose Turn was
   canceled is ended, as the Stop would have ended it live, never handed to a
   Watch).
4. **Runs follow the work.** The cut Turn settles with
   `settled_by = restart_resumed`, which is not a settlement without result, so
   none of its Runs is failed or owes a notice. The Runs' Delivery link moves to
   the continuation in the same writer transaction (one hidden Delivery per
   Run, since a Run belongs to one Delivery, claimed as one batch), so the continued
   Turn settles them with its own outcome and output, and callbacks fire once,
   from that outcome. At shutdown, `release_for_service_shutdown` and the
   scheduled-task teardown leave a Vibey Turn and its Runs unsettled, and a
   Vibey run that fails because the service is stopping leaves its Turn open, so
   startup finds them as lost active Turns like a conversation Turn.
5. **Ordering.** The continuation is admitted before any input queued while the
   service was down; that input waits behind it, as behind any active Turn, and
   may be steered into it by the existing steer path.
6. **Loop bound.** A Turn is not continued when it is the third consecutive
   `restart_resume` in its ancestry without a committed model response in
   between. Such a Turn falls back to item 1's notice, which keeps Retry.
7. **Presentation.** No notice, no ⚠️ reaction, no interruption copy. The
   session shows the continued Turn as running. The fallback notices of item 1
   point to Retry where the notice shows it, or to sending `continue`, instead
   of asking for a resend (copy fix in both languages). A Turn completed from its
   committed final loses the 👀 the dead process left on its IM message, once
   that transport can deliver.

## Ownership and touch points

- `core/session_turns.py`: `recover_durable_delivery_state` reconciles the
  cut Turn's steer receipts (`_recover_steer_attempts`), then asks
  `_settle_restart_cut_vibey_turn` before today's settlement of a lost Vibey
  Turn. `_terminalize_durable_turn(restart_resume=True)` reserves the
  continuation, moves the Runs, writes the cut Turn's terminal row
  (`completed` / `restart_resumed`, so its Activity group carries no failure),
  and claims the continuation as the successor ahead of the FIFO, all in one
  writer transaction; any failure rolls the transaction back and the Turn
  keeps today's notice. `release_for_service_shutdown` skips a Vibey Turn
  nobody asked to stop.
- `core/backend_failure_retry.py`: `restart_resume_plan` (eligibility, item 1
  and the bound of item 6), `reserve_restart_resume` (the notice-free
  reservation, sharing `_vibey_resume_snapshot` with `reserve_retry`), and the
  `restart_resume` branch of `admission_denial` (the cut Turn must still be the
  Session's latest Turn; input queued behind it does not deny it).
- `storage/agent_transcript.py`: `last_response` and `committed_final`, bounded
  reads of the context tail, and `last_turn_text`, the silent final's result,
  bounded by the Turn's start. A final follows the loop's own rule: no tool calls,
  not the `error`/`aborted` partial a failed stream commits, and no input after
  it. `storage/background.py`:
  `move_agent_run_deliveries_in_connection`. `storage/message_deliveries.py`:
  `restart_resume_binding`.
- `core/run_settlement.py`: `SETTLED_BY_RESTART_RESUMED`, outside
  `SETTLEMENTS_WITHOUT_RESULT`.
- `modules/agents/vibey/agent.py`, `recovery.py`, `tools.py` and agent-core
  `settle_open_calls` / `bash.settle_in_foreground`: item 3's T2 split
  (startup defers a running job of an unsettled Turn; the continuation's T2
  waits; `VibeyRecovery.settle_session` hands over before a fallback notice).
  Another addition the code required: stopping the service stops the Model Hub
  gateway before Turns end, so a run in a model call fails during shutdown. A
  run that ends without completing while the service is stopping leaves its
  Turn open instead of settling it, so startup continues it like any other cut
  Turn.
- `core/scheduled_tasks.py`: no change needed. Its shutdown cancellation only
  covers executions still in flight, and a Turn-owned Run has already handed
  its execution to the Turn; `recover_processing` skips Delivery-linked Runs;
  `settle_agent_runs_from_terminal_turn` settles nothing for `restart_resumed`,
  which is neither a settlement without result nor carries `settles_run`.
- The continuation keeps the cut Turn's routing: platform, scope, author,
  metadata (Harness provenance included), admission context (with the IM
  message the indicator belongs on), and FIFO position: the cut Turn's
  `submitted_at`, with an id that extends its initial Delivery's (the FIFO
  tie-breaker, as timestamps have second resolution), so a continuation requeued
  by a failed start still runs before input queued behind the cut Turn. A continuation that cannot be claimed at once (its runtime not
  admitted, or a start check refusing it) is never left waiting unclaimed, where
  a later denial would drop the work unannounced: the transaction rolls back
  and the Turn keeps today's notice and Retry.
- Docs: `agent-core-contracts/recovery.md` T4, `backend-failure-retry.md`.
  Public docs (`avibe-docs` `agents/vibey.mdx` Restart bullet, both languages)
  change in a follow-up in that repository.

## Validation

Scenario IDs in `tests/scenarios/message_delivery/catalog.yaml`:

- `MESSAGE-DELIVERY-327`: startup recovery with a cut conversation Turn: one
  continuation Delivery, no notice, a transcript resume of the cut Turn; with
  the final response already committed: settled completed, no continuation;
  a silent final settles with the Turn's narration before it as the result.
- `MESSAGE-DELIVERY-328`: cut Harness Turn: shutdown leaves the Turn and its Run;
  the Run is not failed, its Delivery moves to the continuation, the continued
  Turn's result settles it, no owed restart notice; a Run steered into the
  Turn whose receipt was never materialized moves and settles the same way,
  after a retry when startup could not materialize it.
- `MESSAGE-DELIVERY-329`: ineligible cases each fall back to today's settlement
  and notice: Stop requested, archived Session, Agent switched off Vibey, the
  fourth restart in a row without a model response. A start never accepted
  stays on the unknown-start path it takes today.
- `MESSAGE-DELIVERY-330`: a command running at the restart: startup leaves it
  to the continuation, which gives a job exiting inside its window (from launch)
  its real output, hands over one whose window the downtime spent, and ends it
  on a Stop; a committed handover is reused; the command never starts again.
  `MESSAGE-DELIVERY-328` also checks a Run settles with the answer given after
  the command's real output. The fallback still names its Watch
  (`MESSAGE-DELIVERY-322`).
- `MESSAGE-DELIVERY-331`: input queued during downtime runs after the
  continuation, even when queued in the cut Turn's second and the
  continuation's first start is requeued.
- `MESSAGE-DELIVERY-332`: a run that fails because the service is stopping
  leaves its Turn open.
- `MESSAGE-DELIVERY-322` keeps the Watch-naming notice without Retry for the
  fallback.
- Local Incus: restart the master environment during a long Vibey conversation
  Turn and during a Vibey Agent Run with a callback; both continue without a
  notice, and the callback reports the continued result.

## Known by design

- The response being generated at the restart is generated again: its partial
  output was never committed.
- A `write` or `edit` cut mid-call gets the interrupted result and the model is
  told to re-read the file, as after any interruption.
- While the continuation waits for a surviving command it shows as running
  without new activity, as a live foreground command does; it starts its model
  call after the wait. A Stop then ends the command and the continuation before
  any model call.
- A final reply committed but not yet delivered when the process died is in the
  Workbench transcript; its IM delivery is not retried (agent-core recovery,
  Delivery section). The Turn settles from it as completed.
- Native backends (Claude Code, Codex, OpenCode) keep today's behavior.
