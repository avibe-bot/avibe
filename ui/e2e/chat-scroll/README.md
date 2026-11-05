# Chat Scroll Stability

From `ui/`, install the Playwright browsers with
`npx playwright install chromium webkit`, then run `npm run test:chat-scroll`.

The fixture renders the shipped `ChatPage` inside the shipped phone `AppShell`
(the design shell's providers and network stub) on a 400-row conversation of
mixed rows: one-line asks, markdown lists, code, tables, chart images that size
themselves when they load, Harness callbacks, and execution-process chips. The
bootstrap holds the newest 50 rows; older pages are served from memory after a
configurable delay; a live row can be pushed over the event stream. No request
leaves the page, and no Avibe backend, credentials, or persisted state is used.

`recorder.ts` logs every frame (scrollTop, scrollHeight, where a reference row
sits in the viewport and in the content) and every programmatic scroll of the
transcript, with the function that made it. It changes nothing the product does.

## Change contract

Each case fails on the code before the fix and holds one way the transcript used
to move under the reader:

- An upward fling gets the older page while there is still history above the
  reader. It never waits at the top for the page, and starting the load moves
  nothing.
- The reading position holds within a pixel through every older page, the
  300-row retained-window cap, and the images that arrive with each page.
- An image that loads in the row across the top edge does not move what the
  reader sees.
- An image that lands above the reader while they scroll is compensated. It is
  not taken for the reader's own position.
- The reader's own scroll is not written back when a row commits before that
  scroll's event (asynchronous scrolling on iOS and WebKit).
- A reader starting to scroll up is not pulled back to a tail that is still
  streaming.
- Rows mounted after a chat opened on its skeleton appear at once instead of
  fading in from blank.

Chromium runs with iPhone 13 emulation and real touch input through CDP (drags
and flings). Playwright's mobile WebKit takes no wheel or touch scrolling, so
WebKit runs at the phone's size with the wheel. Neither is iOS: the top
rubber-band, a scroll write under a finger, and iOS momentum need a device. The
query parameters the fixture reads are listed at the top of `fixture.tsx`, so the
same page can be driven on a phone.
