# Native Show window controls

## Change contract

Implement the approved `show-pages/desktop-native-controls/group-*` design from
`design/desktop-native-window-controls-20261121` at `16b0211068`, rebased onto
master `2988c4b041` (#4037). The macOS standalone Show window owns a single native
title-bar group: Annotate, Open Chat, Share, Open in Browser. Its SF symbols are
14 pt, hover wash 20 pt centered in a 28 pt hit target, gaps 4 pt and trailing
inset 9 pt. The accessory occupies 137 pt including its leading spacer.

On November 22, the approved native title bar changed to the system compact
style: an empty `NSToolbar` with `NSWindowToolbarStyle::UnifiedCompact`.
It measures 40 pt on the local macOS 27.2 system, compared with 32 pt before
this change (28 pt was the button target, not the measured native row).
The traffic lights move from 9 to 13 pt below the top edge and from 9 to 12 pt
from the left edge, entirely through AppKit layout. The four control sizes
and group width remain unchanged.

Workbench AppWindow controls retain their behavior and wording. The main
Workbench's native spacing is a separate branch and review. No packaging,
installation, production service changes or backend API changes are included.

## Behavior and ownership

- Native buttons dispatch annotation/share events only on the Show route and
  registered Runtime origin. Installation uses the intended destination even
  while cookie projection keeps the initial document at `about:blank`.
  These open the existing compact
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
- Title and accessory height are read from `frame - contentLayoutRect` after
  setting the compact toolbar. The Show installer clears AppKit's
  `FullSizeContentView` flag, so the WebView ends at `contentLayoutRect`
  instead of extending under the toolbar. Standalone Show pages do not use the main window's 28 pt CSS inset and need
  no additional padding. Popovers remain below the native content boundary.
- Only the Show controls installer sets this toolbar. Files, Editor, Terminal
  and the main Workbench keep their existing native chrome.
- The public compact style is available on macOS 11 and later. macOS 13–15
  retain their system-centered title. Their exact system row height has not
  been measured on this machine; the layout follows their actual AppKit
  metric rather than forcing a 40 pt frame over the native title bar.

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
- The hermetic native WebKit probe installs the controls at `about:blank`, then
  navigates to the Runtime document and drives all four AppKit controls. It verifies
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

## November 22 compact title-bar validation

- The hermetic AppKit/WebKit probe covers initial blank-page installation, all
  four dispatches, native vertical centers, the WebView/content boundary, narrow
  titles, and an ordinary non-Show window with no toolbar.
- The opt-in `AVIBE_SHOW_CONTROLS_FULLSCREEN=1` path enters a real full-screen
  Space and exits; the controls and content boundary are restored correctly.
- Real system-composited window captures cover Chinese/English and light/dark,
  a 720 pt long title, and a hovered control. They were taken on macOS 27.2;
  macOS 13–15 remains an explicit platform acceptance gap.
- The same 56 focused frontend tests and native-control browser consumer pass.
  The compact row does not change web shell startup or Workbench first paint.
