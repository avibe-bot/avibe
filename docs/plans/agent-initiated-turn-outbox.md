# Agent-initiated turn Outbox capture

## Background

When the Claude Code backend finishes a **background task** (`run_in_background:
true`) or fires a **ScheduleWakeup**, the harness re-invokes the agent loop
**inside the same SDK process**. That produces a fresh assistant/result stream
on Avibe's long-lived `ClaudeAgent._receive_messages` receiver — but Avibe never
sent a query, so it never opened a runtime turn for it.

### Symptom (verified)

A background task completes, the agent writes a user-facing reply, and the user
never receives it. The reply is not persisted to `messages`, not delivered to
IM, not pushed — it exists only in the Claude Code transcript.

Runtime evidence (session `ses6efm3vrtnw`, `~/.avibe/logs/vibe_remote.log`):

```
2026-06-28 02:49:41 - Dropping stale assistant emit for superseded runtime turn in avibe::ses6efm3vrtnw
2026-06-28 02:49:41 - Dropping stale toolcall emit for superseded runtime turn in avibe::ses6efm3vrtnw
2026-06-28 02:50:09 - Dropping stale result  emit for superseded runtime turn in avibe::ses6efm3vrtnw
```

The reply WAS produced and DID reach `emit_agent_message`; the outbound
active-turn guard dropped every emit of the turn.

## Root cause

Two outbound chokepoint guards in `core/message_dispatcher.py`
(`emit_agent_message`) drop any emit whose context fails
`_is_current_runtime_turn` →
`AgentService.emit_matches_runtime_turn(context)`:

* result guard (`canonical_type == "result"`)
* assistant/tool/log guard

`emit_matches_runtime_turn` returns `True` only when
`gate.token == context[agent_runtime_turn_token]`. For an agent-initiated turn:

* the previous turn's terminal result already **released** the gate
  (`gate.token == ""`), and
* the receiver's reused context still carries the **previous** turn's token,
* `_pending_requests` is empty, so no fresh token is adopted.

`"" != "<old token>"` → guard fails → output dropped, with no persist, deliver,
stream, or notify. This is precisely the "agent acts on a non-user trigger"
Outbox gap the product vision calls out (Inbox → Processing → Outbox; user
messages are one signal, not the only trigger).

Scope note: this is the receiver-alive case (idle timeout is 600s; the verified
repro completed in ~1.5 min). A background task that runs **longer** than the
idle timeout is a separate, larger problem (the session is evicted and the SDK
client/process torn down before the reply is produced) and is out of scope here.

## Solution

Make an agent-initiated turn a **first-class turn** so its output rides the same
INBOUND (session → running) and OUTBOUND (terminal result → idle + persist +
deliver + notify + gate release) chokepoints as a user turn.

