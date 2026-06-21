# Message Search — Implementation Plan

Status: proposed · Owner: TBD · Design: `design.pen` (frames: Workbench search entry,
Search overlay, Chat jump+highlight; M·Inbox entry, M·Search results, M·Chat highlight)

## 1. Background

The Workbench has no way to search **message content**. Today the only search is
session **title** search (`GET /api/workbench/sessions?q=`, `workbench_sessions_service.list_sessions`,
used by the `#`-mention picker). There is **no** message-content search API and **no**
FTS index. Users can't find a past answer/decision once it scrolls out of view.

Design is approved (Slack-like):

- **Desktop**: a persistent **Search** entry at the top of the left sidebar (the only
  always-present chrome on desktop) → a centered **⌘K command palette** with results
  grouped by session, the matched term highlighted, keyboard nav.
- **Mobile**: a **search field at the top of the Inbox** → a **full-screen results**
  view (no sidebar on mobile; the bottom tab bar is the persistent chrome).
- **Both**: selecting a result **jumps to that message** in the chat, scrolls to it,
  and gives it a few-second background highlight.

## 2. Goal

Global message-content search across all sessions, with grouped + highlighted results,
that deep-links into the chat and scrolls to + highlights the matched message — on
desktop (⌘K palette) and mobile (full-screen page), reusing existing primitives.

## 3. Verified current state (file:line)

- Messages list: `GET /api/sessions/:sessionId/messages` — `vibe/ui_server.py:4470`.
  Service `storage/messages_service.py:196 list_session_messages(conn, session_id,
  after_id, before_id, limit=50, types=('user','result','notify','error'),
  include_metadata_sources=('show_page',), tail=False)`. Ordering is the composite
  `(created_at, id)` cursor (asc for `after_id`, desc-then-reversed for `before_id`/`tail`).
- `messages.content_text` (plain text projection) exists — migration
  `storage/alembic/versions/20260526_0006_messages.py`. **No FTS5 anywhere.**
  Storage is SQLAlchemy + stdlib `sqlite3`, WAL (`storage/db.py`). Latest alembic rev `0022`.
- Title-search LIKE escaping to reuse: `storage/workbench_sessions_service.py:131`
  (`ilike(f"%{esc}%", escape="\\")`, escaping `\ % _`).
- ChatPage transcript: `ui/src/components/workbench/ChatPage.tsx` — windowed by
  `loadOlderMessages()` (beforeId cursor, fires at `scrollTop < 120`), manual iOS
  scroll-anchor (`anchorRef` + `ResizeObserver`, container has `[overflow-anchor:none]`).
  Reads `useParams` only — **`useSearchParams` not imported yet**.
- Sidebar: `ui/src/components/workbench/WorkbenchSidebar.tsx` — order is
  Inbox (hover popover) → Capabilities (`CAPABILITY_NAV`) → Projects. Search goes
  **between Inbox and Capabilities**.
- Routes: `ui/src/App.tsx` — chat is `/chat/:sessionId`; add `/search` after `/more`.
- i18n nested under `workbench.*` (`ui/src/i18n/{en,zh}.json`); add `workbench.search.*`
  + `workbench.nav.search`.
- API client: `ui/src/context/ApiContext.tsx` — `URLSearchParams` + `getJson`/`getCachedJson`;
  `listSessionMessages` is the template for a new `searchMessages` + an `aroundId` extension.
- Primitives in `ui/src/components/ui/`: `command.tsx` (palette base — reuse first),
  `dialog.tsx`, `input.tsx`, `badge.tsx`, `button.tsx`, `markdown.tsx`.

## 4. Key decisions

> **Data check (2026-06-16, live `~/.avibe/state/vibe.sqlite`, read-only):** 34,502
> messages total; DB is 160 MB but mostly non-message tables — `messages.content_text`
> is only ~6 MB. A full-table `LIKE '%q%'` scan measured **16–18 ms** (worst case
> included). Scoped to the search set below (Workbench `result` = **2,044 rows /
> 1.3 MB**) a `LIKE` runs in **~5 ms**. SQLite is 3.51 (FTS5 + trigram available).
> → LIKE is comfortably fast; FTS5 is unnecessary now.

1. **Engine: `LIKE` on `content_text` for v1** (data-backed above). Substring +
   bilingual correct (CJK + Latin + code), reuses the ilike-escape, no
   migration/triggers, app-side snippet. FTS5 (`trigram` tokenizer) stays the later
   upgrade behind the same API if the corpus ~10×'s and latency becomes noticeable.
