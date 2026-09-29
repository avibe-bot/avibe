# Mobile selection toolbar on iOS 27

## Problem

iOS 27 can keep a touch that starts near a native text-selection handle in its
selection gesture recognizer. The WebView may deliver `pointerdown` while
withholding the later `pointerup`, `pointercancel`, and capture-loss events.

The selection toolbar is currently positioned only 8px from the selected range.
That places its buttons inside the native handle hit region. PR #2249 added
pointer ownership that is released only by those terminal events, so a withheld
`pointerup` leaves the toolbar pinned to stale selection state.

## Contract

- A missing pointer end event must not leave the toolbar in a permanently owned
  or stale state.
- Clearing or changing the selection must hide or recompute the toolbar even if
  a previous touch ended without a terminal event.
- On coarse pointers, the toolbar is placed at least 44px plus the existing
  8px gap from the first or last selection line used as its anchor.
- Quote, Ask in a new session, and Copy still activate once on a complete
  pointer gesture; keyboard activation remains unchanged.
- Desktop placement and selection behavior remain unchanged except for using
  the same simpler activation handlers.

## Implementation

- Remove pointer capture and the `activePointerId` lifetime state.
- Track the first and last client rects of the selection instead of only the
  aggregate range rect.
- Place the touch toolbar above the first line or below the last line with a
  52px safe offset. If neither full slot fits, dock to the farther viewport
  edge.

## Validation

- Component tests simulate pointerdown without any terminal pointer event,
  clear the selection, select new text, and activate the new toolbar.
- Geometry tests verify the toolbar clears the native 44px handle region.
- Focused UI tests, changed-file lint, and production build are required.
- Playwright WebKit and Chromium probes must include the missing-terminal-event
  model; event-complete taps alone are not sufficient.
- Physical iPhone iOS 27 PWA validation remains required for final confidence.
