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
     completed (`terminal_result`), with no continuation and no notice;
   - the continuation bound is not exhausted (item 6).
   Anything else keeps today's behavior: the Turn settles as interrupted, its
   Runs settle without result, and the user gets today's notice.
2. **What continues.** The same hidden `agent_initiated` Delivery the Web Retry
   creates (`vibey_resume`, `vibey_source_turn_id` = the cut Turn), admitted
   through the ordinary P3 path, with server-owned history binding kind
   `restart_resume` instead of a notice. Vibey resumes its committed transcript;
   no input row, no visible `continue`, no replay of a committed tool call.
   Accepted steers of the cut Turn and its retry ancestry are pinned as for
   Retry.
3. **Tool calls.** T2 runs first, unchanged: an exited job gives its output, a
   running job is handed to a Watch, a call without job state gets the
   interrupted result. The continuation proceeds after a Watch handover: the
   handover result is already committed, so nothing runs twice, and the model
   sees the same handover result a long command produces in a live Turn. The
   "no Retry after a handover" rule stays for the manual notice only.
4. **Runs follow the work.** The cut Turn settles with
   `settled_by = restart_resumed`, which is not a settlement without result, so
   none of its Runs is failed or owes a notice. The Runs' Delivery link moves to
   the continuation Delivery in the same writer transaction, so the continued
   Turn settles them with its own outcome and output, and callbacks fire once,
   from that outcome. At shutdown, `release_for_service_shutdown` and the
   scheduled-task teardown leave a Vibey Turn and its Runs unsettled, so startup
   finds them as lost active Turns like a conversation Turn.
5. **Ordering.** The continuation is admitted before any input queued while the
   service was down; that input waits behind it, as behind any active Turn, and
   may be steered into it by the existing steer path.
6. **Loop bound.** A Turn is not continued when it is the third consecutive
   `restart_resume` in its ancestry without a committed model response in
   between. Such a Turn falls back to item 1's notice, which keeps Retry.
7. **Presentation.** No notice, no ⚠️ reaction, no interruption copy. The
   session shows the continued Turn as running. The fallback notices of item 1
   name Retry on the Web instead of asking for a resend (copy fix in both
   languages).

## Ownership and touch points

- `core/session_turns.py`: `recover_durable_delivery_state` (lost active Turns,
  eligibility, continuation instead of `_report_interrupted_turn`);
  `release_for_service_shutdown` (leave Vibey Turns and Runs unsettled).
- `core/scheduled_tasks.py`: service-teardown settlement and
  `recover_processing_requests` must not settle Runs of a Turn startup will
  continue; `settle_agent_runs_from_terminal_turn` must ignore
  `restart_resumed`.
- `core/backend_failure_retry.py`: a notice-free reservation for
  `restart_resume`, sharing the Vibey resume snapshot and boundary checks with
  `reserve_retry`; `admission_denial` honors the new binding kind.
- `core/run_settlement.py`: `SETTLED_BY_RESTART_RESUMED`.
- `modules/agents/vibey/agent.py`: none expected beyond what Retry uses; the
  resume path already settles T2 and pins inputs.
- Docs: `agent-core-contracts/recovery.md` T4, `backend-failure-retry.md`.

## Validation

- Startup recovery with a cut conversation Turn: one continuation Delivery, no
  notice, Vibey resumes from the transcript; with the final response already
  committed: settled completed, no continuation.
- Cut Harness Turn: the Run is not failed, its Delivery moves to the
  continuation, the continued Turn's outcome settles it, the callback carries
  that outcome, no owed restart notice.
- Ineligible cases each fall back to today's notice: Stop requested, archived
  Session, Agent switched off Vibey, start not accepted, loop bound reached.
- Running command at shutdown: T2 hands it to a Watch, the continuation still
  starts, the command is not started again.
- Input queued during downtime runs after the continuation.
- Local Incus: restart the master environment during a long Vibey conversation
  Turn and during a Vibey Agent Run with a callback; both continue without a
  notice, and the callback reports the continued result.

## Known by design

- The response being generated at the restart is generated again: its partial
  output was never committed.
- A `write` or `edit` cut mid-call gets the interrupted result and the model is
  told to re-read the file, as after any interruption.
- Native backends (Claude Code, Codex, OpenCode) keep today's behavior.
