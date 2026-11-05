# Local Sync Store · Plan

> **Status**: Approved by the owner 2026-11-05 14:38, with all nine
> recommendations in section 9 (D1-D9) as written. Phase 1 implementation
> started: the store and its write-through first (#3264), then the access
> digest (section 6.2), then the chat read path, then retiring the conversation
> snapshots.
> **Owner**: cyhhao
> **Security**: every implementation PR that touches what the store keeps,
> when it is wiped, or the sync API needs a Codex security review
> (`@codex security review`) in addition to the normal gates.
> **Relation**: extends `docs/plans/web-ui-local-cache.md` and, from Phase 1,
> supersedes its conversation parts (section 2.3).

## 1. The short answer

The owner asked for a real native-app model: one local database that keeps
everything the client has seen, persistently, not just the 40 most recently
opened conversations. He also asked how far the Home Screen app and the iOS app
can each go.

It can be done in three phases:

1. **Phase 1, client only.** Every message the client already receives is
   written to a local message store. That covers bootstraps, the event stream,
   prefetches, and older pages. Every surface reads that store first. There is
   no conversation cap and no server change.
2. **Phase 2, a server change log.** The instance records every change to
   sessions and messages in a monotonic log. The client pulls "changes since
   my cursor", backfills history in idle time on unmetered networks, and checks
   which sessions it may still read. Any conversation then opens from local
   data, and history can be read offline.
3. **Phase 3, the iOS app.** The shell backs the same store with native SQLite
   through `window.avibeNative`. It picks the iOS Data Protection class and
   syncs in the background when iOS allows.

The hard limits come from the platforms, not from Avibe (section 7):

- No web platform guarantees that local data survives. The best available is
  "persistent mode": WebKit grants it by heuristics, for example to a Home
  Screen web app.
- An iPhone Safari tab loses everything a script wrote after 7 days of Safari
  use without interacting with the site. A Home Screen web app is exempt.
- On iOS, web code cannot run in the background except to handle a push, and
  each push must show a notification.
- A native iOS app can refresh in the background only when iOS chooses to run
  it.

Offline sending and editing are out of scope in every phase.

## 2. Today's state (read on `master` 33546a651, 2026-11-05)

### 2.1 Client caches

| Cache | Storage and key | Cap | Cleared by |
| --- | --- | --- | --- |
| Snapshot store (`ui/src/lib/localSnapshots.ts`) | IndexedDB `avibe-local-snapshots` v1, object store `snapshots`, record id `principal\nkey` | 512 KB serialized per record, else not stored; a quota error clears the store | Sign-out and revocation (`forgetLocalSnapshots`); `authorization.changed` (`clearLocalSnapshots`); a new reader deletes other readers' records |
| Inbox (`WorkbenchInboxProvider`) | `inbox:first-page`, schema 1 | First page (30 rows) plus the unread map; writes debounced 1 s | As above |
| Projects sidebar (`WorkbenchProjectsProvider`) | `projects:bootstrap`, schema 1 | One bootstrap payload | As above |
| Conversations (`ui/src/lib/chatSnapshots.ts`) | `chat:<session_id>`, schema 2, tied to the UI build | 50 tail rows each, 40 sessions, LRU down to 32 | As above, plus archive, moving to the background, and a bootstrap 403/404 |
| Idle prefetch (`ui/src/lib/chatPrefetch.ts`) | Writes `chat:*` | The 5 newest Inbox sessions without a snapshot, plus the main Bot's session; 3 at a time; skipped on connections reported as metered | Not applicable |
| Voice status (`ui/src/lib/voiceAvailability.ts`) | `voice:asr-status` | One record | As above |
| Boot outcome (`ui/src/lib/bootSession.ts`) | localStorage `avibe.boot-session.v1` | Until `session_expires_at` (local: 12 h) | Sign-out, revocation, login required |
| Drafts (`ui/src/lib/sessionDraftLocalCache.ts`) | localStorage `avibe.session-draft.v1.*` | Per session | Its own conflict rules |
| Chat view mode (`ui/src/lib/chatViewMemory.ts`) | localStorage `avibe.chat.show-page-sessions.v1` | 200 ids | Not applicable |
| Read cache (`ApiContext.getCachedJson`) | Memory | 1.5 s default TTL, 30 s for config | Server events and authorization changes |
| Push worker (`ui/public/push-sw.js`) | Cache API entries for push launch and endpoint | Two entries | Not applicable |

In-memory mirrors are being added by other lanes on 2026-11-05: chat snapshots
for the chat lane, and per-project session lists (`projects:sessions:<id>`) for
the sidebar lane. IndexedDB reads are asynchronous, so a mirror is the only
way to paint cached rows in the first frame.

### 2.2 What the current plan guarantees

`web-ui-local-cache.md` section 3 sets four invariants. This plan keeps all
four:

1. The server stays authoritative. A live response replaces the cached view
   of the same window.
2. Correctness never depends on the cache.
3. There is no widening of access. Data is partitioned per reader and removed
   when access ends.
4. A cached surface renders fully usable, and the live answer may only narrow
   it.

What that plan cannot do is the owner's ask:

- It keeps a 50-row tail of at most 40 conversations.
- Older pages are never stored.
- A conversation never opened, or evicted, waits for the network.
- Nothing can be read offline beyond those tails.

### 2.3 How this plan relates to it

- **Kept:** the snapshot store and its Inbox, Projects, voice, and boot
  records. Those surfaces are one page each and are already instant.
- **Superseded in Phase 1:** the conversation snapshot (`chat:*`), its
  40-session and 50-row caps, the UI-build rule for transcript rows, and the
  per-feature chat mirror. The message store replaces them (section 4).
  Prefetch keeps its trigger rules and writes into the message store.
- **Superseded in Phase 2:** for the message store, the whole-store clear on
  `authorization.changed` becomes a visibility check (section 5.5).
- When Phase 1 lands, `web-ui-local-cache.md` gets a status note that points
  here for conversations.

## 3. Server data model (read from code)

### 3.1 Messages

- **Table** `messages` (`storage/models.py`). Every platform writes here; the
  Web transcript reads a session's rows that have a transcript type.
- **Transcript types** come from `vibe/message_types.json` through
  `types_with("transcript")`: `user`, `harness`, `annotation`, `output`,
  `result`, `notify`, `vault`, `error`, `interim`. Agent Activity rows
  (`assistant`, `tool_call`) are not transcript rows.
- **Ids** are `msg_<microsecond clock hex><8 random hex>`
  (`storage/messages_service.py::_new_message_id`). They sort roughly by time
  but are not a sequence: two processes can interleave them, and the transcript
  is not ordered by id alone.
- **Order.** The transcript is ordered by
  `(coalesce(delivered_at, created_at), id)` (`transcript_order_value`). A
  queued Delivery becomes a Message when it is accepted, at the tail.
  `promote_suppressed_native_message` sets `delivered_at` to now, which moves
  a row to the tail.
- **`context_seq`** is a per-session order for model context only. It is null
  for display rows, so it cannot drive sync.
- **Edits happen in place, and `updated_at` is not reliable.** Several writers
  change a row without touching `updated_at`:
  - `set_quick_reply_chosen` (`storage/messages_service.py`);
  - the failure-retry link (`core/backend_failure_retry.py`);
  - `note_turn_mode` (`core/handlers/model_hub/service.py`).

  Others do touch it:
  - `persist_agent_message` (`core/message_mirror.py`), which can also change
    `type`, `scope_id`, and `native_message_id`;
  - `mark_session_read`;
  - the context commit in `storage/agent_transcript.py`.
- **Rows move between sessions.** `promote_suppressed_native_message` rebinds
  `session_id` to the visible target session.
- **Rows are not deleted.** No code path deletes from `messages`. Teardown of a
  session that has history archives it instead
  (`storage/sessions_service.py`).
- **Some fields are derived at read time** and are not stored on the row:
  - sender labels (`attach_sender_labels`);
  - Harness provenance: the source session's title and Agent, attached only
    when the reader may read that source session;
  - Vault request type and status;
  - `failure_retry.state`, read from the Delivery.

  These can change without any write to the message row.
- **`read_at`** is one value for the instance, not per reader.
- **Attachments** are `media_objects` rows served by token at
  `GET /api/media/<token>` and `GET /api/media/<token>/meta`.
  - Each request checks authorization against the referencing sessions.
  - Tokens can be revoked (`revoked_at`).
  - Responses carry `Cache-Control: private, no-store`, so bytes cached under
    one identity are not reused under another.

### 3.2 Sessions and visibility

- **Table** `agent_sessions`.
  - Ids are random (`ses` + 10 characters); Bot sessions use the `ses_`
    namespace (`ses_main`, `ses_<handle>`).
  - `status` is `active` or `archived`. Archive is terminal
    (`workbench_sessions_service.archive_session`), but the row stays readable
    by id.
  - `visibility` is `foreground`, `background`, or `system`.
  - `scope_id` names the project, and a PATCH can move a session to another
    project (`_publish_session_update_activity`).
  - Other columns: `pinned`, `agent_status` (the running dot), `last_active_at`,
    and the composer draft columns.
- **Who may read a session** is decided by
  `project_access_service.get_effective_session_role`:
  - The instance owner reads everything.
  - Bot sessions are owner-only. A removed Bot's session is hidden from the
    owner's lists too (`core.bots.hidden_bot_session_ids`).
  - Sessions outside any project are readable by instance managers.
  - Project sessions follow the project's policy:
    - `inherit` gives the instance role;
    - `restricted` gives the lower of the instance role and the best matching
      binding (email, email domain, or organization group);
    - an archived project is invisible except to managers.
  - Organization group membership comes from the control plane's claims. Access
    can therefore change with no write to any local table.

### 3.3 Drafts, the queue, Agent Activity

- **Drafts.** `agent_sessions.composer_draft_text` is synced through
  `/api/sessions/:id/draft`, with its own localStorage cache. They are out of
  scope here.
- **Queue.** Queued Deliveries (`message_deliveries`) are live state for
  editors and above. They are not stored, as today.
- **Agent Activity** comes from `agent_events`:
  - it is loaded per turn through `GET /api/sessions/:id/activity`;
  - tool traces are deleted after 30 days by default
    (`storage/agent_events_retention.py`);
  - it stays online-only.

### 3.4 Live events

`GET /api/events` (`vibe/ui_server.py`, `vibe/sse_broker.py`):

- **Fan-out.** It is an in-memory fan-out with a 200-event queue per subscriber.
- **No replay.** There are no event ids and no `Last-Event-ID` replay. A
  subscriber that drops an event has its stream closed, and the client
  catches up on reconnect: it reads the 50-row tail, the queue, the turn state,
  and the session row (`ChatPage` `catchUpAfterGap`). The catch-up does not
  close a gap of more than 50 rows; the reader reaches the missing rows only
  by scrolling up into them.
- **Filtering per reader:**
  - by role (`can_receive_workbench_event`);
  - by the session's current role or the project's read access
    (`_workbench_event_visible_to_context`);
  - by payload projection (`_workbench_event_payload_for_context`).
- **Events that matter here:**
  - `message.new`: a full message row, plus synthesized `tool_call` rows when
    activity streaming is on;
  - `message.updated`: a full row;
  - `session.activity`: `created`, `updated` (title, visibility, pinned),
    `archived`, `user_message`, and placement changes;
  - `inbox.session.updated` and `inbox.unread.changed`;
  - `turn.start`, `turn.end`, and `queue.updated`;
  - `authorization.changed` and `projects.changed`.

Not every row write publishes an event. The event stream is fast, but it is
not a complete change stream.

### 3.5 Read APIs

- **`GET /api/sessions/:id/bootstrap`** returns:
  - the session;
  - `can_chat`;
  - the Agents;
  - the config;
  - the 50-row tail with `next_before_id`;
  - the queue and the draft (editors only);
  - the turn state.
- **`GET /api/sessions/:id/messages`** pages with `before_id`, `after_id`,
  `around_*`, or `tail=1`. The limit is clamped to 500.
- **`GET /api/inbox`** pages with `limit` (default 30) and `before`.
- **Other reads:** `GET /api/workbench/projects-bootstrap`,
  `GET /api/sessions`, and `GET /api/sessions/:id`.
- **Compression.** API responses are gzip-compressed when the client accepts it.
- **Session renewal.** Bootstrap requests marked `X-Avibe-Prefetch: 1`, and the
  push worker's Inbox read, do not renew the remote session
  (`renew_remote_access_cookie`).

### 3.6 What a change feed can be built on

Nothing that exists today will do:

- message ids are not a sequence;
- `updated_at` misses in-place writers (section 3.1);
- `context_seq` covers inputs only;
- SQLite's `rowid` sees inserts but not updates;
- access changes may touch no local row.

Phase 2 therefore adds a change log written by SQLite triggers (section 5.1).
Triggers catch every writer, including ones added later, with no discipline per
call site. The repository already defines triggers this way (`vault_requests`
in `storage/models.py`, created both by `after_create` DDL and by an alembic
revision).

### 3.7 Volume on a real instance

The owner's instance holds about five months of history (since 2026-05-30).
Sizes are of serialized transcript JSON, without the `model` blob that the API
strips.

| Slice | Messages | Sessions | Size |
| --- | --- | --- | --- |
| Foreground, active | 56,345 | 668 | 129 MB |
| Background, active (Agent runs, Harness) | 20,937 | 1,050 | 55 MB |
| Foreground, archived | 5,667 | 54 | 13 MB |
| Foreground, active, last 30 days | 15,721 | n/a | 51 MB |
| Foreground, active, last 90 days | 38,465 | n/a | 104 MB |
| Tail 50 of every foreground active session | 20,067 | 668 | 41 MB |

The average transcript row is about 2.4 KB. On the wire, gzip shrinks this
several times over.

## 4. Client store design

### 4.1 Storage layout

The message store is a separate database, `avibe-local-store`. The snapshot
database `avibe-local-snapshots` stays at version 1 and is untouched, so a tab
still running an older build keeps reading and wiping its snapshots as today.
Sign-out and revocation in a current build clear both databases.

**Wiping across builds.** A tab running an older build does not know the
message store exists, so its sign-out cannot clear it directly. The shared
sign-out generation in localStorage (`ui/src/lib/signOutGeneration.ts`) closes
that gap. Every build since #3228 bumps it on sign-out and on revocation.

- The message store records the generation in `meta`.
- At every open, and on every `storage` event for that key, a current build
  compares the two. A newer generation wipes every store before any read.
- So an older tab's sign-out wipes the store immediately in any open
  current-build tab, and otherwise at the next open of a current build.
- In between, the data stays on disk but nothing displays it, and the server
  has already ended the session.
- A build older than #3228 does not bump the generation. Its sign-out reaches
  the store through the login-required rule in section 6.2.

The new stores hold one reader at a time. A `meta` record names that reader;
any mismatch at open, or a new reader being set, clears them before the first
read or write. This is what the snapshot store already does today ("setting a
principal deletes every other principal's records"), made structural.

| Store | Key | Value | Indexes |
| --- | --- | --- | --- |
| `meta` | `'reader'` | `{reader, schema, signOutGeneration, accessDigest?, createdAt}` | None |
| `sessions` | `id` | `{id, row, scopeId, lastActiveAt, openedAt, observedAt, cursor?, bytes}`; `row` is the `WorkbenchSession` as served | `lastActiveAt`, `scopeId`, `openedAt` |
| `messages` | `id` | `{id, sessionId, orderAt, row, observedAt, bytes}`; `orderAt` is the canonical `coalesce(delivered_at, created_at)`; `observedAt` is defined in section 4.2 (P1) | `[sessionId, orderAt, id]` |
| `ranges` | `[sessionId, fromAt, fromId]` | `{sessionId, from, to, startReached, atTail, verified, verifiedAt}` | `sessionId` |
| `media` | `token` | `{token, sessionId, meta, thumb?, bytes}` | `sessionId` |

The `messages` store keeps only rows the server stored: no Delivery still
being sent and no claimed-input projection. This is the existing
`isStoredTranscriptRow` rule. A row's shape is checked by a parser when it is
read, and a row that fails the check is dropped and fetched again. Transcript
rows are therefore no longer tied to the UI build: a weekly release must not
wipe history. A hand-bumped `schema` in `meta` is the only wholesale reset.

### 4.2 Contiguity ranges and gap tracking

A range says: "every transcript row of this session whose order key falls
between `from` and `to` is on this device, as of `verifiedAt`". `from` and `to`
are always keys of rows the server returned. A range never covers keys no page
has shown.

- `startReached` means nothing is older: the page that produced the range had
  no `next_before_id`.
- `atTail` means nothing was newer when the range was verified. It holds only
  while the event stream stays continuous: every `onConnected` catch-up clears
  it on all ranges until a new tail page sets it again.

**Writing a server page** happens in one transaction. The page can be a
bootstrap tail, a `tail=1` read, a `before_id` page, or an `around_*` window.
Two invariants govern every page. Phase 2 change pulls follow them too.

**P1. Last observed wins, per row.**

- Every stored row carries `observedAt`: when the request that returned it
  started, or when the event that carried it arrived.
- A write replaces or deletes a row only when its own `observedAt` is not older
  than the row's.
- A delete leaves a tombstone, `{id, sessionId, observedAt}`, so an older
  response that is still in flight cannot bring the row back. An upsert older
  than the tombstone is dropped. Reads skip tombstones.
- The store drops any response whose request started more than 2 minutes
  before it lands. That bounds how long a tombstone matters, and tombstones
  older than 10 minutes are purged. Wipes remove them with everything else.
- Session rows and session removals follow the same rule. For example, a
  visibility answer that started before a newer feed upsert of that session
  does not delete it.
- So a page that was in flight never overwrites or deletes a row that the
  stream, or a later-started page, delivered after the page's request started.
  This holds for every page shape, for deletions and upserts alike. Today's
  rule that a row streamed during the load survives (#3243), and a
  quick-reply choice made in another tab while an older page loads, are both
  cases of it.

**P2. A page speaks only for what it proves.** A page is authoritative for its
own rows. It reaches beyond them only as far as the server's answer proves:

| Reach | Proven by | Otherwise |
| --- | --- | --- |
| Up to positive infinity | A tail page: it is the server's newest rows | Not applicable |
| Down to negative infinity | A null `next_before_id`: nothing is older. An empty tail with a null `next_before_id` means the session has no transcript rows | The first row's key |
| Up to a held range (a join) | The page contains that range's first row with the order key the client stored | The last row's key |
| Up to positive infinity, `around_*` | A null `next_after_id` | The last row's key |
| Anything, `around_*` | The response carries `anchor_id` | Nothing is applied: an anchor the server cannot resolve returns no rows and null cursors |

**Overlap proves joins.** Older history is fetched with one row of overlap.

- `before_id` names the range's second row, so the page should end with the
  range's first row. A one-row range uses an `around_id` window on that row
  instead.
- If the server has since moved that row, the join is not proven:
  `promote_suppressed_native_message` changes both its session and its
  `delivered_at` (section 3.1). The row is then missing from the page or has
  another key.
- An unproven page stands alone between its own first and last rows, and the
  gap stays visible to gap fetches.
- The same holds when the server ignores an unresolvable `before_id` and
  answers with the newest rows.

**Steps.**

1. Delete the session's local rows inside the page's proven reach that the
   page does not contain and that are not newer than it (P1). They were moved,
   retyped, or never existed.
2. Upsert the page's rows, except rows the store holds with a newer
   `observedAt`.
3. Record coverage from the first to the last returned row, with
   `startReached` and `atTail` as flags. Merge it with a range only where a
   shared row with the same order key proves continuity.

**Coverage details.**

- Coverage never reaches past returned rows. An empty page records coverage
  only when it reaches both ends.
- A page with a null `next_before_id` that joins a range sets `startReached`
  on it.
- A tail range counts as verified from the moment its request started.
  Event-stream rows that arrived while it was in flight, on a continuous
  stream, extend it once it lands.
- After a disconnect that let more than 50 rows arrive, the old tail range
  ends at its last row and has lost `atTail`. The new tail starts at its first
  row. The two share no row, so they stay separate, and the gap between them
  stays visible to the offline marker and to gap fetches.

This generalizes invariant 1 from "the live window replaces the cached window"
to "every live page is authoritative for what it proves".

**Live rows.**

- A `message.new` row is stored at once. It extends a tail range only while the
  event stream has stayed continuous since that range was verified, meaning
  `ApiContext` has fired no `onConnected` catch-up in between.
- After a reconnect, the catch-up tail read either merges with the stored tail
  (they share a row) or leaves a gap between two ranges.
- A `message.updated` row replaces the stored row and does not change any range.

**Gaps.**

- Online, scrolling into a gap fetches it with `before_id` and closes it.
- Offline, the transcript shows one quiet marker at the gap: "Earlier messages
  are not on this device".

### 4.3 Read paths

| Surface | Phase 1 read | Phase 2 read |
| --- | --- | --- |
| Open a conversation | The hot mirror (memory) for the first frame, else the tail range from IndexedDB; then the bootstrap replaces the tail interval, as today | The same paint. The bootstrap is still called for live state (`can_chat`, queue, draft, turn state), but the transcript is already current from the change log |
| Scroll up | Local rows inside a range paint at once. Online, the `before_id` request still runs and replaces its interval (stale-while-revalidate) | Local rows only; a request runs only at a gap or an unverified range |
| Deep link or search jump (`around_id`) | Local when the anchor and its neighbours are inside a range; then the server window replaces | Local when inside a range |
| Inbox | Snapshot (unchanged) | Snapshot online. Offline, the first page from the snapshot, then older sessions from the `sessions` store by `lastActiveAt` |
| Project sidebar lists | Snapshots (unchanged) | The same, with the `sessions` store as the offline fallback |
| Search | Server only | Server only (local search is a later plan) |
| Agent Activity, queue, drafts | Unchanged | Unchanged |

**The hot mirror** keeps the session rows and the tails of the 40 most recently
active conversations in memory, per reader:

- it is filled from IndexedDB as soon as the reader is named;
- every write updates it;
- it is cleared on a reader change and on every wipe.

It replaces the per-feature mirrors being added now with a single mirror that
has a single invalidation path.

When the Inbox reads from this store (Phase 2), it keeps the `feedStatus`
contract of `InboxState`:

- `pending` until the first answer;
- `failed` after a read failure with no retry;
- back to `pending` when an authorization change clears the window, even if no
  list is on screen.

### 4.4 Write-through sources (Phase 1)

Each of these writes through the page rule in section 4.2:

- `getSessionBootstrap`, from the chat page and from prefetch;
- `listSessionMessages` (`tail`, `before_id`, `around_*`), from reconcile,
  load-older, reload-latest, and deep links.

Each of these writes a single row or session:

- the `message.new` and `message.updated` event handlers;
- session rows from `getSession`, `session.activity` updates, and the bootstrap.

Phase 1 also extends prefetch:

- Beyond the 5 newest sessions, it warms the tail of every session on the
  Inbox's first page that has no stored tail, once per document.
- Its existing conditions still apply: idle, visible, not metered, non-renewing,
  and nothing marked read.

Phase 1 deliberately does not walk whole histories. The existing APIs could
page through everything, but without a change log nothing would keep those
pages correct: quick-reply answers, retry states, moved rows, and lost access
would go stale without anyone noticing. Phase 1 keeps what it can revalidate
whenever the reader looks at it.

### 4.5 Quota and eviction

- **Budget.** The store uses the smaller of the owner's size budget (decision
  D2) and half of `navigator.storage.estimate().quota`. It measures itself by
  the stored `bytes` fields, because `estimate()` is padded and coarse.
- **Persistence.** It calls `navigator.storage.persist()` at launch when running
  standalone (Home Screen, installed app, iOS shell) and records the result.
  WebKit and Chromium decide silently, while Firefox prompts, so a plain
  browser tab calls it only after the reader turns offline history on
  (decision D5).
- **Eviction order** when over budget:
  1. Thumbnails.
  2. History beyond the backfill depth (decision D3), oldest first, in the least
     recently active sessions.
  3. Whole sessions by `lastActiveAt`.

  The main Bot's session, pinned sessions, and the hot set are evicted last.
  Eviction trims ranges from the old end and clears `startReached` on what
  it trims.
- **Quota errors.** On `QuotaExceededError`, the store evicts one batch and
  retries once. If that fails, it clears the message store and keeps the
  snapshot records.

### 4.6 Attachments

The store keeps attachment metadata from `/api/media/<token>/meta` (name, type,
size, pixel size). Phase 2 adds a small thumbnail, made in the client from an
image the reader actually displayed (at most 64 KB as WebP or JPEG). Originals
are not stored (decision D1). The server sets `Cache-Control: private,
no-store` on purpose and authorizes every byte, and originals can be very
large. Offline, an attachment shows its card and thumbnail, and opening it says
it needs a connection.

### 4.7 Tabs, workers, and the reader fence

- Writes are idempotent upserts under P1, and each page is applied in one
  transaction.
- A page or event is dropped if its request started before any of these:
  - a wipe;
  - a reader change;
  - a forget of its session (archive, 403 or 404, visibility check).

  These are the owner, epoch, and generation rules of today's `chatSnapshots.ts`,
  carried over to every write.
- Only one tab backfills at a time, using the Web Locks API
  (`navigator.locks.request(..., {ifAvailable: true})`; Safari 15.4+, Chrome 69+
  per MDN browser-compat-data).
- The push service worker (Phase 2) writes through the same module.
- Every sync response names its reader (section 5.2). The store refuses a
  response whose reader differs from `meta.reader`, whichever tab or worker
  received it.

## 5. Sync API design (Phase 2)

### 5.1 Change log

A new table and its triggers come in one alembic revision; the same DDL is
attached to the `Table` with `after_create`, so databases built from models in
tests get it too.

```sql
CREATE TABLE sync_changes (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reused
  kind       TEXT NOT NULL,                      -- 'message' | 'session'
  entity_id  TEXT NOT NULL,
  session_id TEXT,
  old_type   TEXT,                               -- messages only
  new_type   TEXT,
  changed_at TEXT NOT NULL
);
CREATE INDEX ix_sync_changes_session_seq ON sync_changes (session_id, seq);
```

Triggers:

- **`messages` after insert:** one row.
- **`messages` after update** of `session_id`, `type`, `content_text`,
  `content_json`, `metadata_json`, or `delivered_at`:
  - one row for the new session;
  - when `session_id` changed, a second row for the old session, so the row
    leaves it.
  - `read_at` and `context_seq` are not watched.
- **`agent_sessions` after insert, update, or delete:** one row. Updates are
  watched only on the columns the `WorkbenchSession` payload reads. A test fails
  when the payload builder reads a column the trigger does not watch.
- **Derived state.** When a message's derived state changes without a row
  write, the publisher of that `message.updated` calls `touch_message(conn, id)`
  in the same transaction. Today that is the failure-retry state.
- **Other derived fields lag.** This covers message sender labels and
  provenance titles, and on sessions, the Agent display name read from the
  `agents` table. They update when their row next changes or is read again,
  as the derived message fields do (section 6.3).

Cost and bounds:

- Each write adds one bounded insert in the transaction it already holds.
- A turn's `note_turn_mode` adds one change per row of that turn.
- Retention runs on the existing maintenance worker, in batches like
  `agent_events_retention`. It drops rows older than 30 days and records
  `sync_floor_seq` in `state_meta`.
- The log has an incarnation: a random id in `state_meta`
  (`sync_log_incarnation`). `AUTOINCREMENT` is monotonic only for the life of
  the table, and the sequence can move backwards two ways:
  - a downgrade followed by an upgrade recreates the table, which writes a new
    incarnation;
  - restoring a backup (`storage/backups.py::_restore_sqlite_backup_locked`)
    copies the saved database, old incarnation included. The restore path must
    therefore write a new incarnation before the service answers any sync
    request. So must any other path that replaces the database file.

  A seq check alone cannot catch a restore: once the restored log grows past a
  client's old cursor, that cursor looks valid and silently skips changes.
- The queries below are `SEARCH` by primary key or by `(session_id, seq)`, and
  their plans are pinned in tests (AGENTS.md section 6, controller event loop).

### 5.2 Endpoints

All are viewer-level reads. Each response carries:

- `reader`: the `/api/session` `author_id`;
- `incarnation` and `floor_seq`;
- `access_digest` (section 5.5).

Cursors are opaque strings of the form `<incarnation>.<seq>`.

- **`GET /api/sync/sessions?cursor=<cursor>&limit=200`.** Sessions changed after
  the cursor, coalesced per session, in `seq` order:
  - `{session_id, op: 'upsert', session, last_seq}` for a session the reader
    may read now and that is active;
  - `{session_id, op: 'remove', reason: 'archived'}` for an archived one.
  - A session the reader cannot read now never appears.
  - Without a cursor, the endpoint runs in snapshot mode. It pages the
    reader's currently readable sessions by id, and its continuation is
    separate from the log cursor:
    - the first snapshot request pins `resume_cursor`, the log head when the
      snapshot started, and every page repeats it;
    - each page returns `snapshot_after`, the last session id it covered, and
      `has_more`;
    - the client passes `snapshot_after` until `has_more` is false. Only then
      does it switch to incremental mode with `resume_cursor`.

    Changes made while the snapshot was paging come back in incremental mode,
    because the cursor was pinned before the first page.
- **`GET /api/sessions/:id/changes?since=<seq>&limit=200`.** The current state
  of every message in that session changed after `since`, in `seq` order:
  - `op: 'upsert'` with the transcript payload, for a row now in this session
    with a transcript type;
  - `op: 'remove'` for a row that left the session or the transcript;
  - changes whose old and new types are both non-transcript are skipped.
  - Authorization is the same session check as `GET .../messages`.
- **`POST /api/sync/visible {session_ids: [...]}`** (CSRF-protected, at most
  500 ids per call, so the client batches). Returns the subset the reader may
  read now. Unreadable and nonexistent ids are indistinguishable, so the
  endpoint reveals nothing about other sessions. Section 5.5 explains
  why this is a check and not a tombstone stream.
- **`sync_seq` on existing reads.** The bootstrap and `GET .../messages` add
  `sync_seq`, the log head when the read ran.
  - The server reads the head before it reads the rows. A cursor that starts
    early only replays changes, and replaying is idempotent because changes
    carry current state. A cursor that starts late would skip changes.
  - A session's first cursor starts there.
  - That page only proves its own interval. Every range the session already
    held outside it, such as Phase 1 history, becomes unverified at that
    moment.
  - An unverified range still paints at once. Online, it is revalidated by its
    page request, as in Phase 1, until backfill or a view replaces it. It is
    never read as local-only truth.
- **Background requests do not renew the session.** Backfill and push-worker
  reads send `X-Avibe-Prefetch: 1`, and `renew_remote_access_cookie` learns the
  new paths. An unattended device must not keep a session alive.

**Limits.**

- Pages are clamped to 200.
- A cursor returns `reset: true` when any of these holds:
  - its `seq` is below `floor_seq`;
  - its incarnation is not the current one, because the table was recreated;
  - its `seq` is above the log head. This is a cheap extra guard; rotating
    the incarnation on restore is what makes restores safe.

  On a reset, the client marks the affected ranges unverified, takes the
  session list again in snapshot mode, and starts each held session over from
  a fresh tail. Nothing is wiped.
- SQLite has a single writer, so seqs become visible in commit order. A reader
  never sees seq n+1 before seq n, and advancing a cursor past the scanned
  rows skips nothing.
- The server scans at most `limit` raw log rows per call, however many of them
  the reader can see, and returns `next_cursor` past the scanned rows. Work is
  bounded by the page, not by history.

### 5.3 Backfill

The client runs backfill when it is idle and visible, on a connection the
Network Information API does not report as metered, and under the Web Locks
lease. When the API is missing, as on Safari, the reader's setting decides
(decision D6). The steps are:

1. Sync the session list (`/api/sync/sessions`): finish the snapshot pages if
   one is in progress, then pull incremental pages until `has_more` is false.
2. Rank sessions: the main Bot first, then pinned, then by `lastActiveAt`.
3. For each session, fetch the tail, then walk `before_id` pages of 200 until
   `startReached`, the depth (decision D3), or the budget is reached.
4. Pause when the page is hidden and resume where it stopped.

Backfill keeps at most one request in flight, leaving the connection to the
reader's own requests.
On a metered connection, only session rows and tails sync.

### 5.4 How the event stream and the log fit together

- The event stream stays the low-latency path. Its rows are applied at once, as
  today.
- `message.new`, `message.updated`, and `session.activity` gain `seq`. The
  publisher reads the change it wrote in its own transaction. This lets the
  client tell whether a streamed row is newer than what it pulled.
- Not every write publishes an event, so the log is the source of truth. A
  session's cursor advances only by pulling `changes since`, which happens:
  - at `turn.end`;
  - on reconnect, replacing today's 50-row tail reconcile, so a gap of any size
    is recovered;
  - on resume;
  - every 10 minutes while the page is visible.

  While the stream stays continuous, those pulls are small.

### 5.5 Gaining and losing access

Access can change with no local write: a project policy, an organization group
claim, an instance role, or a removed Bot. A cursor sees only later log rows, so
it misses both directions:

- a session that became readable without changing never enters the store;
- a session that became unreadable never leaves it.

The feed also never streams "you lost this" tombstones, because those would
tell a reader about sessions they cannot see.

**Gaining access.**

- Every sync response carries `access_digest`, a hash of the reader's instance
  role, readable project ids (`accessible_project_ids`), and hidden Bot
  session ids. Its cost is bounded by the number of projects.
- When the digest differs from `meta.accessDigest`, the client takes the
  readable session list again in snapshot mode and adds what it lacks.
- `authorization.changed`, launch, and resume each trigger a feed pull. A group
  claim refreshed on the server is therefore noticed at the next pull.

**Losing access.**

- The feed only ever names sessions the reader may read now.
- The client checks the sessions it holds with `POST /api/sync/visible` and
  deletes everything for those no longer visible. It checks:
  - at launch, on resume, and on reconnect;
  - on `authorization.changed` and whenever the access digest changes;
  - after a session moves to another project;
  - every 10 minutes while visible.
- Archive arrives as `op: 'remove'`, and live through `session.activity`
  `archived` as today.

This replaces the Phase 1 whole-store clear on `authorization.changed` for the
message store (decision D8).

### 5.6 Compatibility

- **Older instance.** A newer client against an older instance gets a 404 from
  `/api/sync/sessions` and stays in Phase 1 mode for that document.
- **Older client.** An older client never calls the new endpoints and ignores
  the new fields.
- **Downgrade.** The alembic downgrade drops the triggers and the table. An
  older server binary on an upgraded database (rollback without downgrade)
  still has working triggers but no pruning. The log then grows until the
  instance is upgraded again; this is noted in the release notes for that
  revision. A later upgrade recreates the table with a new incarnation, so
  every client cursor resets (section 5.2).
- **Revision id.** The revision follows `20261030_0072_session_digests` at the
  time of writing. The implementation PR rechecks the latest revision at its
  final rebase.

## 6. Security and privacy

### 6.1 What changes

Today a lost or shared device can hold at most 40 conversation tails. With
Phase 2 it holds the reader's whole readable history up to the budget. For the
owner that is every project's transcript, about 130 MB today. The server checks
access on every request; local data is checked only when the device talks to
the server again.

### 6.2 Wipe rules

| Event | Phase 1 | Phase 2 |
| --- | --- | --- |
| Sign-out from the UI | Wipe both databases before navigating (existing path, extended). A sign-out in an older-build tab wipes through the sign-out generation (section 4.1) | Same |
| Revocation (`remote.authorization` revoked, or a sync response saying so) | Wipe | Wipe |
| A different reader confirmed by `/api/session` | Wipe before the first paint (`meta.reader` mismatch) | Same |
| Login required (session expired) | Wipe the message store. Phase 1 has no way to recheck access when the reader comes back | Hide at once; keep for the same reader, then run the visibility check before showing it again (decision D7) |
| `authorization.changed` | Clear the message store (as today) | Visibility check (section 5.5) |
| `authorization.changed` missed (every tab closed, or the stream down) | The next session check finds the access digest changed and applies the change as the event would have (below) | Visibility check |
| Archive, move to the background, a 403 or 404 on a session read | Delete that session | Same, plus `op: 'remove'` |
| Reader no longer allowed a session | Found by the access digest when a whole project was lost, otherwise on the next live read of it | Visibility check |

Revocation cannot erase data from a device that never comes back online.
Against a lost device, the protection is the device lock and OS encryption. The
plan says so plainly in user documentation.

**The access digest (Phase 1).** Only a tab whose event stream is up receives
`authorization.changed`. Without a further check, a reader who lost a project
while every tab was closed, or while the stream was down, would keep its
history on the device until each conversation was read live again.

- Server: `/api/session` returns `access_digest` for a current remote reader
  (`project_access_service.reader_access_digest`). It is a hash of what that
  reader may read:
  - for a role that manages the instance, the role alone, since such a role
    reads every project, so a new project does not change it;
  - for any other role, the role and the ids of the projects it may read.
  If it cannot be computed, the field is null, and the client keeps what it
  holds until its next check.
- Client: the digest last seen is kept per reader in localStorage
  (`avibe.access-digest.v1`), shared by every tab and outliving them. It is
  compared at every `/api/session` answer, which includes each open, and after
  every stream gap for a remote reader. A local reader has no digest, so a gap
  costs it nothing.
- On a mismatch the client does what a delivered `authorization.changed` does:
  - drop the authorization-sensitive reads;
  - clear the snapshots and the message store (keeping the reader);
  - tell every surface.
  The new digest is recorded only after that clear, so a page that closes first
  clears again at its next check.
- Gaining a project changes the digest too, as the event clears on any access
  change today. A first digest, or one recorded for another reader, is no
  change: a different reader empties the stores anyway.
- What this does not catch:
  - a session moved into a project the reader cannot read, while its project
    set stays the same (LSYNC-002, found on the next live read until the
    Phase 2 visibility check);
  - a removed Bot's session.
  The Phase 2 `access_digest` (section 5.5) adds hidden Bot session ids, and
  turns a mismatch into a visibility check instead of a clear.

### 6.3 Threats by surface

- **iPhone (Safari, Home Screen, iOS app).**
  - With a passcode, files are encrypted at rest. The default Data Protection
    class, "complete until first user authentication", leaves files readable
    after the first unlock until reboot ([Apple](https://developer.apple.com/documentation/uikit/encrypting-your-app-s-files)).
  - Which class WebKit uses for website data in Safari or in a `WKWebView` app
    is **not verified**.
  - Phase 3 lets the app choose the class for its own SQLite file (decision D9).
- **Desktop app.**
  - The shell loads only the loopback Workbench (`desktop/README.md`); it uses
    WKWebView on macOS and WebView2 on Windows.
  - The store sits on the same disk as `~/.avibe/state`, which already holds
    the full database, so local history adds nothing at rest.
  - Backfill is off for loopback origins (section 7).
- **Shared browser profile.** One reader per database, and a wipe on reader
  change or sign-out. Anyone using the profile while the reader is still signed
  in is that reader, exactly as today. In a plain browser tab, backfill is off
  by default (decision D6), so a borrowed browser keeps only what was opened.
- **Show Pages share the origin.**
  - `/show/<id>/` serves agent-authored pages on the Workbench origin without a
    sandbox, so a page's script can read IndexedDB.
  - The same script can already call every read API with the reader's cookie,
    so this adds no new kind of access. It does add offline reads and the
    window before a visibility check.
  - The structural fix is a separate Show Page origin, already listed in
    `docs/plans/ios-app.md` section 16. This plan does not block on it.
- **Derived fields keep what the reader saw.** A stored Harness row may keep a
  source session title after the reader loses access to that source session.
  This is accepted: the reader saw it, and the row is replaced the next time
  its interval is read.

### 6.4 Review

The plan needs a Codex security review at implementation time. These PRs
need one:

- the Phase 1 store and wipe paths;
- the Phase 2 sync endpoints and triggers;
- the Phase 2 change from whole-store clear to visibility check;
- every Phase 3 PR that adds a native credential path or a Data Protection
  choice.

## 7. Platform matrix

The quota and eviction rows cite WebKit's
[storage policy](https://webkit.org/blog/14403/updates-to-storage-policy/)
(Safari 17 and iOS 17), WebKit's
[tracking prevention](https://webkit.org/tracking-prevention/) page, and MDN's
[storage quotas](https://developer.mozilla.org/en-US/docs/Web/API/Storage_API/Storage_quotas_and_eviction_criteria).

| Platform | Quota | Eviction risk | Persistence | Background sync | What this plan does |
| --- | --- | --- | --- | --- | --- |
| iPhone Safari tab | Origin up to 60% of disk, overall up to 80% (browser app) | LRU by origin over quota or under storage pressure. ITP deletes all script-written storage after 7 days of Safari use without interaction with the site | `persist()` is granted by heuristics; a tab is unlikely to qualify (not verified) | None. Web Push is for Home Screen web apps only (iOS 16.4+, [Apple](https://developer.apple.com/documentation/usernotifications/sending-web-push-notifications-in-web-apps-and-browsers)) | Phase 1 write-through; backfill off by default |
| iPhone Home Screen web app | Same as the browser | Exempt from the 7-day cap; LRU eviction remains unless persisted. WebKit may skip a persistent origin and one with an active page | `persist()` granted by heuristics; running as a Home Screen app is WebKit's own example | Only inside a push event, and every push must show a notification ("Safari doesn't support invisible push notifications"). No Background Sync or Periodic Sync in Safari (MDN browser-compat-data) | Phases 1 and 2; the push worker syncs the pushed conversation while showing the notification |
| Android Chrome (tab or installed app) | Origin up to 60% of disk | LRU under pressure unless persisted | `persist()` granted without a prompt by heuristics | Chrome has Background Sync (49+) and Periodic Background Sync (80+), per MDN browser-compat-data; eligibility rules not verified here | Phases 1 and 2; periodic sync is a later option |
| Android app (System WebView, `docs/plans/android-app.md`) | Chromium policy | Not verified | Not verified | No Background Sync in WebView (MDN browser-compat-data); native WorkManager would follow the iOS Phase 3 pattern | Phases 1 and 2 through the WebView |
| Desktop app (Tauri: WKWebView on macOS, WebView2 on Windows) | macOS: 15% origin, 20% overall (non-browser app); Windows: Chromium policy | Low; the data is also on the same disk in `~/.avibe` | Not needed | Not needed: loopback | Phase 1 write-through only; no backfill on loopback origins |
| iOS app, `WKWebView` store | 15% origin, 20% overall (non-browser app) | See below | Default persistent data store (`ios/README.md`) | Web code runs only while the app is in front | Phases 1 and 2 unchanged |
| iOS app, native SQLite (Phase 3) | App container; no WebKit quota | None from WebKit. The file lives in Application Support, not in Caches, which iOS may purge | Under app control | `BGAppRefreshTask` runs when the system chooses ([Apple](https://developer.apple.com/documentation/backgroundtasks/bgapprefreshtaskrequest)). Background pushes are low priority, throttled, and best kept to "two or three per hour", with 30 seconds to run ([Apple](https://developer.apple.com/documentation/usernotifications/pushing-background-updates-to-your-app)). The Notification Service Extension runs on each visible push (`docs/plans/ios-app.md` section 8) | Phase 3 |

**The 7-day rule inside a `WKWebView` app.**

- ITP is on by default in every `WKWebView` app since iOS 14 and macOS Big Sur
  ([WebKit](https://webkit.org/blog/10882/app-bound-domains/)).
- In WebKit's source,
  [`ResourceLoadStatisticsStore::registrableDomainsToDeleteOrRestrictWebsiteDataFor`](https://github.com/WebKit/WebKit/blob/main/Source/WebKit/NetworkProcess/Classifier/ResourceLoadStatisticsStore.cpp)
  skips:
  - domains in `domainsExemptFromWebsiteDataDeletion()`: app-bound domains,
    managed domains, persisted domains, and the standalone (Home Screen)
    application domain;
  - domains with recent Web Push interaction.
- Expiry is measured in "operating dates": the days the data store was in use
  (`includeTodayAsOperatingDateIfNecessary`), not calendar days.
- So in our shell the instance's data could be removed only after the app had
  been used on 7 separate days without one tap on the instance origin. In a
  single-site app that is practically impossible.
- This is inferred from source and has **not been observed on a device**.
- `WKAppBoundDomains` would make the exemption structural for tunnel hosts
  under `avibe.bot`, but:
  - it is limited to 10 domains, set at build time;
  - it cannot list Enterprise custom hostnames;
  - it restricts script injection on other domains.

  Phase 3 evaluates it rather than adopting it blindly.

**Why Phase 3 uses native SQLite at all.** The IndexedDB of a `WKWebView` lives
inside WebKit's website data. Native code cannot read it or write to it. So:

- background sync driven by `BGAppRefreshTask` or a push has nowhere to write
  unless the store is native;
- the app can choose the store's Data Protection class only for a file it
  owns;
- the store leaves WebKit's quota and eviction.

Without background sync, the native store buys little over IndexedDB, which
Phases 1 and 2 already give the shell.

## 8. Phases

### Phase 1: write-through and local-first reads (client only, about 2-2.5 weeks)

**Scope:**

- the `avibe-local-store` database, with the cross-build wipe through the
  sign-out generation;
- the store module with ranges, interval replacement, the hot mirror, budget
  and eviction, and `persist()`;
- write-through from every source in section 4.4;
- local-first reads in `ChatPage` for the tail, older pages, and deep links;
- the offline gap marker;
- prefetch writing into the store, warming the Inbox's first page;
- wipe rules from section 6.2;
- `chat:*` records deleted on first run, with no migration code: prefetch refills
  the recent ones within a day;
- the access digest in `/api/session` (section 6.2), which merges before the
  chat read path.

**Acceptance criteria:**

- Any conversation opened before paints its last known tail in the first frame
  (hot mirror) or within 100 ms of route mount (IndexedDB), with no spinner.
  There is no 40-conversation cap.
- History already seen scrolls from local data. Online, each page is
  revalidated by the request today's code already makes, and the result
  replaces its interval: a row deleted or moved on the server disappears.
- In airplane mode, conversations seen before open and their seen history
  scrolls. A gap shows the marker, and nothing else breaks.
- The Home Screen app requests persistence and reports the result in a
  diagnostics line.
- Sign-out, revocation, reader change, and login required each leave no message
  row behind, including from a write scheduled before the event.
- No server change beyond the access digest in `/api/session`.

**Tests:** unit tests with `fake-indexeddb` for:

- interval replacement in both directions;
- an unresolved `around_*` anchor (no `anchor_id`) leaves the store untouched;
- P1 for every page shape: a page in flight never overwrites or deletes a row
  the stream delivered after its request started, and never resurrects a row
  that a newer page or change pull removed (tombstone);
- P2: an overlap row that moved, or an ignored `before_id`, leaves the page
  unjoined and the gap visible;
- range merge, abut, and gap;
- replacement intervals: a tail reaches positive infinity, exhausted history
  reaches negative infinity, and an empty page still clears its interval;
- coverage stays bounded by returned rows: a disconnect longer than one tail
  leaves two separate ranges and a visible gap;
- tail extension only on a continuous stream;
- eviction order;
- a quota error;
- every wipe trigger with a pending write;
- a sign-out in an older-build tab, by generation bump, which wipes at the next
  open and on the `storage` event;
- two tabs under the lock;
- the reader fence;
- schema mismatch.

Plus `ChatPage` tests: first-frame paint from the mirror, offline older pages,
and the gap marker. Each with a mutation check, as the lanes do today.

The Phase 1 wipe and fence rules are rows of the scenario catalog
`tests/scenarios/local_sync/catalog.yaml`:

- LSYNC-006 (revocation), LSYNC-007 (sign-out), and LSYNC-008 (another reader)
  as defined below;
- LSYNC-018: a sign-out in another tab, including an older build, wipes
  through the sign-out generation;
- LSYNC-019: an authorization change clears the store;
- LSYNC-020: a session that leaves the Inbox is forgotten, and a read of it
  still in flight cannot restore it;
- LSYNC-021: a login-required answer wipes the store, and a failed session
  read does not;
- LSYNC-022: a tab still naming an earlier reader can neither write into nor
  claim the store another tab holds;
- LSYNC-023: an access change that no `authorization.changed` delivered clears
  the store at the next session check, at an open or after a stream gap.

**Rollout:** no flag, as with the existing cache. A missing or failing store
falls back to today's behavior (invariant 2).

### Phase 2: change log, sync, backfill, offline history (about 3-4 weeks)

**Scope (server, about 1.5-2 weeks):**

- `sync_changes`, its triggers, and retention;
- `touch_message` for derived-state publishers;
- the three endpoints;
- `sync_seq` on existing reads and `seq` on events;
- non-renewing background reads.

**Scope (client, about 1.5-2 weeks):**

- the sync engine and cursors;
- backfill;
- visibility checks;
- pulls replacing the tail reconcile;
- the push worker syncing the pushed session;
- offline Inbox and sidebar fallback from the `sessions` store;
- the storage line in Settings: size used, the "Keep history on this device"
  toggle, and "Remove offline data".

**Acceptance criteria:**

- After one backfill, opening any readable conversation within depth and budget
  needs no request for correctness.
- A reconnect after a gap of any size recovers exactly the changed sessions.
- Offline, every synced conversation reads to its synced depth.
- A project the reader is granted, with no session write, appears after the
  next pull.
- A session the reader loses access to leaves the device at the next visibility
  check: at most 10 minutes while open, at resume otherwise.
- Every message writer produces a change.
- Query plans are pinned.

**Tests:** a new scenario catalog `tests/scenarios/local_sync/catalog.yaml`.
IDs appear in test names and PR bodies. Role semantics reuse the existing
`PERMISSIONS-*` scenarios.

| ID | Scenario |
| --- | --- |
| LSYNC-001 | A member without access to a restricted project receives no session or message change from it, in snapshot mode or with a cursor |
| LSYNC-017 | A snapshot of more than one page enumerates every readable session through `snapshot_after`; a change made mid-snapshot arrives through the pinned `resume_cursor` |
| LSYNC-002 | A session moved into a project the member cannot read is deleted from the member's device at the next visibility check |
| LSYNC-003 | Narrowing project access (binding removed, group claim changed, project archived) removes the member's held sessions at the next check |
| LSYNC-004 | Bot sessions never reach a non-owner's feed; a removed Bot's session leaves the owner's device |
| LSYNC-005 | An archived session arrives as `remove` and its local history is deleted |
| LSYNC-006 | A revoked reader's next sync wipes every store before any further read |
| LSYNC-007 | Sign-out wipes; a write scheduled before sign-out cannot restore data |
| LSYNC-008 | A different reader signing in on the same device sees nothing of the previous reader, from the first frame |
| LSYNC-009 | A sync response naming a reader other than the store's is not written |
| LSYNC-010 | A cursor below the retention floor resets that session without losing rows; its ranges become unverified |
| LSYNC-011 | Every message writer produces a change: insert, `persist_agent_message`, `set_quick_reply_chosen`, failure retry, `note_turn_mode`, `promote_suppressed_native_message` (a change for both sessions), and `touch_message` (table-driven) |
| LSYNC-012 | An expired session hides the store at once; the same reader's sign-in shows it only after the visibility check; a different reader wipes it |
| LSYNC-013 | Backfill and push-worker reads do not renew the remote session |
| LSYNC-014 | A member granted access to an existing project, by a binding or a group claim with no session write, gets that project's sessions after the next pull, through the access digest |
| LSYNC-015 | A cursor from another incarnation resets instead of silently missing changes: after the table is recreated, and after a backup restore, which rotates the incarnation even when the restored log later grows past the old cursor |
| LSYNC-016 | A session's first cursor marks its pre-existing ranges outside the fresh page unverified; they are revalidated before being read as local-only truth |

Manual checks:

- On an iPhone Home Screen app over the tunnel: first backfill time and size,
  the `persist()` result, airplane-mode reading, and a push syncing the pushed
  conversation.
- On a member account on the regression instance: the visibility check.

**Rollout:**

1. The server ships first, with the endpoints dormant.
2. The client detects the feed per document.
3. The toggle defaults follow decision D6.

### Phase 3: iOS native store (about 2-3 weeks, after the iOS app's Phase 1)

**Scope:**

- An `avibeNative` capability `store` (contract v1 minor bump; spec in
  `docs/plans/ios-app.md` section 4 first): `store.open({origin, reader})`,
  `store.apply({pages, changes})`, `store.read({...})`, `store.wipe()`, and the
  `store.changed` event.
- A SQLite file per instance origin and reader, in Application Support, with
  the chosen Data Protection class.
- The Web UI's store module gets two adapters, IndexedDB and native, behind one
  interface. The hot mirror and all rules stay in TypeScript, so behavior is
  identical.
- Background sync: `BGAppRefreshTask`, plus a background push when the instance
  has new messages for the device. It calls the same Phase 2 endpoints and needs
  a native credential (decision D9).
- Sign-out, instance removal, and app reinstall remove the file.

**Acceptance criteria:**

- New messages are present when the app opens after a background refresh ran
  (best effort, measured).
- The file's protection class is the chosen one.
- Wipe paths match section 6.2.
- Phase 2 behavior is unchanged when the capability is missing.

**Tests:** the shell's XCTest suites, contract fixtures shared with the Web UI
adapter, and the LSYNC scenarios run against the native adapter where they
apply.

## 9. Decisions for the owner

1. **D1. Attachments.** Recommended: metadata always, plus client-made
   thumbnails of images that were displayed. Originals are not cached. They can
   be large, the server deliberately marks them `no-store` and authorizes every
   byte, and a later per-conversation "Keep files offline" can add them.
2. **D2. Size budget.** Recommended: 1 GB, or half the origin quota if smaller.
   Your whole foreground history is about 130 MB of JSON today, and the last 90
   days about 104 MB, so 1 GB covers years of history plus thumbnails.
3. **D3. Default backfill depth.** Recommended: full history within the budget,
   newest sessions first, on unmetered connections. On metered connections,
   only session rows and the 50-row tails (about 41 MB for your 668 sessions).
   Background sessions (Agent runs and Harness) are not backfilled; they are
   stored when opened.
4. **D4. Show "available offline".** Recommended: no per-conversation badge.
   Offline, show one connection banner and the gap marker. Settings shows size
   used, the toggle, and "Remove offline data". This keeps the chrome quiet and
   avoids content that suddenly changes.
5. **D5. Persistent storage prompt.** Recommended: no Avibe prompt. Call
   `persist()` silently in the Home Screen app, installed apps, and the iOS
   shell. In a browser tab, call it only when the reader turns history on,
   because Firefox prompts.
6. **D6. Defaults by surface.** Recommended: backfill on for the Home Screen app,
   installed Android apps, and the iOS app. Off, but available, for plain
   browser tabs. Off for the desktop app on loopback. Write-through (Phase 1)
   is on everywhere.
7. **D7. On session expiry.** Recommended: Phase 1 wipes, because it cannot
   recheck access. Phase 2 hides the store at once, keeps it for the same
   reader, and runs the visibility check before showing it again. A wipe on
   expiry can only run when the app next launches, so it does not protect a
   device nobody opens, and it would re-download everything at every 90-day
   cap.
8. **D8. Access changes.** Recommended: in Phase 2, replace the whole-store
   clear on `authorization.changed` with the visibility check, behind a
   security review. Otherwise every member policy change re-downloads the full
   history.
9. **D9. iOS Phase 3 credential and protection.**
   - **Credential.** Recommended: let native code use the WebView's existing
     session cookie, only for the non-renewing read endpoints of section 5.2,
     behind a security review. That adds no credential and no server auth
     change. The alternative is a per-device read-only sync token: tighter
     scope, but a new credential type with its own issuance and revocation.
   - **Protection.** Recommended: "complete until first user authentication",
     so background sync can write while the phone is locked. A stronger class
     protects less than it seems: the session cookie that can fetch everything
     again stays in WebKit's store in the same container, at a class we do not
     control (section 10).
   - If you decline native background sync, skip Phase 3: the shell already
     gets Phases 1 and 2 through IndexedDB.

## 10. Not verified

- Which Data Protection class WebKit applies to website data (IndexedDB) in
  Safari, Home Screen web apps, and `WKWebView` apps.
- The ITP 7-day removal inside a `WKWebView` app. It is enabled by default, but
  the reasoning here comes from WebKit source, not from a device. The same goes
  for whether `WKAppBoundDomains` would be appropriate for the shell.
- Whether a Safari tab, as opposed to a Home Screen app, is ever granted
  `persist()`.
- How long an iOS push worker may run in a push event before iOS stops it.
- Chrome's eligibility rules for Periodic Background Sync, and Android System
  WebView eviction behavior.
- Real IndexedDB write throughput on an iPhone for a 100+ MB backfill. Phase 2
  measures it first and sizes batches from the result.
