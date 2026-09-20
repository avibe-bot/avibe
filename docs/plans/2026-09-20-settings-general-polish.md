# Settings: unified cards, a menu-placement choice, and one left column

## Background

Three things about the Settings surface were inconsistent enough to notice.

1. **The General page's cards are the only filled ones.** `SettingsPanel`'s
   `preference` variant rendered `bg-surface-2`, so Appearance sat on a lighter
   board while every other settings page draws its cards as an outline on the
   page background. The design source (`design.pen`, frames `le5QU` / `Q8zxF1`)
   does specify surface-2 there, but it specifies one page in isolation; the
   product reads them as a set.

2. **Settings has exactly one layout and no way to ask for the other.** Since
   PR #2051 Settings is *standalone*: it takes the whole window and its rail
   replaces the app sidebar. The older *inline* layout — Settings opening beside
   a sidebar that stays visible and live — is a real preference, and the shell
   still has all the machinery for it.

3. **The two left columns are different widths.** The app sidebar is 248px
   (draggable, published as `--app-sidebar-w`); the Settings rail was a literal
   196px. Because standalone Settings replaces the sidebar, opening Settings
   moved the left column 52px and moved it back on close.

## Goal

- General's cards look like every other settings page's cards.
- The owner can choose whether Settings takes over the window or opens beside
  the app sidebar, from a control drawn like the theme selector next to it.
- Opening standalone Settings never shifts the left column.

## Where the preference lives: localStorage, not the database

Chosen: `localStorage`, key `avibe.settings.menu-placement.v1`.

- It is a per-person, per-device *view* preference, the same tier as theme —
  which already lives in `localStorage` under `vibe-remote-theme`.
- There is no per-user preference store on the server. `UiConfig` is
  instance-wide; `language` lives there precisely *because* it is owner-managed.
  Copying that path would let one member re-lay out every other member's window.
- A preferences table plus an API, a role projection and cache invalidation is a
  whole new concept to carry one binary choice — and `config/v2_config.py` is a
  shipped on-disk surface, so the schema would be permanent.
- The house pattern already exists (`actionShortcuts.ts`, `agentsViewMemory.ts`):
  `useSyncExternalStore` + a versioned key + a custom event + an injectable
  `Storage` for tests.

Accepted cost: no cross-device sync, and clearing browser data resets it —
identical to theme, and reversible later if a real preference store ever exists.

## Solution

### One derived question, answered once

Three trees need the same answer and would otherwise each re-derive it.
`ui/src/lib/settingsMenuPlacement.ts` exports the store *and*
`useStandaloneSettingsMenu()`, which owns the whole derivation.

`inline` is a claim about something *else* being on screen, so the stored
preference only means anything where an app sidebar is actually there to sit
beside. Two things take that away, and the hook folds in both:

- **The viewport.** Below `md` there is no room for two rails, so Settings
  covers the shell.
- **The shell.** The setup wizard owns the whole window and draws no sidebar at
  all, so a caller passes `shellHasSidebar: false` and gets standalone on a full
  desktop window too. Without it, inline would offset Settings past an empty
  strip and narrow its rail for a neighbour that does not exist.

Either way the answer is standalone, and neither one forgets what the owner
picked for the windows that do have a sidebar. That `/setup` is the chromeless
shell is stated once, as `isChromelessShellPath` in `settingsOverlay.ts`, because
three separate decisions turn on it: whether `AppShell` draws its chrome, and —
for a Settings surface opened from there — the overlay's own offset and its
rail's width.

### What each consumer does with it

- **`AppShell`** previously used one flag, `settingsOpen`, for two different
  questions. It now distinguishes *Settings is the foreground route* (the
  sidebar toggle's label, still `settingsOpen`) from *Settings took over the
  shell* (`settingsCoversSidebar`). Everything **behind** Settings reads the
  latter: the `aside`'s `inert`/`aria-hidden`/visibility, the `WorkbenchSidebar`
  and `AppsLauncher` route-surface boundaries, the content column's left offset,
  the mobile dock, `NewSessionSheet`, `SearchPalette`, the ⌘K guard, and
  `WindowLayer`. Inline Settings therefore leaves a fully live shell — which is
  exactly what shipped before #2051 (verified against `66fc91db^`).
- **`SettingsOverlayRouteSurface`** picks its own left edge: standalone starts at
  the screen edge with no left border; inline takes the primitive's
  `--app-sidebar-w` offset and a border, so the two edges stay together while the
  sidebar is dragged. It publishes `data-settings-menu-placement` for tests.

  It also decides what an interaction *outside* the surface means — and once
  inline leaves the sidebar live, the sidebar is not "outside" at all. It is the
  surface this one sits beside, and its affordances already own what they do, so
  the whole column (marked `data-app-sidebar`) is exempt from dismissal rather
  than the one resize handle that was exempt before. The resize edge moves this
  surface's own left edge, so grabbing it must not close what the drag is laying
  out; a sidebar link already navigates, and that navigation *is* the way out of
  Settings. Letting dismissal fire too would race two navigations —
  `closeSettingsOverlay` traverses history asynchronously while the link pushes
  synchronously — and could land on the retained origin instead of the route
  that was clicked. One navigation, chosen by the sidebar.
- **`SettingsLayout`**'s rail is `var(--app-sidebar-w)` when standalone (so it
  tracks even a dragged sidebar) and stays 196px inline, where spending a second
  full-width column on a secondary nav would cost 496px of left chrome.

### Incidental fix

`DialogSurfaceContent`'s default offset was a literal `md:left-[240px]` — both
dead (its only caller overrode it) and wrong (the sidebar has been 248/variable
for a while). It is now `md:left-[var(--app-sidebar-w)]`, which is what lets the
inline caller need no override at all.

### The card

`ChoiceCards<T extends string>` was extracted from the existing appearance
radiogroup rather than copied: same DOM, same roles, same keyboard handling, now
serving both the theme miniatures and the two placement miniatures (a 2-column
sketch for standalone, a 3-column one for inline). The placement card is hidden
below `md` — a control that changes nothing on the viewport you are holding is
not a choice.

## Deliberate divergence

`design.pen` `le5QU` / `Q8zxF1` fill the preference card with `--surface-2`.
The card now carries `--background`. Consistency with every neighbouring
settings page was judged worth more than fidelity to one frame; radius (12),
padding (22) and gap (20) still follow the source exactly, which is why
`preference` remains its own variant. Recorded here and in the PR rather than
edited into `design.pen`.

## Validation

- `settingsMenuPlacement.test.tsx` — default, persistence, refused storage,
  same-tab propagation across React trees, cross-tab `storage` events, and both
  fold-ins: the viewport, and a shell that draws no sidebar.
- `SettingsGeneralPage.test.tsx` — the placement radiogroup is its own group,
  arrowed and drawn like the theme one beside it, and does not touch the theme key.
- `AppShell.test.tsx` — the shell retires only where Settings replaces it, and
  covers the shell below `md` even when inline is stored.
- `SettingsLayout.test.tsx` — rail width per placement, including standalone from
  a setup origin while `inline` is stored.
- `SettingsOverlayRouteSurface.test.tsx` — the surface's left edge and border per
  placement, standalone from a setup origin, and what an outside interaction
  means: a live sidebar keeps its own affordances (the resize edge lays this
  surface out, a sidebar link navigates and lands on the route it names), while
  the rest of the shell is still a way out.
- `e2e/workbench-general/geometry.spec.ts` — measured in a browser: the 248 rail,
  the card's background matching a neighbouring page's card, and inline actually
  putting a live sidebar beside Settings at the sidebar's own width.
