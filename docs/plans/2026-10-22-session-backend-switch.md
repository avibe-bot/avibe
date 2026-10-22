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
- no durable Turn of the session is unsettled, the row is not `running`, and it
  is not a pending fork. The Turn row is written by the controller under the
  same SQLite write lock before anything is dispatched, so it is the reservation
  the switch commits against; `agent_status` and the controller's in-memory
  registry both lag it;
- the controller is reachable and reports no backend Activity and no unclaimed
  Activity output for the session. A detached Claude Activity runs on the old
  client and would otherwise keep producing output for a session that moved.
  That answer is a point-in-time read, so the caller first reads the session's
  newest durable Turn id and the switch commits only if it is still the newest
  (`update_session(runtime_idle_through_turn=...)`). Every Activity starts in a
  turn, and every turn writes its row before dispatch, so an unchanged watermark
  means the controller's answer still holds at commit;
- the request selects an Agent on the new backend. The Agent identity routes the
  next turn ahead of `agent_backend`, so a backend-only edit is refused (`400`).

Refusals stay retryable `409`s: `backend_locked` (running or unsettled Turn,
pending fork, bound Vibey) and `backend_busy` (background backend work,
controller unreachable).

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
backend, not running, no unsettled Turn, the same newest Turn, not archived.

A backend change that names no model or reasoning effort resets both, with
their explicit-override marker: they belong to the old backend's catalog, and
the new Agent's defaults apply instead. The Chat header always sends the picked
Agent's pair; this protects API callers that send only the Agent.

A forked session's spent fork source (`fork_source_native_session_id`,
`fork_source_backend`) is dropped by the switch, so the emptied native cannot
re-arm a native fork or pin the picker to the fork's backend.

### While the handoff is pending

- `bind_agent_session_by_id` refuses a bind of `from_native`, or from any
  backend other than the row's. A late event from the old runtime or a one-off
  Agent run on another backend cannot rebind or relabel the switched row.
- The durable binding refresh carries the row's `native_session_id` into the
  routing projections, so a Delivery admitted before the switch cannot offer
  the replaced native to the new backend's resume helpers.

### First turn on the new backend (controller)

At dispatch, `MessageHandler` reads the session row by primary key, whichever
projection selected it (Workbench target or IM/CLI run target). A turn that
continues the session's own conversation on the switched-to backend (not a
namespaced subagent, not a one-off run on another backend):

1. If the row has no native yet, releases any runtime the target backend still
   caches for this session (`BaseAgent.release_native_runtime`): Claude ends its
   SDK client, Codex releases its thread binding. This covers A -> B -> A.
2. Carries the handoff in `AgentInputMetadata.handoff`, which every backend
   renders at its last write boundary. The canonical Message keeps the user's
   text.
3. Is tagged with the handoff id.

Any failure there fails the turn before dispatch; the next turn retries.

The marker is spent once both facts hold, in either order: a tagged turn's
native-start receipt (`accept_backend_handoff`, in the receipt transaction) and
the row bound to the new native (`settle_backend_handoff_on_bind`, in the bind
transaction). Claude reports acceptance before its init binds the id; Vibey,
Codex and OpenCode bind first. A native bound before any tagged turn was
accepted (a Codex thread whose `turn/start` failed) keeps the marker, so the
next turn still carries the handoff.

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
- A namespaced subagent turn after a switch starts its own conversation without
  the handoff; the session's own conversation still receives it.

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
