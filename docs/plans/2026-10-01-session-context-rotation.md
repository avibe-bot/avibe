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
2. A turn ends with context occupancy at 62% of the window. Avibe records that
   peak on `S`.
3. The user sends the next message. At turn start the recorded peak is past
   the ratio, so `S` is rotation-due. Before the backend is invoked, Avibe
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

### Rotation lifecycle (core)

Rotation has two persisted phases. Both are driven from one turn-start
chokepoint: after the durable turn is `starting` and before the backend target
(`agent_session_target`) is built from the row. Rotation never runs mid-turn,
because steering and interrupts target the live native turn.

1. **Due.** A session is due when the explicit marker
   `metadata_json.context_rotation_due` is set, or when its persisted
   `context_usage` meets the pressure rule and its policy is `auto` (see
   Context pressure trigger). Triggers only write state and never rotate, so a
   request made while a turn is running takes effect on the next turn and
   cannot race it.
   - At the chokepoint, a due session that is not already pending first asks
     the backend whether its native runtime is settled
     (`native_runtime_settled`, below). If the runtime is not settled, rotation
     is deferred: `due` stays set and this turn runs on the current native as
     usual. The check is repeated at every later turn start.
   - Once the runtime is settled, the storage supersede runs. The same
     transaction clears `due` and sets `context_handoff.pending`.
   - A due session with no bound native has nothing to rotate; `due` is simply
     cleared.
2. **Pending** (`metadata_json.context_handoff.pending`). On every turn start
   while pending:
   1. Release the backend runtime for the session (idempotent; see below).
      If release fails, the turn fails before dispatch and `pending` stays set,
      so the next turn retries the release. A handoff is never sent through the
      old cached runtime.

      The supersede only ran after the runtime was settled. A pending turn
      never dispatches before release succeeds. So no backend work can start on
      the old runtime between the settled check and the release.
   2. Build the handoff from fresh durable state and prepend it to this turn's
      input. The native id is either empty (the normal case) or already bound
      to a new native that never accepted input. The second case happens when
      Codex `_start_thread` binds the thread and the following `turn/start`
      fails. Either way the new backend context has not seen a handoff, so
      sending one is correct.
   3. Clear `pending` only on acceptance evidence. That evidence is the native
      start receipt of the turn that carried the handoff; binding a native id
      is not enough.
      - When the handoff is injected, the durable turn records the
        `context_handoff.rotated_at` it carries.
      - `SessionTurns.on_native_start` already writes the receipt
        (`bind_native_start`) inside one SQLite transaction. The pending clear
        joins that same transaction: compare-and-set on the recorded
        `rotated_at`, so a late receipt cannot clear a newer rotation.
      - The receipt and the clear therefore commit together or not at all. A
        native that accepted the handoff never receives it again.

The phases are never skipped. The handoff is resent only when no receipt was
committed. The one case where the native may have accepted it anyway is a
process crash between native acceptance and the receipt transaction. That is
the same window in which ordinary turn delivery can already duplicate a turn,
and it is not widened.

### Backend runtime release (agent abstraction)

Clearing the row is not enough, because backends cache live runtimes:

- Codex checks the in-memory `_threads[base]` before the database;
- Claude reuses a cached SDK client per composite key.

An idle turn boundary also does not mean the runtime is idle. Claude admits a
new human turn while a detached background Activity is still running on the
same SDK client (`docs/plans/claude-result-provenance.md`). Releasing that
client would orphan the Activity and lose its output. The handoff cannot carry
that output either, because it lists only durable Harness items.

Two backend-neutral hooks cover this.

`BaseAgent.native_runtime_settled(session) -> bool` is the rotation
precondition. Its default is `True`:

- Claude: `False` while the backend-neutral `SessionActivityRegistry` has an
  active Activity or completed, unclaimed Activity output for the session's
  runtime key (`has_active`, `has_completed_output`), or the agent still holds
  an Activity output record. This is the same evidence that
  `_activity_output_pending` already reads.
- Codex and OpenCode: `True`. Neither registers detached Activities today, and
  the chokepoint already guarantees no live native turn. A backend that later
  registers Activities must implement the hook.

`BaseAgent.release_native_runtime(session)` drops the cached runtime. Its
default is a no-op:

- Claude: `_cleanup_runtime_session(...)`;
- Codex: `invalidate_thread(...)` plus clearing cached developer instructions;
- OpenCode: no-op, to be verified, since its per-request state is already
  cleared.

The existing `clear_sessions` paths cannot be reused. They delete or archive the
Avibe rows that `/new` retires.

Release must be idempotent and cheap when nothing is cached, because it runs on
every pending turn start. Each backend's release must be complete: it removes
every cache that a resume consults before the database.

### Handoff

Built at the chokepoint while `pending` is set, as one block in the turn input.
It does not go into the system prompt, so system prompt bytes stay stable for
backend caches. Rendered from a `core/prompts/` template.

