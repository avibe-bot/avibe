# Web UI Local Cache · Plan

> **Status**: Approved 2026-11-04, including Phase 3 (section 7). Implementation
> started with Phase 1 (#3225).
> **Owner**: cyhhao

## 1. Background

On a phone, especially in the Home Screen web app, almost everything waits for
the network. The owner wants the Web UI to behave like a native client:
open a list or a conversation and see the last known content at once, while
newer data loads in the background and appears when it arrives.

The cause is in our code, not WebKit (read on `master`, 2026-11-04):

- The Web UI is already a single-page app (`createBrowserRouter`), and
  `/assets/*` is served with long-lived immutable caching. Code is not the slow
  part.
- Nothing about the data persists. `ApiContext` keeps only an in-memory read
  cache with a 1.5-second default TTL (`getCachedJson`), and the providers start
  empty: `WorkbenchInboxProvider` starts with `inboxSessions = []`, and
  `ChatPage` resets `messages` on every session change before its bootstrap
  request.
- A cold start runs sequential round trips before any content shows. First
  `index.html`, then `/api/session` and, for an owner, `/api/config` in the
  route guard (`App.tsx`), which renders `guardLoading` until both return. Only
  then do the providers and the chat bootstrap (`/api/sessions/:id/bootstrap`)
  start. Over a tunnel, each round trip is a visible wait.
- The iPhone Home Screen web app reloads the whole page after a few seconds in
  the background (device lab, 2026-11-03; see `docs/plans/ios-app.md`
  section 5), so this cold path runs again on every return.

## 2. Goal

- Opening the Inbox or the sidebar shows the last known list immediately.
- Opening a conversation shows its last known messages immediately; new
  messages merge in when the bootstrap and the event stream deliver them.
- It works in every surface that runs the Web UI: browser tab, Home Screen web
  app, desktop app, and the planned iOS shell.

Out of scope: offline sending, offline editing, and caching Apps, Settings, or
Show Pages.

## 3. Invariants

1. **The server stays authoritative.** Cached data only fills the screen until
   the live response arrives. A live response replaces the cached view of the
   same window; it does not merge with it, so rows deleted or hidden on the
   server cannot survive in the cache.
2. **Correctness never depends on the cache.** A missing, evicted, corrupt, or
   incompatible cache means today's behavior. Every read and write is
   best-effort and failure-silent.
3. **No widening of access.** A snapshot is only ever data that this principal
   already received from the server. It is partitioned by principal, is never
   shown to another principal, and is removed when access ends (section 4.4).
4. **Actions stay live.** Sending, approvals, and settings act against the server
   as today. A cached capability (for example `can_chat`) only decides what is
   rendered while the bootstrap is in flight; the server still enforces it.

## 4. Design

### 4.1 Storage

IndexedDB, one database per origin, which already separates instances, since
each instance has its own origin. One object store holds keyed snapshot
records:

```ts
type SnapshotRecord<T> = {
  key: string;          // e.g. "inbox:first-page", "projects:bootstrap", "chat:<session_id>"
  principal: string;    // reader principal from /api/session: "local" or "remote:<sub>"
  schema: number;       // bumped when a record shape changes; mismatches are ignored
  build: string;        // UI build id; transcript records from another build are ignored
  savedAt: number;
  value: T;
};
```

`ui/src/lib/localSnapshots.ts` owns open/read/write/delete, the caps, and quota
handling. There is no new runtime dependency. Tests use `fake-indexeddb` as a dev
dependency, because jsdom has no IndexedDB.

Caps:

- Chat snapshots hold the bootstrap tail window (50 rows today) for the 40 most
  recently opened sessions, evicted least recently used.
- A record over 512 KB serialized is not stored.
- A quota error clears the whole store.

### 4.2 What is cached and how it is read

| Surface | Snapshot | Shown when | Replaced by |
| --- | --- | --- | --- |
| Inbox feed and unread map (`WorkbenchInboxProvider`) | First page (30 rows), unread map | Provider mount, before `listInbox` | The first `listInbox` page and unread map; later SSE upserts are written back, debounced |
| Projects sidebar (`WorkbenchProjectsProvider`) | `projects-bootstrap` payload | Provider mount | `getWorkbenchProjectsBootstrap` |
| Conversation (`ChatPage`) | Session row, tail messages, `next_before_id`, agents, default agent, `can_chat` | Session switch, before the bootstrap | `getSessionBootstrap`: cached rows are dropped and the bootstrap window installed, keeping any rows that arrived over SSE meanwhile |

Pages render cached data exactly like live data. The loading state is
kept separately, so existing "loading" affordances (for example a disabled
composer) can still key off the live bootstrap. The first implementation shows
no "stale" badge; the event stream and the bootstrap usually land within a
second.

Phase 2 also prefetches, during browser idle time, the bootstrap for the few
most recent Inbox sessions that have no snapshot (the five newest, each tried
once per page load). That way the first tap on each one is fast too. It uses
the same request the chat page makes, at most three at a time, while the page
is visible. Where the Network Information API exists, a connection it reports
as cellular, metered, or data-saving is skipped. Where the API is missing
(Safari, including the iPhone Home Screen app), the prefetch runs. The request
is declared a prefetch, so it does not renew the remote session, and it marks
nothing read.

### 4.3 Writes

- A record is written after each successful live read it caches, with the
  principal from `InstanceAuthorizationContext.readerPrincipal`.
- Inbox writes after SSE upserts and conversation writes after `message.new` /
  `message.updated` are debounced (1 s) and capped to the same windows.
- Nothing is written while the principal is unknown, for example on a LAN setup
  host where `/api/session` reports no author id.

### 4.4 Invalidation

`ApiContext` already clears its in-memory read cache on server events and
authorization changes (`clearReadCacheMatching`, `isAuthorizationSensitiveReadPath`).
The snapshot store hooks into the same points, so there is one invalidation
policy:

| Signal | Effect |
| --- | --- |
| `/auth/logout` from the UI | Clear the whole store before navigating |
| `/api/session` returns a different principal, or login required | Ignore and delete other principals' records |
| `remote.authorization` revoked or unavailable; `authorization.changed` | Delete Inbox, Projects, and conversation records; refetch as today |
| `session.activity` archived; a bootstrap answering 403 or 404 | Delete that conversation's record |
| Session hidden from the reader (it leaves the Inbox) | Delete that conversation's record |
| Schema or build mismatch | Ignore the record and overwrite it on the next write |

Show Pages share the Workbench origin, so they can read IndexedDB. They can
already call the same APIs with the same cookie, so the cache adds no new
exposure.

### 4.5 Boot without waiting (Phase 3)

The route guard waits for `/api/session` and, for owners, `/api/config` before
rendering the shell. Phase 3 stores the guard's last successful outcome
(principal, setup complete, capabilities) and, on cold start, renders the shell
from it while the same checks run in the background:

- It only applies when the outcome is younger than the personal session TTL
  (24 h, `vibe/remote_access.py`), the last check confirmed a signed-in
  principal, and the user did not log out since.
- Any other result tears the shell down to the existing gate (login,
  revoked, unavailable, setup) and applies section 4.4.
- A login-required result marks the stored outcome unconfirmed, so the next
  cold start waits again.

This is the biggest single saving on a cold start. It also changes what renders
before authentication is re-checked; the owner approved it on 2026-11-04
(section 7), and its PR gets a security review.

## 5. Phases

### Phase 1: store, Inbox, and sidebar (about 1 week)

- [ ] `ui/src/lib/localSnapshots.ts` with caps, quota handling, and
  principal partitioning
- [ ] Logout and principal-change clearing; event-driven invalidation in
  `ApiContext`
- [ ] Inbox feed and unread map snapshot in `WorkbenchInboxProvider`
- [ ] Projects bootstrap snapshot in `WorkbenchProjectsProvider`
- [ ] Unit tests for the store and invalidation, and provider tests for
  cache-then-live replacement

### Phase 2: conversations (about 1-1.5 weeks)

- [ ] Conversation snapshot in `ChatPage` with replace-not-merge on bootstrap
- [ ] Debounced write-back from the event stream
- [ ] Idle prefetch of recent Inbox sessions
- [ ] Tests: a cached row deleted on the server disappears after the bootstrap;
  a streamed row that arrived during the load survives

### Phase 3: boot without waiting (about 0.5-1 week)

- [ ] Stored guard outcome, background re-check, and tear-down path
- [ ] Security review of the pre-check render window

## 6. Validation

- **Measure before and after**: time from launch or tap to first list rows
  and first message bubbles, on an iPhone Home Screen app over the tunnel and
  in a desktop browser with network throttling. Use `performance.mark` around
  the first render of each surface in development builds. The target is under
  100 ms from route mount on a snapshot hit.
- **Correctness cases**:
  - a server-side deletion while offline;
  - a revoked session;
  - a principal switch on the same device;
  - logout;
  - a schema bump;
  - a quota failure;
  - two tabs writing the same record.
- `npm run build` and the existing UI test suites.

## 7. Decisions for the owner

1. **Approved 2026-11-04:** Phase 3, rendering the shell before `/api/session`
   re-validates, within the 24-hour session window.
2. Whether a sign-in on a shared device should be able to opt out of local
   caching. Not proposed for now: logout already clears the cache.