2. **Scope: Workbench only** — `platform = 'avibe'`. Exclude IM channels
   (slack / lark / discord, ~2.5k msgs). Results grouped by session.
3. **Types: `user` + `result`** — your prompts **and** the agent's rendered final
   replies. Both are rendered in the transcript, so a clicked result always lands on a
   visible message. Exclude `assistant` (27.8k streaming/intermediate rows the transcript
   does **not** render — searching them would yield un-jumpable results), and
   `tool_call`/`notify`/`error`/system noise. Skip `content_text IS NULL`.
4. **Deep-link: `/chat/:sessionId?msg=<id>`** (query param), cleared (replace) after the
   scroll so a refresh doesn't re-trigger.
5. **Mobile: dedicated `/search` route** (full-screen `SearchPage`), reached from the
   inbox field. Desktop uses the ⌘K modal (no route).
6. **Palette: reuse `ui/command.tsx`** if it fits; otherwise compose `Dialog` + `Input`
   + a results list. Either way the result row + snippet renderer is shared with mobile.

**Deferred for v1** (decided 2026-06-17; the API/data layer don't block adding them later):
- *Result pagination / "load more":* NOT a speed lever — LIKE's cost is the content scan,
  not the row count (~5 ms now). It's a UX/payload nicety. v1 returns top-N (default 50,
  cap 200); add a keyset cursor (`created_at`+`id`) only if results commonly exceed that.
- *Time-range filter:* the search set is small (~4k rows) and results are recency-ordered
  + session-grouped, so "find something recent" is already covered. If wanted later,
  presets (7d / 30d / all) over a `created_at` range (which also speeds the scan) beat a
  full date picker.

## 5. Architecture

- **Backend**: a `search_messages` service + `GET /api/search/messages`; plus an
  `around=<id>` window mode on the existing messages list (needed so the chat can load
  the page that contains a deep-linked message).
- **Frontend**: `ApiContext.searchMessages()` + `listSessionMessages({aroundId})` →
  `useMessageSearch` (debounced) → `SearchPalette` (desktop) / `SearchPage` (mobile),
  both rendering a shared `<ResultGroup>` / `<Snippet>` → on select, navigate to
  `/chat/:id?msg=…` → `ChatPage` loads the window, scrolls, and highlights.
- **Snippet contract** (one shape, reused everywhere): the API returns each match as
  `{ id, session_id, author, source, created_at, snippet: { prefix, match, suffix } }`
  (already split around the matched term, so the client just renders `match` with the
  gold highlight — no client-side offset math, no XSS surface).

## 6. Phases — one branch `feat/message-search`, atomic commits, single PR at checkpoint

### P1 — Backend: search + around-window (+ unit tests)
- `storage/messages_service.py`: `search_messages(conn, query, *, platform='avibe',
  types=('user','result'), limit=50)` → groups by session
  (`session_id, title, project_id, project_name`, each with `matches[]`). Build snippet
  segments in Python (case-insensitive find on `content_text`, window ±~40 chars,
  collapse whitespace). Extract the ilike-escape into a shared helper reused by title +
  content search (avoid duplicating the escape in two services).
- `storage/messages_service.py`: add `around_id` (+ `around_limit`, default 25) to
  `list_session_messages` — resolve anchor `(created_at, id)`, run the before+after
  queries, merge, and return `{ messages, has_more_before, has_more_after }`.
- `vibe/ui_server.py`: `GET /api/search/messages?q=&limit=` and the `around_id` param on
  the messages endpoint.
- Tests: Latin + CJK + LIKE-metachar query, empty/whitespace query, grouping,
  snippet windowing, around-window (oldest/newest/middle targets).

### P2 — API client + shared search hook + snippet renderer
- `ApiContext.tsx`: `searchMessages(q, limit)`; extend `listSessionMessages` with
  `aroundId`. Add response types.
- `useMessageSearch(query)` hook: ~200 ms debounce, abort in-flight on change,
  loading/empty/error states.
- `<Snippet>` (prefix + `<mark class="search-hl">match</mark>` + suffix) and a
  `<SearchResultRow>` (role chip + snippet + relative time) shared by desktop + mobile.

### P3 — Desktop: command palette + sidebar entry + ⌘K
- `SearchPalette.tsx` (reuse `command.tsx`/`Dialog`): query input, grouped results,
  ↑↓ / ↵ / Esc, on-select `navigate('/chat/:id?msg=…')` and close.
