# Vibey macOS tray icon

## Change contract

Use the first preview approved by Max on 2026-10-16, without redrawing its
silhouette, eyes, or status marks. The second preview was rejected.

- Serving: solid cloud with transparent eyes.
- Starting and stopping: the same cloud with three transparent dots.
- Unreachable: the same cloud with a transparent exclamation mark.
- Stopped: the original outline cloud.

The macOS status item is a monochrome template at creation and after every
status refresh. Menu text, lifecycle behavior, desktop pet, Dock/application
icons, and non-macOS tray rendering keep their existing behavior.

## Assets and rendering

`desktop/src-tauri/icons/tray/` contains the approved 18px and 36px PNGs,
encoded as RGBA for Tauri with identical decoded pixels.
Tauri embeds the 36px images at compile time; the pinned tray implementation
sets their logical height to 18pt. macOS therefore has full Retina detail and
scales the same image on 1x screens. The 18px exports preserve the approved
1x artwork for inspection. There is no new runtime decoder or dependency.

Use `set_icon_with_as_template` for refreshes: the pinned macOS implementation
of plain `set_icon` resets the template flag.

## Validation

- Compare all eight decoded RGBA buffers byte-for-byte with the approved preview.
- Extend the existing tray test boundary to validate dimensions, monochrome
  alpha, distinct status silhouettes, and the shared starting/stopping image.
- Build the desktop bootstrap UI; run formatting, focused Rust tests and lint.
- Exercise native template creation and refresh in an isolated tray-only
  preview if the local desktop permits capture. It must not start a Runtime,
  register product plugins, or modify the installed Avibe application.
- Require exact-head Codex review, no unresolved threads and passing CI.
  Do not request a review through a comment, merge, or release.

## Local evidence

All eight decoded RGBA buffers match the selected V1 preview exactly. Desktop
bootstrap and Workbench UI builds, Rust formatting, and all-target/all-feature
workspace Clippy passed. The focused tray tests passed.
A temporary tray-only Tauri program displayed all four images in the real macOS
menu bar. After `set_icon_with_as_template`, every native image reported
`isTemplate() == true` and an 18x18pt size. A cropped menu-bar screenshot was
captured; the probe was then stopped and removed from the worktree. This proves
native image rendering and replacement, not the full packaged Runtime lifecycle.