* `AgentService.begin_agent_initiated_turn(agent_name, context, runtime_key)`
  (shared, reusable by any backend): if the runtime gate is **free**, acquire it
  (acquiring a free `asyncio.Lock` never yields → the `locked()`-check + acquire
  is atomic on the loop, so a concurrent user turn can't slip in), mint a fresh
  token, stamp it onto the context, and mark the session running. Returns the
  token, or `None` when a real turn already owns the gate (let it own the
  output).
* `ClaudeAgent._receive_messages`: when an assistant/result message arrives with
  **no pending request**, call a helper that opens an agent-initiated turn and
  synthesizes a pending `AgentRequest`. From there the existing handlers
  (token adoption, FIFO pop on result, EOF/error/cancel settle) treat it exactly
  like a normal turn — the terminal result releases the gate via the dispatcher's
  result `finally`.

No change to the guards themselves: stale stragglers from stopped/superseded
turns (a DIFFERENT non-empty gate token, or a held gate) are still dropped.

## Interaction with idle Claude eviction (and how long a session can wait)

`SessionHandler.evict_idle_sessions` reclaims a Claude SDK client (cancels its
receiver + disconnects/reaps the ~220MB subprocess) once a session is idle past
`claude.idle_timeout_seconds` (default **600s / 10 min**; swept every ~100s). A
session flagged `active` is exempt from this plain idle eviction — only the
stuck-active backstop (`max(idle_timeout * multiplier, floor)`, ~30 min) can
reclaim an active one.

Two interactions, both handled:

1. **The silent wait before the reply (a CEILING, not a conflict).** After the
   turn that started the background task goes idle, the session sits idle with
   `last_activity` frozen at that turn's last streamed message. If the background
   task reports back within ~`idle_timeout` (default 10 min) the receiver is
   still alive and the agent-initiated turn opens normally. If it stays silent
   longer, the idle sweep evicts the session first — the receiver is gone, so the
   eventual reply has nowhere to land (this is "Case B": the reply, and likely
   the task itself as a child of the reaped process, is lost). This bound is
   fundamental: Avibe cannot keep a Claude subprocess alive indefinitely on the
   chance a maybe-never task will report back, and it has no signal that a
   background task is pending inside the SDK process. **Answer to "how long can
   it wait": ~`claude.idle_timeout_seconds` (default 600s) from the prior turn's
   last activity.** Raising the wait = raise that config (at the cost of more
   lingering subprocesses); a smarter pending-work-aware extension would need an
   upstream SDK signal and is a separate follow-up.

2. **Mid-turn eviction parity (closed by this change).** `begin_agent_initiated_turn`
   marks the workbench dot running but does NOT add the session to
   `active_sessions`; a user turn does (in `handle_message`). Left as-is, an
   in-flight agent-initiated turn would be protected only by per-message activity
   touches and could be reclaimed mid-turn if it went quiet > idle_timeout (e.g.
   it started its own long background work), whereas a user turn survives to the
   ~30-min backstop. Fix: the Claude detection marks the session active on open
   (mirroring `handle_message`); the existing terminal-result / EOF / error /
   cancel paths already mark it idle, so it stays balanced and the stuck-active
   backstop still reclaims a wedged turn. This also routes a force-eviction
   through `force_cleanup_stuck_active_session` (settles dot + releases gate)
   rather than a bare `cleanup_session`. No gate/active-flag/dot leak in the
   open-vs-evict race.

Known minor edge (not fixed here): the shared receiver CancelledError path
releases the gate + marks the session idle but does not settle the workbench
`agent_status` dot. So if a plain `cleanup_session` cancels an agent-initiated
turn before its terminal result (i.e. `/clear` or an auth/config refresh fires
in the sub-second window after the dot goes `running`), the dot can stay green
until the next turn settles it (or `reset_stale` on restart). This is
pre-existing behavior of the cancel path for any in-flight turn, narrow, and
self-healing; the idle-eviction trigger specifically is already covered because
marking the turn active routes eviction through `force_cleanup_stuck_active_session`,
which emits a terminal result. Settling the dot in the shared cancel path is a
separate, broader change (it also affects normal turns) left as a follow-up.

## Evidence layers

* unit — `AgentService.begin_agent_initiated_turn` opens on a free gate / no-ops
  on a held gate; `emit_matches_runtime_turn` passes after open.
* receiver integration — `tests/test_claude_agent_initiated_turn.py`: drive
  `_receive_messages` with unsolicited assistant+result, assert a turn is opened,
  the result is NOT dropped (guard passes), and the gate is released.
* residual manual — local Incus regression: start a background bash task in an
  agent session, let it finish, confirm the completion reply reaches the channel.

## Todo

- [x] Root-cause + evidence
- [x] `begin_agent_initiated_turn` in `modules/agents/service.py`
- [x] detection + synthetic request in `modules/agents/claude_agent.py`
- [x] idle-eviction parity: mark session active on open
- [x] regression test
- [x] ruff + focused pytest

## Review fixes (PR #687)

- **Codex P1 — suppressed synthetic result leak.** A malformed-tool-use synthetic
  API error pops the turn's real pending request and arms
  `_suppressed_synthetic_results` for the PAIRED `ResultMessage`, which the result
  branch skips (`continue`) with no terminal emit. That paired result reached the
  new detection hook with an empty FIFO + free gate, so it opened an
  agent-initiated turn that was then skipped — leaking the gate / pending request
  / active flag until EOF and blocking the next user message. This hit **normal
  user turns** too (any turn ending in a malformed tool call), not just
  agent-initiated ones. Fix: `_maybe_begin_agent_initiated_turn` returns early
  when `composite_key in _suppressed_synthetic_results` (the set is cleared by
  `_consume_suppressed_synthetic_result` / cleanup, so real later turns still
  open). Regression test:
  `test_suppressed_synthetic_result_does_not_open_a_turn`.
- **Codex P1 — receiver deadlock on queued gate waiters.** `asyncio.Lock` can be
  momentarily unlocked while it still has queued waiters (a user turn that blocked
  on the gate while the previous turn held it). `locked()` is False in that window,
  so `await gate.lock.acquire()` would SUSPEND the long-lived Claude receiver
  behind the queued user turn — and the receiver is the only reader of that turn's
  terminal result (which releases the gate), so the session deadlocks. Fix:
  `begin_agent_initiated_turn` is now strictly non-blocking — it bails unless the
  gate is free AND has no live waiters (`_lock_has_live_waiters`), so the
  `acquire()` completes synchronously. Regression test:
  `test_does_not_block_when_a_user_turn_is_queued_on_the_gate`.
- **Codex P2 — Stop control for agent-initiated turns (DEFERRED, follow-up).**
  `begin_agent_initiated_turn` writes session status `running` but does not add a
  `Turn` to `SessionTurnManager.in_flight` or publish `turn.start`, so the
  Workbench cancel endpoint returns `not_in_flight` — a long-running
  agent-initiated turn shows running but has no working Stop button. Deferred:
  doing it right is real FSM work (a `Turn` needs a cancelable task; `in_flight`
  must be popped on every agent-initiated terminal/EOF/error/cancel path;
  `turn.start`/`turn.end` published; reset-stale interaction) — a medium change
  with its own review/test surface, not a bugfix-PR add-on. Agent-initiated turns
  are typically short (report a result and end) and still settle themselves on
  their terminal result, so the gap is narrow. Tracked as a follow-up issue.
- **Codex P2 — unsolicited output lost when a user turn is contended (DEFERRED,
  follow-up).** The P1 fix makes the open strictly non-blocking, so when a user
  turn holds/has-just-acquired the gate but hasn't appended its pending request
  yet, an agent-initiated reply buffered behind the previous turn is dropped by
  the outbound guard (it can't open its own turn without risking the deadlock).
  This is an inherent tradeoff of the gate-based, deadlock-safe approach: the PR
  is still a strict improvement (was 100% loss for agent-initiated output → now
  loss only in this contended window), and the loss is now logged instead of
  silent. Fully preserving the reply across a concurrent user turn needs a
  non-gated persist fallback or an output re-queue — part of making
  agent-initiated turns first-class FSM citizens. Tracked with the Stop-control
  follow-up.

## Backend parity (Codex / OpenCode)

The bug needs two co-conditions: (1) the backend re-invokes its own agent loop
while Avibe thinks the session is idle, and (2) a long-lived Avibe-side listener
receives that output. Only Claude has both today (background tasks +
ScheduleWakeup are Claude Code harness features; SDK streaming receiver is
long-lived). Codex has a persistent reader but no self-invocation (every turn is
an Avibe `sendUserTurn`). OpenCode's poll loop is turn-scoped (stops after the
turn) and also has no self-invocation. So neither needs alignment now;
`begin_agent_initiated_turn` is backend-agnostic and ready if either gains a
self-invocation source. NB: Avibe's own `vibe task` / `vibe watch` are NOT
affected on any backend — they dispatch through `handle_message`, which opens
the gate.
