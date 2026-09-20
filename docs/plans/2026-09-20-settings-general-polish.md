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
>
> A fifth head put that class on three heads, now claiming a transparent
> `z-20` backdrop swallows every sidebar interaction. That one is not true:
> Radix returns `null` from `DialogOverlay` unless the dialog is modal, and the
> surface's only caller is non-modal, so the backdrop never reached the DOM. The
> class's real root cause is what makes three reviewers keep finding new
> mechanisms for one violation: *"inline leaves the shell live"* was stated only
> as prose and class names, and the primitive carried dead markup that reads
> exactly like the violation. So the contract is now stated where covering is a
> real concept — a browser hit test — and the dead backdrop is deleted rather
> than explained.
>
> The same head carried a *fourth* instance of the class that is true, and it
> corrects the rule rather than the code: `WindowLayer` really is `fixed inset-0`
> at `z-20`, above the sidebar's `z-10`, deliberately, so a window can be dragged
> over the sidebar and maximize can fill the screen. Under inline Settings that
> layer stays live behind an opaque `z-30` surface, so an open window shows as a
> strip over the one column inline exists to keep, with its title bar, controls
> and content all hidden. So the rule is not "inline leaves the shell live" but
> **inline keeps the sidebar column live; whatever the opaque surface covers
> retires in both placements** — one consumer, `WindowLayer`. Everything else is
> either inside the sidebar column or floats above the surface, so it keeps
> `settingsCoversSidebar`.
>
> (As merged this also took the `AppsLauncher` with the layer, on the grounds
> that a control whose every result is hidden is not a live control. That was
> wrong in the user-visible direction — see *Follow-up: the launcher is a sidebar
> control* at the end.)

### What each consumer does with it

