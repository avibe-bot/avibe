# Session model rework — `(scope, anchor)` backend-pin (no workdir)

## Background

`agent_sessions` rows are looked up by `(scope_id, agent_variant, session_anchor)`
with `LIMIT 1`; the only DB-unique key is the PK `id`. Consequences:

- An IM thread's **backend** is resolved from **channel (scope) routing**, then
  used as part of the lookup key — so changing a channel's backend re-routes
  **existing** threads too. Evidence: a real `discord::user::…` anchor has two
  rows, `codex` + `claude`.
- The avibe Chat header can switch an existing session's backend, leaving the
  old `native_session_id` on the row → the new backend fail-louds on a stale
  native (Codex P1 `#3330391462`).

## Confirmed fact (backend source of truth)

When a turn dispatches, the backend is resolved in `MessageHandler` as:
`vibe_agent.backend  >  agent_sessions.agent_backend  >  new-session route seed`
(message_handler.py:201-206), then `agent_service.handle_message(agent_name)`.

So the **actual backend comes from the AGENT** — the session's `agent_name`
resolved to a VibeAgent whose `.backend` is the `agents` table's `backend`
column. `agent_sessions.agent_backend` is only a **fallback** (used when no
agent resolves) → **redundant** for any session that has an agent.

## Target model (maintainer-decided)

- A thread/session is **pinned to one backend** for life. Changing a scope's
  backend only affects **newly created** threads.
- **Unique key = `(scope_id, session_anchor)`** — one row per thread. No
  `workdir`, no `agent_variant` in the key, ever.
- **Backend = the session's AGENT's backend** (`agents.backend` via
  `agent_name`). `agent_sessions.agent_backend` is a redundant denormalized
  copy (kept only as a fallback for agent-less legacy sessions for now).
- Lookup: by `(scope, anchor)`, **take the latest** (`last_active_at`) if
  several.
- `/resume` is scope-level only (done) and creates a fresh record (done).

### OpenCode & workdir

OpenCode currently appends `:working_path` to `session_anchor` to rotate a new
session per cwd. We DROP that: the OpenCode directory is a **per-request** param
(`x-opencode-directory`), so one session per `(scope, anchor)` is reused across
cwds (directory passed each turn). `workdir` is never part of any key. This
supersedes the earlier read-side cwd fix and the opencode-cwd write-path P2.

## Steps

1. **avibe (b)** — forbid switching a session's backend when it already has a
   native. Changing the **agent is allowed as long as the new agent's backend
   equals the current one**; a cross-backend change is rejected.
2. **OpenCode anchor** — `session_anchor` becomes the bare base (no
   `:working_path`); the cwd lives only in the `workdir` column as metadata.
3. **Lookup** — `_find_agent_session_row_id` keys on `(scope_id, session_anchor)`,
   `ORDER BY last_active_at DESC LIMIT 1`; drop `agent_variant` (and never add
   `workdir`).
4. **Backend resolution** — for an **existing** thread, read the backend from
   its `(scope, anchor)` row's agent; channel routing only seeds **new** threads.
5. **Write uniqueness** — binds find-or-create by `(scope, anchor)` only; never
   a second backend for an existing `(scope, anchor)`.
6. **Migration** (alembic) — (i) strip `:cwd` from existing OpenCode anchors
   (`anchor=base`, keep cwd in `workdir`); (ii) dedup `(scope, anchor)` rows
   keeping the most-recently-active; (iii) add a UNIQUE index on
   `(scope_id, session_anchor)`.
7. **Tests + supersession** — addresses Codex P1 `#3330391462`; supersedes the
   opencode-cwd P2 `#3330391467` and reshapes the subagent write P2 `#3330391464`.

## Risks / open

- Central lookup change → blast radius across all platforms + avibe.
- Migration rewrites how existing sessions are keyed (incl. live regression
  data). Write + test on a copy first; never run against a real env unprompted.
- Whether to **drop** `agent_sessions.agent_backend` column (true redundancy) or
  keep as a fallback for agent-less sessions — defer the column drop.