**One budget covers the whole block.** The budget is a small fraction of the
session's known context window, or a fixed character cap when the window is
unknown. Sections fill it in priority order. Every list is truncated to what
fits and ends with the count of omitted items plus the command that lists them
all, so the size stays bounded however much open work exists.

1. **Continuity notice.** This is a continuing conversation in Avibe session
   `S`; the earlier backend context was rotated.
2. **Open work**, one line per item, read from the durable store at that moment.
   It comes before the transcript because the transcript cannot reconstruct it.
   Each item appears once. The builder deduplicates by identity key rather
   than relying on the sources being disjoint, because the sources are not
   written atomically with each other. For example, `_process_run_callback`
   enqueues a callback and only afterwards moves the parent run's
   `callback_status` from `pending` to `sent`, in a separate operation. A
   handoff built in that gap sees both.
   - The identity key of a delegated run is its run id. A queued callback
     carrying `parent_run_id` takes the same key.
   - When both appear, one line is kept: the run, marked "callback queued".
   - Other items are keyed by their own id.
   - **Delegated runs:** every `agent_runs` row with `callback_session_id = S`
     and `callback_status = 'pending'`, whatever its run status. This includes
     runs that have finished but whose callback has not yet been delivered.
     The handoff owns this query rather than reusing the banner helper, which
     shows only active runs.
   - **Watches and tasks** bound to `S`: the rows of
     `derive_session_harness_activities` whose `item_kind` is `watch` or
     `task`. That helper also returns active delegated runs (`agent_run`), which
     the query above already covers, so they are dropped here.
   - **Queued deliveries** for `S`.
   - **Pending vault requests** for `S`.
3. **Recent transcript.** The tail of `list_session_messages(...,
   types=TRANSCRIPT_TYPES, tail=True)`. It is taken newest first until the
   remaining budget runs out, then restored to chronological order.
4. **History pointer.** Older history is available on demand through
   `vibe data query` over `messages` where `session_id = S`. The memory package
   was removed in #2120, so on-demand query replaces memory retrieval.

v1 summaries are mechanical; there is no LLM summarizer in the runtime today.
An agent-authored handoff note is a follow-up, added only if transcripts show
material loss.

### Context pressure trigger

Context occupancy is in memory only today (`MessageDispatcher._session_token_total`)
and is lost on restart. Persist it as
`metadata_json.context_usage = {"peak_tokens", "window", "compacted", "observed_at"}`,
covering the current native since its last rotation.

**When it is written.** An observation is written whenever a native phase
settles, not only at an Avibe turn end:

- the end of an Avibe turn;
- the terminal `ResultMessage` of a detached Claude Activity phase, which can
  arrive after the originating turn has ended
  (`docs/plans/claude-result-provenance.md`).

Each write merges into the stored value through the locked per-key writer:

- `peak_tokens` takes the maximum of the stored value and the phase's peak
  snapshot, so a drop after a compaction does not hide the peak;
- `compacted` is OR-ed;
- `window` takes the latest known value.

The supersede moves `context_usage` into the snapshot row
(`context_tokens`, `context_window`) and clears it on the active row.

Occupancy, in tokens:

- Claude: per-assistant-message usage (`_extract_context_tokens`).
- Codex: `tokenUsage.last.totalTokens` from `thread/tokenUsage/updated`.
- OpenCode: none yet (follow-up: step-finish token parts).

Window, in priority order:

1. The backend's own report:
   - Claude: `ResultMessage.model_usage[<model>].contextWindow`. The field is
     present at both pinned floors, `claude-agent-sdk` 0.2.158 (Windows,
     `>=0.2.158,<0.2.160`) and 0.2.162 (other platforms). This also covers
     direct, non-Hub launches, whose `ModelHubLaunch` carries no
     `context_window`. A unit test that parses a result message pins the field
     at the Windows floor.
   - Codex: `tokenUsage.modelContextWindow`.
2. The configured Model Hub `context_window`.

Each source is optional at runtime. If a source reports no window, the next
one is used; if none does, the window is unknown and only the compaction
signal applies.

Backend compaction observed in any native phase sets `compacted`:

- Codex: `thread/compacted`; its handler is a no-op today.
- Claude: a `SystemMessage` with subtype `compact_boundary`. The CLI bundled
  at the 0.2.158 floor emits it.
- OpenCode: an assistant message carrying `info.summary`.

The rotation is due when either signal holds:

- **Ratio.** `peak_tokens >= 0.6 * window`, with both values known. The ratio
  sits below backend auto-compaction thresholds and leaves headroom for the
  handoff. It is an internal constant, not a user setting.
- **Compaction.** `compacted` is true. This covers a single turn that jumps
  past the backend threshold before any snapshot is emitted. It is also the
  only automatic signal for OpenCode and for any session whose window is
  unknown.

