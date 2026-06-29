# Agent-initiated turns as first-class FSM citizens (#688)

## Background

#687 made the Claude backend's **agent-initiated** output (a background-task
completion / ScheduleWakeup reply) actually reach the user, by opening a runtime
gate turn for it (`AgentService.begin_agent_initiated_turn`). But that turn was
only half-integrated: it set the sidebar dot to `running` (`on_running`) and held
the gate, yet it was never registered with the turn **FSM**
(`SessionTurnManager.in_flight` + `turn.start`/`turn.end`). Two gaps (Codex review
on #687):

- **No Stop control.** The Workbench cancel endpoint delegates to
  `SessionTurnManager.cancel`, which returns `not_in_flight` with no entry — a
  long-running agent-initiated turn shows `running` but can't be stopped.
- **Contended-delivery loss.** The deadlock-safe gate open bails when a user turn
  holds/just-acquired the gate, so an agent-initiated reply buffered in that
  window is dropped (now logged, not silent).

## Part A — FSM registration / Stop control (this PR)

`SessionTurnManager.register_agent_initiated_turn(context)`:

- An agent-initiated turn has **no query-sending dispatch task** (the backend
  already started), so it does NOT go through `dispatch_turn` / `_run`. The
  unsolicited output is ALREADY streaming on the long-lived receiver, so the sink
  + `in_flight` are registered **synchronously** here (before the receiver's next
  emit — no output-before-sink race), and a small holder task keeps the turn open
  until the terminal result's `done_event`.
- Settling (pop sink + `in_flight` + `turn.end` + flush the send-while-busy queue
  on natural completion) mirrors `_run`'s finally.
- Stop works: `cancel` interrupts the backend via the shared
  `handle_stop(turn.context)` path (Claude `client.interrupt()` by
  `composite_session_id`) and cancels the holder.
- avibe-only: a turn with no workbench session id (IM/CLI) has no Stop control or
  sink, so it's a no-op there — the gate + outbound chokepoint still deliver.

Wired into `begin_agent_initiated_turn` right after `on_running` (guarded; the
backend-agnostic open stays backend-agnostic).

## Part B — contended-delivery preservation (NOT in this PR; needs design)

Preserving the reply when the gate is contended is harder than a receiver-side
patch:

- Cleanly distinguishing an *orphaned agent-initiated* result from a *stale
  straggler* (which must stay dropped) is subtle.
- A `persist`-only fallback bypasses the dispatcher's text cleaning / file-link /
  quick-reply handling.
- For **IM** sessions, persisting a row does NOT deliver to the user's phone
  (that needs the dispatcher's `im_client.send_message`), so a persist fallback
  only helps avibe/workbench.

A correct fix needs proper output deferral / re-queue across the contending turn,
which is a separate design effort. The loss is rare (requires the buffered-output
+ just-acquiring-user-turn race) and is currently logged, not silent. Tracked.

## Evidence layers

- Unit — `tests/test_agent_initiated_turn_fsm.py`: registration sets `in_flight`
  + sink + `turn.start` synchronously; terminal result `done_event` settles (pop
  + `turn.end` + flush); `cancel` interrupts the backend (`handle_stop`) and
  settles without flush; no-op without a workbench session id; no-op when a turn
  already streams. Adjacent suites green (agent_service, internal_server,
  controller_agent_status, dispatch, stream_chunk; 110 total).
- Contract / Scenario — N/A.
- Residual manual — local Incus regression: start a long background task in an
  agent session, confirm the sidebar Stop interrupts the agent-initiated turn.

## Todo

- [x] `register_agent_initiated_turn` + wire-in
- [x] FSM unit tests
- [ ] ruff + focused pytest (done locally) → CI
- [ ] Part B design decision (defer vs build)
