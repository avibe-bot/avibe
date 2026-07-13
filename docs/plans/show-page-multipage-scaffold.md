# Show Page default multi-page scaffold with nested routing (#865)

## Background

A new Show Page workspace starts from a single-page React app (`core/show_pages.py`
`_write_default_runtime_files`). The scaffold proves the page renders but does not show
that a workspace can grow into a multi-page application, so agents tend to keep editing
one `App.tsx`. This issue makes multi-page routing an obvious, executable default without
prescribing what a "page" means.

This is a **capability/affordance** change, not a content policy. It does not define one
page per topic/feature/conversation/history, is not append-only, and does not touch the
durability/recovery scope owned by #669 (platform-owned Git checkpoints).

## Refinement (owner feedback, 2026-07-13)

The first cut of the demo over-prescribed: it shipped a content-heavy, bilingual (zh/en)
landing page with a table-of-contents, cards, and a nested `items` list+detail demo, and
it **replaced the user-facing "being generated" placeholder** that used to show right
after "Visualize". That reads as a template the agent must follow, and shows the user a
fake demo instead of a progress state. The scaffold was slimmed back to the affordance:

- `src/pages/index.tsx` is a clean **English placeholder** ("Your page is being
  generated…") — what the user sees while the agent builds. It renders one built-in
  component (`Card`) and leaves `Button`/`Badge` as commented imports to hint the UI kit.
- `src/pages/second.tsx` is a **one-line** example page — just enough to show "add a file
  under `src/pages/` = add a route".
- Removed: the `items` list/detail pages, the bilingual `t()` helper + `activeLocale`, and
  the nav machinery (`navItems`/`titleCase`) — the app shell no longer renders a nav.
- The file-based hash router (discovery, nesting, `[param]`, deep-link/refresh) is
  unchanged; the starter is English-only and minimal.

A follow-up polish (owner feedback, same day) restored the "live" feel the flat
card had lost: the placeholder now shows a pulsing emerald dot (`animate-ping`),
and — only after ~90s (so it never nags on arrival) — reveals a copy-able nudge
prompt (`Button`, with a manual-select fallback when the clipboard is blocked). The
example `second.tsx` became a tidy Card-based page. Both stay English-only.

## Serving model (verified against current code)

- avibe proxies **both** private `/show/<id>/` and public `/p/<share>/` to the **same**
  managed Vite dev server (`vibe-show-runtime`). There is no separate public build; the
  public surface only neutralizes the HMR client via inert shims.
- avibe performs **no** SPA/index.html fallback itself; it passes the upstream status
  through. The runtime's Vite dev server (`appType` defaults to `spa`) does serve a
  base-prefixed `index.html` for nested HTML requests, on both surfaces.
- `index.html` and `src/main.tsx` are the **runtime-owned app shell**. `main.tsx` builds
  `globalThis.__AVIBE_SHOW__` (sessionId, basePath, events paths, writeToken). avibe's
  inline `basePath` regex is anchored to the path end, so it is *not* nested-safe.
- No router is bundled (shared vendor = react, react-dom, jsx runtime, `@avibe/show-ui`,
  `motion`, `lucide-react`). The workspace has no `package.json`.

## Decision: dependency-free hash router, file-based discovery

Router type — **HashRouter (client-only, hash fragment)**:
- Deep-link + refresh on nested routes work in **both** serving modes with zero reliance
  on server SPA-fallback behavior — the browser only ever requests the app root, so the
  existing root-document serving already covers it.
- **No shell edit**: `index.html`/`main.tsx` are untouched. The document URL never leaves
  the app root, so `__AVIBE_SHOW__.basePath`, event endpoints, `./api/*` handler calls,
  and relative assets all keep resolving — avoiding a whole class of relative-URL bugs an
  agent would otherwise hit when adding a page.
- No new dependency; no cross-repo runtime change; no release-gating chain.
- Tradeoff (known-by-design): URLs carry a `#` fragment. This is the standard, fully
  bookmark/PWA-compatible cost of client-only routing over a proxied/static mount.
  Browser-history routing remains a clean future option (the runtime already SPA-falls
  back) but would require a nested-safe `basePath` in the shell and absolute app-level
  fetch URLs; out of scope here.

Discovery — **file-based via Vite `import.meta.glob('./pages/**/*.tsx', { eager: true })`**:
- Adding a page = adding a file under `src/pages/`. No edit to the shell, the router
  engine, or a central route table. Directories create nested path segments; `[param]`
  files create dynamic segments.
- Per-page metadata: optional `export const meta = { title?, order?, nav? }`. Nav is
  derived from static (non-dynamic) pages that opt in; the demo is a starting point the
  agent may restyle, extend, or replace.

Route derivation: `./pages/index.tsx` -> `/`; `./pages/items/index.tsx` -> `/items`;
`./pages/items/[id].tsx` -> `/items/:id`.

## Files

Runtime-owned shell (unchanged, skip-if-exists): `index.html`, `src/main.tsx`,
`api/health.ts`.

Fresh-workspace multi-page demo (written only when `src/App.tsx` does not yet exist, so
existing single-page workspaces stay byte-identical):
- `src/App.tsx` — router host: `ThemeProvider` + `Layout` (nav) + `<RouterView/>`.
- `src/router.tsx` — dependency-free hash-router engine + file-based discovery.
- `src/pages/index.tsx` — Home (overview + how to add a page).
- `src/pages/items/index.tsx` — a list page linking into the nested route.
- `src/pages/items/[id].tsx` — nested dynamic route; deep-linkable/refreshable.
- `src/styles.css` — minimal: keeps `@import "tailwindcss";` + `@import
  "@avibe/show-ui/theme.css";`, plus a small base; demo styling uses Tailwind + show-ui.

## Compatibility

- Existing single-page workspaces: `src/App.tsx` present -> the whole demo set is skipped;
  shell/styles preserved. Renders unchanged.
- The runtime's fallback `ensureSessionTemplate` (single-page) is shadowed by avibe's
  scaffold in the real flow (avibe writes first); no functional dependency on it, so it is
  intentionally out of scope for this PR.

## Verification

- Unit (pytest): scaffold content/structure, fresh-vs-existing gating, shell untouched,
  single-page compatibility, styles imports preserved.
- Build + routing: real Vite build of a scaffolded workspace; route-derivation and matcher
  correctness (static > dynamic precedence, nested, params, 404).
- Real browser (desktop + mobile): nav between routes, nested direct-load via
  `#/items/<id>`, refresh, layout, clean console/network. Local Incus regression for the
  user-facing agent-authored flow if needed.

## System prompt

Update the Show Pages guidance so the shell boundary is explicit and adding a page is the
obvious file-drop pattern — without implying pages must map to topics/history. Keep the
prompt small; the scaffold's executable code is the primary mechanism.
