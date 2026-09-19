# Sidebar footer and floating surfaces

## Contract

- Restore the desktop Settings entry to the previous 44px icon-only control,
  with a translated accessible name and tooltip in both open and closed states.
  Apps consumes the remaining row width, including after sidebar resizing.
- The desktop Apps launcher and Dock paint above application windows and the
  route-sized Settings panel, regardless of which surface opened first.
- Activating a Dock tile dismisses Settings through its existing outside-
  interaction behavior; newly opened, focused, or restored windows must receive
  pointer input rather than merely exist in the DOM.
- Version details escape the sidebar stacking context, paint as one opaque
  surface above the launcher, and remain anchored to the version badge.
  Desktop placement follows resizing and stays inside the viewport.
- Preserve the existing mobile version panel placement and touch target.
- Preserve queue behavior, navigation, Dock pinning, version reads, and update
  actions. No backend, dependency, or persistence changes.

## Validation

Use the existing hermetic Workbench browser harness to exercise real Settings,
Dock, and version components together. Assert hit targets at overlapping points,
icon-only Settings width, Apps width, version opacity, and placement after resize.
Run the existing badge browser suite for desktop/mobile interaction, focused shell
unit tests, UI lint, and the production build. The focused sidebar floating-surface
suite also runs in the existing CI browser-interaction job.

The owner's page feedback supersedes the equal-width Apps/Settings footer in
the September 18 Workbench design; all other visual tokens remain unchanged.
