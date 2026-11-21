# Native Show window controls

## Change contract

Implement the approved `show-pages/desktop-native-controls/group-*` design from
`design/desktop-native-window-controls-20261121` at `16b0211068`, rebased onto
master `2988c4b041` (#4037). The macOS standalone Show window owns a single native
title-bar group: Annotate, Open Chat, Share, Open in Browser. Its SF symbols are
14 pt, hover wash 20 pt centered in a 28 pt hit target, gaps 4 pt and trailing
inset 9 pt. The accessory occupies 137 pt including its leading spacer.

Workbench AppWindow controls retain their behavior and wording. No packaging,
installation, production service changes or backend API changes are included.

## Behavior and ownership

- Native buttons dispatch annotation/share events only on the Show route and
  origin at which their window was opened. These open the existing compact
  annotation picker and share popover through invisible anchors. Anchors mirror
  the native group and end at the content's top edge. Only one picker is open at
  a time. A shield lets an outside click dismiss it instead of entering the Show.
- Annotation uses the window's existing `ShowPageAnnotationHost` bridge. Opening
  enables the remembered mode; selecting a mode and turning annotation off use
  the existing callbacks. Workbench callers retain the default visible trigger.
- Native chat/browser actions derive the session and Runtime from the registered
  App window, checking that the Runtime receipt and window are still current.
  Open Chat sends `avibe:desktop-show-chat` with that session to the main Runtime
  document. Its router opens `/chat/<session>?view=chat`; the shell raises main.
  A full document navigation would retire every App window, so this path must
  remain an in-document navigation. It has no full-page fallback on older UIs.
- Open in Browser opens the private `/show/<session>/` URL on the same Runtime.
  Its label uses `apps.window.openInBrowser`; Workbench's new-tab label is unchanged.
- The macOS 26 title keeps #4037's native truncation/hiding rule, with the wider
  accessory as its boundary. Earlier macOS versions keep their system title.

## Design record

The chosen group frames are `approved`. They cannot honestly be `shipped`:
AppKit's title bar is represented by a web stand-in, as in #4037; its annotation
and sharing popovers are real product components. The discarded overflow option
remains `proposed`. The native symbols are SF Symbols, while the stand-in uses
Lucide counterparts; platform title placement and inactive traffic lights are
system-owned.

## Validation and acceptance

- Focused frontend tests cover same-document chat routing, annotation state,
  route eligibility, unavailable annotation, and existing share/lifecycle flows.
- A browser scenario drives the actual standalone route and its iframe bridge:
  remembered mode, mode switch, mutually exclusive pickers, button-relative
  placement, dismissal and re-entry.
- The hermetic native WebKit probe drives all four AppKit controls, verifies
  geometry, route gating, title truncation at 720 pt, narrow-title hiding, and
  cleanup after close. It exports optional system-appearance title-bar captures under
  `AVIBE_SHOW_CONTROLS_SHOTS` without activating the application.
- UI build, design typecheck, design screenshots and focused Rust validation are
  required before push. PR close-out requires exact-head Codex review, green CI,
  and zero unresolved threads.
- Main owns the signed local acceptance package. Final human checks there are
  raising the real main window on this chat while Show stays put, opening the
  system browser, and ordinary native hover/keyboard behavior in both OS themes.
  Offscreen native captures prove geometry; overriding an offscreen window's
  appearance does not faithfully simulate the system title bar's dark backdrop. No package is
  built or installed by this lane.
