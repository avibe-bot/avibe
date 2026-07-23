# Contract: Agents Graph API + Session Visibility (FROZEN 2026-07-23)

Frozen interface contract for the two parallel lanes of
`agents-run-graph-and-session-visibility.md`. **Deviations require
orchestrator sign-off first** — do not negotiate field changes lane-to-lane.

Amendments (orchestrator-approved):

- **A1 (2026-07-23, PR #956)**: graph node gains `runs[]` (recent runs for
  the detail-panel timeline, newest first, capped at 10 — avoids an N+1
  fetch). Additive.
- **A2 (2026-07-23, PR #956)**: top-level `live_unreachable: boolean` —
  `true` when the controller/running-agents service is unreachable and the
  payload degrades to DB-only (client shows a degraded banner, mirroring the
  running list's `unreachable` pattern). Additive.
- **A3 (2026-07-23, PR #956 review r1)**: session-less orphan processes are
  **NOT synthesized as graph nodes** (nodes stay = `agent_sessions` rows;
  orphans have no session, no lineage, no chat). Capability restored via a
  client-side **orphan strip** above the canvas (and a section in the mobile
  list): rows sourced from the existing `GET /api/running-agents` payload
  (`session_id == null`), each with backend/pid/elapsed and the existing
  Kill flow (`POST /api/running-agents/end`, 2-step confirm). Graph payload
  unchanged. Session-anchored orphans keep rendering as `status:orphan`
  nodes.
- **A4 (2026-07-23, PR #956 review r1)**: callback edges are emitted **only
  when `callback_status` is non-null** (a recorded outcome/progress state).
  Sync-delegated runs that merely record a callback route (null status,
  nothing will be delivered) do not produce an edge. Supersedes the earlier
  "null → pending" mapping.
- **A5 (2026-07-23, PR #956 review r1)**: `include_ended=0` means
  **non-terminal sessions** — live (`active|idle|orphan`) **plus `queued`**
  — not "live only". Queued work is upcoming and must be visible in the
  active view; the counts object keeps `queued` populated accordingly.
- **A6 (2026-07-23, cross-lane)**: visibility reconcile **event contract**:
  backend (M1, PR #957) emits the existing `session.activity` SSE event with
  the session's current `visibility` on every PATCH/CLI visibility or scope
  change. Frontend consumption is **M2's (PR #956)**: the Inbox context
  removes a session card when an event carries `visibility='background'`
  (and refetches/re-adds on `'foreground'`); absent `visibility` field ⇒
  no-op (defensive pre-M1). M1 does not touch `ui/**` for this; the gap is
  recorded in #957's inventory as assigned to #956.
- **A7 (2026-07-23, PR #956 review)**: graph endpoint path renamed
  `/api/agents/graph` → **`/api/agents-graph`**. Rationale: `/api/agents/…`
  is the agent-resource namespace and agent names are user-creatable slugs —
  an agent literally named `graph` would shadow the route. Endpoint and its
  sole consumer live in the same PR; no migration cost. Reserving the word
  `graph` in the agent-name domain was rejected.

## 1. Visibility enum (M1 owns, M2 consumes)

- `agent_sessions.visibility` ∈ `"foreground" | "background"`.
  No other values. Absent/legacy rows read as `"foreground"` after backfill.
- Standalone session: `agent_sessions.scope_id IS NULL`.

## 2. Session update (M1 owns)

`PATCH /api/sessions/<session_id>` accepts (in addition to existing fields):

```json
{ "visibility": "background" }
{ "scope_id": "avibe::project::proj_272e944ca452" }
{ "scope_id": null }
```

- `visibility` and `scope_id` are independent; either may appear alone.
- Response: the standard session payload, now always including
  `visibility` and (nullable) `scope_id`, `project_id`.
- Errors: `400` invalid value; `404` unknown session. Changing `scope_id`
  never mutates the session's stored `workdir`.

CLI equivalent: `vibe session update [--visibility foreground|background]
[--scope-id <scopes.id>|none]`.

## 3. Graph endpoint (M2 owns; shape frozen for both)

`GET /api/agents-graph?window=24h&project=<project_id|all|standalone>&include_ended=1&include_background=1`
(path per amendment A7)

- `window`: `1h|6h|24h|7d` (default `24h`); bounds history lookback
  (`agent_runs.created_at >= now - window`, plus all currently-live
  sessions regardless of window).
- `include_ended=0` ⇒ non-terminal sessions: live (`active|idle|orphan`)
  plus `queued` (amendment A5). `include_background=0` ⇒ foreground nodes
  only (edges to hidden nodes are dropped with them).

Response:

```json
{
  "ok": true,
  "generated_at": "2026-07-23T02:00:00Z",
  "window": "24h",
  "live_unreachable": false,
  "counts": { "active": 3, "idle": 1, "queued": 1, "ended": 4,
              "background": 5, "foreground": 3 },
  "nodes": [
    {
      "session_id": "ses7y3jff7b6r",
      "title": "Vaults M2 · 总控分派",
      "agent_name": "pm",
      "agent_backend": "claude",
      "model": "claude-fable-5",
      "reasoning_effort": "max",
      "status": "active",
      "live": true,
      "visibility": "foreground",
      "scope_id": "avibe::project::proj_272e944ca452",
      "project_id": "proj_272e944ca452",
      "scope_label": "vibe-remote",
      "platform": "avibe",
      "workdir": "/Users/cyh/vibe-remote-project",
      "openable_in_chat": true,
      "created_at": "2026-07-21T10:10:22Z",
      "last_active_at": "2026-07-23T01:58:00Z",
      "elapsed_seconds": 4320,
      "run_counts": { "total": 6, "running": 1 },
      "runs": [
        { "id": "run_9f2kc1", "status": "running", "run_type": "agent_run",
          "created_at": "...", "started_at": "...", "completed_at": null }
      ]
    }
  ],
  "trigger_nodes": [
    {
      "definition_id": "def_abc123",
      "definition_type": "scheduled",
      "name": "每日选题灵感",
      "schedule_label": "cron 10:17",
      "enabled": true
    }
  ],
  "edges": [
    { "kind": "spawn",    "from": "ses_caller",        "to": "ses_child",
      "run_count": 2, "last_run_id": "run_x", "last_at": "..." },
    { "kind": "callback", "from": "ses_child",         "to": "ses_caller",
      "status": "pending", "last_run_id": "run_x" },
    { "kind": "trigger",  "from": "def:def_abc123",    "to": "ses_child",
      "run_count": 5, "last_at": "..." }
  ],
  "truncated": false
}
```

Semantics:

- Node `status` ∈ `active|idle|orphan` (live, from running-agents service)
  or `queued|succeeded|failed|canceled` (latest run outcome for non-live).
  `live` distinguishes the two families.
- `title` may be null → client falls back to `agent_name + session_id`
  suffix. `scope_label`/`project_id`/`scope_id`/`platform` null ⇒ standalone
  (`独立`).
- Edge node references: sessions by `session_id`; trigger chips by
  `def:<definition_id>`.
- `spawn` edges: aggregated per (caller session → callee session) from runs
  with `source_kind='agent'` and `source_actor` set.
- `callback` edges: emitted when `callback_session_id` is set **and**
  `callback_status` is non-null (amendment A4 — sync runs that only record a
  return route draw nothing). Target may equal the spawn caller; the client
  decides rendering. `status` ∈ `pending|sent|failed|skipped`.
- `trigger` edges: runs with `run_type in ('scheduled','watch')` grouped by
  `definition_id`.
- Cap: server may truncate to the most recent 300 nodes; set
  `truncated: true` when it does.

## 4. Graph payload consumers

- Desktop: React Flow canvas (Agents → 运行 tab).
- Mobile: grouped list rendering of the same payload (tree indentation by
  spawn edges).
- The running list's `end` action continues to use the existing
  `POST /api/running-agents/end` — the graph detail panel calls it
  unchanged.
