# Show Page Annotation — Phase 1: Default Overlay, Control Surfaces, Authenticated Writes

Status: approved for implementation (owner sign-off 2026-07-20, design frames approved).
Owner decisions baked in: snapDOM for capture, "workbench access = logged-in user"
auth model, desktop header segmented control (collapsed by default), mobile popup
mode picker with last-mode memory.

## Summary

The annotation pipeline already exists end to end: `show_session_events`
persistence, transcript projection (`metadata.source = "show_page"`), optional
agent dispatch (`payload.dispatch: true`), SSE streaming, `vibe show mark` /
`vibe show event` CLI, and a feature-complete `AnnotationOverlay`
(smart/screenshot) in `@avibe/show-sdk/react`. What is missing is the product
surface: nothing mounts the overlay, nothing controls it, screenshot capture
depends on `getDisplayMedia` (unavailable on iOS), and event writes are
capability-token based rather than identity based.

Phase 1 closes exactly that gap:

1. Every Show Page (private and public) automatically gets the annotation
   overlay via server-side HTML injection — no scaffold changes, old pages
   included.
2. One annotation state machine with four control surfaces: chat header
   control, in-page floating toolbar (standalone tabs), `window` API, and an
   agent-driven SSE control event (+ CLI).
3. Event writes carry an authenticated user identity. Public pages accept
   writes only from logged-in workbench users.
4. Overlay visuals rebuilt to the approved design (design.pen frames
   `H8oicB`, `i6HVWm`, `kn94D`, `urZTa`, `WVGjS` in `avibe-docs/design.pen`).
5. Screenshot capture switches to same-origin DOM rendering (snapDOM) with
   `getDisplayMedia` as a desktop-only fallback.

## Non-Goals (Phase 1)

- No screenshot attachment storage; the captured image still travels inside
  the event payload (Phase 3 moves it to attachments). Cap capture size
  (long edge ≤ 2048 px) to bound payload growth.
- No rich chat card rendering for `show_page` messages (Phase 3).
- No overlay theming; the overlay ships a self-contained fixed dark floating
  style (mint/violet accents) that works over light and dark pages.
- No freeze-mode entry point, no multiple screenshot drafts.
- The legacy `AgentationToggle` in the main workbench UI stays untouched.
- No role hierarchy; identity is recorded to make future roles possible.

## Architecture

```
                 ┌────────────────────────────────────────────┐
 Chat header ────┤ postMessage: avibe:annotation:control/state │
 (embedded host) └────────────────────────────────────────────┘
                                     │ same-origin iframe
                                     ▼
  Show Page HTML ── injected <script module> bootstrap ── mounts AnnotationOverlay
                                     ▲                    (separate React root,
        vibe show annotate ── SSE ───┘                     portal to body)
        (system.annotation.control)
```

- **Injection point (avibe)**: `vibe/ui_server.py` already injects the
  `__AVIBE_SHOW__` config script into private Show Page HTML
  (`_inject_show_runtime_config`). Phase 1 extends this to also inject a
  `<script type="module">` tag loading the runtime-served annotation bootstrap,
  and performs the same injection on public `/p/` HTML (config **without**
  `writeToken`).
- **Bootstrap module (vibe-show-runtime)**: the runtime dev server exposes a
  session-independent entry (e.g. `__show/annotation.js` resolved as a Vite
  virtual module / vendor asset) that imports the SDK overlay and mounts it in
  its own React root appended to `document.body`. It must not touch the user
  app's module graph beyond sharing the runtime-managed React instance, and it
  must never crash the host page (top-level try/catch, bail silently).
- **Host detection (overlay, runtime side)**: `location.search` contains
  `vibe-embed=1` → `embedded` host (chat iframe): floating toolbar hidden, mode
  pill shown, controlled via postMessage. Otherwise `standalone`: floating
  FAB/toolbar shown (bottom-right), full local control.
- **Auth-aware UI**: overlay asks `GET <basePath>__show/me`; when
  `canAnnotate` is false (anonymous public visitor) the FAB stays hidden.

## Frozen Interface Contracts

