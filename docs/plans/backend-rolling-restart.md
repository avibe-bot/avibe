# Backend Rolling Restart

## Goal

Restart Claude, Codex, and OpenCode runtime state without destabilizing the
Avibe service, Web UI, tunnel, or Session context. A restart must never allow
two turns to work on the same Session concurrently.

## Contract

The shared runtime owns a backend restart barrier with four states:

`READY -> DRAINING -> SWITCHING -> READY`

- `READY`: new turns may start.
- `DRAINING`: turns accepted before the barrier continue. New turns wait. A
  Workbench message for a busy Session remains in its durable queue.
- `SWITCHING`: no new turn can enter while the old runtime is terminated and
  refreshed configuration is activated.
- `READY`: queued work resumes through the normal Session gate.

The Session FSM remains the authority. A Session changes generation only after
its active turn reaches IDLE. Stop and send-now use the existing interruption
path; after the old turn settles, the queued message resumes on the new runtime.
Backend-native background Activities are a second, orthogonal liveness axis:
the drain also waits for active Activities and their pending output to settle.

## Why the barrier is shared

Claude, Codex, and OpenCode have different process models, but the safety
invariants are identical. Keeping the barrier in `AgentService` and the durable
queue interaction in `SessionTurnManager` avoids backend-specific queue and
locking rules.

The first implementation uses a single-writer cutover for every backend.
OpenCode must not run two servers against the same local state concurrently.
Codex and Claude may later prepare a new generation during DRAINING, but only if
their adapters can prove isolated ownership. That optimization does not change
the Session protocol.

## Timeout

Draining is bounded. At the deadline, the coordinator:

1. cancels matching Workbench waiters without flushing them onto the old
   runtime;
2. marks remaining backend Activities killed and settles their owning Runs;
3. force-refreshes the backend runtime, which terminates the old process tree;
4. releases stale runtime-turn tokens;
5. reopens the backend barrier and flushes queued Sessions.

The default drain timeout is five minutes and can be overridden for operations
and tests with `AVIBE_BACKEND_RESTART_DRAIN_TIMEOUT_SECONDS`.

## Invariants

- At most one active turn per Session and runtime key.
- Restart coordination never stops the Avibe service, UI, or tunnel.
- A failed refresh reopens the barrier; it cannot deadlock future turns.
- Concurrent restart requests for one backend coalesce into one operation.
- Queue rows are claimed only by the existing Session queue transaction.
- Old-turn completion and timeout force-cutover are idempotent races.

## Scenarios

- `BRR-001`: idle backend refreshes immediately and accepts the next turn.
- `BRR-002`: active Session drains; its new message remains queued until IDLE.
- `BRR-003`: another Session cannot enter the switching window.
- `BRR-004`: Stop/send-now settles the old turn and advances cutover.
- `BRR-005`: drain timeout cancels the old waiter, force-refreshes, then flushes.
- `BRR-006`: refresh failure reopens the barrier and preserves queued input.
- `BRR-007`: concurrent restart requests coalesce without duplicate process kills.
- `BRR-008`: backend restart does not touch service, UI, or tunnel lifecycle.

## Service restart boundary

This protocol is also the prerequisite for a future service-process handoff,
but it does not claim that a SIGTERM of the current Avibe process can preserve
an in-memory receiver. A true service rolling restart additionally needs an
external supervisor and cross-process ownership handoff. Until that exists,
backend restarts use this protocol; service restart remains crash-recovery
semantics and must not pretend to be seamless.
