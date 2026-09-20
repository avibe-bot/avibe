# Queued Attachment Previews Browser Regression

From `ui/`, install the existing Playwright Chromium browser with
`npx playwright install chromium`, then run `npm run test:queue-attachments`.

The command typechecks the fixture and tests, starts a dedicated Vite server on
loopback port 5216, and runs desktop and mobile Chromium in isolated browser
contexts. The real `QueueStrip` receives in-memory queued messages, and every
`/api/media/...` request is fulfilled by the test — including one deliberate
abort, which is how a failed image load is produced without a second code path.
No Avibe backend, credentials, or persistent application state is used.

The tests verify that a queued image paints at 20x20 and centred, that a row
with attachments is exactly as tall as a text-only row in every state, that the
inline run is capped at three attachments on desktop and two at 390px with a
`+N` that matches the width, that disclosure adds a second line and collapse
restores the original height, that the disclosed sheet lists every file in
order, that a queued image opens in a single-image viewer even though the
fixture's gallery contains the same URL, that an unavailable image keeps its
slot and surrenders its full filename to a tap, that the keyboard reaches the
previews in reading order and can work the disclosure without expanding the
message text, that inspecting attachments leaves the queue's rows, order, and
count untouched, and that the queue body stays at 128px with the composer on
screen. Failed traces are written under `e2e/.artifacts/queue-attachments/`.

For interactive inspection on a regular Vite dev server, open
`/e2e/queue-attachments/fixture.html`. Media URLs 404 there unless a backend is
running, which is itself the unavailable-image state. These fixtures are not
included in production builds.