Field names below are frozen. Deviations route through the orchestrator, never
lane-to-lane.

### 1. Injected config (extends existing `__AVIBE_SHOW__`)

```ts
globalThis.__AVIBE_SHOW__ = {
  sessionId: string,
  basePath: string,
  eventsPath: string,        // unchanged
  streamPath: string,        // unchanged
  writeToken?: string,       // private pages only (unchanged)
  annotation: {
    authenticated: boolean,  // server-known auth state at render time
    mePath: string,          // "__show/me" relative to basePath
  }
}
```

### 2. Overlay window API (runtime/SDK)

```ts
__AVIBE_SHOW__.annotation.api = {
  enable(mode?: "smart" | "screenshot"): void,  // no mode → last-used (localStorage), default "smart"
  disable(): void,
  setMode(mode: "smart" | "screenshot"): void,
  getState(): { enabled: boolean; mode: "smart" | "screenshot"; available: boolean },
  subscribe(cb: (s: ReturnType<typeof getState>) => void): () => void,
}
```

Mode memory: `localStorage` key `avibe:annotation-mode:<sessionId>`.

### 3. postMessage protocol (same-origin, chat parent ↔ iframe)

```ts
// parent → iframe
{ type: "avibe:annotation:control", action: "enable" | "disable" | "set-mode", mode?: "smart" | "screenshot" }
{ type: "avibe:annotation:query" }
// iframe → parent (on mount, on every state change, and in reply to query)
{ type: "avibe:annotation:state", enabled: boolean, mode: "smart" | "screenshot", available: boolean }
```

`enable` without `mode` uses the remembered mode. `available:false` means the
overlay is mounted but writes are not possible (anonymous public visitor).

### 4. Agent control event (SSE) + CLI

New event type accepted by the pipeline: `system.annotation.control`
(actor `system`), payload:

```ts
{ action: "enable" | "disable" | "set-mode", mode?: "smart" | "screenshot" }
```

- No transcript projection (`transcript_text` empty ⇒ no chat message).
- Never dispatches an agent turn.
- Reaches the page through the existing `show.event` SSE stream; the overlay
  subscribes and applies it in both hosts.
- CLI: `vibe show annotate [--session-id <id>] (--on | --off | --mode smart|screenshot)`
  → posts this event via the existing CLI show-event path. `--on --mode X` is
  one event with `action:"enable", mode:"X"`.

### 5. Authenticated writes

- Every accepted human event write records the author into the event payload
  and message metadata:

```ts
payload.author = { kind: "user" | "local", email?: string }
```

  Remote (avibe.bot OAuth session): `{ kind: "user", email }` — the email from
  the validated OAuth session. Local LAN / same-machine (no OAuth configured):
  `{ kind: "local" }`.

  Contract note (revised 2026-07-20 after Lane A finding): role hierarchy
  (owner/member/…) is a control-plane concept that does not exist on the
  device today — the OAuth session carries only `email`/`sub`/`instance_id`,
  and no role claim is issued anywhere. Recording the stable `email` IS the
  forward-compatibility hook: a future role system maps emails to roles at
  read time; stored events never need rewriting. Do not invent a role at
  write time.
- Private `/show/<sid>/__show/events` POST: unchanged token requirement
  (`X-Vibe-Show-Token`), now additionally records `author`.
- Public `/p/<share>/__show/events` POST: **allowed** when the request carries
  a valid workbench OAuth session (same cookie/validation path as the
  workbench); otherwise `403 public_show_events_login_required`. No
  `writeToken` required on the public path; CSRF is covered by SameSite
  cookie plus a required custom header (`X-Vibe-Show-Client: overlay` — value
  checked for presence, mirrors the private header pattern).
- New endpoint on both surfaces: `GET <basePath>__show/me` →
  `{ authenticated: boolean, canAnnotate: boolean }` (private surface: always
  `true/true`; public surface: depends on OAuth session; anonymous GET is
  allowed and returns `false/false`).
- GET/stream visibility is unchanged (public stays redacted read-only).

### 6. Chat iframe host marker

