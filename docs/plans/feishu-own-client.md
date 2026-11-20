# Feishu/Lark: our own client instead of `lark-oapi`

## Background

The Feishu adapter uses a small part of `lark-oapi`: five IM v1 message calls,
the long-connection client that delivers events, and the event dispatcher for
four event types. The SDK's package `__init__` imports every generated API, so
an instance with Feishu enabled loads about 10,600 extra modules and about
400,000 tracked objects, which costs about two seconds of start-up and about
180 MiB of resident memory. Importing a single submodule costs the same,
because Python runs the package `__init__` first.

## Goal

The adapter talks to Feishu with code we own: plain HTTPS for the calls, and
our own implementation of the long connection. `lark-oapi` leaves the runtime
dependencies. Users see no change: the same messages, cards, files, reactions,
reconnects and error texts.

## Steps

1. **Message calls** (`modules/im/feishu_api.py`). The five endpoints go over
   aiohttp, like the calls the adapter already sent itself, with the adapter's
   own tenant-token cache.
2. **Event parsing.** Our own dispatch of the four event types. The SDK keeps
   its socket client, imported as before and run so that it can be stopped
   (`modules/im/feishu_sdk_ws.py`). No gain yet: importing any part of the SDK
   costs all of it.
3. **Long connection.** Our own client beside the SDK's behind one switch
   (`sdk`, `listen`, `parallel`, `own`), a local fake Feishu server, and
   differential tests that drive both clients with the same script.
4. **Removal.** `lark-oapi` moves from the runtime dependencies to the test
   dependencies, where it stays as the reference for the differential tests.

## The long-connection protocol

Feishu documents the limits but not the wire protocol; it is read from the
official open-source SDKs (Python, Go, Node), which agree on everything below
except where noted.

Documented by Feishu:

- An event must be handled within 3 seconds. Otherwise it is sent again after
  15 seconds, 5 minutes, 1 hour and 6 hours, four times at most. Delivery is at
  least once; `event_id` identifies an event.
- At most 50 connections per app. With several connections, each event goes to
  one of them at random.
- A card callback must be answered within 3 seconds and is not sent again.

From SDK source:

- **Endpoint.** `POST {domain}/callback/ws/endpoint` with `AppID` and
  `AppSecret` returns a `wss://` URL carrying a one-time ticket, and a client
  configuration (`PingInterval`, `ReconnectCount`, `ReconnectInterval`,
  `ReconnectNonce`). Code 0 succeeds; 1 and 1000040343 are retried; any other
  code is final.
- **Handshake.** A plain WebSocket handshake on that URL. A refusal carries
  `Handshake-Status`, `Handshake-Msg` and `Handshake-Autherrcode`: 403, and 514
  with 1000040350 (connection limit), are final; the rest are retried.
- **Frame.** Each WebSocket message is one protobuf `Frame` (proto2): `SeqID`,
  `LogID`, `service`, `method` (0 control, 1 data), `headers` (key and value
  pairs), `payload_encoding`, `payload_type`, `payload`, `LogIDNew`. Header keys:
  `type` (`event`, `card`, `ping`, `pong`), `message_id`, `sum`, `seq`,
  `trace_id`, `biz_rt`.
- **Ping.** A control frame with `type=ping` on connect and every
  `PingInterval` seconds. The `pong` reply may carry a new client configuration
  as JSON, applied at once.
- **Fragments.** `sum` is the number of pieces and `seq` the zero-based index;
  pieces share a `message_id` and are joined in order when all have arrived.
