# Main sidebar resizing — issue #2044

## Goal and status

Let desktop users reveal more of project/session titles by dragging the right edge of the global Workbench sidebar. Issue: https://github.com/avibe-bot/avibe/issues/2044.

Owner approved implementation on 2026-09-19 with one explicit correction at 05:59–06:00 Asia/Shanghai: highlight the divider only, no visible handle or grip. The design gate is cleared for that corrected appearance, and the orchestrator resolved the width numbers against `origin/master` at 8f09a61b0: the shipped 248px is the minimum and twice it is the maximum.

## Behavior contract

- Scope: the leftmost global sidebar owned by `ui/src/components/AppShell.tsx`, shared by Workbench and Settings routes.
- Initial and minimum width are 248 CSS px — the shipped width, so the default shell is unchanged. Maximum is 496 CSS px. Both are fixed: bounds never compound across repeated drags.
- One width value feeds the sidebar, every desktop main-content offset, and the Settings overlay's left edge.
- Resting divider uses the existing border token. Hover, keyboard focus, and active drag change only that thin line to the existing cyan accent token. There is no visible handle, grip, button, dot, or center ornament. A roughly 8px invisible hit area makes the edge easy to grab; show a horizontal resize cursor. Reuse existing token classes.
- Horizontal pointer movement continuously clamps width to the bounds. The line highlight persists throughout the active drag; pointer capture keeps movement and release reliable outside the edge. Release, cancellation, lost capture, unmount, or leaving desktop layout ends the gesture and gives back any borrowed cursor/selection styling. Only the pointer that started a drag can end it, and a second pointer cannot take the edge over.
- Keyboard focus exposes the same affordance. Left/Right adjust width in a small fixed step; Home/End reach the bounds. The vertical separator carries a translated accessible name and current/min/max values.
- Width survives route changes within the mounted app shell. Reload persistence is not required, and one mount's drag must not leak into a later mount or a standalone app tab.
- Mobile navigation, standalone/chromeless apps, and window layering retain their existing behavior.

## Implementation

- `--app-sidebar-w: 248px` joins `--app-shell-h` / `--mobile-nav-clearance` in `ui/src/index.css`. The sidebar, both `<main>` offsets and the Settings overlay read `w-[var(--app-sidebar-w)]` / `md:ml-[var(--app-sidebar-w)]` / `md:left-[var(--app-sidebar-w)]`, so the default renders with no JS and one value moves the whole layout.
- `SidebarResizer` is a leaf inside the `<aside>`, mounted behind the shell's existing `isDesktop`. It holds the number for `aria-valuenow` and writes the custom property on `document.documentElement` — the only channel that reaches the portaled Settings overlay — so a drag re-renders the leaf and never the sidebar tree. Unmount removes the property and restores the body styling.
- Gesture: real `setPointerCapture` with React's own pointer handlers on the strip (capture retargets the gesture there), an end path shared by `pointerup`, `pointercancel` and `lostpointercapture` and keyed to the active pointer id, plus the same end at unmount. Width is always `clamp(startWidth + dx)` from the gesture's own start, so repeated drags cannot compound. No jsdom fallback path: the test stubs pointer capture instead.
- `role="separator"` + `aria-orientation` + `tabIndex=0` + the value trio (APG window splitter), arrows stepping 16px and Home/End reaching the bounds through the same clamp. One new `appShell.resizeSidebar` key in `en.json` + `zh.json`.

Use existing React/CSS and native DOM events. No new dependencies, resizable/split-pane framework, backend settings, generalized layout system, or sibling-panel refactor. Consult the existing EditorApp resize interaction and AppWindow pointer capture patterns without changing those components.

Authorized files: `ui/src/components/AppShell.tsx` + its test, the colocated `SidebarResizer.tsx` + test, the two i18n files, narrowly scoped `ui/src/index.css`, this plan, plus the approved extension — `ui/src/components/settings/SettingsOverlayRouteSurface.tsx` (shared offset, its comment, and its dismissal guard) and the existing hermetic `ui/e2e/workbench-general` suite. Do not modify backend code, dependency files, unrelated UI, or design source files. Report any further scope extension before editing it.

Single implementation lane; no other lane owns these task files. Work only in this isolated worktree. Other sessions have unrelated work in the primary checkout; never change it.

## Known by design

- A newly opened app window still cascades from `x: 264`, a number chosen to clear the 248 sidebar. Windows sit above the sidebar (`z-20` over `z-10`), so under a widened sidebar a fresh window opens *over* it: the window is fully visible and usable, and the sidebar is partly covered until the window is moved. Following the width there would give the window manager a layout dependency it does not have today, so it is recorded rather than changed.
- Dragging the edge while the Settings overlay is open keeps the overlay open by design: the overlay is dismissed by interaction outside itself, and its own left edge is what the drag moves, so the resizer is exempted next to the existing Settings-toggle exemption.

## Validation and delivery

- Focused React tests exercise fixed bounds over repeated gestures, one shared layout width, release/cancel/lost-capture/unmount cleanup including restoring non-empty body styling, pointer identity, keyboard interaction, and desktop-only/standalone exclusion.
- Real browser against the hermetic `workbench-general` fixture proves actual geometry (sidebar, content offset, and the portaled Settings overlay after a drag), line-only hover/focus/active feedback with no ornament, release away from the edge, keyboard adjustment, and a bounded dark/light look at the maximum width. No production credentials, browser profiles, local service, or the stateful Model Hub E2E suite.
- Run changed-file lint, focused tests, and `npm run build` before push. Record evidence and residual acceptance work here and in the PR.
- Read repo AGENTS.md and `.agents/skills/pr-delivery-loop/SKILL.md`; load `impeccable` and `background-watch-hook` as needed. Open a real non-draft PR against master, drive current-head Codex review + all CI + zero unresolved threads, respect circuit-breaker rules, and never merge without owner instruction.
- The owner acceptance environment is https://avibe-cloud-e2e-app.avibe.bot per parent workspace AGENTS.md. Do not deploy a branch there without coordinating its shared use; do not recreate the retired local Incus/Lima environment. No deployment or local service restart is needed for this lane.

## Acceptance in minutes

Open Workbench at desktop width. Hover the sidebar's right edge: only the line turns cyan and the horizontal resize cursor appears. Drag out past 496px, then back past 248px: the fixed bounds hold and the main area follows. Release away from the edge; navigation and text selection work normally. Repeat with Settings open — it stays open and its left edge follows. Tab-focus the separator and adjust it with arrows, Home and End. Confirm mobile navigation and standalone apps still use their usual layout.

## Progress

- [x] Issue filed; baseline and call sites inspected.
- [x] Task worktree created from fetched origin/master (8f09a61b03b7c2066266a05526ff0e9b61fd7b4f).
- [x] Lane preparation: contract checked against source, UI deps installed, baseline shell tests green.
- [x] Mockup approved with owner correction (line highlight only), and the 248/496 bounds plus the overlay/e2e scope extension resolved by the orchestrator.
- [x] Implementation, focused tests (13 + 4 shell cases), lint, build, and real-browser evidence (7 hermetic Chromium cases, dark/light captures at 496) complete.
- [ ] Exact-head bot review, CI, and unresolved-thread gates clear.
- [ ] Owner-authorized integration and acceptance.