The workbench chat appends `vibe-embed=1` to the Show Page iframe `src` query
string. No other component may repurpose this parameter.

### 7. Bootstrap asset path

The injected tag is exactly
`<script type="module" src="{basePath}__show/annotation.js"></script>`.
Lane A guarantees `__show/annotation.js` is NOT treated as an events/API path
and proxies through to the runtime on both `/show/` and `/p/` surfaces (public
surface uses the same immutable-safe serving as other runtime assets). Lane R
guarantees the runtime serves a JS module at
`/sessions/<sessionId>/app/__show/annotation.js` for every session workspace,
including pre-existing ones.

## Lane Split

Three lanes, mutually exclusive file scopes. Contracts above are the only
coupling; no lane-to-lane negotiation.

### Lane R — `vibe-show-runtime` (overlay, bootstrap, capture)

Scope: `packages/sdk/**`, `packages/runtime/**`, `examples/**` (optional demo),
docs touched only for README pointers. Do not touch `packages/ui` (SDK must not
depend on the UI package — CONTRIBUTING boundary).

1. Visual rebuild of `AnnotationOverlay` + `CommentPopover` + markers to the
   approved design: dark floating chrome (`#11111C` surfaces, mint `#5BFFA0`
   human accents, violet `#7C5BFF` agent accents), anchor chip + intent chips
   (评论/修改/疑问/批准 ⇒ intent `comment|change|question|approve`), severity
   accents, numbered markers (user mint / resolved gray / agent violet bot),
   screenshot region chrome (dim masks, corner handles, numbered items, batch
   card), mode pill, mobile bottom-sheet comment card (touch targets ≥ 44 px),
   standalone FAB ⇄ pill-toolbar. Self-contained styles; no external CSS deps.
2. Host modes: `embedded` (hide toolbar, show pill, obey postMessage) vs
   `standalone` (FAB + toolbar) per `vibe-embed=1` detection.
3. Control plane: window API (contract §2), postMessage listener/broadcaster
   (§3), SSE `system.annotation.control` handling (§4), mode memory
   (localStorage key in §2), auth gating via `__show/me` (§5) — hide FAB /
   show "login required" hint when `canAnnotate` is false.
4. Bootstrap entry served by the runtime (contract "Bootstrap module"):
   mounts overlay automatically, resilient to user-page errors, no scaffold
   edits, works for existing session workspaces.
5. Capture: add `@zumer/snapdom` as a runtime-managed dependency, lazy-loaded
   on first screenshot-mode activation. Strategy: snapDOM same-origin DOM
   render (crop to region, long edge ≤ 2048) → fallback `getDisplayMedia`
   (desktop only) → error message. Keep the existing payload shape.
6. Tests: `npm run check` green; unit tests for control plane state machine,
   mode memory, host detection, and capture strategy selection (mock snapdom).

### Lane A — `avibe` backend (injection, auth, control event, CLI)

Scope: `vibe/ui_server.py`, `vibe/cli.py`, `vibe/i18n/**`, `core/show_pages.py`,
`core/show_session_events.py`, `storage/**` (only if a migration is truly
needed — prefer payload-level author, no schema change), `tests/**`,
`docs/plans/` updates. Do not touch `ui/**`.

1. Extend HTML injection: annotation bootstrap `<script module>` on private
   **and** public Show Page HTML; config gains `annotation` block (§1); public
   config carries no `writeToken`.
2. `GET __show/me` on both surfaces (§5).
3. Public events POST: login-gated acceptance (§5), `403
   public_show_events_login_required` for anonymous, CSRF header presence
   check, author recording. Private POST: author recording added.
4. New event type `system.annotation.control` (§4): accepted, persisted,
   SSE-published, empty transcript, never dispatched.
5. CLI `vibe show annotate` (§4) following existing `vibe show mark` patterns
   (session resolution, `--json`, live-UI post path).
6. Tests: extend `tests/test_show_pages*.py` / `test_show_session_events.py` /
   `test_ui_show_pages.py` patterns — injection on both surfaces, me endpoint
   matrix, anonymous vs logged-in public POST, author recording, control event
   no-transcript/no-dispatch, CLI arg handling. `ruff check` on touched files.

