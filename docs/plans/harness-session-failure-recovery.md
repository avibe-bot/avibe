# Harness Session Failure Recovery

## Background

Issue #915 reports a direct Agent Run queued on one long-lived Session while
other Sessions continue normally. The first lease hypothesis is wrong:
the scheduler's per-session reservation is process-local and is released when
its short dispatch task returns. A Workbench turn then has two longer-lived
owners:

1. `SessionTurnManager.in_flight`, which holds the Session FSM until a terminal
   result completes the dispatch sink.
2. `AgentService._turn_gates`, which holds the backend runtime FIFO from backend
   acceptance until that same terminal result.

Two missing convergence edges reproduce the failure family:

1. The pre-fix `HFR-002` fault injection proves that an accepted backend can
   die without producing a terminal result. The backend liveness probe reports
   `False`, but no owner consumes that fact, so both live owners remain held.
2. The pre-fix Incus `HFR-004` restart proves the exact no-visible-owner symptom.
   The old owner reaches a terminal Run state, but its queued successor remains
   represented by both a Workbench queue row and a queued Run carrying
   `workbench_queue_holds_run`. The scheduler deliberately excludes that Run
   because the Session FSM owns its FIFO position, while the restarted FSM has
   no boot edge that re-enters the persisted queue.

No stale scheduler lease is involved in either case.

## Goal

Convert definitive loss of the backend runtime into the same idempotent silent
terminal event used by every normal failure path. Preserve unlimited duration
for live Agents, FIFO order, one turn per Session, and current restart/cancel
semantics.

## Design

- Keep `AgentService` as the backend-runtime owner. Once a backend explicitly
  marks a turn accepted, supervise that exact runtime-key/token pair.
- Recover only after the backend-specific probe returns `False` twice around a
  short race grace period. `True` and `None` are never recoverable states.
- Emit one silent failed terminal result while the token is still current.
  Existing outbound handling atomically settles the Run, signals the Session
  sink, releases the runtime gate, flushes the persisted Workbench queue, and
  drains callbacks.
- Token identity, not PID or age, guards recovery. A released or superseded
  token makes the supervisor a no-op, preventing PID reuse and late-result
  races from affecting a newer turn.
- Capture backend liveness at acceptance against the concrete transport or
  receiver-task generation that owns the turn. A replacement runtime for the
  same cwd/session key cannot make the old accepted owner appear alive.
- Extend the existing Session FSM projection with authoritative live ownership:
  owner source/run id, acquired time, backend, acceptance, and backend liveness.
  `vibe runs show` may attach this live projection; it is diagnostic only and is
  not a second recovery source.
- On controller boot, re-enter only when the queue head is a persisted
  Workbench-held row that names a still-queued Agent Run. A per-Session
  recovery lock makes this idempotent.
  If an older scheduler-owned Run is queued, defer to it; its normal terminal
  or synchronous completion then asks the FSM to recover the successor. A
  user-owned queue head remains untouched even if a held Agent Run is behind
  it, preserving the explicit Stop contract.
- Codex liveness remains positive while an already-read notification is queued
  or being delivered, so recovery cannot overtake a delayed normal terminal.

## Fault Matrix

| ID | Boundary | Expected result |
| --- | --- | --- |
| HFR-001 | Failure before backend acceptance | Existing exception/terminal path fails the Run and releases the FIFO |
| HFR-002 | Backend exits after acceptance, before terminal delivery | Definitive liveness loss fails owner once and starts queued successor once |
| HFR-003 | Scheduler execution canceled while claimed | Claimed Run returns to queued and the process-local Session reservation releases |
| HFR-004 | Controller restarts with running Run and persisted queued work | Startup re-enters the persisted FSM queue after any older Run settles; pure user queues stay parked |
| HFR-005 | Live long-running backend exceeds all probe intervals | Owner remains protected regardless of age |
| HFR-006 | Liveness false blip or unknown state | No recovery unless the same token is definitively dead after grace |
| HFR-007 | Late terminal after recovery | Token guard drops it; Run/callback and successor remain exactly once |
| HFR-008 | User cancel and restart overlap | Terminal/cancel idempotency prevents duplicate turn or callback |

## Real Regression

Repository-managed local Incus Worktree target
`fix-session-queue-recove-ca3d3d2f` supplied both black-box proofs:

- Backend death: Session `ses7dr38p3qsp`, owner Run `91962dcd8f50`, successor
  `ef4b716f05e1`. At `05:05:53.727Z`, after live owner diagnostics reported
  `native_turn_started=true` and `backend_alive=true`, SIGKILL targeted only
  Codex PGID 8205. The owner failed at `05:05:54.756Z` with
  `backend_runtime_exited_before_terminal`; the successor started at
  `05:05:54.761Z`, completed once, and left an empty queue.
- Restart pre-fix: Session `sesswwbh4da32`, owner Run `fae757453b2b`, successor
  `3838c93fbfaa`. The service restarted at `05:08:19.393Z`; the owner became
  terminal, but the successor and its queue row were still queued at
  `05:20:48Z`. With the fix synced into the same preserved environment, boot
  logged persisted-queue recovery at `05:21:14.433Z`; the same successor
  started once at `05:21:14.428Z`, completed once at `05:21:35.520Z`, and the
  queue became empty.

No host Avibe service or remote Incus environment was touched.
