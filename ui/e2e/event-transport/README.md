# Event transport browser evidence

This suite loads a production Vite build of the actual
`ui/src/lib/workbenchEventSource.ts` and a built companion Show SDK into fresh
headless browser contexts. A test-owned Node HTTP/1.1 server serves every
document, capability response, SSE/WS stream and ordinary fetch. No request is
fulfilled through Playwright routing. No Avibe service, account, app window or
persistent browser profile is involved.

Build the Show SDK in its own checkout first. Then, from `ui/`:

```bash
SHOW_SDK_DIST=/absolute/path/to/show/packages/sdk/dist \
  npx vite build --config e2e/event-transport/vite.config.ts
EVENT_TRANSPORT_WEBKIT=/absolute/path/to/webkit/pw_run.sh \
EVENT_TRANSPORT_EVIDENCE_DIR=/absolute/path/to/findings/event-transport \
  npx playwright test --config playwright.event-transport.config.ts
npx tsc -p e2e/event-transport/tsconfig.json --noEmit
node e2e/event-transport/collect-results.mjs \
  /absolute/path/to/findings/event-transport \
  /absolute/path/to/findings/event-transport-browser-results.json
```

The fixture's production build records exact source/output hashes and git heads
in `.build/provenance.json`. Test JSON evidence includes these hashes, browser
versions, fetch timings, server-observed HTTP versions, stream admissions,
cursor queries and cleanup counts. Generated fixture output stays ignored.

Both client families cover old/new client × old/new Runtime. An old client uses
native EventSource and ignores capabilities; the new client is the compiled
production implementation. A new fixture Runtime advertises WS; an old Runtime
does not. These are capability/transport fixtures, not authorization tests of
the actual Runtime routes.

All eight documents and their scripts finish loading before subscriptions
start. In each legacy combination six admitted SSE streams occupy the six
HTTP/1.1 slots; two further logical subscribers wait. After two seconds the test
cancels those pending sources (freeing no occupied stream slot), then closes
one admitted source. The ordinary real fetch can then complete. New/new admits
all eight WS streams and samples ten ordinary fetches without closing them.

Additional browser cases cover WS rejection without SSE downgrade, SDK
cursor-preserving reconnect and retry cleanup, instance changes from WS to SSE,
Unicode/frame fragmentation and Workbench cancellation during capability
negotiation. Workbench reconnection remains owned by ApiContext; this suite
does not create a second reconnect owner.

To pierce the real Runtime boundary as well, after building this fixture run
from the Avibe repository root with the test environment's Python:

```bash
EVENT_TRANSPORT_BROWSER=1 \
PLAYWRIGHT_BROWSERS_PATH=/absolute/path/to/installed/playwright/browsers \
EVENT_TRANSPORT_WEBKIT=/absolute/path/to/webkit/pw_run.sh \
  python -m pytest tests/test_event_websocket_browser.py -q -s
```

That opt-in test uses test-owned HOME/config/SQLite and a real ephemeral uvicorn
listener. It checks four Workbench plus four SDK subscriptions, real Chinese
Show event replay after a cursor, ordinary session reads, and broker release
when the pages close. No installed Runtime or Desktop window is touched.
