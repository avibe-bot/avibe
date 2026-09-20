# Chat Paging Browser Regression

From `ui/`, install the existing Playwright Chromium browser with
`npx playwright install chromium`, then run `npm run test:chat-paging`.

The command typechecks the fixture and tests, starts a dedicated Vite server on
loopback port 5198, and runs desktop and mobile Chromium in isolated browser
contexts. The real `Transcript` component receives in-memory messages and page
responses. No Avibe backend, credentials, or persistent application state is
used. This suite is separate from the live Model Hub suite.

The tests verify one page per upward request, stable reader position across
the 300-message retention cap, fast responses with unchanged content height,
empty pages followed by wheel or touch input, keyboard focus and repeat handling,
continuous gestures, nested scrolling, and explicit failure recovery.
Screenshots and failed traces are written under `e2e/.artifacts/chat-paging/`.

For interactive inspection on a regular Vite dev server, open
`/e2e/chat-paging/fixture.html`. Query parameters select the initial retained
count (`count=300`), response delay (`delay=0`), or an empty/failed first page
(`empty=1` / `fail=1`). `empty=10` returns ten empty pages; `nested=1&count=1`
includes a scrollable activity fixture. These fixtures are not included in
production builds.
