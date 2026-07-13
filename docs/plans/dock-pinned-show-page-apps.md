# Dock-Pinned Show Page Apps

Status: design approved-pending-owner-review · Plan created 2026-07-13
Owner: PM session `sesf875xc5svz` · Design frames: `design.pen` (see §8)

## 1. Background & Goal

Show Pages today are per-session visualizations opened from the chat header.
The product direction is: **a Show Page is an app the agent built for the
user**. This feature makes that literal — the user can pin a session's Show
Page to the workbench Dock, where it behaves like any other app: a Dock tile,
a Mac-style window, minimize/maximize/close, open-in-browser-tab.

This is deliberately the seed of the "Avibe as Agent OS installs apps" model
(§7): the v1 data model (ordered dock list + param-driven app windows) must
survive that evolution.

Non-goals (v1): mobile window management (windows stay desktop-only, ≥ md),
publishing a session page as a standalone/detached app, third-party app
installation, per-app permissions.

## 2. UX Spec

### 2.1 Pin from the share popover
- `ShowPageShareControl` (chat header, visible in show-page mode) gains a
  **Pin to Dock** section: icon + label + `Switch`, with a one-line caption
  ("Shown in the Dock and opens as an app").
- Pinning is **independent of share visibility** — a private page can be
  pinned. Pin/unpin never changes visibility, never deletes the page.
- The popover already calls `ensureShowPage` on open, so the page exists by
  the time the toggle renders. Toggle is optimistic with server round-trip.

### 2.2 Dock tile
- Pinned pages appear in the Dock after the built-in tiles, in user order.
- Tile visual: rounded-square **letter avatar** — first grapheme of the app
  title (supports CJK/emoji) on an accent tint **derived by hashing
  `session_id`** into the brand accent set (mint/cyan/violet/gold), so
  multiple pinned apps are distinguishable without an icon pipeline.
- Label + running indicator + minimized thumbnails behave exactly like
  built-in apps (same `Dock.tsx` tile anatomy).
- Title = live session title when loadable, else `title_snapshot` from the
  pin record, else `session_id` prefix.

### 2.3 Window behavior
- Click tile → focus existing window for that `session_id` if one is open,
  else `openApp('showpage', { params: { sessionId, title } })`.
- Window = existing `AppWindow` chrome: traffic lights (close / min / max),
  drag, 8-dir resize, persistence — all inherited, no new window code.
- **New:** title-bar **right side** gets an "open in new tab" icon button
  (generalized as `AppDefinition.externalHref?(params)`; only `showpage`
  defines it in v1). Opens the private URL in a browser tab.
