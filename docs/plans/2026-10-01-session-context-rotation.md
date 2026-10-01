# Session Context Rotation

Status: proposed (2026-10-01)

## Background

Avibe is moving toward a small set of long-lived built-in agents (main
assistant first; memory and ops roles later) that the user talks to instead of
managing many sessions. A main-agent session is an ordinary `agent_sessions` row
with a type marker; its transcript stays in `messages` like every other session.
The desktop pet will bind to that one session id.

A session that never ends will eventually fill its backend context window.
Backends auto-compact today, but a perpetual session needs more than that:

- compaction is lossy and repeats indefinitely; Avibe cannot see or shape what
  it keeps, and it does not know which runs, watches, or approvals are still
  open for the session;
- the backend transcript behind a resumed native session grows without bound;
- a bound session cannot change backend (`SessionBackendLockedError`), and a
  vanished native session fails the turn loudly. Neither is acceptable for a
  session that is supposed to last for months.

## Decision

Keep one Avibe session and swap the backend-native session underneath it.

The model already separates the two layers this needs:

- `agent_sessions.id` is the conversation the user sees. Messages, deliveries,
  turns, unread state, the queue, SSE events, Harness callbacks
  (`agent_runs.callback_session_id`), run definitions, and the Workbench URL all
  key on it.
- `agent_sessions.native_session_id` is the backend context (Claude session id,
  Codex thread id, OpenCode session id).

Context rotation replaces the second without touching the first, and hands the
new native session a bounded handoff. It is a session-layer mechanism available
to every session; the main agent is its first user. No `conversation` table is
introduced: it would duplicate the session row and split every consumer above
between two owners.

### Concrete example

1. The user has talked to the main agent (session `S`, native Claude session
   `n1`) for weeks. A delegated run `R` is still running with
   `callback_session_id = S`.
2. A turn ends with context occupancy at 62% of the window. Avibe marks `S` as
   rotation-due.
3. The user sends the next message. Before the backend is invoked, Avibe
   archives `n1` as a superseded snapshot row, clears `S.native_session_id`,
   releases the backend's cached runtime for `S`, and prepends a handoff block
   to this turn's input.
4. Claude starts a fresh native session `n2`; the ordinary first-bind path
   records it on `S`.
5. `R` finishes. Its callback targets `S` and lands in `n2` with no special
   handling. The user's transcript, the pet's binding, and the Workbench URL
   never changed.

## Design

### Rotation primitive (storage)

Generalize `SQLiteSessionsService.replace_agent_session_native` into a
"supersede native binding" operation whose replacement may be empty:

- In one write transaction: verify the expected native id, insert the existing
  inert snapshot row (`<anchor>:superseded:<id>`, archived, background), then
  set the active row's `native_session_id` to `""`.
- Record why in the snapshot's `superseded_native_binding` metadata:
  `reason`, `context_tokens`, `context_window`. The snapshot rows are the
  rotation history; no new table or event type is added.
- Set `metadata_json.context_handoff = {"pending": true, "rotated_at", "from_native", "reason"}`
  on the active row.

Clearing to empty instead of pre-creating a replacement lets every backend
reuse its normal first-bind path. This matters for Claude, which only learns a
new session id from the init message of the first query.

### Execution point (core)

Rotation runs only at turn start: after the durable turn is `starting` and
before the backend target (`agent_session_target`) is built from the row, so the
context carries the empty native id. It never runs mid-turn; steering and
interrupts target the live native turn.

Every trigger only sets a due marker (`metadata_json.context_rotation_due`).
That single turn-start chokepoint consumes it. A rotation requested while a turn
is running therefore takes effect on the next turn and cannot race it.

### Backend runtime release (agent abstraction)

Clearing the row is not enough, because backends cache live runtimes:

- Codex checks the in-memory `_threads[base]` before the database;
- Claude reuses a cached SDK client per composite key.

Add one backend-neutral hook, `BaseAgent.release_native_runtime(session)`.
Its default is a no-op:

- Claude: `_cleanup_runtime_session(...)`;
- Codex: `invalidate_thread(...)` plus clearing cached developer instructions;
- OpenCode: no-op, to be verified, since its per-request state is already
  cleared.

The existing `clear_sessions` paths cannot be reused. They delete or archive the
Avibe rows that `/new` retires.

### Handoff

Built at the chokepoint and prepended once to the first turn of the new native
session, as a bounded block in the turn input. It does not go into the system
prompt, so system prompt bytes stay stable for backend caches. Rendered from a
`core/prompts/` template:

1. **Continuity notice.** This is a continuing conversation in Avibe session
   `S`; the earlier backend context was rotated.