- **`AppShell`** previously used one flag, `settingsOpen`, for two different
  questions. It now distinguishes *Settings is the foreground route* (the
  sidebar toggle's label, still `settingsOpen`) from *Settings took over the
  shell* (`settingsCoversSidebar`). Everything in or above the sidebar column
  reads the latter: the `aside`'s `inert`/`aria-hidden`/visibility, the
  `WorkbenchSidebar` route-surface boundary, the content column's left offset,
  the mobile dock, `NewSessionSheet`, `SearchPalette` and the ⌘K guard. Inline
  Settings therefore leaves a live, navigable sidebar — which is exactly what
  shipped before #2051 (verified against `66fc91db^`).

  The one exception reads `settingsOpen`, and it is the reason both flags exist:
  `WindowLayer` spans the whole viewport *above* the sidebar so windows can be
  dragged over it, so inline's opaque surface would leave it live-but-invisible
  rather than live. It retires under either placement and comes back when
  Settings closes. `AppsLauncher` does *not* go with it — see the follow-up.
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

### Incidental fixes

The first two are in `DialogSurfaceContent`, and are the same shape: markup that
looked authoritative and was in fact dead.

- The default offset was a literal `md:left-[240px]` — dead (its only caller
  overrode it) and wrong (the sidebar has been 248/variable for a while). It is
  now `md:left-[var(--app-sidebar-w)]`, which is what lets the inline caller
  need no override at all.
- It also declared a transparent full-viewport backdrop at `z-20`, above the
  app sidebar's `z-10`, with a comment crediting it for focus and accessibility
  isolation. None of that was happening: Radix's `DialogOverlay` returns `null`
  unless the dialog is modal, and this surface's one caller is deliberately
  non-modal precisely so the shell behind stays live. The element never
  existed at runtime — but on the page it reads as a layer over a live sidebar,
  which is how it produced a P2. Deleted; a caller that wants a real backdrop
  wants `DialogContent`.

The third is the sidebar's own maximum width, and it predates this PR:
`MAX_SIDEBAR_WIDTH = 496` is viewport-blind, so a full drag in a 768px window
already leaves a 272px workbench today. Inline Settings spends 196 of that on
its rail, which is what made it visible. The fix belongs to the sidebar, not to
its consumers — each of them would otherwise re-derive the same budget, and
capping only while Settings is open would move the column the moment Settings
opened. So the maximum now reserves `MIN_WIDTH_BESIDE_SIDEBAR` (768 − 248 = 520,
exactly what a default sidebar already leaves at `md`, so no shipped
configuration changes), and follows the viewport as well as the gesture:
narrowing the window after a wide drag reaches the same starved layout, just
later. An untouched shell still publishes no inline width at all.

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
  shell to the Settings surfaces above it in a single-app tab. Plus the two
  exception: the window layer retires under *either* placement and comes back
  when Settings closes, while the launcher retires only under standalone. The
  `AppsLauncher` stub honours its route-surface boundary the way the real one
  does, so it cannot report a live Apps button in a state the shell retires it.
- `SidebarResizer.test.tsx` — the width budget as a pure function across five
  viewports, a drag and an `End` press both stopping at the affordable maximum
  rather than the constant one, a window narrowed *after* a wide drag giving the
  width back, and an untouched shell still publishing no inline width when the
  budget moves. Verified non-vacuous: disabling the re-clamp fails the narrowing
  test (it also caught a real bug while being written — the first re-clamp read
  an already-clamped width, so it could never see one that no longer fit).
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
  putting a live sidebar beside Settings at the sidebar's own width. It also
  asks the browser what is under the pointer over that sidebar's Settings
  toggle, which is the only place "nothing covers the live shell" can be
  settled — a class name, a bounding box and jsdom all miss a transparent
  layer. Verified non-vacuous: injecting a real `fixed inset-0 z-20` div into
  the surface fails it.

## Follow-up: the launcher is a sidebar control

Reported after merge: *switching to the inline menu makes the Apps button at the
bottom of the sidebar disappear.* It does, and the cause is the paragraph above —
the launcher was retired together with the window layer it opens into.

The reasoning behind that ("a control whose every result is hidden is not a live
control") is sound about the *layer* and wrong about the *control*. Apps is a
sidebar control, sitting in the same bottom row as the Settings toggle and the
service status, and inline's whole claim is that the sidebar column stays live.
A column that keeps every button except one has no rule the user can see; it just
looks like something broke. So the launcher goes back to `settingsCoversSidebar`,
like every other control in that column.

What made retiring it look necessary is real, though: the window layer is hidden
while Settings is open, so a window opened from there would arrive invisible.
That is answered the way the sidebar already answers it for its links — **bringing
a window forward is a way out of Settings**. Clicking Inbox leaves Settings by
going to Inbox; opening Files leaves Settings by opening Files. The Settings
toggle beside it uses the same exit.

Where to put that is the only real design question. Not in `AppsLauncher`: the
Dock, a deep link, a restored window and a window focusing itself all reach the
same layer, and each would have to ask what happens to be covering it. So
`WindowManagerProvider` takes an `onWindowForeground` callback and calls it from
the only two places a window can reach the top — `focus` and `openApp`, with
`restore` funnelling through `focus` — and `AppShell` supplies the exit. The
callback is held in a `useLatestRef` so `focus`/`openApp` keep their identity;
they are part of the context value every window consumes.

The second report — *clicking blank space in the app sidebar while the inline
menu is open also dismisses it* — does **not** reproduce on `b45fbc604`. Swept in
a browser with `document.elementFromPoint` at two x positions across sixteen y
positions of the sidebar: every point that closed Settings had a real link under
it (the brand row's `a[href="/"]`, the tree's `a[href="/inbox"]`) and navigated
there, which is the designed exit; every genuinely blank point — the aside
itself, the tree's scroll container, the bottom row, the whole right-hand column —
kept it open. The brand link is content-sized (measured 159.5px wide against a
248px rail), so the apparent blank to its right is not part of it. Most likely a
build predating `4f2e34ab1`, where inline still dismissed on any outside
interaction; that build would also show the missing Apps button.

### Validation

- `WindowManagerProvider.test.tsx` — every way a window reaches the top announces
  itself (`openApp`, `focus`, and `restore` through `focus`), the ways down do not
  (`focusCanvas`, `minimize`, `close`), and a provider with no listener still
  works.
- `AppShell.test.tsx` — the launcher retires under standalone and stays under
  inline; a foreground announcement while Settings is open returns to the origin
  route and brings the window layer back; one outside Settings changes nothing.
  Verified non-vacuous: restoring the old boundary fails the first, dropping the
  prop fails the second, dropping the `settingsOpen` guard fails the third.
- `e2e/workbench-general/geometry.spec.ts` — in a browser: Apps is gone under
  standalone, back under inline, and the pointer actually lands on it rather than
  on the surface above it; opening a Dock tile from there closes Settings and
  shows the window.

### Review round: three consequences of the new exit

Codex review of `9bf337e2` found three, all real, all the same shape — a
foreground announcement crossing from the window manager into a surface that
does not own it. One announcement per window meets one exit per gesture; a
close meets a focus decision; and a lazily-loaded route meets a surface that has
already been retired. Keeping the announcement at the manager stays right — the
Dock, ⌘K search, a deep link and a restored window all reach the same layer, and
none of them should have to ask what is covering it — so each consequence is
answered where it lands.

**One gesture, one exit.** The Dock's "Show all windows" restores every
minimized window in a loop, so N announcements arrive in one synchronous batch,
before React can re-render with Settings closed. Announcing stays per window and
truthful; *leaving* becomes per gesture. `AppShell` holds a latch cleared after
every commit, so it covers exactly the batch it is for. Without it the exit's
history traversal runs N times and lands N-1 entries before the origin — on a
route the user never asked to see.

**The window keeps the focus it took.** Settings hands focus back to the control
that opened it. Not on this exit: the window that caused it has already claimed
DOM focus, and the window chords (⌘W / ⌘M) resolve their target from
`document.activeElement`, so restoring the sidebar toggle would leave that window
on screen and deaf, with ⌘W falling through to the browser's close-tab. Only the
shell knows the close had a cause and only the surface owns the focus decision,
so the shell publishes a one-shot `SettingsFocusHandoffContext` ref and the
surface spends it in Radix's deferred close callback. A ref, not state: nothing
renders differently for it, and the callback runs after the render that closed
the surface. The shell clears it on any commit that still shows Settings — every
commit except the one this exit produces — so a flag raised for a close that then
did not happen cannot eat the next, ordinary close's return focus.

**A retired route does not launch.** `RouteSurfaceActivityBoundary` scopes
navigation, not the window manager, so `/apps/show/:sessionId` resolving its lazy
chunk after Settings took the foreground would bring a window forward behind an
opaque overlay — and half-handle itself, since the boundary refuses the redirect
while the route's once-only latch is already spent. Both halves of that handoff
are foreground gestures, so the whole effect waits on `useRouteSurfaceActive()`
and runs whole when the surface is live again.

### Validation (review round)

- `AppShell.test.tsx` — three announcements in one batch land on the origin, not
  two entries past it; the window exit raises the handoff flag and the next
  visit's ordinary close does not see it. Non-vacuous: dropping the latch lands
  on `session-1`, dropping the raise reads `false`, dropping the clear reads
  `true` on the second close.
- `SettingsOverlayRouteSurface.test.tsx` — a retained app window keeps DOM focus
  when it is what closed Settings, against the neighbouring rule that the same
  window loses it on an ordinary close. Non-vacuous: dropping the early return
  moves focus to the toggle.
- `ShowPageRoute.test.tsx` — a retired surface neither opens the window nor
  redirects, and still owes both once it is live. Non-vacuous: dropping the gate
  opens the window while retired.
- `e2e/workbench-general/geometry.spec.ts` — in a browser, opening a Dock tile
  beside inline Settings leaves focus inside the window. Non-vacuous: dropping
  the early return fails it.

## Review round 2: the exit cannot be refused

The round-2 finding asked the window-foreground exit to carry an authorization
verdict: a dirty `/apps/editor` retained behind Settings would put the
router-wide unsaved-changes blocker in front of `closeSettingsOverlay()`, and
cancelling that prompt would leave the window appended and focused behind a
Settings surface that never closed — half of an action the user declined.

The premise does not hold, and the reason is the same boundary that answered the
round-1 `ShowPageRoute` finding. `useUnsavedChanges` reads
`useRouteSurfaceActive()` and registers `null` while its surface is inactive, so
the route retained behind Settings withdraws its registration for the whole
visit. `shouldBlock` then sees an empty registry and lets the close through:
there is no prompt, no verdict to propagate, and no half-applied state to guard
against.

That is the right answer rather than a lucky one. Closing the overlay returns to
the very route the overlay was opened over, which stayed mounted the entire time
with its draft intact — nothing is discarded, so a "Discard unsaved changes?"
prompt would be false in both of its branches. Adding `authorizeRouteAction()`
here would be a no-op today (it reads the same empty registry) and a lie the day
the withdrawal changed.

Both round-1 and round-2 findings therefore share one class — what an inactive
route surface may still do — and that class already has a single owner in
`RouteSurfaceActivityBoundary`. Round 1 brought `ShowPageRoute` under it; the
unsaved-changes registry was already there.

`UnsavedChangesProvider.test.tsx` pins the guarantee at the level of the
behaviour rather than the mechanism: leaving the overlay while a dirty route is
retained prompts nothing and closes Settings, and the same harness still prompts
when a navigation really does leave that route. Non-vacuous both ways —
dropping the `routeSurfaceActive` gate reproduces exactly the prompt the finding
described.

### Out of scope, found while diagnosing

Navigating from an open Settings surface to a *different* route unmounts the
retained dirty route without prompting, because its registration is withdrawn
for the duration of the visit. That is pre-existing behaviour in the
unsaved-changes feature, independent of the Settings menu placement work, and it
wants its own change: the withdrawal should cover transitions that return to the
origin, not every transition made while the overlay is open.

## Review round 3: the announcement belongs to the caller

Round 3 found two more launch paths with the shape round 1 fixed in
`ShowPageRoute`: `ShowPageLaunchControl.openWindow` awaits `prepare()` before
`openApp`, and `ChatPage.openLocalFile` awaits `fileMeta`. Both components stay
mounted behind the Settings overlay, so a user who opens Settings mid-await gets
Settings thrown away by a launch they had already moved on from. Both premises
check out.

Three findings-bearing heads, all one class, so the circuit breaker applies and
the diagnosis moves up a level. The class is mine: `onWindowForeground` is new
in this PR, and the mistake was where it was wired. Putting the announcement
inside the provider's `focus` and `openApp` gave every caller an obligation none
of them knows about — "clear whatever covers the window layer" — and left each
one to discover for itself that the obligation has a condition. Patching two
more call sites would have bought the same bug a fourth head.

The announcement is the one thing the window manager does on someone else's
behalf, so it is the one thing that depends on who is asking. It moves to
`useWindowManager`, the single consumer boundary, where `useRouteSurfaceActive()`
already answers "is this caller still what the user is looking at". The provider
exposes `announceForeground` and calls it from nothing; the hook calls it ahead
of `openApp`, `focus`, `restore` and `toggleMaximize`. Every present and future
caller inherits the gate, and none of them has to know it exists.

The gate reads its answer when the method is *called*, not when the closure was
made — through `useLatestRef`, not the memo's dependencies. That is the whole
point for the suspended launches above: their closure was created while the
surface was live and only the call is late, so a render-time check would still
let the stale continuation announce.

Rejected: gating each awaited call site (the shape that already needed three
rounds to enumerate), moving the exit up to shell chrome (it would stop the Dock
from working in inline mode, which is the bug this PR opened with), and teaching
`RouteSurfaceActivityBoundary` about windows (it would couple routing to the
window manager to serve one caller).

`LibraryRoute` gets the `surfaceActive` gate that `ShowPageRoute` got in round
1 — same latch, same half-done handoff, and it was simply missed.
`toggleMaximize` reaches the top through `focus`, so it announced before this
refactor; it is wrapped too, rather than silently losing the behaviour.

`WindowManagerProvider.test.tsx` pins both halves: a retired caller still gets
its window opened and focused but announces nothing, and a reference captured
while the surface was live announces by whether the surface is live at the call.
Non-vacuous — removing the gate fails both.

### Correction to this PR's own validation

`npx tsc --noEmit` in `ui/` checks nothing: `ui/tsconfig.json` is a solution file
with `"files": []` and only project references. The real typecheck is `tsc -b`,
which is what `npm run build` runs — and it caught a nullability error in the
first version of the hook that the vacuous command had reported clean.

## Review round 4: a command route is not a place

Fourth findings-bearing head, same class, so the breaker applies again and the
diagnosis goes up another level. This time it reaches something I wrote in
round 1 and then reasoned from three times.

Round 1 claimed that `RouteSurfaceActivityBoundary` "already refuses the
redirect" from a retired surface, and deferred `ShowPageRoute`'s whole handoff
on that basis. The boundary does no such thing. It swallows `go` and `push`,
but `replace` is *diverted* to `inactiveReplace`, and
`SettingsOverlayRouteSurface` always supplies one. The claim came from the
round-1 test itself, which rendered the boundary without that prop and so
manufactured the premise it went on to verify. The round-3 note repeated it and
extended the same gate to `LibraryRoute`.

With the real boundary in view, the deferral is the defect Codex reported: a
command that waits is a command that fires later, once the user has moved on.
Both gates are removed. `ShowPageRoute` and `LibraryRoute` run when they are
asked, retired or not, and neither half reaches over the foreground — the
window manager withholds the announcement (round 3) and the boundary routes the
redirect to the surface. `LibraryRoute` is not lazily loaded, so its gate was
unreachable on top of being wrong.

Removing the deferral is necessary and not sufficient. The exit had the same
bug by a second road:

`replaceBackground` rewrote the retained origin's *location* and kept its
recorded `historyIndex`. That index names the history entry the origin was read
from, and that entry still holds the pre-rewrite url. `closeSettingsOverlay`
prefers a history pop whenever the index is usable, so leaving Settings landed
back on the command url, mounted the command a second time, and raised its
window over the app the user had just picked from the launcher — Codex's
scenario exactly, reproduced with the deferral already gone. The rewrite now
clears the index, so the exit replaces forward onto where the origin actually
is.

Unconditional, including a rewrite that only carries new state. Two rules would
have let the same surface answer differently for a url change and a state
change, and the state case is not benign either: the existing
"maintains only the retained origin" test asserts the rewrite survives the
exit, which was true in jsdom only because a memory router leaves
`history.state` null. One rule makes that assertion true in a browser too.

Rejected: Codex's proposed "a window-caused Settings exit supersedes pending
handoffs", which adds a concept to carry the deferral rather than removing it;
and abandoning a handoff that cannot run, which silently drops a navigation the
user asked for.

Evidence. `ShowPageRoute.test.tsx`'s handoff describe is rewritten around a real
`inactiveReplace` — the prop whose absence caused this. Two surface tests pin
the exit against a seeded `history.state.idx`, and both fail without the index
fix. `C-SETTINGS-10` plays the reported gesture end to end in a browser, where
the history stack is real: hold the lazy chunk, open inline Settings, release
it, pick Files from the launcher, and require Files to stay in front. It fails
against either half of the fix reverted on its own.

## Review round 5: who put the entry there

Round 4 traded one misfire for a quieter one, and Codex caught the trade. With
the index cleared, the exit replaces the Settings entry going forward, so the
entry the command was pushed into survives underneath. Press Back and the
command url mounts again and raises its window.

Verified in a browser rather than argued: after the launcher exit the stack is
`[/apps/show/ses-show, /]` at index 1, and Back lands on index 0, focuses the
existing Show Page window — two windows, not three — and replaces to `/`.

No API erases a history entry you are not standing on, and every way of
standing on this one runs the command to get there. So the question is not how
to erase it but whether it should be erased, and that turns on who put it
there. On desktop nothing in the product navigates to `/apps/show/:id`: the
Dock calls `wm.openApp` directly, the App Library and app search both branch to
the window on a desktop viewport, and the only caller that routes is the mobile
Dock drawer — a surface with neither this Settings placement nor this launcher.
A desktop session holds that entry only because the user opened that url.

Back returning them to the page that url names, raising its window, is what the
url means; a fresh visit to it does the same. The alternative Codex asks for —
removing the spent command from history — would send Back out of the app
instead. The two halves are one rule: an exit the user did not aim at the Show
Page must not raise it, and a Back they did aim there must. `C-SETTINGS-10`
now carries both, which is the coverage gap the finding named.

## Review round 6: the gate already reaches window bodies

Codex read the announcement gate as covering retained routes only, on the
premise that `WindowLayer` sits outside any `RouteSurfaceActivityBoundary`. It
does not: the layer renders one around its windows, fed by the same `active`
prop the shell hides it with, and `useWindowManager` reads it from there. So a
window body is retired exactly when the layer is, and the awaited
`AppsPreviewPage.openInEditor` and `AppsFileBrowserPage` content-hit paths it
named are already covered — the suspended-closure case is the one the gate was
written for.

The premise was reachable, though, because nothing pinned the wiring end to
end. The gate's tests drive `RouteSurfaceActiveContext` directly, and the
layer's boundary has no test at all, so both halves read as plausible while
neither states that window bodies inherit the answer.

`windowLayerForeground.test.tsx` now renders the real layer under the real
provider, captures the manager from a window body the way an await holds it,
retires the layer, and requires the late `openApp` to open its window without
announcing — then to announce again once the layer is live. Both fail if the
layer stops passing `active` into its boundary.
