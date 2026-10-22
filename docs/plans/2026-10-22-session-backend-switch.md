# Session Backend Switch

Status: implemented (2026-10-22)

## Background

A session that already has a backend-native conversation cannot change backend
(`SessionBackendLockedError`). The lock exists because a native session can only
be resumed by the backend that created it; switching the backend while the row
still held the old native made the new backend fail on a foreign id.

The main reason to lift it is to let people move existing sessions onto Vibey.

## Goal

- A bound session on Claude Code, Codex, or OpenCode can switch to any enabled
  Agent, including Vibey and the other native backends.
- A bound Vibey session stays on the Vibey backend. Agents within the Vibey
  backend remain switchable, as before.
- The Avibe session id, transcript, deliveries, Harness callbacks, Watches,
  Tasks, and the Workbench URL do not change. Only the backend-native context
  underneath is replaced.
- The first turn on the new backend receives a bounded handoff so the work
  continues.

This is the backend-switch follow-up of
`docs/plans/2026-10-01-session-context-rotation.md`, without that plan's
context-pressure triggers or automatic policy.

## Rules

A cross-backend change on a session with a bound native is accepted only when:

- the current backend is not `vibey`;
- the session is not running (controller in-flight registry and
  `agent_status`), and it is not a pending fork;
- the controller is reachable and reports no backend Activity and no unclaimed
  Activity output for the session. A detached Claude Activity runs on the old
  client and would otherwise keep producing output for a session that moved.

Refusals stay retryable `409`s: `backend_locked` (running, pending fork, bound
Vibey) and `backend_busy` (background backend work, controller unreachable).

Unbound sessions keep today's behavior.

## Design

### Switch (storage, one transaction)

`update_session` takes the bound-switch branch:

1. Snapshot the current binding as an inert archived row
   (`<anchor>:superseded:<id>`), the shape `replace_agent_session_native`
   already writes. The insertion is shared by both callers.
2. Clear `native_session_id` and apply the new Agent route.
3. Record `metadata_json.backend_handoff = {id, from_backend, from_native,
   to_backend, switched_at}`.

The UPDATE re-asserts the state the decision was made from: same native, same
backend, not running, not archived.

### Stale bind guard

While `backend_handoff` is present, `bind_agent_session_by_id` refuses to bind
`from_native` again. A late init or thread event from the old backend's runtime
therefore cannot rebind or relabel the switched row.

### First turn on the new backend (controller)

At dispatch, in `MessageHandler`, when the session target carries
`backend_handoff`:

1. If the row has no native yet, release any runtime the target backend still
   caches for this session (`BaseAgent.release_native_runtime`): Claude drops
   its SDK client, Codex unloads its thread mapping. OpenCode and Vibey cache
   nothing that survives an empty native. This covers A -> B -> A, where A
   may still hold the old conversation in memory.
2. Build the handoff off the loop and attach it to `AgentInputMetadata`, which
   every backend renders at its last write boundary. The canonical Message
   stays the user's text.
3. Tag the turn with the handoff id.

`SessionTurns.on_native_start` clears the marker inside the native-start receipt
transaction, compare-and-set on the id. A turn that fails before the native
accepts it leaves the marker, so the next turn resends the handoff.

A forked session's spent fork source (`fork_source_native_session_id`,
`fork_source_backend`) is dropped by the switch, so the emptied native cannot
re-arm a native fork or pin the picker to the fork's backend.

### Handoff content

One bounded block, rendered in English for the model:

1. Continuity notice: Avibe session id, previous backend, fresh context.
2. Open Harness work bound to the session (delegated runs, Watches, Tasks), from
   `derive_session_harness_activities`, truncated with an omitted count.
3. Recent transcript tail, newest first until the character budget is spent,
   then shown in order. Each message is clipped.
4. A runnable `vibe data query` for the full history.

## Known by design

- The handoff is lossy: tool calls, file reads, and reasoning of the old backend
  are not carried. The workspace on disk is shared, and the full transcript is
  queryable.
- An idle runtime left on the old backend is reclaimed by that backend's idle
  eviction. It is idle by the switch rule and cannot rebind the row.
- Legacy non-durable turns do not record a native-start receipt; on that path
  the handoff can be sent again on the next turn.

## Todo

- [x] Plan
- [x] Storage: shared snapshot insert, bound-switch branch, Vibey lock
- [x] Stale bind guard
- [x] Handoff builder and `AgentInputMetadata.handoff`
- [x] Dispatch: release, inject, tag; receipt clears the marker
- [x] `release_native_runtime` for Claude and Codex
- [x] PATCH preflight (`backend_busy`)
- [x] Chat header picker rule and error copy
- [x] Tests
- [ ] Public docs (avibe-docs, en and zh)