2. **Recent transcript.** The tail of `list_session_messages(...,
   types=TRANSCRIPT_TYPES, tail=True)`, newest first until a fixed character
   budget, then restored to chronological order.
3. **Open work**, read from the durable store at that moment:
   - `derive_session_harness_activities(conn, S)`: watches, tasks, and delegated
     runs awaiting callback;
   - queued deliveries;
   - pending vault requests for `S`.
4. **History pointer.** Older history is available on demand through
   `vibe data query` over `messages` where `session_id = S`. The memory package
   was removed in #2120, so on-demand query replaces memory retrieval.

`context_handoff.pending` is cleared only when the new native id is bound. If
the first turn fails before binding, the next turn rebuilds the handoff from
fresh state.

v1 summaries are mechanical; there is no LLM summarizer in the runtime today.
An agent-authored handoff note is a follow-up, added only if transcripts show
material loss.

### Context pressure trigger

Context occupancy is in memory only today (`MessageDispatcher._session_token_total`)
and is lost on restart. Persist the last observation per session at turn end in
`metadata_json.context_usage = {"tokens", "window", "observed_at"}`:

- Claude: per-assistant-message usage (`_extract_context_tokens`). The window
  comes from the Model Hub launch config; whether the SDK reports the window
  for official models is to be verified.
- Codex: `tokenUsage.last.totalTokens` and `tokenUsage.modelContextWindow` from
  `thread/tokenUsage/updated`.
- OpenCode: none yet (follow-up: read step-finish token parts).

The rotation is due when `tokens >= 0.6 * window` and both values are known.
The ratio sits below backend auto-compaction thresholds, so Avibe rotates before
the backend compacts and leaves headroom for the handoff. It is an internal
constant, not a user setting. When the window or tokens are unknown, automatic
rotation does not fire and the backend's own compaction remains the fallback.

### Policy

- Automatic rotation is a per-session policy,
  `metadata_json.context_rotation = "auto"`. It is off by default, so existing
  sessions behave exactly as today.
- Main-agent sessions will default to `auto` when the session type lands.
- Explicit rotation works on any session: `vibe session rotate <session-id>
  [--reason <text>]` sets the due marker. It is useful for testing and for an
  agent that wants to rotate itself at a natural break.

## Persisted shape

All additions are optional `metadata_json` keys on `agent_sessions`:
`context_rotation`, `context_rotation_due`, `context_handoff`, and
`context_usage`. The snapshot rows also get extra `superseded_native_binding`
fields. There is no schema migration:

- older releases ignore the keys;
- rows written by older releases have no keys and keep today's behavior.

## Invariants and tests

- Rotating preserves everything keyed by the session id. Seed one row of every
  table that references the session, rotate, and assert that every seeded row is
  unchanged and still points at `S`.
- After rotation the active row has an empty native id, and exactly one new
  archived snapshot row holds the previous id.
- The next turn binds a fresh native id through the normal bind path, receives
  the handoff exactly once, and clears `context_handoff.pending`.
- A failed first turn leaves `pending` set, and the following turn delivers the
  handoff.
- No rotation happens while a turn is live. A request made mid-turn takes effect
  at the next turn start.
- Per backend: after release, a turn does not reuse the cached runtime or thread
  for `S`.
- A session without `context_rotation = "auto"` never rotates automatically,
  whatever its usage.

## Follow-ups (same primitive, separate PRs)

- Self-heal when the native resume target is gone (Claude/Codex currently fail
  the turn) for sessions with rotation enabled.
- Backend switch on a bound session through rotation instead of
  `SessionBackendLockedError`.
- OpenCode context usage.
- Agent-authored handoff note.

## Risks to verify during implementation

- Any turn entry path that does not pass the chokepoint, or that carries a
  cached native id (`_cached_target` in `agent_run_target.py`; non-durable IM
  paths), would resume the old context. Inventory them all and route them
  through the chokepoint.
- Backend runtime markers in `metadata_json` are keyed to the old native. Codex
  re-checks `marker.thread_id`; audit the rest.
- Snapshot rows must stay invisible to sidebar and archive listings, native
  session listing, and the Claude process reaper, as they are for the existing
  OpenCode repair.

## Todo

- [ ] Storage: supersede-to-empty plus handoff and due markers.
- [ ] Turn-start chokepoint: consume the due marker, rotate, release the
      runtime, inject the handoff.
- [ ] `BaseAgent.release_native_runtime`, implemented for Claude and Codex.
- [ ] Handoff prompt template and builder.
- [ ] Persist context usage at turn end (Claude, Codex) and evaluate the 0.6
      rule.
- [ ] `vibe session rotate` CLI and per-session `context_rotation` policy.
- [ ] Tests for the invariants above.
- [ ] Update `skills/use-avibe` docs for `vibe session rotate`.
