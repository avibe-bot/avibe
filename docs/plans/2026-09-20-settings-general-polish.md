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
- **The shell.** Some shells draw no sidebar at all, so there is nothing to open
  beside and the answer is standalone on a full desktop window too. Without this,
  inline would offset Settings past an empty strip and narrow its rail for a
  neighbour that does not exist.

Either way the answer is standalone, and neither one forgets what the owner
picked for the windows that do have a sidebar.

*Which* shells those are is the shell's own business, so `AppShell` **publishes**
the answer through `ShellSidebarContext` rather than letting each Settings surface
recognise them by pathname. Two shells draw no sidebar and they disagree on what
the answer even depends on: the setup wizard is a matter of the route, while a
single-app tab (`?standalone=1`) turns on a document flag frozen at mount and
deliberately *not* re-read from the URL the shell rewrites underneath it. No
pathname predicate can recover that second one, and the route a Settings surface
lands on says nothing about the shell it opened over anyway — the config-recovery
banner's Diagnostics link opens Settings from a single-app tab onto an ordinary
settings route. So the shell states `shellDrawsSidebar = !chromeless && !setupShell`
beside the two conditions that actually decide whether an `<aside>` renders, and
both consumers read it.

`isStandaloneSettingsMenu(placement, isDesktop, shellHasSidebar)` is the pure rule
and takes no defaultable parameter — a forgotten optional boolean is
indistinguishable from "there is a sidebar" and fails silently. Everything inside
the shell calls the zero-argument `useStandaloneSettingsMenu()`; `AppShell`, being
the publisher, feeds the rule directly.

> Scope note: this publication model replaced a first attempt that re-derived the
> question from `isChromelessShellPath(pathname)` at each call site. Two review
> heads produced findings of one root-cause class — the derivation could only ever
> cover the cases someone remembered to enumerate — which tripped the review-loop
> circuit breaker. The diagnosis was that `AppShell` already owned a concept for
> this (`chromeless`) and the pathname predicate was a second, parallel one
> answering the same question; making the existing owner publish it is the smallest
> complete fix, and it is reversible and contract-preserving.
>
> A fourth head then produced a finding of the *other* class already seen on
> `b1c5c330d6` — "which interactions belong to the live sidebar", answered by DOM
> ancestry, missing everything portaled out of it. Same escalation: rather than
> name one more surface, inline stopped dismissing on outside interaction at all,
> which is what the reachable-surface inventory above says it should never have
> done. Standalone's shipped behaviour is untouched.

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

  It also decides what an interaction *outside* the surface means, and inline
  turns out not to have an outside in the sense that handler assumes. Inline
  covers everything right of the sidebar, so what stays reachable is the sidebar
  column (z-10, left of the surface) and whatever the shell floats above the
  z-30 surface: the Apps launcher and its Dock at z-40, menus and floating
  details at z-50. Every one of those belongs to the live shell and already owns
  what it does — there is no neutral background left to click at. So **inline
  does not dismiss on outside interaction at all**, and is left by Escape, by the
  Settings toggle, or by navigating.

  That is deletion rather than exemption, and deliberately so. The resize edge
  moves this surface's own left edge, so grabbing it must not close what the drag
  is laying out; a sidebar link already navigates, and that navigation *is* the
  way out — letting dismissal fire too would race two navigations
  (`closeSettingsOverlay` traverses history asynchronously while the link pushes
  synchronously) and could land on the retained origin instead of the route that
  was clicked. Naming the exempt surfaces cannot express this, because
  `AppsLauncher` portals itself to `document.body` to clear the route panel's
  stacking context: it belongs to the sidebar without descending from it, and so
  do the menus and details beside it. Any DOM-ancestry test covers only the
  portals someone remembered.

  Standalone keeps the dismissal that shipped, including its single
  `data-settings-toggle` exemption — it does own the whole viewport, so "outside"
  there means what it always did.
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
- `AppShell.test.tsx` — the shell retires only where Settings replaces it, covers
  the shell below `md` even when inline is stored, and publishes a sidebar-free
  shell to the Settings surfaces above it in a single-app tab.
- `SettingsLayout.test.tsx` — rail width per placement, including standalone where
  the shell draws no sidebar while `inline` is stored.
- `SettingsOverlayRouteSurface.test.tsx` — the surface's left edge and border per
  placement, standalone where the shell draws no sidebar, and what an outside
  interaction means: inline dismisses on none of them — the resize edge, the
  sidebar's quiet space, a control *portaled* out of the sidebar, or the rest of
  the shell — while Escape and a sidebar link each still leave, in exactly one
  navigation. Standalone still dismisses on the same portaled control, which is
  the shipped behaviour. The portal case is the one an ancestry test cannot pass.
- `e2e/workbench-general/geometry.spec.ts` — measured in a browser: the 248 rail,
  the card's background matching a neighbouring page's card, and inline actually
  putting a live sidebar beside Settings at the sidebar's own width.
