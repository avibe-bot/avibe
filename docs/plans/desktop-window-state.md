# Desktop window state persistence

## Status

Frozen implementation contract for gap G6. Companion to
`desktop-product-gaps.md` (G6) and `tauri-desktop-vertical-slice.md`.

Owner: Avibe core
Date: 2026-09-10
Baseline: `desktop` branch after #1975. The main window is still a
single Tauri window labeled `main` (`tauri.conf.json`: 1200×800,
min 880×600, centered).

## Product outcome

The Avibe window reopens at the size and position the user last left
it, including after a Runtime adopt/start cycle. Maximized is
restored as maximized. Full-screen / Spaces assignment is **not**
restored (macOS Spaces restore is a common way to lose the window).

If the saved frame would land off the current display configuration
(external display unplugged), the window is clamped onto a visible
display and kept at or above `minWidth` × `minHeight`. A missing or
corrupt save falls back to the current 1200×800 centered default.

## Frozen mechanism

`tauri-plugin-window-state` (Tauri v2), keyed by window label.

What is persisted, per label:

- `x`, `y`, `width`, `height`
- `maximized` (bool)

What is not persisted:

- full-screen / simple-fullscreen;
- workspace / Spaces / virtual-desktop membership;
- monitor identity (clamp-to-visible is the recovery, not a saved
  display id);
- Workbench scroll / SPA route (that is product UI state, not the
  shell frame).

The plugin hooks the **native window frame**, not the WebView
document. Bootstrap content swapping and the later navigation to the
Runtime origin do not reset the frame. Order of operations at
launch, frozen:

1. Create the `main` window (hidden or shown — plugin-dependent,
   but the restored frame is applied **before** the user sees it).
2. Existing bootstrap state machine runs (content changes; frame
   does not).
3. On successful `/ready`, navigate the WebView to the Runtime
   origin (content changes; frame does not).
4. User moves/resizes: the shell debounces calls to the plugin's
   existing `save_window_state` (the plugin writes on demand / on
   close, it does not debounce to disk by itself). Do not introduce a
   second store. Debounce is a shell-side timer around that API.

Closing to tray and re-showing ("Open Avibe") must restore the
**last shown frame**, not re-center. Quit and relaunch reads the
same store.

## Multi-window (G8) without a contract change

The plugin already namespaces by label. G8 adding `settings` or
`show-<id>` windows gets a frame per label for free, provided each
new window has a stable label. This contract does not require G8;
G8 must not invent a second persistence path.

v1 of this contract only guarantees the `main` label. Additional
labels become guaranteed when those windows exist.

## Tests (invariants)

- Saved frame round-trip: a `main` window at a non-default size and
  position is restored on the next launch of the same app data
  directory.
- Off-screen clamp: a saved origin that lies entirely outside the
  current display bounds is moved so at least the title bar is
  visible; size stays ≥ minWidth × minHeight.
- Corrupt / missing store → 1200×800 centered, no panic, no
  bootstrap failure.
- Bootstrap then navigate does not overwrite a restored frame with
  the config.json defaults.
- `shell_boundaries.rs` still forbids remote capabilities; the
  plugin is not WebView-callable as a command the Workbench can
  invoke to move the window (if the plugin exposes commands, they
  stay off the Workbench capability).

Prefer a focused integration test over mocking the plugin: launch
the shell against a fake Runtime in a throwaway config dir, set
frame, quit, relaunch, assert. If that is too heavy for CI, a unit
test of the clamp function plus a source assertion that
`tauri-plugin-window-state` is registered for `main` is the
minimum; the clamp tests must not enumerate display sizes — they
state the "title bar visible, size ≥ min" property.

## Explicit non-goals (v1)

- Remembering the Workbench route or scroll.
- Remembering full-screen.
- Per-display docking layouts.
- Syncing frame state across machines.

## Sequencing

No dependency on G1, G4, G5, or G8. Cheapest desktop-feel win;
safe to ship before deep links. If G8 is scheduled, doing G6 first
means the new windows inherit persistence instead of growing a
second store.