- `WorkbenchSidebar.tsx`: a Search row between Inbox and Capabilities that opens the
  palette.
- `AppShell.tsx`: global ⌘K / Ctrl-K listener to open the palette from anywhere.

### P4 — Mobile: search page + route + inbox field
- `SearchPage.tsx` + `<Route path="/search">` in `App.tsx`: full-screen — back +
  active input + grouped results (reuses `<SearchResultRow>`).
- `InboxPage.tsx`: a full-width search field at the top → `navigate('/search')`.
  Remove the now-redundant header search icon (single entry).

### P5 — Chat jump + highlight (the trickiest)
- `ChatPage.tsx`: import `useSearchParams`; on `?msg=<id>`, if the id isn't in the
  loaded window call `loadAroundMessage(id)` (around API) to rebuild the window centered
  on it; then `scrollIntoView` the row and toggle a highlight class for ~3 s, then clear
  the param (replace).
- iOS integration: set a transient flag so the programmatic scroll-to-target is **not**
  fought by the `anchorRef`/`ResizeObserver` restore; only resume anchoring after the
  jump settles.
- Windowing: around-mode introduces a middle window, so add a **load-newer** path
  (`after_id`) symmetric to the existing load-older, gated by `has_more_after`.
- CSS: `.search-hl` (inline gold mark) + `.msg-highlight` (row mint-soft background
  pulse fading over ~3 s) in the UI's CSS-var system (not arbitrary one-off colors).
- `MessageRow`: accept a `highlighted` flag (keyed on `message.id`).

### P6 — i18n, states, dogfood
- `workbench.search.*` + `workbench.nav.search` in `en.json` + `zh.json` (1:1).
- Empty / no-results / long-snippet / CJK / very-old-target states.
- Manual dogfood on the Incus regression env (desktop + iOS Safari path), since the
  scroll-anchor interaction can't be fully unit-tested.

### P7 (optional, later) — FTS5 acceleration (NOT a Chinese fix)
**Finding (2026-06-17, probed on the app's Python `sqlite3`, lib 3.50.4):** FTS5 is
available; the only built-in substring tokenizer is `trigram`, and trigram needs **≥3
chars** — 2-char Chinese queries (`回归`/`部署`/`服务`/`退避`) all return **0**, 1-char too.
Chinese search terms are overwhelmingly 2 chars, so **built-in FTS5 cannot do Chinese
search**. `LIKE %回归%` matches correctly. Making FTS5 cover Chinese needs either (a) a
LIKE fallback for <3-char queries (so the common Chinese case stays on LIKE regardless),
or (b) an external CJK word-seg tokenizer (jieba/ICU) shipped as a per-platform native
extension — a packaging burden for a `uv`-installed local app (ICU is not compiled in;
`enable_load_extension` was allowed on the dev Mac but that is not portable).
- So FTS5 here is a **speed-at-scale accelerator for ≥3-char (mostly Latin/long) queries
  + `bm25()` ranking**, behind the same `search_messages` API, with LIKE retained for
  short/CJK queries. Worth doing only if LIKE latency on ≥3-char queries becomes
  noticeable at scale. Migration: `messages_fts` (trigram, external-content on
  `content_text`) + INSERT/UPDATE/DELETE sync triggers + backfill (cheap at any size).
  **Decision: stay on LIKE for v1.**

## 7. Evidence layers (for the PR description)

- **unit**: `search_messages` (Latin/CJK/escape/empty/grouping), snippet windowing,
  `around_id` window (oldest/middle/newest).
- **contract**: `/api/search/messages` and `messages?around_id=` response shapes.
- **scenario**: search → select → jump → highlight closed loop (add to the relevant
  `tests/scenarios/*` catalog if one fits).
- **manual**: regression-env dogfood — desktop ⌘K + iOS scroll-to-message/highlight.

## 8. Risks / edge cases

- **iOS scroll-to-message vs the manual anchor** — highest risk; the jump must suppress
  anchor restore or it'll be yanked back. Validate on a real iOS Safari path.
- Around-window for a very old target, then scrolling both directions (load-newer added).
- Highlight cleanup on unmount / rapid re-navigation (clear timers).
- `content_text IS NULL` (media-only messages) — skip in search.
- LIKE performance at scale — bounded by personal-scale data; P7/FTS5 is the escape hatch.
- Keep transport/platform out of core: search is platform-agnostic in the service; the
  endpoint just exposes it (consistent with the multi-platform rule).
