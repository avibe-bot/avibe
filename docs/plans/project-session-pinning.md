# Project Session Pinning

## Background

Project session lists are currently ordered only by recent activity. Users need
to keep any number of important sessions at the top of their current project,
with the preference persisted by the Avibe backend and reflected across open
clients.

## Goal

- Add Pin / Unpin as the first session action on desktop and mobile.
- Keep every pinned session ahead of unpinned sessions within its project.
- Preserve recent-activity ordering inside the pinned and unpinned groups.
- Persist the state in SQLite and broadcast changes over the existing
  `session.activity` stream.
- Keep cursor pagination correct when a project has more pinned sessions than a
  single page.

## Design

`agent_sessions.pinned` is the aggregate-owned state. It is a non-null boolean
column (SQLite integer) because pinning belongs to a Session and a Session has at
most one current project. Moving a session carries its pin state to its new
project; archiving naturally removes it from active lists.

The session list order becomes:

1. `pinned DESC`
2. `last_active_at DESC`
3. `created_at DESC`
4. `id DESC`

The cursor predicate includes the pinned group before the existing activity
tuple. `PATCH /api/sessions/:id` accepts a strict boolean `pinned` field and the
canonical `session.activity` update includes the resulting state. Clients patch
the visible row immediately and reconcile the loaded project window so pinning
an older session, or unpinning the last visible pinned session, cannot leave a
stale page.

## Verification

- Migration adds the column and list index without changing existing rows.
- Service tests cover unlimited pinned rows, group ordering, and pagination
  across the pinned/unpinned boundary.
- API tests cover persistence, validation, and SSE payloads.
- UI build and focused unit tests cover client-side ordering.
- Browser verification covers menu placement and live row movement.
