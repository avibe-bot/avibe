# Sidebar footer and floating surfaces

## Contract

- Restore the desktop Settings entry to the previous 44px icon-only control,
  with a translated accessible name and tooltip in English and Chinese before
  Settings entry and after return. Apps consumes the remaining row width,
  including after sidebar resizing.
- The desktop Apps launcher and Dock paint above application windows while the
  Workbench is foreground. Standalone Settings owns the full viewport; the
  retained sidebar is hidden/inert and launcher/Dock presentation is withdrawn.
- Return through the foreground Settings close control or browser history before
  activating a Dock tile. Pinned Dock state survives the round trip; transient
  hover can reopen after return. Newly opened, focused, or restored windows must
  receive pointer input, and existing windows retain their identity/state.
- This replaces the former route-panel/outside-interaction premise under the
  September 20 integration ruling `99b63fc52`. Preserve the compact footer and
  foreground z40 launcher; do not restore interaction with inactive surfaces.
- Version details escape the sidebar stacking context, paint as one opaque
  surface above the launcher, and remain anchored to the version badge.
  Desktop placement follows resizing and stays inside the viewport.
- Preserve the existing mobile version panel placement and touch target.
- Preserve queue behavior, navigation, Dock pinning, version reads, and update
  actions. No backend, dependency, or persistence changes.

## Validation

Use the existing hermetic Workbench browser harness to exercise real Settings,
Dock, and version components together. Assert hit targets at overlapping points,
icon-only Settings width, Apps width before/after Settings at both sidebar sizes,
full-viewport Settings isolation, retained pin/window identity, version opacity,
and placement after resize. Keep all eight upstream cases, including both
unchanged VersionBadge theme oracles, with traffic/page-error evidence on failure
as well as completion.
Run the existing badge browser suite for desktop/mobile interaction, focused shell
unit tests, UI lint, and the production build. The focused sidebar floating-surface
suite also runs in the existing CI browser-interaction job.

The owner's page feedback supersedes the equal-width Apps/Settings footer in
the September 18 Workbench design; all other visual tokens remain unchanged.