Both are evaluated at the turn-start chokepoint, from the persisted
`context_usage`. So pressure that a detached phase recorded while the session
was idle is seen by the next human turn. The rotation itself then follows the
lifecycle above.

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

Safe degradation. All four keys are read through one normalizer, and each key
is validated independently:

- A value that is malformed (wrong type, partial object, unparsable timestamp)
  or of an unknown newer shape is treated as absent for that key alone.
- A malformed value logs one warning per session and process.
- The normalizer never raises on the turn-start, turn-end, or startup paths.
- Writers always write the full canonical shape.

Concurrent writers. The rotation keys share the `metadata_json` column with each
other and with backend runtime markers. Several writers can touch it at the same
time: `vibe session rotate` setting `due` mid-turn, the usage writer,
the turn-start supersede, and the receipt clear.

Every write of a rotation key goes through one storage method. It follows
`set_agent_session_runtime_marker`: inside one transaction it takes
`reserve_write_lock`, re-reads the current metadata, changes only its own key,
and writes the result back. No writer persists a metadata snapshot read
earlier, so a usage write cannot erase a `due` marker set during the turn.

What "treated as absent" means for each key:

| Key | Behaves as |
|---|---|
| `context_rotation` | off |
| `context_rotation_due` | not due |
| `context_usage` | no observation |
| `context_handoff` | not pending |

The last row means that if an interrupted rotation leaves a malformed handoff,
the next turn starts a fresh native without a handoff. That degrades
continuity but does not fail dispatch.

## Invariants and tests

- Rotating preserves everything keyed by the session id. Seed one row of every
  table that references the session, rotate, and assert that every seeded row is
  unchanged and still points at `S`.
- After rotation the active row has an empty native id, and exactly one new
  archived snapshot row holds the previous id.
- The next turn binds a fresh native id through the normal bind path and
  receives the handoff. `context_handoff.pending` is cleared only by that turn's
  native start receipt.
- Any failure before the receipt leaves `pending` set, and the following turn
  delivers the handoff. The failures covered are a release error, a failure
  before bind, and a bind followed by a failed `turn/start`.
- A release failure blocks dispatch. No pending turn reaches the backend's old
  cached runtime.
- The handoff never exceeds its budget, for any number of open items, and every
  truncated list reports its omitted count.
- Every run with `callback_session_id = S` and a pending callback appears in the
  handoff, whatever its run status.
- A run whose callback is already queued while its `callback_status` is still
  `pending` appears once.
- A native phase whose occupancy peaks at or above the ratio, or that observes
  a backend compaction, makes the next turn start rotate, even if its last
  snapshot is lower. This holds for a detached Claude Activity phase that
  settles after its turn ended.
- A crash or failure after the receipt transaction does not resend the
  handoff. Seeding a committed receipt leaves `pending` cleared; with no
  committed receipt, `pending` stays set.
- The window comes from the first source that reports one. With none, ratio
  rotation is skipped and compaction still marks the session due.
- Load fixtures: rows with no rotation keys (every released shape), and rows
  with each key malformed or in an unknown shape. Turn start, turn end, and
  startup all succeed, and each key behaves as absent.
- Interleaving: set `due` between the usage writer's read and its write.
  Both `due` and `context_usage` survive, and the next turn rotates.
- A Claude session with an active detached Activity, or with completed Activity
  output not yet delivered, is not rotated. `due` stays set, the turn runs on
  the old native, and the Activity output is delivered. The next turn start
  after it settles rotates.
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
- Audit every other kind of process-local, per-session state that release
  drops, such as steering state, pending reactions, and queued requests. Each
  kind must either be settled under `native_runtime_settled` or be safe to
  drop at a turn boundary.
- Snapshot rows must stay invisible to sidebar and archive listings, native
  session listing, and the Claude process reaper, as they are for the existing
  OpenCode repair.

## Todo

- [ ] Storage: supersede-to-empty plus handoff and due markers.
- [ ] Turn-start chokepoint: consume the due marker, rotate, release the
      runtime, inject the handoff.
- [ ] `BaseAgent.native_runtime_settled` (Claude Activity gate) and
      `BaseAgent.release_native_runtime`, implemented for Claude and Codex.
- [ ] Handoff prompt template and builder.
- [ ] Persist peak usage and window on every settled native phase, including
      detached Claude Activity results (Claude, Codex). Observe backend
      compaction on all three backends. Evaluate the ratio and compaction rules
      at turn start.
- [ ] Clear `context_handoff.pending` inside the `on_native_start` receipt
      transaction.
- [ ] Single metadata normalizer with malformed-shape fixtures, and one
      locked per-key merge writer.
- [ ] `vibe session rotate` CLI and per-session `context_rotation` policy.
- [ ] Tests for the invariants above.
- [ ] Update `skills/use-avibe` docs for `vibe session rotate`.
