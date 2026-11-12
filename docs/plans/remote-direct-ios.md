# Remote direct paths: the iOS App side

Status: **Design, rewritten on 2026-11-12 for a persistent local proxy.** The owner chose this architecture after the WebKit measurement in section 2. It replaces the earlier design, in which the App switched WebKit between the tunnel and the direct path by changing the data store's proxy configuration. Nothing here is built: #3537 (Swift) and #3532 (Web UI) implement the earlier design and are reworked against this one after it lands. The iOS owner's review is requested after the fact, as before; the open items in section 13 stay his. This work does not raise the App's minimum iOS version (17.0), adds no capability or entitlement, and does not touch signing, TestFlight, or the notification service extension.

Statements about behaviour below are of three kinds, and say which: **measured** (with where), **candidate** (the design's choice, gated by a named qualification cell in section 11), or **rule** (what the code must do).

Parent plans: [`remote-direct-phase1.md`](remote-direct-phase1.md) (rows A1 to A5, I4), [`remote-direct-ingress.md`](remote-direct-ingress.md) (stream framing), [`remote-direct-rendezvous.md`](remote-direct-rendezvous.md) (what a client must do). Bridge contract: [`ios-app.md`](ios-app.md) section 4.

## 1. What the App does

The App keeps loading `https://<slug>-app.avibe.bot`, on the same account data store, with the same cookies, cache, and passkeys. WebKit's connections for the instance's exact host go to one loopback listener inside the App, by an HTTP CONNECT proxy route that WebKit holds for the data store. The listener never terminates TLS: WebKit's TLS session, with its SNI and certificate validation, runs end to end to the instance on every path. For each new CONNECT the listener chooses a backend:

- **direct**: a stream over the App's direct transport to the instance's strict ingress;
- **public**: the App's own TCP connection to the same exact host on port 443, the instance's public origin (in Avibe's deployment, its Cloudflare Tunnel hostname).

```text
WKWebView on the account data store (runtime, child windows, hidden transports)
  -> route (never cleared; changed only to add a host or move port): CONNECT proxy 127.0.0.1:P, matchDomains [exact instance hosts]
  -> App listener on 127.0.0.1:P   CONNECT <allowed host>:443 only
       -> direct backend           transport stream; canonical CONNECT head; the instance's status; then bytes
       -> public backend           TCP to <same host>:443; then bytes
  -> TLS for <host>, inside the tunnel, between WebKit and the instance; ordinary remote authentication
```

When the direct path is lost, the listener closes the connections it carried on that path. WebKit then opens new connections, and the listener sends them to the public backend. Leaving home Wi-Fi therefore recovers over Cloudflare without killing or relaunching the App. That is the owner's required outcome; section 11 lists what must still be measured before it is a fact on devices.

What the App never does: change or remove a route to switch paths; move, migrate, or replay a connection; accept a CONNECT for any authority but an allowed instance host on port 443; read or terminate TLS.

## 2. What is measured, and what follows from it

Measured on the iOS 26.5 simulator (build 23F77) in Actions run 43050816127, with a TLS fixture, GET requests only (`packaging/webkit-proxy-change/` at `384a152e`, on the branch `experiment/webkit-proxy-change`). Every matrix case uses a new named data store created in a running process.

| Observation | Cases |
| --- | --- |
| A route written to a new store before its first load is used by the page and by `fetch`, over HTTP/1.1 and HTTP/2. | `cold-before-webview`, `cold-before-first` |
| A route written while the store already has a live or pooled connection to that host is not used: reload, WebView recreation on the same store, and new requests stay on the old connection. With a slow request keeping the old HTTP/1.1 connection busy, the extra socket WebKit opened also ignored the new route. | lifecycle `reload`, `recreate`; `idle`; `inflight-install` |
| Emptying the route did not stop proxying while the old proxy connection lived, and after a NetworkProcess restart the emptied route came back. | `remove-open`, `restart-empty` |
| After the store's only connection to that host was closed, the next connection followed a changed route: from a proxy to none, and (HTTP/1.1 only) from none to an absent or refusing proxy, where the fetch then failed. Neither is a change between two proxy ports, and neither shows that closing the App's own sockets empties WebKit's pool. | `remove-close`; `failover-absent-false`, `failover-refuse-false` |
| With a constant route, closing the proxy's old CONNECT moved later fetches, a rebuilt `EventSource`, and a new WebSocket to the proxy's new backend. A CONNECT that was not closed kept its backend for the requests pooled on it. | `constant-proxy`, `constant-proxy-surviving` |
| `allowFailover = true`: with nothing listening, or a refusing proxy, the next HTTP/1.1 fetch went directly to the origin. With a stalling proxy, it failed within the bound either way. | `failover-absent-*`, `failover-refuse-*`, `failover-stall-*` |
| App relaunch with a different route written before the first load switched to it. | lifecycle `relaunch` |

What follows: **changing the route is not a way to switch paths**, and clearing it is unsafe. A constant route to a listener that switches backends is the way, measured on this simulator, to move new connections. Nothing here is measured on iOS 17, on a device, with real Cloudflare, during suspension, across a radio change, or for non-idempotent requests.

## 3. Components

| Component | Owns | Plan row |
| --- | --- | --- |
| `DirectTransporting` | The iroh endpoint, the rendezvous fetch, probing, and loss detection (section 12). Reports path state; opens direct streams. | A2 |
| Listener | The one loopback socket, the CONNECT checks, the backend choice for each CONNECT, the relays, and their path generations. | A3 |
| Public backend | Native TCP to the instance's public origin, the exact host on port 443, bounded and cancellable. | A3 |
| Path controller | Tags, the preference, path generations, invalidation, and the bridge state. | A3 |
| Route owner | The only code that writes `proxyConfigurations`. It writes; it never clears. | A3 |
| Store lifecycle | `AccountWebsiteDataStore.rotate()` tells the controller first, as today. | A3 |
| Bridge | `direct.status()` and `direct.transportChanged`. | A4 |
| Web UI | Reconnects SSE and safe terminals when the shell reports closed connections. | I4 |

**Without a registered transport, nothing of this runs**: no listener is created, no route is written, the status is always `tunnel`, and no event fires (rule). The first build that registers a transport is the owner's to approve.

## 4. The listener and its port

- **One listener per App process** (rule), bound to `127.0.0.1`. It is not bound until a transport is registered and a route is about to be written.
- **Port epochs.** The listener's port P is chosen by the system at the first bind. It keeps that port for as long as it can (section 9). If it must move to a new port, that starts a new port epoch, and every live route is rewritten to it (recovery R1, section 9). Within one epoch, every route points at the same P.
- **Exclusive binding** (rule). P is bound without `SO_REUSEPORT`, so no other socket can share it while the listener holds it.
- **The ownership check** (rule). After every bind, the App connects to P and sends a random nonce made for this launch; only the listener's own accept loop echoes it. This is a liveness and correlation check, to catch a listener that is not serving or a port that is not ours. It is not cryptographic authentication, and a hostile local process is not assumed unable to interfere with it. A failed check is a port loss (section 9).
- **The allowlist** is the set of exact hosts written to any route in this process, on port 443 only (rule). It is bounded at 16 hosts. Past that bound no host is added to any route, and new hosts use WebKit's normal path. Being on the allowlist only lets a host be relayed. Whether the direct backend may carry it is decided per CONNECT by the live selection (section 6), and that decision shrinks immediately when the selection or account ends.

| Request from WebKit | Answer |
| --- | --- |
| `CONNECT <allowed host>:443 HTTP/1.1` | A backend is chosen (section 6) |
| Any other authority: another host, a subdomain, another port, no port, userinfo, an IP literal | `403` |
| Any method but `CONNECT` | `405`. The one exception is the ownership check, answered only when it carries this launch's nonce. |
| Not HTTP/1.1, a body, a repeated `Host`, a disagreeing `Host`, a malformed head, a head over 4 KiB | `400` |
| Head not complete within 5 seconds | Closed |
| More than 64 connections at once | The extra connection is closed at once, with no answer |

`matchDomains` is a suffix match, so `x.<host>` reaches the listener too; the `403` row is why the listener checks the authority itself.

A relay copies both directions with backpressure (64 KiB chunks, the next read only after the previous write), passes a half-close on, and gives the other direction 30 seconds after one side finishes. A relay has no idle timeout: a healthy SSE stream is quiet between heartbeats, and a terminal can be idle for hours.

**What a CONNECT does not tell the listener.** A CONNECT names a host. It does not say which data store, WebView, app, or process sent it, nor which account's lease it belongs to. The listener therefore cannot authenticate its caller, and nothing here claims it can.

- Any process on the phone can connect to `127.0.0.1:P` and is served like WebKit. It reaches only the allowed hosts, through TLS it must complete itself with the instance, and the instance's remote authentication decides every request it makes.
- What such a caller can take: the 64 connection slots, which push WebKit's new connections to failure or failover. And, while direct is preferred for a host, its streams ride this App's admitted direct peer and count against that peer's ingress limits at the instance.
- The same holds for a connection from an older data store of this App: the listener cannot tell it apart. Lifecycle, not authentication, keeps it in bounds (section 7): an account transition closes every relay, and releases the old store's WebViews before the new runtime opens, as today.

These are exposures of the design. They are tested in section 11 and not claimed absent.

## 5. Routes: writing them, and when

The **route owner** is the only writer of `proxyConfigurations` (rule). A route is one `ProxyConfiguration(httpCONNECTProxy: 127.0.0.1:P)`, with `matchDomains` set to exact instance hosts (no wildcard, no suffix of a shared zone) and `allowFailover` as in section 8.

Rules:

- **A route points at the current port epoch's P.** It changes only when a host is added, or when R1 moves every route to a new epoch (section 9).
- **A store's host list only grows while the store lives** (candidate; whether adding a host takes effect is cells Q2 and Q3). No host is removed and no route is cleared: clearing is unreliable (section 2). A host the user no longer uses is relayed only to its public origin (section 6), never on the direct backend. The host list lives and dies with the store and the process, and is bounded by the allowlist.
- **A host is added before the first load of that host in that store.** The listener is bound and checked first. The App's own load waits for both, with a bound; past it, the load goes ahead without a route, on WebKit's normal path. The bound is a proposal, set from measurement.
- **A failed or ignored write leaves that host on WebKit's normal path.** That path is the instance's public origin. Within one epoch, every route points at the same P. So a write that WebKit applies to some connections and not others sends each connection either to the listener or to the normal path, never to a third place.

| When | What is written | Status |
| --- | --- | --- |
| Cold launch, signed in, with a stored instance selection | The account store's route with that host, before the store's first load of anything | Candidate, cell Q1. Measured analogue: a new named store, created in a running process and routed before its first load (section 2). |
| Sign-in, sign-out, account switch (the store rotates) | Nothing on the new store until an instance is selected | Rule |
| The first instance selected on a store that has already loaded the control plane | The host is added before the runtime's first load | Candidate, **open cell Q2** |
| Instance switch to a host this store has not loaded | The host is added (union) before its first load | Candidate, **open cell Q3** |
| Instance switch to a host already in the route | Nothing | As the cold case |

The measured analogue is in a running process, so nothing here is limited to a process cold start. Q2 and Q3 decide whether a store that is already in use can take a new host.

**A suspected bypass.** No new CONNECT can mean any of several things: the request was served from cache, a service worker answered it, the load failed, it reused a tunnel already open, or WebKit did not apply the route. A missing CONNECT is therefore evidence of a bypass only in one case, which the App makes itself. The App issues a native no-store request for a routed host on the current runtime, outside service-worker scope. It completes with a network response. During that request the listener had no relay open for that host and received no CONNECT for it.

That case marks the store and host `suspected-bypass` for diagnostics, and direct is not preferred for it until a later CONNECT for that host arrives. Nothing else is inferred from silence.

If Q2 or Q3 turn out negative, sign-in and newly used instances stay on the normal path for the rest of the process, and the shortfall is visible. Any workaround (a second store, a broader match) is outside this contract and needs a design review.

## 6. Choosing a backend for each CONNECT

**The direct backend is authorized per CONNECT, from live state only.** It may be chosen only while all of these hold:

- the CONNECT host is the **current** selection's host;
- the selection's store has a route listing that host;
- every tag (section 7) is current, including the account lease;
- the transport has reported the path `usable` for those tags, and nothing has suspended that since;
- the host is not `suspected-bypass`.

Otherwise the CONNECT goes to the public backend. Every condition is re-read when a CONNECT arrives. When a selection or an account lease ends, direct stops being chosen in the same main-actor turn. A host from an earlier selection, an earlier account, or an earlier store never gets the direct backend again. Its callers are relayed only to the public origin, which adds no credential and restores no authority: whatever the caller sends travels inside its own TLS session, and the instance's remote authentication judges it.

**Before any byte is forwarded, a failure is not yet a failure of the tunnel.**

1. **Early bytes are held.** Bytes WebKit sends after its CONNECT head (it may send its ClientHello before the answer) are held in a buffer of at most 16 KiB. Nothing is forwarded to any backend until one has been selected. A CONNECT whose early bytes exceed the buffer is closed.
2. **Direct first, when it is authorized.** The listener opens a direct stream and writes the canonical head from the ingress contract, `CONNECT <host>:443 HTTP/1.1` with `Host: <host>:443` (not WebKit's bytes). It then reads the instance's status: 4 KiB at most, within a bound.
3. **A hedge.** If the direct answer has not arrived after a hedge delay, the listener also starts the public dial. The first ready backend is selected: a direct `200`, or an established public connection. The other is cancelled and closed. A selected backend whose selection or path generation went stale before selection counts as not ready.
4. **Fallback.** A direct stream that cannot open, a refusal, or a missed bound leaves the CONNECT to the public backend, and the transport is told (`pathDidFail`).
5. **No backend ready** within its bound: WebKit gets `502`, or the instance's refusal status.

**The bounds.** The hedge delay, the public connect bound, and the direct open and answer bounds are proposals, set from Q7 measurements. #3537's current values (2 s to open, 3 s for the answer) are a starting point, not a published guarantee.

**After the first forwarded byte or the `200`, the tunnel belongs to its backend.** Selection happens before either. If that backend then fails, the relay is closed and WebKit sees a closed connection. The listener never redials, never moves a tunnel to another backend, and never replays a byte. A healthy relay is never closed to change backends, including when direct comes back: only new connections prefer it.

**The public backend** is the instance's public origin, the same authority WebKit would reach without the route. In Avibe's deployment that is the instance's Cloudflare Tunnel hostname, but the backend does not assume a Cloudflare edge. It is a native TCP connection (`NWConnection`):

- **Target:** exactly the CONNECT's host on port 443. That host must be an allowlisted instance host taken from the control plane's instance list, not the control plane's host, not any other host, and not an IP literal.
- **Resolution:** the system resolver. Every resolved address is checked, and the connection is made only to an address that is not loopback, unspecified, link-local, multicast, or private. A local-network path is the direct backend's job, not this one's.
- **Proxy settings:** no App proxy configuration on its parameters, so it cannot be routed back to the App's own listener. What the system's proxy settings do to it, and what happens on networks that allow only an HTTP proxy or block raw TCP, is qualified (cell Q6), not assumed from a parameter's name.
- **Cancellation:** cancelled when the WebKit side closes, when the listener stops, when its path generation is fenced, and when the network changes before it is established.

It carries WebKit's TLS bytes unchanged.

**Diagnostics** count `direct` and `public` relays separately from `suspected-bypass`. Only relays are seen by the listener.

## 7. Tags, path generations, and invalidation

A **selection** is one opened runtime, between the App's `openRuntime` and `stopRuntime` (and the registration exit that returns to the picker). Its tags:

- the selection number;
- account and instance, and the account generation, from `MobileRuntimeContext`;
- the data-store identifier;
- the instance authority;
- the rendezvous generation.

A transport report counts only for the selection it was made for, and is dropped after that selection ends. Before direct is authorized, the controller checks the lease with `MobileSessionCoordinator.isCurrentRuntime`, as the App's own route and WebView registration do. Within a selection, a rendezvous generation that a newer one replaced is never accepted again.

**Every relay records its backend, its selection, and the path generation it was opened on.** The path generation advances on each event below. A late report or callback that carries an older selection or generation changes nothing.

| Event | New CONNECTs | Direct relays opened before the event | Public relays |
| --- | --- | --- | --- |
| The transport reports the path `lost` (its connection closed or failed) | Public | Closed | Kept |
| A network change callback | Public, until the transport validates the path again | Kept | Kept |
| Validation after a network change fails, or runs past its bound | Public | Closed | Kept |
| Validation succeeds on the same transport connection | Direct again | Kept: healthy, and never reported as moved | Kept |
| The rendezvous generation changes (a new instance endpoint key or addresses) | Public until `usable` on the new one | Closed | Kept |
| The selection ends (instance switch, picker, registration exit) | Public for that host | Closed | Kept |
| The App enters the background | Public | Closed, and the transport stops | Left to the system |
| An account transition (sign-in, sign-out, switch, store rotation) | Public | Closed | **Closed**: nothing of the old account is kept |

**A network callback alone is not proof of loss** (rule). It changes only what new connections prefer, until the transport validates the path. **Public relays** are closed when their own connection fails, or reports itself not viable for longer than a bound that is set from measurement. They are never closed for performance, nor because a better path appeared.

Closing a relay closes WebKit's connection to the listener. Over HTTP/2 one connection carries many requests, so every request on a lost direct connection fails with it. That is the cost of a lost path, not of the closing. Nothing replays them.

**Detection is not a measured bound.** How fast "lost" arrives is the transport's (section 12). The 8 to 17 s measured with a desktop iroh client, against the instance's 6 s idle timeout and 1.5 s keep-alives, is not a device bound and not an end-to-end failover time (cell Q7).

## 8. Failover by WebKit (candidate safety net)

`allowFailover = true` is the candidate (it was `false` in the earlier design). It is a safety net under the native public backend, not a replacement for it and not a guarantee.

- **When it can engage.** The listener never refuses an allowed host while it can dial the public origin itself. So WebKit's own failover can engage only when the listener cannot be reached (for example while the port is closed for a re-bind), or when both backends failed.
- **What is measured:** only HTTP/1.1 fetch on the 26.5 simulator, with nothing listening or with a refusing proxy (section 2). A stalling proxy did not fail over within the window.
- **What is not measured** (cell Q6): navigation, HTTP/2, WebSocket, iOS 17, networks that allow only an HTTP proxy, and networks that block raw TCP.
- **Stickiness is measured, not assumed.** Q6 measures whether WebKit, after failing over, keeps bypassing the route, and for how long. In the field, only section 5's suspected-bypass check can see it. A bypass by WebKit is outside the listener's sight and is never counted as a `public` relay.
- **Fallback position.** If Q6 shows failover is harmful or sticky, the value becomes `false`. A listener that cannot be reached then fails those loads until it is re-bound, and section 9 applies.

## 9. Lifetime: launch, background, resume, and port loss

**Cold launch.** With a transport registered and a stored selection, the App first binds and checks the listener, then writes the route for that host. Only then does it let the store's first load (the bootstrap) begin. The wait has a bound, set from measurement. On a bind or check failure, or past the bound, no route is written in this launch, everything uses WebKit's normal path, and diagnostics record it.

**Background.** Two candidates, decided by device cell Q5:

- **B1: keep P bound while suspended.** Apple's archived TN2277 advises against it: a suspended App's listening socket can keep accepting in the kernel with nobody serving it, and its resources can be reclaimed.
- **B2: close the listener on background, and re-bind exactly P on `willEnterForeground`,** exclusively, with the ownership check. This is the default candidate, following TN2277. Q5 is still a device gate before it is relied on.
  - While the port is closed, WebKit's connections to P are refused. With failover (section 8) they may take WebKit's normal path.
  - App-owned navigation on resume, such as a push tap or the shell's own reload, waits for the re-bind and check, with a bound.
  - WebKit's own resumed connections cannot be ordered.

Either way, the background closes every direct relay and stops the transport (section 7). Public relays left open may be ended by the system.

**Port loss is an error state, never a steady one** (rule). It covers three cases: P cannot be re-bound exclusively, the ownership check fails, or the listener's socket fails. A route that points at a port the App does not hold is never treated as an accepted state.

**Recovery candidate R1**, in-process and without a relaunch:

1. Close every relay the App holds.
2. Bind a new port P2 with the same check. This starts a new port epoch.
3. Rewrite every live route to P2, with the same hosts.
4. Count recovery as confirmed only when a CONNECT for a routed host arrives on P2 within a bound.

Closing the App's own relays does not prove that WebKit's pool to the old port is gone. If something else now holds P and accepts connections, WebKit may keep connections to it that the App cannot see or close.

The nearest measured analogue is section 2's closed-pool rows, which are not a port change. Cell Q4 must cover the cooperative case, an occupied old port that accepts and stalls new connections, and a pool that survives on an occupied port.

Until R1 is confirmed, the controller is in a **recovery-not-qualified** state:

- direct is not authorized;
- diagnostics record it;
- the shell's load-failure view stays available to the user.

If Q4 shows that R1 cannot recover the occupied-port case, there is no safe in-process fallback. Whether to show a visible recovery, or to find another mechanism, is then a product decision. It is not decided here, and a relaunch is not the accepted answer.

**NetworkProcess restart.** With a constant route and a live listener, a restarted NetworkProcess receives the same route again (WebKit [24046](https://github.com/WebKit/WebKit/pull/24046)). On releases without that fix, the route can be lost, and the store then uses WebKit's normal path. Restarting the NetworkProcess is a test instrument only, never a shipping mechanism.

## 10. Bridge

The contract is in [`ios-app.md`](ios-app.md) section 4.

- **`direct.status()`** answers `{preferred, sequence}`: the backend that new connections from the calling document will try first. It is never a claim about where an existing connection runs.
- **`direct.transportChanged({preferred, sequence, closed})`** is emitted:
  - when `preferred` changes, with `closed: false`;
  - once for each path generation in which the listener closed at least one relay. It comes after the close, with `closed: true`.

**The trade-off in `closed`.** `closed` is aggregate. Neither the shell nor the Web UI can tell which of the Web UI's streams ran on the lost path, because over HTTP/2 a stream does not know its connection and the listener does not know the Web UI's streams.

So on `closed` the Web UI reconnects only what is safe to reconnect: the SSE stream, which catches up, and terminals that reattach without loss. They are reconnected at once, even if they were healthy on the public backend. That costs one catch-up read and one reattach, and loses nothing.

It does not proactively touch anything that could lose state or repeat an action:

- plain shells;
- uploads;
- requests in flight.

These react only to their own connection's error. The event adds no retry of any request.

A preference change alone triggers nothing. Old and new backends coexist.

## 11. Tests and qualification

**Automated, in `ios-shell` on the simulator, with stand-ins.** These are App-logic tests; they do not qualify WebKit:

- the listener table in section 4, against real loopback sockets;
- exclusive bind, the ownership check, and a stand-in that occupies P;
- backend choice: the direct stand-in failing at open, refusing, or stalling before `200`; the hedge; early bytes held and never forwarded to the loser; the early-byte bound; a backend failing after `200` closes the relay with no redial;
- path generations: each row of section 7 closes exactly the relays it should, and a late report for an older selection or generation changes nothing;
- authorization: a host of an ended selection, an earlier account, or an earlier store never gets the direct backend; an account transition closes relays of both backends;
- the route owner: written once per host and never cleared, counted at WebKit's real setter, and nothing without a transport;
- the bridge: emission rules, `sequence`, duplicates, and origin filtering.

**WebKit behaviour.** It is qualified by the measurement harness in `packaging/webkit-proxy-change/`, not by App tests. The cells this design needs:

| Cell | Question | Where |
| --- | --- | --- |
| Q1 | A new named store, routed before any load, after a relaunch with the App's real store: HTTP/1.1, HTTP/2, navigation, `fetch`, SSE, WebSocket | Simulator 17.0 and newest; device |
| Q2 | A store that has loaded the control plane: a route for the instance host added before its first load, used? | Simulator 17.0 and newest; device |
| Q3 | Within that store, a second host added (union) before its first load, with the first host's pool alive: used, and the first host unaffected? | Same |
| Q4 | A route changed from P to P2: do new connections go to P2? Two cases: every connection to P closed cooperatively, and P held by another process that accepts and stalls | Same |
| Q5 | Suspension: B1 versus B2; does the kernel accept while suspended; is P free on resume; how often does re-binding P fail | Device |
| Q6 | `allowFailover`: absent, refusing, and stalling listener for navigation, HTTP/2, and WebSocket; stickiness; proxy-only and raw-TCP-blocked networks | Simulator, then device |
| Q7 | Loss detection and takeover by the public backend, end to end: Wi-Fi to cellular, cellular to Wi-Fi, blackhole, direct absent, refused, stalled. The 8–17 s detection measured with a desktop client against the instance's 6 s idle timeout and 1.5 s keep-alives is not a device bound. | Device |
| Q8 | No replay: authoritative per-request IDs counted at the instance for POST, upload, and terminal input across a path loss, a refusal, and a stall. The App's own no-redial rule does not prove that WebKit never retries. | Device |

**Device matrix (A5).** On an iPhone at the App's minimum, iOS 17.0 (or the lowest 17.x that can be held, recorded as such), and on one at the current release, against a real instance with a real certificate:

- TLS over HTTP/1.1 and HTTP/2, SSE, WebSocket;
- direct absent, refused, stalled, and blackholed;
- Wi-Fi to cellular and back;
- background and resume, with B1 and B2;
- a stale or occupied port, and a NetworkProcess restart (test instrument);
- no replay (Q8);
- cookies, passkeys, and remote authentication unchanged on every path;
- and the Phase 1 list (Local Network permission states, AP isolation, account and instance switch).

The first build that uses a transport goes to the owner first; enabling it for any instance is the owner's decision.

## 12. The direct transport (A1, A2)

What the transport owes the listener:

- `select(target, report:)`, `deselect()`, `pathDidFail(for:)`, and `openStream(for:)`, as in #3537;
- reports of `usable(generation)`, `verifying` (after a network change, before its probe), and `lost`.

Its rules:

- **The binary.** Link only the gated `IrohLib` artifact whose pin in `packaging/iroh-apple/release.json` is `published`, with a matching checksum. Today the pin is `pending` (tag `iroh-apple-v1.2.0-1`, iroh 1.4.1, deployment target 17.0), so no App build links it. The pin belongs to the gate lane.
- **Nothing exists before a `ready` rendezvous answer:** no endpoint, no UDP socket, and therefore no Local Network prompt. The endpoint uses the minimal preset: no relay, no discovery, a key generated per launch.
- **Rendezvous.** Native code fetches it with a same-origin request on the account store (D3). The answer never crosses the bridge. The request is an ordinary instance request. It reaches the listener like any other, and goes to the public backend because direct is not authorized before the answer (rendezvous contract, section 5).
- **Probe.** Open a stream and complete a CONNECT within a short bound (1 second proposed). It is used for the first `usable`, after a network change (`verifying`), and after `pathDidFail`.
- **Loss.** `lost` at once when the transport's connection closes or a stream cannot open. A dead connection closes through the instance's 6 s idle timeout with 1.5 s keep-alives; how fast that is on a device is cell Q7. The App adds no traffic counter of its own.
- **Cancellation.** `openStream` must honour cancellation promptly: the listener's open bound is cooperative. This is tested against the real transport, not only the stand-in.
- **Interoperability.** It is tested against the published instance sidecar before either side's iroh pin changes.

Open-source notices for iroh are shipped by the licenses work, a separate task, before any build that links it.

## 13. Open for the iOS owner

1. **D3, the rendezvous fetch.** A fixed script in the runtime primary's `WKContentWorld.defaultClient`, or a second hidden transport on the instance origin. We would start with the first.
2. **D4, Local Network permission,** and **D5, the minimum iOS version:** unchanged from the Phase 1 plan.
3. **The cold-launch wait** (sections 5 and 9). The store's first load waits, with a short bound, for the listener. Only builds with a registered transport do this.
4. **Admission while the coordinator is busy.** `isCurrentRuntime` is false during a push-management request, so a `usable` report then is refused once, and the transport reports again.
5. **A navigation in flight when the direct path is lost** ends as a failed load, and Retry loads over the public backend. Should the shell reload once by itself?

## 14. Pull requests

| PR | Content | Status |
| --- | --- | --- |
| #3530 | This document, the bridge contract, and the Phase 1 and parent-plan rows that describe the App | Draft until reviewed |
| #3537 | Swift: listener, backends, routes, controller, bridge. Inactive without a transport. | Draft; reworked against this contract after #3530 lands |
| #3532 | Web UI: reconnect on `closed` | Draft; reworked after #3530 lands |
| Transport (A1, A2) | iroh, rendezvous, probe | After the iroh pin is `published` and the owner approves the first transport build |
| Device validation (A5) | Section 11 | The iOS owner or the owner |