- **v1.1 (owner ask 2026-07-13 21:49, ships as a follow-up PR after #899):**
  a **chat-bubble button to the LEFT of the external-open button** — click
  navigates the workbench to the owning session's Chat page and minimizes
  the window (chat visible immediately; the Dock thumbnail brings the app
  back). Showpage-only, via the same optional-hook pattern as
  `externalHref` (or a small generalization to a title-bar-actions hook —
  implementer's choice, keep it idiomatic). Archived/missing session: still
  navigate; the chat surface owns its own not-found state. Design frame
  updated (`q4E5yl`).
- Body = same-origin `<iframe src="/show/<session_id>/">` — always the
  **owner (authed) surface**, regardless of visibility (authed workbench
  context; HMR means the app updates live while the agent keeps building
  it). Copy the sandbox attrs from `ChatPage.tsx`; per the standing decision
  the workbench show-page iframe is intentionally same-origin-trusted — do
  not harden.
- **Amendment (2026-07-13, review round 1-6 root cause):** the existing
  `serve_private_show_page` route 404s when `visibility == "public"`, which
  would break pinned public pages. Decision: **extend the authed `/show/
  <session_id>/` route to serve `private` AND `public` pages** (identical
  surface); `offline` keeps returning not-found (window placeholder covers
  it). No new exposure: the route stays behind workbench auth, and a public
  page is already anonymously readable via `/p/`; anonymous access still has
  exactly one surface (`/p/`). ChatPage's public→`/p/` switch is untouched.
- Missing/archived/offline page → window body renders a friendly placeholder
  (never a dead button): explain + "Unpin" shortcut. Opening a pinned app
  must **not** auto-create/ensure a page.

### 2.4 Dock management
- **Drag to reorder**: the resident tile row (built-ins + pinned) is
  reorderable via `framer-motion` `Reorder` (already a dependency; do not add
  dnd-kit). Order persists server-side on drop. Minimized-window thumbnails
  section is not reorderable.
- **Right-click menu**: converge the Dock's inline menu onto the shared
  `ui/context-menu.tsx` primitive (reuse ladder: promote the near-duplicate).
  Pinned tiles add **Unpin from Dock**; built-ins (`files`/`terminal`/
  `editor`) have no unpin item in v1. All tiles keep New Window / Show All
  Windows; pinned tiles also get Open in New Tab.
- Unpinning while a window is open leaves the window open; the tile then
  behaves like `preview` (transient tile while any window exists).

## 3. Data Model & Persistence

Pins are **durable product state, not per-browser UI state** → server-side,
in the existing `state_meta` KV (`get_state_meta`/`set_state_meta`), single
key:

```
state_meta["workbench.dock.v1"] = {
  "order": ["files", "terminal", "editor", "show:<session_id>", ...],
  "pins":  [{ "session_id": str, "title_snapshot": str, "pinned_at": iso8601 }]
}
```

- `DockItemId` namespace: builtin ids verbatim; pinned pages as
  `show:<session_id>`; future kinds get new prefixes (`app:<id>`), so the
  order list survives the §7 evolution.
- `order` covers **all resident tiles including built-ins** (built-ins are
  reorderable, just not unpinnable).
- Reconciliation on read (client): drop unknown ids, append missing built-ins
  and missing pins at the end. Server validates writes (set-equality against
  known ids, caps list size, rejects duplicates).
- Window layout stays in localStorage as today (`workbenchPersistence.ts`);
  `showpage` windows rehydrate because the registry knows the appId and
  `params` are persisted already.

## 4. API Contract (freeze before implementation)

All under the existing authed `/api` surface (native FastAPI routes must go
through `CompatApp.dispatch_native_request` so they inherit
`enforce_remote_access_cookie` + CSRF — see the #659 lesson).

```
GET    /api/dock                     → { ok: true, dock: DockDoc }
POST   /api/dock/pins                { session_id }        → { ok, dock }   # idempotent
DELETE /api/dock/pins/{session_id}                          → { ok, dock }   # idempotent
PUT    /api/dock/order               { order: string[] }    → { ok, dock }

DockDoc = { order: string[], pins: DockPin[] }
DockPin = { session_id: string, title_snapshot: string, pinned_at: string }
```

- `POST pins` appends `show:<sid>` to the end of `order`; captures
  `title_snapshot` from the session's current title server-side.
- Errors: 404 unknown session / no show page on pin; 400 invalid order.

## 5. Frontend Changes (map to real files)

| Change | Where |
| --- | --- |
| `showpage` app registration (non-resident, param-driven like `preview`; `lockTheme` unset) | `ui/src/apps/registry.tsx` |
| Iframe window body + placeholder state + letter-avatar icon helper | new `ui/src/apps/ShowPageApp.tsx` (+ small pure helper w/ vitest) |
| Title-bar right-side external-link button via `AppDefinition.externalHref?` | `ui/src/components/apps/AppWindow.tsx` |
| Dock: render pinned tiles from context; Reorder wrapper; shared context menu; unpin item | `ui/src/components/apps/Dock.tsx` |
| `DockProvider` (fetch/reconcile/actions; optimistic) mounted in `AppShell` | new `ui/src/context/DockContext.tsx` |
| Pin section in share popover | `ui/src/components/workbench/ShowPageShareControl.tsx` |
| API methods `getDock` / `pinDockShowPage` / `unpinDockShowPage` / `setDockOrder` | `ui/src/context/ApiContext.tsx` |
| i18n strings (en + zh) | `ui/src/i18n/en.json`, `zh.json` |

Backend: `vibe/api.py` handlers + `vibe/ui_server.py` routes + a small
`core/dock_store.py` (or colocated helpers) over `state_meta`; pytest
coverage for round-trip, idempotency, order validation, unknown-session pin.

## 6. Edge Cases & Rules

- Pin survives session archive; window shows placeholder; unpin always works.
- Never surface `/p/<share_id>` inside the workbench app window (private
  surface only); external-tab open also uses `/show/<sid>/`.
- Two browser tabs: dock doc is last-write-wins on order; pins idempotent.
- Multiple windows of the same pinned app allowed (New Window), matching
  built-ins; tile click focuses the most recent.
- Mobile (< md): windows don't exist; v1 ships desktop-only and files a
  follow-up to surface pinned apps in the mobile Apps surface.

## 7. Future: the OS model (direction, not v1 scope)

1. **App = manifest, not code.** `AppManifest { id, kind: builtin | showpage
   | remote | package, name, icon, entry (component key | url), source,
   permissions? }`. Registry = static built-ins ∪ server-registered
   manifests (`GET /api/apps`). Dock, launcher, and windows consume manifests
   uniformly. Pinned show pages are the first dynamic kind — this PR.
2. **Install = registering a manifest.** Sources: agent-built show pages
   (now), skill-bundled web apps (`askill` ships a `ui` entry), user-added
   remote URLs, later a store. "Publish" flow detaches a session page into a
   standalone app (stable id, own icon/name, copied workspace) — the full
   "agent ships an app to its user" story.
3. **App Library.** A management surface (Settings → Apps, or a built-in
   Library app): installed list w/ kind badges, show-in-Dock toggle, order,
   uninstall, per-app permission tier (third-party kinds get the sandbox
   work: realpath-gate + kernel sandbox). Dock = pinned ⊆ installed + running
   transients.
4. **Compatibility guarantee.** v1's `order` id namespace, pin records, and
   param-driven windows all carry over; nothing here needs re-migration.

### 7.1 Phase 2 (owner decision 2026-07-13): App Library subsumes the Show Pages admin page

The existing `/admin/show-pages` page (`ui/src/components/ShowPagesPage.tsx`,
control-panel nav) does two jobs: full show-page inventory + visibility/link
management. Once pins ship, keeping it standalone would leave two lists over
the same objects. Decision: fold it into the App Library.

- **Library = one built-in app, two views.**
  - *Apps*: exactly the docked set — **one state bit: being an app ≡ being
    in the Dock; no installed-but-undocked middle state** (owner-confirmed
    2026-07-13 evening after the toggle-vs-delete ambiguity). Row actions by
    kind: reorder (all) · remove-from-Dock (showpage — the page itself stays
    in the inventory) · uninstall (remote/package — actually removes) ·
    built-ins locked · Add App (remote URL, §7.3). NO per-row Dock toggle
    here; the Show Pages view's toggle is the single promote/demote gesture.
    (Not every Show Page is an app; every session has a page, so the full
    inventory must not masquerade as the app list.)
  - *Show Pages*: the full inventory (today's admin page content: status,
    visibility private/public/offline, link + share-id, open) **plus a
    per-row "Pin to Dock" toggle** — pinning is the "install" gesture.
  - Same data sources (`/api/show-pages` + `/api/dock`), two projections; no
    new backend.
- **Library ships as built-in app #4** (like Launchpad/App Store: the app
  manager is itself an app) — reorderable, not unpinnable, same as the other
  built-ins.
- **Remove** the `/admin/show-pages` route + nav item; redirect the old route.
  Control panel returns to pure ops/config.
- **Mobile**: windows are desktop-only, so the Library body must also render
  as a full-screen route on mobile (same component, two shells) — no
  capability regression vs. the old admin page.
- **Sequencing**: v1 pin PR (in flight) is scope-frozen and unaffected.
  Phase 2 is a separate lane/PR after v1 merges.
- **Concept frames**: `xCSqW` (Apps view, tabbed, one-bit model) + `td17F` (Show Pages view —
  tabs, filters, expanded row w/ visibility+link+suffix mgmt, per-row Dock
  toggle), both in design.pen right of `NbPMq`.

### 7.1c Phase 2.1 — acceptance-feedback round (owner hands-on, 2026-07-13 23:27; SUPERSEDES the one-bit model)

Owner used the shipped Library and upgraded the model to **two layers**:
**Apps list = "installed" set** · **Dock = the resident subset**. His 7 points,
integrated:

1. **Built-ins are undockable too** (reverses the v1 lock): every tile —
   files/terminal/editor/library — can be unpinned from the Dock via
   right-click or the Apps-view action. Built-ins can NEVER be removed from
   the Apps LIST (no 移出 for them); no lock icons anywhere.
2. **Library tab rename**: "Show Pages / 展示页面" view → **"AI"** (en+zh
   both "AI").
3. **Apps view rows get two distinct actions**: 取消固定/固定到 Dock
   (toggles Dock membership, row STAYS in the list) and — AI-kind rows only —
   **移出** (removes from the Apps list entirely; page itself untouched).
4. **Row click opens the app** (both views).
5. **AI view per-row control**: the Dock switch → an **"添加到 App" button**
   (Button primitive, state-aware: 添加到 App ↔ 移出). It toggles APPS-LIST
   membership; adding also docks by default (keeps the v1 one-gesture feel);
   removing removes from both. (PM default — flag if wrong.)
6. **AI view open affordance**: remove the standalone Open button; an open
   icon sits beside the row title; clicking title/row **opens the app
   window** (reuse the showpage window), NOT a browser tab. Share-link
   management stays in the expanded panel.
7. **Library is itself a listed built-in app** (appears in the Apps view,
   dockable/undockable like the rest), and a **permanent 应用库 entry lives
   in the control-panel sidebar** (undismissable escape hatch; links to the
   existing /apps/library route).
8. **(owner 23:38)** The bottom-left Apps launcher button gets a
   **right-click context menu** (shared primitive): 打开应用库 (+ 显示所有
   窗口 only if trivially supported). Third Library escape hatch alongside
   the sidebar entry and the empty-dock hint.

Data model (compatible evolution, no migration): `pins` = installed AI
pages; `order` = docked ids and becomes a **SUBSET** of {built-ins ∪ pins}
(server validation: unique, known ids, subset — no longer set-equality;
reconcile stops force-appending built-ins). Existing docs (all built-ins in
order) remain valid. API surface unchanged: dock/undock = PUT order with/
without the id; install/remove = POST/DELETE pins (POST also appends to
order per §5 default). Empty Dock is allowed; when empty, the Dock popover
shows an App Library shortcut hint (never a dead surface).

### 7.1b Mobile Dock — Option B locked (owner decision 2026-07-13 22:22)

- The mobile 更多 tab becomes **Apps** (grid icon). Tapping summons a bottom
  drawer = the mobile Dock: same tiles, same server-side order as desktop
  (pin anywhere → appears everywhere). One tap to any app; apps open
  full-screen (existing `/apps/*` routes; pinned pages via a full-screen
  show-page route). Long-press tile = manage/unpin; built-ins locked.
- Former More-page content compresses into a drawer footer chip row:
  **设置** (RENAMED from 控制台 — copy alignment with desktop, owner ask),
  账号 (signed-in / sign-out), 外观, remaining items under 更多….
- **Copy rename ships early**: `more.controlPanel` zh 控制台→设置 (en
  aligned to "Settings") goes into the v1.1 micro-lane (2 i18n lines) so the
  interim UI is aligned before the drawer lands.
- Evolution: this drawer grows into the Option-C home-screen page when the
  app count justifies it (same tab slot, same mental model).
- Design frame `Zb74E` (final, chips updated). Own lane after v1.1.

### 7.1d Ideas 3+4 approved (owner 2026-07-14 02:32)

- **⌘K search reaches apps**: the global search adds an "Apps" result
  section — built-ins + installed apps + ALL AI pages (inventory; searching
  an uninstalled page and opening it is a feature). Enter/click: desktop →
  open the app window (showpage window for pages); mobile → the existing
  mobile open behavior. Additive to existing result sections.
- **Keyboard dock switching**: desktop-only chord to focus/launch the Nth
  Dock tile in current order. NOTE: browsers reserve ⌘/Ctrl+1-9 for tab
  switching (not interceptable) — use **⌥/Alt+1-9** (verify interceptability
  in-code; follow the existing ⌘W/⌘M chord pattern in WindowLayer).
- **AI-view inline rename**: rename affordance in the AI row's expanded
  management panel; saves the SESSION title via the existing session PATCH
  (same as the chat header TitleField). Display already prefers the live
  title everywhere; `title_snapshot` remains a fallback only. Built-ins not
  renamable. (Idea 1 — agent-suggested pinning — deliberately deferred;
  idea 2 — update dots — not scheduled yet.)

### 7.2 Becoming an app: the ladder (owner Q&A 2026-07-13)

Pinning **is** installing — no separate ceremony. Two entrances, one action:
the share-popover switch (v1) and the per-row Dock toggle in the Library's
Show Pages view (Phase 2). The full ladder: **page (session byproduct) → pin
(lightweight app, one switch) → publish (standalone app, stable id/icon,
detached from the session lifecycle — future)**.

### 7.3a Chat as an App — windowed chat (owner-approved direction 2026-07-13 22:06)

The chat-bubble button's end state is **opening a Chat window beside the app
window** (watch the dashboard while instructing the agent; HMR shows the
change live) — not navigating away. Approved plan, two steps:

1. **v1.1 ships navigate+minimize** (§2.3 v1.1) — usable immediately, zero
   risk; its click handler is later swapped for the window-open call, nothing
   wasted.
2. **Chat-as-an-App is its own milestone** (after Phase 2): extract a
   container-relative `ChatSurface` from `ChatPage` (page and window render
   the same component — the exact one-source/two-shells pattern used for
   `ShowPagesView` in #899), register a `chat` app kind (param `sessionId`,
   multi-instance), and upgrade the chat-bubble to open the chat window
   adjacent to the app window. The routed Chat page stays canonical (deep
   links, notifications, mobile). Design frames before kickoff. Product
   significance: chat itself becomes an app in the OS — the Agent-OS metaphor
   completes.

### 7.3 User-added URL apps (kind: remote) — Phase 3

Owner confirmed the bookmark-style want: Add App → name + URL → appears in
the Apps list, pinnable to Dock, opens as a window. Requirements settled now
so Phase 2 leaves no dead end:

- **Embedding fallback is mandatory**: many sites refuse framing
  (X-Frame-Options / CSP `frame-ancestors`). A remote app window must detect
  failure and degrade to "open in new tab" (and/or a per-app "opens in:
  window | tab" setting).
- **Sandbox required**: unlike same-origin-trusted Show Pages, remote
  content gets a sandboxed iframe — this is the on-ramp to third-party app
  permission tiers.
- **Data model**: already compatible — a new `DockItemId` prefix (e.g.
  `app:<id>`/`url:<id>`) and an `apps`/manifest list alongside `pins`; no
  migration of v1 state.
- **Sequencing**: Phase 3, right after Phase 2 (kept out of Phase 2 to keep
  that lane small: Library + migration only).

## 8. Design Frames (design.pen)

Dark-theme frames alongside the Apps v2 set (right of `NbPMq`, y=-2315):

| Frame | id | Content |
| --- | --- | --- |
| Apps · Pin Show Page — Share Popover | `s2YOlP` | popover w/ Pin to Dock switch + rules legend |
| Apps · Dock — Pinned Show Page Apps | `zn8tz` | pinned letter-avatar tiles, context menu, reorder scene, menu rules |
| Apps · Show Page App Window | `q4E5yl` | `X7d3Ev` AppWindow instance + title-bar external-open + iframe dashboard body |
| Apps · Future — App Library / Apps View | `xCSqW` | manifest model, kinds, show-in-Dock toggles, evolution path (not v1) |

## 9. Delivery

Single lane, single PR (frontend-heavy + small backend), per
`.agents/skills/pr-delivery-loop/SKILL.md`. Evidence: pytest (dock API),
vitest (reconcile + avatar helpers), `npm run build`, manual checklist in a
local Incus regression. Docs follow-up in `avibe-docs` after merge.

Owner decisions embodied in the design (flag if you want them changed):
1. Pins are server-side/cross-device (`state_meta`), not per-browser.
2. Pinned-tile icon = letter avatar + hashed accent (no icon pipeline in v1).
3. Built-ins are reorderable but not unpinnable.
4. App window always uses the private `/show/` surface.
