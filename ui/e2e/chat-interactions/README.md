# Chat notice and selection interactions

Run `npm run test:chat-interactions` in `ui/` after installing the Playwright
Chromium and WebKit browsers. CI runs this suite in `lint / ui-checks`.

## Change contract

- The failure notice's Copy record button is on its own line below the turn ID,
  wholly inside the bubble. A long ID wraps instead of creating overflow.
- A completed toolbar touch consumes the trailing `touchend` default action,
  so native tap processing cannot undo Select all after `pointerup`. This is
  not a selection lock: later clearing or selecting other text still works.
- Desktop, keyboard, assistive clicks, incomplete gestures, and the bounded
  missing-terminal-event recovery retain their existing behavior.

## Evidence and ownership

The fixture renders the real MessageRow, SelectionQuoteToolbar, Markdown, i18n,
and CSS. Only message/provenance data is supplied. External requests, unknown
API paths, and writes are refused; the dev server points at a dead backend.
No live Avibe state, credentials, or user browser profile is used.

- Browser layout tests cover 320/390/768px and both languages. The old row layout
  fails the measured next-line assertion; jsdom cannot measure this contract.
- Component tests own the touch completion contract, incomplete gesture and
  subsequent clearing. The pre-fix handler leaves `touchend` uncancelled.
- Native browser taps in Chromium and WebKit additionally verify React's real
  listener is non-passive, and that the toolbar and full range survive past
  the selection debounce. Desktop uses real mouse input.

Headless WebKit is not a physical iOS PWA and does not reproduce its OS selection
menu/gesture recognizer. A physical-device check is still needed: long-press a
partial chat message, tap Select all, wait, copy/quote the full bubble, then
select other text or clear the selection. No automated physical-device pass is
claimed.