### Lane C — `avibe` chat UI (header control, bridge)

Scope: `ui/src/**` only (ChatPage, new components under
`ui/src/components/workbench/`, `ui/src/i18n/en.json`, `ui/src/i18n/zh.json`).
Do not touch `vibe/**`, `core/**`, `tests/**`.

1. Header annotation control, visible only in Show Page mode, placed left of
   the back-to-chat button (design frames `H8oicB`/`kn94D`):
   - Desktop: collapsed = one 28 px icon button (`message-square-plus`);
     active = segmented group (toggle + Smart + 截图) with mint accent.
   - Mobile (`md` breakpoint down): single button opening a Popover
     (existing `ui` primitive) per frame `WVGjS`: Smart / 截图 radio rows +
     "关闭标注" row; button itself toggles on with remembered mode.
2. postMessage bridge to the iframe (§3): send control, receive state, keep
   button/segment state in sync, disable control until first `state`
   (`available` gating + tooltip when unavailable).
3. Append `vibe-embed=1` to the iframe src (§6).
4. Reuse `ui/src/components/ui/` primitives (Button, Popover, Tooltip …);
   all strings through i18n (en + zh).
5. Validation: `cd ui && npm run build` green; no `setState` in effect bodies
   (UI CI gate).

## Sequencing & Integration

1. All three lanes start in parallel; contracts are frozen by this document.
2. Each lane: own worktree under `.worktrees/<repo>/<branch>/`, branch from
   the repo default branch (`avibe`: master, `vibe-show-runtime`: main),
   non-draft PR, Codex review loop to zero unresolved threads, CI green. Lanes
   do not merge; the orchestrator gates and merges.
3. Merge order: R (runtime main) → A → C (A/C independent; R first so the
   bootstrap asset exists for regression source builds).
4. End-to-end verification by the orchestrator in the local Incus regression
   environment (github-source runtime) against the acceptance list below.

## Acceptance Criteria

1. Chat visual mode (private page): header control enables Smart → click an
   element → comment card → send → message appears in the chat transcript
   (`source: show_page`, author recorded) and triggers an agent reply streamed
   back; screenshot mode captures without any screen-share prompt.
2. Same flows on iPhone-class viewport: popover mode picker, bottom-sheet
   comment card, snapDOM capture works (no `getDisplayMedia` dependency).
3. Standalone tab (`/show/<sid>/` direct): FAB → toolbar controls everything;
   same event flows.
4. `vibe show annotate --on --mode screenshot` flips the live page into
   screenshot mode; `--off` exits; no chat message is produced by control
   events.
5. Public `/p/<share>/`: anonymous — markers/read-only visible, no compose
   affordance, POST rejected (`public_show_events_login_required`); after
   logging into the workbench in the same browser — annotation works and the
   recorded author identity matches the logged-in email.
6. Esc exits annotation mode; while active, page controls do not fire; after
   disable, the page behaves exactly as before injection.
7. Gates: runtime `npm run check`; avibe `ruff check` + focused pytest;
   `ui npm run build`; Codex review clean on every PR.

## References

- Design: `avibe-docs/design.pen` frames `H8oicB` (chat smart), `i6HVWm`
  (chat screenshot), `kn94D` (components & rules), `urZTa` (mobile sheet),
  `WVGjS` (mobile mode popup).
- Existing pipeline: `core/show_session_events.py`,
  `vibe/ui_server.py` (§ show events / show page serving),
  `avibe/docs/plans/show-session-event-pipeline.md`.
- SDK inventory: `vibe-show-runtime/packages/sdk/src/{index.ts,react.tsx}`,
  roadmap `vibe-show-runtime/docs/agent-os-implementation-plan.md`
  (this phase completes M6/M8.5 mounting + M12 auth posture).
- Capture: snapDOM (`@zumer/snapdom`), chosen 2026-07-20 (~42 KB gzip, zero
  deps; iOS Safari lacks `getDisplayMedia`).
