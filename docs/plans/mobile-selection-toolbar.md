# Mobile selection toolbar gesture lifetime

## Contract

A primary pointer press on a chat selection action owns the selected text until
that gesture finishes. Selection collapse or transcript scrolling during the
press must not remove the action before release. Quote, Ask in a new session,
and Copy execute exactly once when the same pointer releases inside its button.
Cancellation, lost capture, and release outside the button execute nothing and
restore normal selection tracking. Keyboard activation remains available.

This change belongs to `SelectionQuoteToolbar`; it does not change composer,
fork, clipboard permission, or PWA installation contracts.

Dependencies: none. No existing scenario-catalog ID covers this interaction;
the toolbar component regression is its primary automated contract.

## Validation

- Component regression: an actual DOM selection collapses between pointer down
  and pointer up; the debounce elapses; each action still receives the original
  non-ASCII text exactly once. Existing tests do not exercise this toolbar's
  pointer/selection interaction.
- Cancellation, capture loss, outside release, non-primary input, and ordinary
  selection dismissal cover gesture ownership boundaries.
- Hermetic browser checks exercise native pointer capture and touch activation
  in Chromium and WebKit. WebKit touch emulation does not establish the exact
  event ordering of an iPhone's native selection menu.
- Focused tests, changed-file lint, production UI build, exact-head Codex review,
  and required CI.

## Local results

- The pre-fix component failed the three selection-collapse action cases.
- All 75 tests across the toolbar, archived-chat behavior, and composer shortcuts
  pass after the fix.
- Chromium and WebKit each passed native touch taps for all three actions,
  native mouse pointer capture across selection collapse, and captured release
  outside the button. The probe served the actual component through Vite on
  loopback in disposable browser contexts; clipboard writes were intercepted to
  avoid changing the host clipboard. It reported no page errors.
- No iPhone hardware or native iOS selection menu was exercised.

## Known by design

- The operating system's selection menu remains available.
- Toolbar placement and transcript scroll behavior outside an active button
  gesture remain unchanged.
- No production update, service restart, or merge is part of this change.
