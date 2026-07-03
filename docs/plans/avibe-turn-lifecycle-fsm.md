# avibe Turn Lifecycle → one per-session Turn state machine

Status: **design / pre-implementation**. Branch `refactor/avibe-turn-state-machine` (off master @ e324cc0, which includes #367 `2c88c7b`).
Author: refactor follow-up to PR #367. Tracks task #85; folds in #84 + Codex #3336001455.

> Read this top-to-bottom before touching code. Part 1 is the EXHAUSTIVE current
> behavior (so nothing is lost). Part 2 is the target. Part 3 is the migration.
> Part 4 is the edge-case catalog = the regression suite. Part 5 = open decisions.

---

## 0. Why this exists (the diagnosis)

Across ~15 Codex review rounds on #367, the review **never converged**: each round
surfaced 3–5 new P2s, almost all in the avibe turn lifecycle. Root cause:

> "a turn's lifecycle" for an avibe session is **not one thing** — it is ~6
> independent mechanisms with **no single source of truth**. Every new scenario
> (timeout+stop-fail, scheduled+busy, crash+restored-poll, receiver-crash+token,
> send-now+stuck) is a fresh **reconciliation between these mechanisms**, and Codex
> keeps finding pairs that disagree. That is an infinite tail *by construction*.

Deeper tension: backends are **fire-and-forget async** (`handle_message` returns
before the turn ends; the result/error streams back later on a reused receiver /
poll / event handler), and avibe (strict turn lifecycle: working indicator, Stop,
queue, dot) is **retrofitted onto an IM-shaped loose message flow**. So "did the
turn end / succeed / is it still mine" must be *caught + reconciled*, not *read*.

The fix is **not** more edge patches. It is to make the turn lifecycle ONE
authoritative per-session state machine; the dot, the in-flight gate, the
lifecycle events, and the queue become **projections** of it.

---

## PART 1 — CURRENT STATE (exhaustive map)

### 1.1 The 6 reconciled mechanisms (what each owns, where)

| # | Mechanism | Owns | Lives in |
|---|-----------|------|----------|
| 1 | **Status dot** `agent_sessions.agent_status` ∈ {idle, running, failed} | the persisted sidebar tri-state (sticky `failed`) | `workbench_sessions_service.set_agent_status`/`reset_running_agent_status`; written by `controller.set_agent_status` (broadcasts `session.status`) |
| 2 | **in_flight gate** `dict[session_id → (asyncio.Task, MessageContext)]` | "is a turn busy", the Stop target, the queue gate | `internal_server.create_app` closure (`app.state.in_flight_dispatches`) |
| 3 | **turn-sink + turn_token** | correlate an async terminal emit to the live turn; hold the SSE/dispatch open | `dispatch_turn` stamps token + registers sink; `controller.register/pop_turn_sink`, `active_turn_sinks` |
| 4 | **send-while-busy queue** (`messages.type='queued'`) | messages typed while a turn runs | `messages_service.enqueue/list/pop_queued`, `promote_pending`; drained by `internal_server._flush_queue` |
| 5 | **lifecycle events** `turn.start` / `turn.end` | the browser working indicator + Stop visibility | published by `internal_server._run_turn` |
| 6 | **stuck-turn sentinel + crash recovery** | 600s-timeout-stop-unconfirmed holding; reset stale `running` on boot | sentinel task in `_run_turn` finally; `controller._reset_stale_agent_status`; OpenCode `restore_active_polls` |

No single object knows "the turn's state". The state is **derived** and **scattered**:
busy = `in_flight` has a not-done task; running = dot column; working = the FE bit;
done = a token-matched terminal `result` reached the outbound chokepoint.

### 1.2 The two status chokepoints (the ONE good invariant — keep it)

- **INBOUND → running**: `modules/agents/service.py` `AgentService.handle_message:35-37`
  — every source/backend funnels here; `if session_id: set_agent_status(session_id,"running")` then `await agent.handle_message`. avibe-gated (only avibe ctx carries `agent_session_id`).
- **OUTBOUND → idle/failed**: `core/message_dispatcher.py` `emit_agent_message:460-467`
  — only `canonical_type=="result"` AND `_is_active_turn(context)` settles `failed if is_error else idle`.

`_session_id_from_context` (`controller.py:661-665`) reads `platform_specific["agent_session_id"]` — only avibe turns carry it, so IM/CLI never touch the dot.

### 1.3 End-to-end turn lifecycle (interactive Chat)

1. UI server writes a `pending` message row, POSTs `/internal/dispatch_async` (session_id, text).
2. `_dispatch_async` (internal_server): **gate decision** — busy OR pre-existing queue → `promote_pending(→queued)` (+ flush if idle); else `_run_turn`.
3. `_run_turn`: create `_runner` task; `in_flight[session_id]=(task,ctx)`; publish `turn.start`. Run `dispatch_turn(ctx, text, source, on_chunk=_noop_chunk)` — **always a sink** so the turn is HELD open until the terminal result.
4. `dispatch_turn` (`services/dispatch.py`): stamp `turn_token=uuid` on ctx; `register_turn_sink(token, done_event)`; `await handler.handle_user_message` (or `handle_scheduled_message`); then **`await done.wait()` up to 600s** (`TURN_STREAM_TIMEOUT`).
5. `handle_user_message → _handle_turn → AgentService.handle_message` → **set dot running** (inbound chokepoint) → `agent.handle_message` (fire-and-forget submit; returns before reply).
6. Backend later emits the terminal `result` (success/error/silent) on its receiver/poll/handler, carrying the turn_token → `emit_agent_message` → **outbound chokepoint settles dot** (idle/failed) → empty/silent → `_signal_turn_complete → mark_turn_complete` sets the sink `done_event`.
7. `done.wait()` returns → `dispatch_turn` returns → `_runner.finally`: pop `in_flight`, publish `turn.end`, flush the queue (unless stop/timeout/cancel rules say otherwise).

### 1.4 Per-backend participation (the async-correlation complexity)

All three are **pure participants**: they emit **exactly one terminal `result` per turn**
(success / `is_error=True` / `level="silent"` stop) carrying the turn's token; they never
write the dot, in_flight, queue, or lifecycle events. Differences that the FSM must absorb:

- **Claude** (`claude_agent.py`): ONE long-lived **reused receiver** per session serving all
  turns; a per-session **FIFO `_pending_requests`**; the receiver's captured ctx holds **turn 1's
  token**, so every terminal emit must `_adopt_pending_turn_token` from the FIFO-matched request
  (4 sites: success pop, in-loop auth head, `_retire_failed_auth_turn`, receiver-crash head — adopt
  BEFORE `_clear_pending_reactions` which nukes the whole FIFO). Asymmetry: assistant-auth clears-all;
  system/result-auth retire-one. **No turn restore on restart.**
- **Codex** (`codex/agent.py`,`event_handler.py`): in-memory **turn registry** (`_turns`,
  `_active_turns`, `_pending_turn_starts` bootstrap race); **inherits** the token by carrying
  `request.context` (never stamps/adopts); **interrupts-before-start** within a session
  (`handle_message:130-155`) — the in-session preempt safety net. **No turn restore.** First-turn
  bind swaps `platform_specific` to a copied dict (token survives).
- **OpenCode** (`opencode/agent.py`,`poll_loop.py`): turn = a **2s poll loop**; `(final_text,
  should_emit)` contract (`should_emit=False` ⇒ "I already emitted my terminal result, don't
  double-settle"); the only backend with **persisted crash recovery** — `restore_active_polls`
  re-spawns polls on boot and **re-marks the avibe session `running`** (it bypasses the inbound
  chokepoint; recovers the dot the boot reset cleared). Inherits token via shared ctx; restored
  polls are effectively tokenless (rely on fail-open, no live sink after restart).

### 1.5 The gate internals (internal_server)

- `in_flight` busy-check is `entry is not None and not entry[0].done()` — used by `_turn_state`,
  `_dispatch_async`, `_cancel`, `_send_now`, `_submit_scheduled_turn`.
- `_run_turn(session_id, ctx, text, source=HUMAN)`: the runner; on terminal/timeout in its `finally`
  it (a) for **timeout+stop-unconfirmed (`stuck`)** installs a **self-healing sentinel task** that
  registers a sink under the timed-out token, awaits a late result OR `STUCK_TURN_RECOVERY_TIMEOUT`
  (600s) cap, defers `turn.end` until release, and on cap-expiry emits a failed result; honors
  `flush_on_cancel`/`stop_no_flush` on release; (b) else pops in_flight + publishes turn.end +
  conditionally flushes.
- `_flush_queue`: `pop_queued` → merge into ONE `user` row → publish `message.new`+`inbox.session.updated`+`queue.updated` → rebuild ctx from current session row → recurse `_run_turn` (as **SOURCE_HUMAN**).
- `_submit_scheduled_turn` (gate entry for scheduled): busy/queued → append a `queued` row
  (`author=source=harness`); idle → `_run_turn(source=SCHEDULED)`.
- `_cancel`: read in_flight; `task.done()`→already-finished; else `handle_stop(stored ctx)` →
  True→`task.cancel()`; False→409 `stop_failed` (no cancel). Sets `stop_no_flush` before the await.
- `_send_now`: busy+has-queue → `flush_on_cancel` + interrupt + cancel; idle → flush directly.
- Markers: `flush_on_cancel` (send-now wants the queue to run on cancel), `stop_no_flush` (plain Stop keeps the queue).

### 1.6 Scheduled / watch / webhook / agent_run entry

`scheduled_tasks._execute_request:1556-1564`: `if platform=="avibe" and session_id and gate: await gate.submit_scheduled(...); return None` else `handle_scheduled_message` (IM, byte-identical). `_build_context` sets `channel_id=session_id`, `agent_session_id`, `turn_source="scheduled"`, `suppress_delivery`, `delivery_override`, `agent_session_target`. Triggers: cron/one-shot (`task_run`/`scheduled`), `hook_send`, watch-waiter (`watch`), `agent_run` (`vibe agent run` default async); `webhook` is defined-but-unwired.

### 1.7 The frontend contract (MUST be preserved verbatim)

Single `EventSource('/api/events')`; envelope `{type,data,ts}`. Two distinct FE state machines:

- **`working`** (Chat page, one bit): set on `turn.start`, **cleared ONLY on `turn.end`** (the
  authoritative "turn over"); reconciled by `GET /turn-state → {in_flight}` on reconnect/visibility;
  11-min fallback; a `result`/`error` row hides the *thinking bubble* early but does NOT clear `working`.
  Drives the Stop button (busy ⇒ Stop, else Send).
- **`agent_status`** (sidebar dot, tri-state idle/running/failed): from the session row +
  `session.status` event; reconciled by `listSessions` on reconnect (trusts REST over replayed events).

Events that MUST keep firing (name + `session_id` minimum): `turn.start`, `turn.end`,
`session.status`(+agent_status), `queue.updated`, `message.new`(full row), `inbox.session.updated`,
`inbox.unread.changed`, `session.activity`, `connected`.
REST that MUST keep its contract: `POST /messages → {id}` (started) vs `{queued:true}` (202);
`GET /turn-state → {in_flight}` (truthful incl. the post-POST registration window; FE has a 4s grace);
`POST /cancel → {ok:false,code:'not_in_flight'}` when idle (≠ transport failure);
`POST /queue/{id}/send-now → code 'stop_failed' | status 'empty'`.
Message `type` filter (`user/result/error/notify` + `metadata.source=='show_page'`) identical between
the REST list and the `message.new` stream; `assistant`/`tool_call` persisted but NOT streamed/listed;
`system` never persisted. Unread = `result` only. `error`/`notify` show but don't count unread.

---

## PART 2 — TARGET: one `SessionTurnManager` FSM

### 2.0 LOCKED DECISIONS (Alex, 2026-06-02)

- **NO turn-duration timeout.** An agent turn may legitimately run for hours; the
  controller must NEVER kill it on a timer. Remove `TURN_STREAM_TIMEOUT` (the 600s
  `dispatch_turn` cap) entirely — `await done.wait()` waits for the agent's REAL
  terminal result, however long. This deletes the cause of the whole STUCK problem.
- **NO STUCK state, NO sentinel.** They existed ONLY to handle the timeout. Gone.
  FSM = **IDLE ↔ RUNNING** (+ enqueue when busy).
- **Genuine failure is detected by REAL signals, not a timer**: backends emit a
  terminal `error` result on crash/connection-loss/auth-fail (already built); the
  user's **Stop** ends a wedged turn; controller restart resets stale `running`.
  Concurrent sends never collide because a busy session **enqueues** (the gate).
- **KEEP transport-level health timeouts** — Codex's 120s per-RPC-call timeout,
  OpenCode's 15s `wait_for_session_idle`. These bound individual handshake/abort
  calls, NOT the agent's working duration; they do not kill long agents.
- **Frontend coupling**: ChatPage's 11-min `WORKING_FALLBACK_MS` force-clear was
  tied to the backend 600s timeout — REMOVE it. `working` clears only on `turn.end`,
  reconciled by `GET /turn-state` (already polled on reconnect/visibility), never a
  timer. Otherwise a long agent's Stop button/indicator would vanish at 11 min.
- A turn that is genuinely wedged (alive, no output, no error, user never stops)
  blocks only ITS session until Stop/restart — accepted (vs killing real long agents).
  If silent-wedge ever becomes real, fix it at the backend (heartbeat/error emit),
  not a turn-kill timer.

### 2.1 The model

ONE authoritative `Turn` per avibe session, owned by a `SessionTurnManager` on the
Controller (`controller.session_turns`). A session has **at most one active Turn**.

```
Turn:
  session_id, turn_token (uuid), source (human|scheduled),
  state: RUNNING                    # the only live state; terminal is transient → retire
  context (MessageContext started under)   # for Stop / interrupt / restored-poll
  task (asyncio.Task)                       # for cancel
  done_event                                # dispatch_turn hold-open (NO timeout — waits for the real result)
  flush_intent: keep_queue | flush_on_release   # was stop_no_flush / flush_on_cancel
  started_at
```

State machine (per session) — just IDLE ↔ RUNNING (no timeout, no STUCK):

```
        submit(idle)                terminal_result(matching token)  [success/error]
 IDLE ───────────────▶ RUNNING ───────────────────────────────────▶ (retire → IDLE; dot idle/failed; turn.end; flush)
   ▲                     │
   │ submit(busy)        └─ stop(confirmed) / cancel ──────────────▶ (retire → IDLE; dot idle, silent; turn.end)
   │  → enqueue (runs after, in order)
   │
   └── boot: reset stale RUNNING dot → idle;  OpenCode restore → re-enter RUNNING

 A RUNNING turn stays RUNNING until the agent emits its terminal result OR the user stops it —
 NO timer. dispatch holds open via ``await done.wait()`` with no timeout.
```

### 2.2 Projections (derived — the FSM is the single writer)

| Today (scattered) | Becomes (projection of the FSM) |
|---|---|
| `agent_status` column | written by the FSM on RUNNING-enter (running) + terminal (idle/failed). The persisted value is the dot; `failed` stays until the next RUNNING. |
| `in_flight` busy | `session has an active Turn (RUNNING\|STUCK)`. `/turn-state.in_flight` = that. |
| `turn.start`/`turn.end` | emitted by the FSM on RUNNING-enter / terminal-release. `turn.end` deferred while STUCK (already the rule). |
| turn-sink + token | the Turn owns `done_event` + `turn_token`; `terminal_result` is matched by token = the ONE active-turn guard (replaces `_is_active_turn` + `_stream_chunk`-complete + `mark_turn_complete`). |
| queue | unchanged storage; the FSM flushes on terminal-release per `flush_intent`. |
| sentinel | the `STUCK` state + its transitions (not an ad-hoc task pattern). |

### 2.3 What stays exactly the same (the contracts)

- The **two chokepoints** stay the FSM's two main transitions: inbound `handle_message` →
  `manager.on_running(session_id)` (or the gate's `submit` already set it); outbound
  `emit_agent_message` terminal `result` → `manager.on_terminal_result(ctx, is_error, level)`.
- The **backend contract** is unchanged: emit exactly one terminal `result` per turn carrying the
  token. Claude's token adoption, Codex's registry, OpenCode's poll/restore stay backend-internal —
  the FSM just consumes the terminal result + token.
- The **frontend contract** (1.7) is unchanged — same events, same REST, same message types. The FSM
  emits the same events as projections.

### 2.4 What the FSM ELIMINATES

- 3 duplicated token guards → 1 (`Turn.is_active_emit(token)`).
- duplicated flush contracts (runner finally vs sentinel) → 1 terminal-release handler.
- in_flight + dot + working-bit as separate stores → 1 `Turn.state` (+ persisted dot projection).
- the sentinel as a bespoke task → the `STUCK` state.
- the gate decision spread across `_dispatch_async` + `_submit_scheduled_turn` → 1
  `manager.submit(session_id, ctx, text, source)` (busy→enqueue, idle→run) used by BOTH Chat + scheduler.
- folds in #84 (the queue re-run loses scheduled provenance) — the FSM enqueues a Turn *intent*
  (carrying source + suppress_delivery), so a flushed scheduled run re-runs as `SOURCE_SCHEDULED` with
  its delivery metadata, not as a plain human turn; and publishes `queue.updated` on enqueue (#3336001455).

### 2.5 Home + shape

`core/session_turns.py`: `class SessionTurnManager` holding `dict[session_id → Turn]`.
Wired as `controller.session_turns`. Thin callers:
- `internal_server._dispatch_async` → `manager.submit(...)`; `_cancel`/`_send_now`/`_turn_state` → manager.
- `message_dispatcher.emit_agent_message` (outbound) → `manager.on_terminal_result(...)`.
- `service.handle_message` (inbound) → `manager.on_running(...)` (idempotent confirm of submit).
- `scheduled_tasks` → `manager.submit(source=SCHEDULED)`.
- `controller._reset_stale_agent_status` → `manager.reset_stale()`; OpenCode restore → `manager.restore_running(session_id, ctx)`.
The dispatch hold-open (`dispatch_turn` sink/`done_event`) stays, owned per-Turn.

---

## PART 3 — Migration (phased, behavior-preserving; "万无一失")

- **Phase 0** — this doc + lock the edge-case catalog (Part 4) as a regression test list. ✅ when reviewed.
- **Phase 1 — extract, don't change.** Introduce `SessionTurnManager` and MOVE the existing
  in_flight + dot-writes + sink/token + queue + sentinel + lifecycle into it, BEHIND the current
  external behavior. HTTP handlers / chokepoints / scheduler / restore become thin callers. **No
  observable behavior change**; every Part-4 edge test stays green. (This is the risky-but-mechanical
  re-homing — do it in small, individually-tested commits.)
- **Phase 2 — collapse the reconciliation.** Now that one owner exists: unify the 3 token guards;
  delete the duplicated flush; simplify Claude's adoption to "tag the terminal emit with the active
  Turn's token" provided by the FSM; the sentinel becomes the STUCK transition.
- **Phase 3 — fold in the deferred follow-ups.** #84 (scheduled provenance through the queue) +
  #3336001455 (`queue.updated` on enqueue) fall out of the unified `submit`/enqueue.
- Each phase = its own reviewed PR; run the Part-4 regression list + ruff + the CI groups each time.

---

## PART 4 — Edge-case catalog (the regression suite; from ~15 Codex rounds)

Each MUST stay green through every phase (most already have tests — cited):

1. Dot settles idle on success result; failed on `is_error`; notify never settles. (`test_message_dispatcher_result_fallback`)
2. Superseded/older-token result does NOT settle the new turn's dot. (active-turn guard)
3. Tokenless result does NOT settle when a live tokened sink exists. (the tightened guard)
4. The 3 guards share one rule (absent/mismatched token = stale when a live tokened sink exists).
5. Intentional stop = single silent `result` (level=silent): dot idle, NO bubble, stream released. (codex/opencode/claude `handle_stop`)
6. Terminal failures emit `result`+`is_error` (NOT notify) on every backend path incl. OpenCode retry-exhaustion (+ `return None,False` so the idle "(No response)" doesn't reset it).
7. Claude reused-receiver: 2nd+ turn terminal emit adopts the FIFO token → dot settles promptly (not 600s). Receiver-crash + auth + system + assistant paths.
8. `_clear_pending_reactions` clears the whole FIFO (no stale request survives) — the false-positive class.
9. **NO turn-duration timeout** (replaces the old timeout/sentinel tests): a RUNNING turn
   stays running indefinitely until the agent emits its terminal result (long agents run
   for hours, never killed); a busy session enqueues new sends; the user's Stop ends a
   wedged turn; restart resets stale running. (Delete the `test_internal_server` 600s-timeout
   + stuck-sentinel tests; add: long-running turn is NOT auto-settled; busy→enqueue.)
10. Scheduled avibe run: queues behind an active Chat turn (no preempt); gets in_flight + turn.start/turn.end + Stop; IM scheduled byte-identical. (`test_scheduled_tasks`, `test_internal_server`)
11. Restored OpenCode poll re-marks the avibe session running; IM poll does not. (`test_opencode_restore_polls`)
12. Sidebar reconnect refetches the full loaded window (no truncation). (`WorkbenchSidebar`)
13. `error` first-class type: in transcript+inbox, NOT counted unread. (`test_message_mirror`, `messages_service`)
14. Empty/silent result settles + releases but does NOT persist/deliver. (`test_agent_silent_result`)
15. avibe auth-recovery durable copy is button-free + `/setup`-actionable. (`test_agent_auth_service`)
16. Crash recovery: stale running → idle on boot; reconnect trusts REST.
17. `_handle_turn` returns None on dispatched success (ok=not error).

---

## PART 5 — Decisions (RESOLVED with Alex 2026-06-02)

1. ✅ **No turn-duration timeout** — remove `TURN_STREAM_TIMEOUT` + the timed-out/stuck/sentinel
   branch + the FE 11-min fallback. Keep transport-level health timeouts (Codex 120s RPC,
   OpenCode 15s wait-idle). (See 2.0.)
2. ✅ **No STUCK state / sentinel** — FSM = IDLE ↔ RUNNING.
3. ✅ **Manager home**: `controller.session_turns` (Controller-owned).
4. ✅ **Sticky `failed` dot**: keep (FE depends on it).
5. ✅ **Scope**: avibe-only (gated on `agent_session_id`; IM/CLI keep the no-gate path).
6. ✅ **#84** (scheduled provenance through the queue): Phase 3 (don't expand scope during the extract).
7. ✅ **Inbox `replied`/`preview`**: leave as-is (server-computed, orthogonal).

### Revised phases (timeout removal is the first, behavior-FIX step)

- **Phase 1a** — remove the turn-duration timeout + STUCK + sentinel (backend) + the 11-min FE
  fallback; FE relies on `/turn-state` reconciliation. This both implements the locked decision
  AND shrinks the surface before the extract. (A real behavior fix: long agents no longer killed.)
- **Phase 1b** — extract `SessionTurnManager` (IDLE ↔ RUNNING) as the single owner of in_flight +
  dot + sink/token + queue + lifecycle, behavior-preserving; thin callers.
- **Phase 2** — collapse the 3 token guards → 1; delete duplicated flush; simplify Claude adoption.
  **Critical invariant (now that there's no timeout bail-out)**: every legitimate terminal result
  MUST carry the active turn's token, or the (correctly strict) token guard blocks its completion and
  the turn hangs forever. So the FSM must guarantee the token round-trips — bulletproof Claude
  adoption, or have the FSM attach the active Turn's token at the chokepoint rather than trusting the
  backend to echo it. (Pre-existing stale test `test_dispatcher_stream_chunk.py` updated in Phase 1a:
  a tokenless emit must NOT complete a tokened turn — the strict rule; the hang risk is why the FSM
  owns token round-trip.)
- **Phase 3** — fold in #84 + `queue.updated` on enqueue (#3336001455).
