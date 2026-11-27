# Event streams over WebSocket

Base: Avibe a66e8070cd; approved by main on 2026-11-27. Independent from #4365.

## Cause and measured boundary

Each Web UI document and Show iframe holds an HTTP/1.1 SSE connection. Six
persistent streams exhaust the browser's same-origin request pool. Signed RC2
WebKit evidence shows a Show script waiting 242.968 seconds, then completing in
12 ms when an inline Show stream closed; dependency reads timed out in 15.005 s.

An isolated 8-window loopback probe confirms the proposed transport boundary:
Chromium 159 with eight WebSockets returns ordinary fetches in 0.6–2.3 ms;
WebKit 27.2 returns them in 1–4 ms. Six SSE connections make the same fetch wait
until a connection is released (about 2 seconds in both controlled runs).
Local browser tabs share this HTTP/1.1 limitation. HTTP/2 HTTPS multiplexing
avoids the six TCP-slot constraint, subject to negotiated stream limits; Cloud
or proxy deployments using HTTP/1.1 still have the original limit. WS works
across both and must not weaken any admission or resource authorization.

## Frozen cross-repository wire contract (v1)

- The existing authenticated/local `GET /api/session` JSON adds
  `event_stream_transport: "websocket-sse-v1"`. An old successful session payload
  without this field means SSE-only. A failed session probe or a remote session
  that is unauthenticated/unavailable is not permission to fall back.
- The Runtime-injected Show configuration adds
  `eventStreamTransport: "websocket-sse-v1"`. The SDK accepts this optional field
  in its runtime/client options. An absent field means the old Runtime and SSE.
  An explicit client stream URL on a different origin must not inherit another
  origin's capability. Never use a failed WS handshake as capability discovery.
- WS uses the exact existing SSE URL and query, changing only the scheme:
  `/api/events`, `/show/{session_id}/__show/events?stream=1`, or
  `/p/{share_id}/__show/events?stream=1`. `after_id` remains the Show cursor.
- Each WS text message carries bytes decoded from the **existing SSE generator**,
  including `event:`, `id:`, `data:`, blank separators and comment heartbeats.
  Consumers parse SSE frames; no JSON payload or authorization event is renamed.
  Parsing must tolerate multi-frame/chunk messages and retain the last event ID.
- WS admission calls the existing exact-origin socket check, then the very same
  HTTP before-request hooks, route handler and streaming generator as SSE, using
  a synthetic GET request that preserves host, cookie, forwarding/direct-ingress
  metadata, path and query. No permission is copied from Terminal. HTTP admission
  failures close the WS; they do not redirect or downgrade the transport.
- Existing stream filtering, authorization revision/expiry/revocation, retired
  address, queue overflow, public/private Show projection and archive behavior
  remain owned by those generators. Disconnect cancels the generator and frees
  its broker subscription. Client messages are not an event-publishing API.
- SDK callers retain the EventSource-shaped subscription contract (listeners,
  open/error, readyState, close). Reconnection uses bounded backoff and the last
  SSE ID as `after_id`. Closing/disposing prevents retries and late events.
  Web UI keeps its existing reconnect/heartbeat/catch-up owner; closing a source
  cancels negotiation/socket/reconnect work. All state is document/source scoped.
- Old client + new Runtime uses unchanged SSE. New client + old Runtime uses
  SSE and retains the old pool limitation. Only new/new removes that limitation.
  New/new auth failure remains failure, without downgrade.

## Artifact integration

Show SDK has package version `0.0.0` in a private npm workspace, not a separately
pinned published npm dependency of Avibe. Show CI builds/tests all workspaces and
uploads platform runtime archives. Avibe `publish.yml` resolves Show `main` to
one SHA, builds/reuses canonical bundles for that SHA and lock hash, and generates
a hash-verified packaged manifest. Thus the SDK PR must merge before the chosen
Avibe release resolves its Show SHA. No tag, package publication, installation or
production configuration change is authorized in this lane. Validate the paired
source trees locally; release artifact production remains main's release step.

## Validation and delivery

Runtime/UI PR plus separate Show SDK PR, no stacked Avibe PRs. Exercise Chromium
and WebKit with at least seven documents/iframes, the old/new compatibility
matrix, reconnect/cursor replay, switching origins, close cleanup, and denial /
revocation / viewer filtering through the shared authorization path. Main owns
an independent different-family security review before Merge-ready. Signed
Desktop acceptance is deferred. Never operate the active installed app windows.

## Implementation evidence (2026-11-27)

The Avibe branch was rebased to `0a01fa4af8`. Show SDK PR #79 is
`e6e3aaed494e142000a28fca2727e2f9be9a0525`. The paired fixture records source and
artifact SHA256 values, so its pre-commit provenance remains tied to the exact
transport bytes tested.

- 18 focused Runtime integration tests cover HTTP hook denial, exact Origin,
  viewer filtering, real session revocation, private/public cursor replay,
  session separation, client-message rejection, broker cleanup, capabilities,
  and real TLS direct-ingress admission (anonymous rejected; cookie accepted).
- 77 UI tests cover negotiation/denial/closing/switching plus the existing
  ApiContext reconnect, heartbeat, catch-up, local-store and snapshot owners.
- The production-compiled UI/SDK fixture passes 28 Chromium/WebKit scenarios.
  Across both families, all three SSE compatibility combinations hold an
  ordinary HTTP/1.1 fetch for 2005.5–2232 ms, until one occupied slot closes.
  With eight new/new WS sources, Workbench fetches take 0.9–3.8 ms in Chromium
  and 2–3 ms in WebKit; SDK fetches take 0.8–1.4 ms and 2–3 ms respectively.
  WS denial never downgrades; SDK reconnect retains after_id; switching and
  disposal prevent late delivery or retries. Every cleanup reaches zero streams.
- The opt-in `tests/test_event_websocket_browser.py` runs the compiled real
  clients against actual isolated Runtime routes, SQLite Show events and a
  real ephemeral uvicorn listener. Four UI plus four SDK streams coexist;
  persisted Chinese/emoji events replay after the supplied cursor; ordinary
  `/api/session` reads take 6.5–8.2 ms in Chromium 156 and 8–13 ms in WebKit 27.2.
  Closing all pages releases every broker subscription.
- `npm run build`, UI lint/typecheck and changed-Python Ruff pass. The unsigned
  headless engines are test evidence; no installed/signed Desktop was operated.

Evidence is in the owner's `2026-11-27-desktop-full-acceptance/findings/`:
`event-transport-browser-results.json`, individual `event-transport/*.json`,
`event-real-runtime-browser.txt`, `event-python-regression.txt`, and
`event-ui-regression.txt`.

### First-load budget

Unmodified master `fee3ce6063` measures home 624,602 B and chat 927,841 B, against
624,640 / 927,744 B allowances (master already exceeds chat by 97 B). This PR
on `0a01fa4af8` measures 625,575 / 928,804 B: +973 / +963 B for the shared
transport/negotiation/parser, which is needed on the first screen. The separate
budget gate exceeds its unchanged limits by 935 / 1,060 B. Main owns resolution
of the baseline/allowance; this lane does not raise it or hide immediately used
code behind an asynchronous import. PR #4365 is independent and not stacked.
