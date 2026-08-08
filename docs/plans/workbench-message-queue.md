# Workbench message queue (send-while-busy) + turn-lifecycle hardening

Status: proposal (awaiting confirmation). Branch: `fix/workbench-chat-page`.
Supersedes the "refuse the 2nd concurrent message" behavior.

> Historical proposal. The current message-priority and Send now contract is
> defined in `docs/plans/agent-message-priority.md`; where this document describes
> Send now as Stop/cancel, the current contract supersedes it.

## Background — what PR #359 already gives us

PR #359 (`per-session inbox + unified typed message persistence + realtime
bridge`) landed the foundation this feature sits on:

- **Typed, per-session message persistence.** `messages.type` column
  (`user` / `assistant` / `tool_call` / `notify` / `result`), written through
  `storage/messages_service.py:append()`; per-session history via
  `list_session_messages(session_id, …)`. (`storage/models.py`,
  migration `20260531_0009_messages_type.py`.)
- **Controller→UI realtime bridge.** Controller publishes
  `inbox.session.updated` to `core/inbox_events.py:bus` on `result` persist;
  `GET /internal/events` (SSE) → `vibe/inbox_bridge.py` → `vibe/sse_broker`
  → browser (`WorkbenchInboxContext`). This is the authoritative cross-process
  event path.
- **In-flight turn registry.** `core/internal_server.py` keeps
  `in_flight[session_id] = (task, context)` — already knows, per session,
  whether a turn is running (used today by `/internal/cancel`).

What #359 did **not** add: any per-turn correlation id, a terminal/error UI
state, or a pending-message queue. Those are exactly #1/#2/#3 below.

## Re-assessment of #1 / #2 after the merge

### #1 — late straggler reply cross-feeds the next turn (still open)
#359 added **no** per-turn id, so the bug is unchanged and now also corrupts
the **inbox preview** (a late `result` is the most-recent row, so the inbox
card shows stale content). Fix stays: **tag each turn with a unique
`turn_token`**.
- `core/services/dispatch.py`: generate a token per streaming turn, stamp it
  into `context.platform_specific["turn_token"]`, register the sink with it.
- `core/message_dispatcher.py:_stream_chunk`: only forward / set `done` when
  the emit's token matches the registered sink's token; **fail-open** when a
  token is absent (never drops legitimate messages).
- Optional: persist `turn_token` in message `metadata_json` so the inbox
  preview can prefer the current turn.

This is also a **prerequisite for the queue** (see "Flush trigger").

### #2 — surface backend errors + reach a terminal state (still needed)
#359 folds errors into `type="notify"` with no distinct surfacing and no
stream termination. Required behavior (user): show the backend's error text in
the Chat UI **and stop the spinner**, for both **startup** failures and
**mid-run** `error`/`notify` failures.
- Already done (pre-merge, survived): synchronous "no agent dispatched" path →
  `MessageHandler._stream_terminal_error` (streams `kind="error"` + marks turn
  complete).
- To add: a real **error message type** (or a flagged `notify`) persisted +
  rendered distinctly; the backend error emit must `_stream_chunk(kind=error)`
  **and** signal turn-complete so `dispatch_turn` closes the stream — there is no
  turn-duration timeout, so a real terminal signal is the only thing that
  releases the held `await done.wait()`.

## #3 — Message queue (the feature)

Reference: Codex GUI — while the agent is "Thinking", the user keeps typing;
sent messages stack above the composer and fire when the turn finishes.

### Goals
1. User can send messages while a turn is in flight (no refusal).
2. Queued messages render below the transcript / above the composer.
3. A dedicated, persisted queue (survives reload) — its own table.
4. On the turn's `result`, the queue flushes **in order**; multiple queued
   messages are **merged into one** dispatch.

### Data model (new table, per user's spec)
`message_queue` (new SQLAlchemy model + alembic migration):
- `id` (pk), `scope_id`, `session_id` (fk, indexed), `text`,
  `created_at`, `position` (ordering), `status` (`pending` default).
- New `core/services/message_queue.py` (or extend `messages_service`):
  `enqueue(session_id, text)`, `list_pending(session_id)`,
  `pop_all(session_id)` (read-ordered + delete atomically), `remove(id)`.

### Flow
1. **Enqueue (send while busy).** UI server `POST /api/sessions/{id}/messages`:
   if a turn is in flight for the session (ask the controller via a small
   `/internal/turn-state/{id}` or reuse the cancel/in_flight knowledge),
   insert into `message_queue` instead of dispatching; publish a
   `queue.updated` event (reuse the inbox bridge) so the browser shows it.
2. **Display.** New UI: pending items above the composer (delete per item),
   driven by `queue.updated` + an initial `GET /api/sessions/{id}/queue`.
3. **Flush on turn end.** When the turn's `result` settles (the clean signal
   is the same turn-complete that #1's `turn_token` / `done_event` gives us —
   so #1 lands first), the controller (or UI server on `turn.end`)
   `pop_all(session_id)`, **merges** the texts (ordering + a separator), and
   re-enters `dispatch_turn` as a single new turn. Empty queue → no-op.
4. **Errors / stop.** If the turn ends via error (#2) the queue should still
   flush (configurable) — or hold + surface. **Stop**: decide whether `/stop`
   also clears the pending queue (likely yes — user intent changed).

### Open questions (need your call)
- **Merge format**: join queued texts with `\n\n`? Or numbered list? Or send
  as one turn with all as context?
- **Flush on error**: after a failed turn, auto-flush the queue or hold it?
- **Stop semantics**: does Stop clear the pending queue or keep it?
- **"Steer"** (from the Codex GUI): inject a queued message *into the running
  turn* rather than after it — out of scope for v1? (It needs backend
  mid-turn input support; Claude/Codex/OpenCode differ.)
- **Where the flush fires**: controller-side (cleanest — it owns turn end +
  dispatch) vs UI-server-side (sees `turn.end` but must re-POST).
  Recommendation: controller-side, triggered by the same turn-complete signal
  #1 introduces.

### Suggested sequence
1. #1 `turn_token` (gives a clean turn-boundary signal + fixes cross-feed).
2. #2 error/terminal (so a failed turn ends deterministically — the queue
   needs a reliable "turn is over" signal whether it succeeded or failed).
3. #3 queue on top (table + enqueue + display + flush-on-complete).

#1 and #2 are small, shared turn-lifecycle fixes; #3 is the feature that
depends on a reliable turn-boundary, which #1/#2 establish.

## FINALIZED decisions (2026-05-31, from Alex)

The open questions are resolved; this is the spec to build.

### Queue behavior
1. **Enqueue while busy.** While a turn is running, messages the user sends
   go into the queue (not dispatched, not refused).
2. **Merge = newline-join.** Multiple queued messages flush as ONE dispatch,
   joined with newlines.
3. **Flush on `result`.** When the agent's `result` lands, the queue
   auto-flushes (the merged message becomes the next turn). Controller-side
   (it owns turn-end + dispatch).
4. **On error → HOLD.** If the turn ends in error, do NOT auto-flush; hold the
   queue (the user decides). (Needs #2's deterministic terminal state.)
5. **Stop does NOT clear the queue.** `/stop` interrupts the turn but leaves
   queued messages intact.

### "Send now" (立即发送) — per-item button
- Each queued item has a **Send now** button (per the Codex GUI screenshot).
- Clicking it = **interrupt the current turn and insert the message
  immediately** (don't wait for the turn to finish).
- **Reuse the existing IM interrupt logic** = the same `/stop` path already
  reused for cancel (Claude interrupt / Codex turn-interrupt / OpenCode abort),
  then dispatch that queued message as the new turn.
- IM platforms keep their current behavior — **no queue on IM**; queue is
  avibe/Web-only.

### Draft type (added requirement)
- The queue table carries a **`type`** column: `queued` (pending sends) +
  **`draft`** (the unsent text currently in the composer).
- `draft` persists the composer text so switching sessions/pages and back
  **restores the unsent draft**. One draft row per session (upsert on edit;
  cleared on send).
- **Same table** as the queue — no extra table.

### Table shape (revised)
`message_queue` (or `session_pending`): `id`, `scope_id`, `session_id`,
`type` (`queued` | `draft`), `text`, `position`, `created_at`, `updated_at`.
- `queued`: ordered list per session, flushed/merged on result or sent-now.
- `draft`: at most one per session, upserted as the user types (debounced),
  removed when the message is actually sent.

### Folded-in Codex P2s (from review of 3bb70f2 — all on my code, do in this batch)
1. ChatPage: don't clear streamed reply if the post-send reload fails.
2. internal_server: identity-guard the `in_flight` pop (stale stream must not
   evict a newer turn's slot).
3. message_handler: filter session `reasoning_effort` per backend support.
4. internal_server cancel: capture the runtime subagent session too.
5. ChatPage: picker rows (`RouteItem`) use the shared `Button` primitive.
(#2/#3/#4 sit in the turn-lifecycle area touched by #1/#2; #1/#5 are quick.)

## UPDATE (2026-05-31): #1 turn-token attempt reverted; #2 landed

**#1 (cross-feed) — turn-token gate tried + REVERTED (Codex P1).**
Implemented a per-turn token (dispatch stamps it into the context; `_stream_chunk`
dropped emits whose context token != the sink's). It broke Claude: Claude reuses
ONE long-lived receiver per session that emits the CURRENT turn while carrying an
EARLIER turn's context (the documented stale-per-turn-context), so the gate
dropped Claude's legitimate current-turn chunks. Reverted — `_stream_chunk`
forwards by session key again; all backends stream correctly.
- The straggler cross-feed is a DEFERRED known edge. A correct fix must have each
  backend tag emits with the current turn id IT knows at emit time (Claude's SDK
  knows), NOT a token riding the stale context. Rare in practice.

**KEPT (token-safe, landed):**
- `mark_turn_complete` token guard — its callers pass the turn's OWN fresh
  context (codex per-turn, opencode awaited, claude sync-error, handler finally),
  so a superseded turn can't close a newer turn's stream.
- **#2 turn-end hooks** on codex (turn/completed failed/interrupted/inactive),
  opencode (handle_message finally after the awaited poll) and claude
  (synchronous failure) release the web-Chat stream on a no-result failure, so a
  failed turn ends the spinner instead of waiting the 600s safety timeout. All
  `mark_turn_complete` calls are defensive (getattr). Residual edge: a Claude
  silent/empty result via the reused receiver still relies on the timeout
  (the mark guard over-skips on its stale context) — minor.

**Remaining for tomorrow:** ChatPage Codex P2s (clear stale model/effort on agent
switch; RouteItem → shared Button; keep streamed reply if post-send reload fails);
then build #3 (the queue: table with queued/draft types, enqueue-while-busy,
flush-merge on result, send-now=stop+insert, draft persistence) per the FINALIZED
decisions above. Codex review of the pushed state (8a5f6f6) passed with no comments (👍).

## DIRECTION CHANGE (2026-05-31, from Alex): session/page-scoped stream, NOT per-turn

Root realization: the web Chat stream is currently TURN-scoped — a user send opens
a `?stream=1` SSE, the turn's reply streams, and the stream closes when the turn
ends. That coupling is the ROOT of #1 (cross-feed) and #2 (terminal-state), and it
can't show agent replies the user didn't trigger (scheduled-task / watch /
proactive messages into the session).

Correct model: the Chat stream is SESSION + PAGE scoped — open as long as the page
is open, showing EVERY message in the session (user / agent / notify) regardless of
what triggered it. Sending a message becomes a plain fire-and-forget POST; the reply
arrives over the persistent session stream.

Foundation already exists (#359): the inbox rides a session-level event bridge
(controller persists -> inbox_events.bus -> /internal/events -> inbox_bridge ->
sse_broker -> browser). The Chat page should ride the SAME bridge, subscribing to
this session's message events.

How it dissolves the backlog:
- #1 cross-feed: GONE by design — no per-turn stream to cross-feed; a stopped
  turn's late straggler is just a late agent message appended to the session
  (which Alex already said is acceptable). No turn-token needed.
- #2 terminal-state: mostly GONE — no per-turn stream to "close". Errors show as
  persisted error messages over the stream. "Thinking" becomes a session-level
  "agent is working" signal (turn.start/turn.end as session events), not a stream
  lifecycle.
- Stop: still interrupts the BACKEND (the /stop mechanism), but does NOT tear down
  the page stream.
- #3 queue: fits naturally — flush -> new turn -> its reply arrives over the same
  persistent stream.

Implementation sketch (tomorrow):
1. `persist_agent_message` publishes a session-scoped `message.new` (the row) for
   agent messages too (today it publishes `inbox.session.updated`; user sends
   already publish `message.new`).
2. Chat page subscribes to this session's `message.new` over the existing
   `/api/events` bridge + appends; initial load via `list_session_messages`.
3. Sending = plain POST (drop the `?stream=1` reliance for closure).
4. "Agent working" indicator from a turn-active session signal.
5. Stop unchanged (interrupts backend; stream stays open).

This SUPERSEDES per-turn streaming: the #1 token work (already reverted) and the
#2 turn-end stream-close hooks become obsolete — revert/replace them during the
rebuild. The error-SURFACING part of #2 stays (as persisted error messages).

## BUILD SPEC (2026-05-31, tonight): message provenance + session-scoped stream

### A. Message provenance (new requirement from Alex)
Messages need a SOURCE/origin distinct from `author` (role). Reconcile with #359's
existing `author` / `author_id` / `author_name` (don't duplicate):
- **`source` (NEW column, user/agent/harness)** — the origin. NOT derivable from
  `author`: a Harness-triggered prompt has author=`user` (role) but source=`harness`.
- **name → reuse `author_name`**: username (user) / agent_name (agent) /
  `task`|`watch` (harness — the sub-type).
- **source_id → reuse `author_id`**: user_id (user) / task_id|watch_id (harness) /
  agent_id (agent).

Touch points (mirror the `type` column added by 20260531_0009):
1. `storage/alembic/versions/20260531_0010_messages_source.py` — `op.add_column`
   `source` (String, nullable) + backfill `update messages set source = case
   when author='user' then 'user' when author='agent' then 'agent'
   when author='system' then 'agent' else null end`. (harness rows can't be
   backfilled — only forward.)
2. `storage/models.py` messages table — add `Column("source", String, nullable=True)`
   + maybe `Index("ix_messages_source", "source")`.
3. `storage/migrations.py` — `LATEST_SCHEMA_REVISION = "20260531_0010"`;
   `HEAD_REQUIRED_COLUMNS["messages"] = {"type","source"}`; add a `source`
   add+backfill block in `_repair_head_required_columns` next to the `type` block
   (drift-repair fallback).
4. `storage/messages_service.append(... source=None ...)` — store it.
5. Populate at persist points:
   - user send (`vibe/ui_server.py` POST /messages): source="user",
     author_name=username, author_id=user_id.
   - agent reply (`core/message_mirror.py:persist_agent_message`): source="agent",
     author_name=agent_name, author_id=agent_id.
   - Harness (scheduled task / watch dispatch into a session): source="harness",
     author_name="task"|"watch", author_id=<task/watch id>. (Find the
     scheduled/watch dispatch path; persist the injected prompt with this
     provenance so the session stream shows it.)
6. Surface `source`/name in the API + render distinctly in the Chat transcript
   (e.g. a "Scheduled task" / "Watch" tag for harness-origin rows).

### B. Session/page-scoped stream (the pivot, expanded)
- `persist_agent_message` (and the user-send path) publish a **session-scoped
  `message.new`** (the full row incl. source/name) onto `inbox_events.bus`
  → `/internal/events` → `inbox_bridge` → `sse_broker` → browser (the #359 path).
- Chat page: on mount, open the persistent `/api/events` subscription, filter
  `message.new` for THIS session → append; initial load via `list_session_messages`.
  Stays open while the page is open (NOT per-turn).
- Sending = plain `POST /api/sessions/{id}/messages` (no `?stream=1`); reply
  arrives via the stream. "Agent working" indicator = a session-level turn-active
  signal (turn.start/turn.end as session events on the same bus).
- Stop: unchanged (interrupts backend); does NOT tear down the page stream.
- This RETIRES the per-turn `?stream=1` proxy + the #1 turn-token + #2 stream-close
  hooks (keep #2's error-as-message surfacing).

### C. Build order (safe-first; migration is highest-stakes → one step at a time + test)
1. Provenance schema (A.1–A.4) + tests (`test_sqlite_state_migration`,
   `test_messages_service`, fresh `ensure_sqlite_state` in a tmp dir). ← do first, verify.
2. Populate provenance (A.5) at user/agent paths; harness path after.
3. Session-scoped `message.new` publish + Chat page persistent subscription (B).
4. Switch send to plain POST; derive the working-indicator from session events (B).
5. #3 queue (table queued/draft, enqueue-while-busy, flush-merge on result,
   send-now=stop+insert, draft persistence) on top.
6. Retire per-turn stream + revert obsolete #1/#2 token/hook code.