- **Acknowledgement.** The received frame goes back with a `biz_rt` header
  added and the payload replaced by `{"code":200}`, by `{"code":500}` when the
  handler failed, or with `"data"` (base64 of the handler's JSON result) for a
  card callback. Only `type=event` frames are handled.
- **Reconnect.** After a random wait of up to `ReconnectNonce` seconds, fetch a
  new endpoint and connect; then every `ReconnectInterval` seconds. There is no
  exponential back-off. The server can change all four values.
- **Credentials.** The connection does not use the tenant access token and has
  nothing to refresh; every reconnect fetches a new endpoint.

The SDKs differ on detecting a dead peer, which is outside the protocol: Python
relies on the WebSocket library's keepalive, Go on a read deadline, Node on
nothing by default.

## Equivalence

- **Differential tests in CI.** For the calls, a loopback stub records what the
  SDK and `FeishuApi` each send for the same call and what each makes of the
  same reply (`tests/test_feishu_api.py`). For the long connection, both
  clients connect to the same fake server and must dispatch the same events and
  send the same acknowledgement bytes.
- **Shadow on a live instance,** in three stages behind the switch: listen (our
  decoder runs on the SDK connection's frames and handles nothing), parallel
  (both connections; whichever receives an event handles it, through one entry
  and one `event_id` table), own (the SDK connection off, still installed).
- **Probe against real Feishu** in the regression environment after a merge.

## Known by design

- An unregistered event type is acknowledged with 200 and ignored. The SDK
  answered 500, which made Feishu send it four more times.
- An event id is remembered for 7 hours instead of 30 seconds, so that none of
  Feishu's four retries is handled as a new event. The table is in memory; a
  retry that arrives after a restart is handled again, as before.
- A card click's deduplication key is still forgotten after 30 seconds: a
  deliberate repeat of the same click must go through.
- A transport failure (timeout, connection refused) now raises aiohttp's
  exception where it raised httpx's. Feishu's own refusals keep their `code` and
  `msg`, and the token refusal keeps the SDK's wording.
- A reply that is JSON but not an envelope raises `FeishuApiError` where the
  SDK raised from inside its models. A send that Feishu calls a success but
  answers without a data object still fails at the call site, as it did.
- A reply is read for what the adapter uses (`code`, `msg`, `data.message_id`,
  `data.chat_id`). The SDK parsed all of `data` into its models, so a malformed
  field the adapter never reads failed the call; it no longer does.
- The token request does not follow a redirect: the app secret goes to Feishu
  and nowhere else. The SDK's `requests` call followed one.
- Requests carry aiohttp's `User-Agent`, not the SDK's.
- Calls that miss the token cache together share one token request; the SDK
  got the same effect by blocking the loop while it fetched.
- The configured `proxy_url` is still not applied to Feishu traffic.
- The setup wizard's temporary connection refuses every event (answer 500), as
  the SDK's empty dispatcher did, so that Feishu sends it again and a running
  adapter receives it.
- Loading the SDK's socket client without its API tree was tried in three
  designs (placeholders in `sys.modules`, a deferred package `__init__`, a
  private renamed copy) and dropped: it is code for a few weeks on the path
  that carries every Feishu message, and each design needed more machinery
  than the weeks justify. The gain arrives when our own connection replaces
  the SDK's.
- The SDK's connections run only through `SdkLongConnection`, one at a time,
  each on a thread and loop of its own. The SDK's client was not written to be
  stopped: one module-level loop for all clients, tasks that never end, a
  `start` that runs one step after another. What a stop guarantees:
  - once `stop()` has returned true, nothing of the connection is left (no
    socket, no task, its loop closed) and it cannot come back;
  - from the moment `stop()` is called, the client is given no endpoint, so
    it begins no new connection, and an event whose hand-over begins after
    that moment is refused, so that Feishu sends it again;
  - a handshake that already had its endpoint may complete; the socket is
    closed within moments. An event already handed over is handled.
  - `stop()` returns false while the thread is inside a blocking call of the
    SDK that cannot be interrupted: its endpoint request has no timeout, as
    before. The thread ends, under the rules above, when that call returns.
- The event shapes are those of lark-oapi 1.7.5, the release the lock file
  pins; `pyproject.toml` now asks for no older one.
- A malformed envelope (a schema or uuid that is not text, a 2.0 event without
  a header, a body of the wrong shape) is refused as the SDK refused it. Only
  a well-formed event of a type we do not handle is acknowledged and ignored.

## Todo

- [x] 1. Message calls and the 7-hour event memory (#4047)
- [ ] 2. Event parsing; the SDK's connection made stoppable
- [ ] 3. Own long connection, fake server, differential tests, switch, probe
- [ ] 4. Shadow stages on a live instance
- [ ] 5. `lark-oapi` out of the runtime dependencies
