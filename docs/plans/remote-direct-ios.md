# Remote direct paths: the iOS App side

Status: **Design, rewritten on 2026-11-12 for a persistent local proxy.** The owner chose this architecture after the WebKit measurement in section 2. It replaces the earlier design, in which the App switched WebKit between the tunnel and the direct path by changing the data store's proxy configuration. #3532 (Web UI) is merged against this design; #3537 (Swift) implements it, inactive without a transport, and is in review. The iOS owner's review is requested after the fact, as before; the open items in section 13 stay his. This work does not raise the App's minimum iOS version (17.0), adds no capability or entitlement, and does not touch signing, TestFlight, or the notification service extension.

Statements about behaviour below are of three kinds, and say which: **measured** (with where), **candidate** (the design's choice, gated by a named qualification cell in section 11), or **rule** (what the code must do).

Parent plans: [`remote-direct-phase1.md`](remote-direct-phase1.md) (rows A1 to A5, I4), [`remote-direct-ingress.md`](remote-direct-ingress.md) (stream framing), [`remote-direct-rendezvous.md`](remote-direct-rendezvous.md) (what a client must do). Bridge contract: [`ios-app.md`](ios-app.md) section 4.

## 1. What the App does

The App keeps loading `https://<slug>-app.avibe.bot`, on the same account data store, with the same cookies, cache, and passkeys. WebKit's connections for the instance's exact host go to one loopback listener inside the App, by an HTTP CONNECT proxy route that WebKit holds for the data store. The App never terminates or reads TLS: it relays opaque bytes, and WebKit validates the certificate for the instance's hostname as it does today. Where WebKit's TLS session ends depends on the backend. For each new CONNECT the listener chooses one:

- **direct**: a stream over the App's direct transport to the instance's strict ingress. WebKit's TLS session ends at that ingress, on the instance's machine, with the instance's own certificate for the hostname.
- **public**: the App's own TCP connection to a public address of the same exact host on port 443. WebKit's TLS session ends at the ordinary public endpoint for that hostname. In Avibe's deployment that is the Cloudflare edge, and the edge's onward tunnel to the instance is a separate hop.

Both paths therefore share the hostname WebKit validates and the instance's own remote authentication for requests that reach it. They do not share the TLS endpoint or the certificate material.

```text
WKWebView on the account data store (runtime, child windows, hidden transports)
  -> route (never cleared or moved; changed only to add a host): CONNECT proxy 127.0.0.1:P, one P per process, matchDomains [exact instance hosts]
  -> App listener on 127.0.0.1:P   CONNECT <allowed host>:443 only
       -> direct backend           transport stream; canonical CONNECT head; the instance's status; then bytes
       -> public backend           TCP to <same host>:443; then bytes
  WebKit's TLS for <host> ends at the strict ingress (direct) or at the public endpoint (public); the App sees only ciphertext
```

When the direct path is lost, the listener closes the connections it carried on that path. WebKit then opens new connections, and the listener sends them to the public backend. Leaving home Wi-Fi therefore recovers over Cloudflare without killing or relaunching the App. That is the owner's required outcome; section 11 lists what must still be measured before it is a fact on devices.

What the App never does: change or remove a route to switch paths; move, migrate, or replay a connection; accept a CONNECT for any authority but an allowed instance host on port 443; read, parse, or terminate TLS. Section 6 states what the last rule costs on the public backend.

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
| Web UI | Reconnects the SSE stream and reattachable terminals once for each new closure the shell reports. | I4 |

**Without a registered transport, nothing of this runs**: no path controller and no listener are created, no route is written, the `direct` capability is absent and `direct.status()` is unsupported, no event fires, and a runtime opens as before (rule). The first build that registers a transport is the owner's to approve.

## 4. The listener and its port

- **One listener per App process** (rule), bound to `127.0.0.1`. It is not bound until a transport is registered and a route is about to be written.
- **One port per process.** The listener's port P is chosen by the system at the first bind and kept for the life of the process (section 9). Every route points at P. No route is ever moved to another port: moving it was measured not to recover WebKit's existing connections (cell Q4, section 2).
- **Exclusive binding** (rule). P is bound without `SO_REUSEPORT`, so no other socket can share it while the listener holds it.
- **The ownership check** (rule). After every bind, the App connects to P and sends a random nonce made for this launch; only the listener's own accept loop echoes it. This is a liveness and correlation check, to catch a listener that is not serving or a port that is not ours. It is not cryptographic authentication, and a hostile local process is not assumed unable to interfere with it. A failed check before any route exists is an initial bind failure; after one, it is a failed attempt, and the port is bound and checked again (section 9). The check connects with `preferNoProxies`, so a system proxy cannot carry it somewhere else. It is made after the App's own exclusive bind, so only this listener accepts on the port then: a check that goes unanswered is this listener slow to answer or its spare connection taken (a failed attempt, step `ownership`), never a stranger, who shows as the bind failing in use. On a re-bind the check's window is what is left of the attempt's bound after the bind, and no second deadline runs beside it, so the record says which part failed.
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

**What a CONNECT does not tell the listener.** A CONNECT names a host. It does not say which data store, WebView, app, or process sent it, nor which account's lease it belongs to. The listener therefore cannot authenticate its caller, and nothing here claims it can. The bytes after the CONNECT are opaque to it, so it also cannot check which name the caller's TLS session or HTTP requests actually carry.

- **Any process on the phone** can connect to `127.0.0.1:P` and is served like WebKit. It can take the 64 connection slots, which push WebKit's new connections to failure or failover.
- **While direct is authorized for a host,** any caller's CONNECT for that host is indistinguishable from WebKit's. It rides this App's admitted direct peer and counts against that peer's limits at the instance. The strict ingress then requires that host in TLS SNI and HTTP `Host`, and the instance's remote authentication decides every request.
- **On the public backend,** the caller's bytes go to the public endpoint for the allowed host. Section 6 describes what that exposes.
- **Older data stores of this App are no different.** What the design enforces is narrower than caller identity, and section 7 states it: ended tags and late reports never authorize direct, relays close at the boundaries listed there, and a host that is not the current selection's gets only the public backend. When the same host becomes current again, a CONNECT for it from an old store, or from another process, cannot be told apart and may use direct. Remote authentication is still required for every request that reaches the instance.

These are exposures of the design. Section 11 tests what the App enforces; it does not test store or caller attribution, which a CONNECT cannot carry.

## 5. Routes: writing them, and when

The **route owner** is the only writer of `proxyConfigurations` (rule). A route is one `ProxyConfiguration(httpCONNECTProxy: 127.0.0.1:P)`, with `matchDomains` set to exact instance hosts (no wildcard, no suffix of a shared zone) and `allowFailover` as in section 8.

Rules:

- **A route points at P.** It changes only when a host is added. It is never moved to another port (section 9).
- **A store's host list only grows while the store lives** (candidate; whether adding a host takes effect is cells Q2 and Q3). No host is removed and no route is cleared: clearing is unreliable (section 2). A host the user no longer uses is relayed only to its public origin (section 6), never on the direct backend. The host list lives and dies with the store and the process, and is bounded by the allowlist.
- **A host is added before the first load of that host in that store.** The listener is bound and checked first. The App's own load waits for both, with a bound; past it, the load goes ahead without a route, on WebKit's normal path. The bound is a proposal, set from measurement.
- **A failed or ignored write that adds a host leaves that host on WebKit's normal path.** That path is the instance's public origin. Every route points at the same P, so such a write, applied to some connections and not others, sends each connection either to the listener or to the normal path, never to a third place.

| When | What is written | Status |
| --- | --- | --- |
| Cold launch, signed in, with a stored instance selection | The account store's route with that host, before the store's first load of anything | Candidate, cell Q1. Measured analogue: a new named store, created in a running process and routed before its first load (section 2). |
| Sign-in, sign-out, account switch (the store rotates) | Nothing on the new store until an instance is selected | Rule |
| The first instance selected on a store that has already loaded the control plane | The host is added before the runtime's first load | Candidate, **open cell Q2** |
| Instance switch to a host this store has not loaded | The host is added (union) before its first load | Candidate, **open cell Q3** |
| Instance switch to a host already in the route | Nothing | As the cold case |

The measured analogue is in a running process, so nothing here is limited to a process cold start. Q2 and Q3 decide whether a store that is already in use can take a new host.

**Whether WebKit uses a route is not observed in the field.** No new CONNECT can mean any of several things:

- the request was served from cache;
- a service worker answered it (`cache: "no-store"` does not get past a controlling service worker);
- the load failed;
- it reused a tunnel already open;
- WebKit did not apply the route.

The App cannot attribute a CONNECT to a data store either. So the shipping contract treats bypass by WebKit as **unobserved**. Diagnostics may count CONNECTs per host, but nothing disables direct, or changes any other decision, because a CONNECT was absent.

A probe that could show a bypass would need a request from the same data store, outside any service worker's control, correlated with the network. Designing and qualifying such a probe is a possible future candidate, outside this contract.

If Q2 or Q3 turn out negative on a runtime, sign-in and newly used instances stay on the normal path for the rest of the process on that runtime. The measurement, not a field signal, is what makes that shortfall known. Any workaround (a second store, a broader match) is outside this contract and needs a design review.

## 6. Choosing a backend for each CONNECT

**The direct backend is authorized per CONNECT, from live state only.** It may be chosen only while all of these hold:

- the CONNECT host is the **current** selection's host;
- the selection's store has a route listing that host;
- every tag (section 7) is current, including the account lease;
- the transport has reported the path `usable` for those tags, and nothing has suspended that since.

Otherwise the CONNECT goes to the public backend. Every condition is re-read when a CONNECT arrives. When a selection or an account lease ends, direct stops being chosen in the same main-actor turn, and a report or tag from it can never authorize direct again. A host that is not the current selection's gets only the public backend, which adds no credential and restores no authority. When the same host is selected again, direct is authorized for it again, for any caller (section 4).

**Before any byte is forwarded, a failure is not yet a failure of the tunnel.**

1. **Early bytes are held.** Bytes WebKit sends after its CONNECT head (it may send its ClientHello before the answer) are held in a buffer of at most 16 KiB. Nothing is forwarded to any backend until one has been selected. A CONNECT whose early bytes exceed the buffer is closed. The bound counts every byte delivered before the commit point (a backend chosen, the `200` not yet written), however it was read; a byte delivered after it is the tunnel's.
2. **Direct first, when it is authorized.** The listener opens a direct stream and writes the canonical head from the ingress contract, `CONNECT <host>:443 HTTP/1.1` with `Host: <host>:443` (not WebKit's bytes). It then reads the instance's status: 4 KiB at most, within a bound.
3. **A hedge.** If the direct answer has not arrived after a hedge delay, the listener also starts the public dial. The first ready backend is selected: a direct `200`, or an established public connection. The other is cancelled and closed. A candidate whose selection or path generation goes stale before it is selected is cancelled, and counts as not ready.
4. **Fallback.** A direct stream that cannot open, a refusal, or a missed bound leaves the CONNECT to the public backend, and the transport is told (`pathDidFail`).
5. **No backend ready** within its bound: WebKit gets `502`, or the instance's refusal status.

**Stickiness** (decision, 2026-11-20, after the owner's trial). While the App's own direct connection runs straight to an IPv4 or IPv6 address (not through the relay), a CONNECT's direct candidate runs alone: the public dial starts only when the direct attempt fails or after **1.5 s** (`stickySeconds`), instead of after the hedge delay. So once a page is on a direct path, its new connections stay there instead of racing the public origin from 0.5 s on. The bound keeps a dead path cheap: a sticky direct attempt, opened and answered, has 1.5 s in all. One that gets no answer in that time is a **miss**: the public dial starts at once and plans hedge again until a direct candidate wins a hedged race, but the path is *not* reported as failed. The miss is decided by the bound, not by which candidate's result comes in first: the public dial starts at that same bound and may be ready before the direct attempt has ended, and the miss is recorded all the same. A miss starts no probe and closes nothing, so what the path still carries is left to the transport's own loss detection (section 7). A direct candidate that fails outright (the stream does not open, the instance refuses, the stream errors) is a failure of the path as before: the public dial starts at once, stickiness is dropped, and the transport is told (`pathDidFail`, which probes the path). Stickiness also ends when the App's path changes away from a direct address, when the path is being verified or is lost, at a background, and at a selection or account change. Through the relay, plans always hedge.

**The race record.** For each CONNECT the App keeps, for diagnosis, how long the direct candidate and the public origin each took to be ready (or `failed`, `timeout` for a sticky miss, `cancelled`, or `none` when not part of the race), which won, the App's path then, and whether the plan was sticky: the last five, in `direct.status()` as `connections`, and one log line each. Timings and families only.

**What the page shows** (decision, Product 2026-11-19, English 2026-11-20). The page names the path in use with four names: Cloudflare 隧道, 中继, 打洞直连, and 局域网直连 (in English: Cloudflare tunnel, Relay, Direct (hole punching), and Direct (local network)). Whether the page is on the direct ingress comes from its own stream (`via` on the `/api/events` handshake); which kind of path from the App (`direct.status()`): the relay, or an IPv4 or IPv6 address. A relayed path is never called direct. In the App, Settings › Remote access says 「本设备正在使用：…」: the Cloudflare tunnel (with 「中继可用，但更慢」 while the relay is up and the page is still on Cloudflare), the relay, or LAN or hole-punched direct with the family. It adds the path's round-trip time when the App has measured one. The App tells LAN from hole-punched only by the class of the selected path's remote address: private, shared address space (100.64/10, which Tailscale uses), unique-local, or link-local is LAN, anything else is hole-punched. A class and a number leave the App (`path_scope`, `rtt_ms`), never the address. A private address reached over a VPN, or two devices behind one carrier's NAT, therefore read as LAN, and a public IPv6 address reached with no punching reads as hole-punched; an App that does not report the class says plain 「直连」. Only an App that keeps its path current (`direct` version 3) names a path; in any App the page's own line 「本页当前连接：…」 is not shown, since only the App can tell the relay from an address. The tag beside the instance name says 「直连」 only on an IPv4 or IPv6 path and 「中继」 through the relay; in a browser it shows nothing. While the page is on the direct ingress, Cloudflare's quality and network path are shown as the fallback route (「备用路径：Cloudflare 隧道」), folded. The race line is unchanged.

**The bounds.** The hedge delay, the sticky bound, the public connect bound, and the direct open and answer bounds are proposals, set from Q7 measurements. #3537's current values (2 s to open, 3 s for the answer) are a starting point, not a published guarantee.

**After the first forwarded byte or the `200`, the tunnel belongs to its backend.** Selection happens before either. If that backend then fails, the relay is closed and WebKit sees a closed connection. The listener never redials, never moves a tunnel to another backend, and never replays a byte. A healthy relay is never closed to change backends, including when direct comes back: only new connections prefer it.

**The public backend** is a native TCP connection (`NWConnection`) to a public address of the allowed host on port 443. That is the same public endpoint WebKit would reach without the route: in Avibe's deployment the Cloudflare edge, which carries the connection on to the instance over its tunnel as a separate hop. The backend does not assume Cloudflare.

- **Target:** exactly the CONNECT's host, port 443. That host must be an allowlisted instance host taken from the control plane's instance list: not the control plane's host, not any other host, and not an IP literal.
- **Resolution:** the system resolver. Every resolved address is checked, and the socket connects only to an address that is not loopback, unspecified, link-local, multicast, or private. A local-network path is the direct backend's job, not this one's.
- **Proxy settings:** no App proxy configuration on its parameters, so it cannot be routed back to the App's own listener. What the system's proxy settings do to it, and what happens on networks that allow only an HTTP proxy or block raw TCP, is qualified (cell Q6), not assumed from a parameter's name.
- **Cancellation of a candidate:** a public dial that has not been selected is cancelled when the WebKit side closes, when the listener stops, when its selection or path generation is fenced, and when the network changes before it is established.
- **A committed public relay** follows its own lifecycle in section 7. A direct-path event never closes it.

It carries WebKit's TLS bytes unchanged.

**The exposure of an opaque relay to a shared public endpoint.** The socket is constrained: a vetted public address of an allowlisted host, on port 443, never an arbitrary address or port. What travels inside it is not. The listener cannot read the caller's TLS SNI or HTTP `Host`, so it cannot assert that the encrypted session names the allowed host.

A public endpoint can serve many hostnames on the same addresses; a Cloudflare edge serves many. A local process that connects to the listener could therefore present another hostname's TLS identity at that edge, and its requests would not reach this instance or its authentication.

- **WebKit always presents the allowed host,** so this concerns other local callers.
- **The direct backend is not affected:** the strict ingress refuses any SNI or `Host` but the instance's.
- **What this is not:** it is not a demonstrated arbitrary internet proxy, nor an authentication bypass for the instance, and it has not been probed against production.
- **Product decision, 2026-11-12:** this is accepted as a documented limitation of the public backend. It is not an Avibe authentication boundary: no proxy authentication, credential, outer TLS, or TLS parsing or termination is added for it. Normal remote authentication and the strict direct ingress stay required. The decision covers only this exposure: the listener still does not attest its caller or the data store, and the resource limits (section 4) and the stale-caller limits (section 7) stay as documented. It does not clear the first build that uses a transport, which remains the owner's to approve.
- **Rejected as fixes:** terminating or parsing TLS, adding credentials, or changing authentication, each of which would change the design.

**Diagnostics** count `direct` and `public` relays, and CONNECTs per host. A connection WebKit makes around the listener is not seen, and is reported as unobserved, not as zero.

## 7. Tags, path generations, and invalidation

A **selection** is one opened runtime, between the App's `openRuntime` and `stopRuntime` (and the registration exit that returns to the picker). Its tags:

- the selection number;
- account and instance, and the account generation, from `MobileRuntimeContext`;
- the data-store identifier;
- the instance authority;
- the rendezvous generation.

A transport report counts only for the selection it was made for, and is dropped after that selection ends. Before direct is authorized, the controller checks the lease with `MobileSessionCoordinator.isCurrentRuntime`. The App's open path (the open, route preparation, the release of a held open, and WebView registration) asks `keepsSelectedRuntime` instead, so it stands across a selection-keeping request such as the push update an open starts. Within a selection, a rendezvous generation that a newer one replaced is never accepted again.

**Every relay records its backend, its selection, and the path generation it was opened on.** The path generation advances on each event below.

- **Advancing the generation does two things only.** It cancels uncommitted candidates of either backend (section 6), and it makes late reports, callbacks and candidates from an older selection or generation ineffective.
- **It closes no committed relay by itself.** A committed direct or public relay is closed only where the table below says Closed.
- **A relay's own end is separate.** A relay whose own connection ends, by EOF, failure or a half-close timeout, terminates as usual, whatever generation it was opened on. An old opening generation alone never marks a healthy relay as stale.

| Event | New CONNECTs | Committed direct relays | Committed public relays |
| --- | --- | --- | --- |
| The transport reports the path `lost` (its connection closed or failed) | Public | Closed | Kept |
| A network change callback | Public, until the transport validates the path again | Kept | Kept |
| Validation after a network change fails, or runs past its bound | Public | Closed | Kept |
| Validation succeeds on the same transport connection | Direct again | Kept: healthy, and never reported as moved | Kept |
| The rendezvous generation changes (a new instance endpoint key or addresses) | Public until `usable` on the new one | Closed | Kept |
| The selection ends (instance switch, picker, registration exit) | Public for that host | Closed | Kept |
| The App enters the background | Public | Closed, and the transport stops | Kept by the App (the system may end them) |
| An account transition (sign-in, sign-out, switch, store rotation) | Public | Closed | **Closed**: nothing of the old account is kept |
| A failure of the listener (section 9) | Public, until the same port is bound and checked again; nothing is rewritten | Kept (a failure closes no relay) | Kept |
| The listener stops for good (no transport any more, process exit) | Not accepted | Closed | Closed |
| The transport's connection changes its kind of path (through the relay, or straight to an IPv4 or IPv6 address) | Direct, as before; sticky on a direct address (section 6) | Kept: the streams continue on the same connection | Kept |
| A page asks to hand over (`direct.handover`, decision 2026-11-19), while the path is usable for its host and the caller is the sole live document of its data store with that host loaded | Direct | Kept | **Closed if quiet**: those for that host that carried no bytes either way for 1 s, waiting up to 3 s for that; busy ones kept |

**A network callback alone is not proof of loss** (rule). It changes only what new connections prefer, until the transport validates the path.

**An update of the system's path that changes nothing is not a network change** (decision, 2026-11-20, after the owner's trial). The system reports its path again on its own, seconds apart on some networks; the instance saw one probe and one rendezvous request for each, and every one suspended a working path for the probe. The App now compares what it depends on and ignores an update that leaves it the same. Compared: whether there is a path; the path's interfaces by name and kind, in the system's order of preference; whether it carries IPv4, IPv6 and DNS; its gateways; and the addresses of those interfaces and of every tunnel interface (`utun`, `ipsec`, `ppp`, `tun`, `tap`) that has a routable address. So Wi-Fi to cellular and back, an address added to or removed from the same interface, and a VPN turned on or off are all still changes. Gateways and addresses are kept only as SHA-256 digests, in memory; no address is kept, logged, or sent. Not compared: whether the path is expensive or constrained, which does not change what the App's own connections can reach. A change this misses is still caught by the transport's own loss detection. `direct.status()` counts both kinds (`counts.path_updates`, `counts.path_updates_ignored`; docs/plans/ios-app.md).

**A committed public relay is closed only for these reasons:**

- its own connection fails, or reports itself not viable for longer than a bound set from measurement;
- a page's hand-over, only while quiet (the row above);
- an account transition;
- port recovery;
- the listener stopping for good.

A loss of the direct path or a selection change never closes it. So a healthy upload or terminal on the public backend survives an unrelated direct loss.

**What is never lost, and what a transport cannot promise** (decision, 2026-11-20).

1. **A request issued during a path change goes to whichever path is ready.** A change of the kind of path advances no generation and fences no plan, so a CONNECT planned across it is answered by the first ready candidate: the direct stream, or the public origin after the hedge (or after the sticky bound). It is never dropped for the change.
2. **Relay to a direct address is a migration inside one connection.** iroh keeps one QUIC connection to the instance's endpoint and moves it between paths (relay, IPv4, IPv6); the App reads the selected path and its changes from `Connection.paths()` / `watchPaths` of the pinned binding. Streams in flight continue; nothing is closed or reopened for it, on either side.
3. **Committed bytes are never replayed.** A relay is never redialled and nothing is sent twice (section 6). What bounds a failure: a *new* connection on a dead direct path falls back to the public origin within the sticky bound, 1.5 s, and that miss alone never ends the path or anything it carries. A request *already on* a direct stream whose path dies fails when the transport reports the path lost and the App closes that relay (a closure); that takes as long as the transport's own detection (the instance's 6 s idle timeout with 1.5 s keep-alives; cell Q7), not 1.5 s. The failure is visible to the sender as a failed request, and the sender retries with its own idempotency key if it has one. Product accepted that bound on 2026-11-20; no liveness probe is added. For diagnosis the App records each close of committed direct relays for a path that is gone (`losses` in `direct.status()`, the last five, and one log line), with its cause: `lost` (the transport reported the path lost), `generation_replaced` (a newer rendezvous answer), or `background`; and how many relays it closed, how long each had carried no byte, and the path they ran on. Only `lost` speaks about a cut request. The listener cannot see requests: after a `lost`, a short time since the last byte means bytes were still moving when the path went, so a request was probably cut. On a path that died silently the time since the last byte is at least the transport's detection time, whatever was waiting on it. The records are kept across a background and cleared when the selection or the account changes.
4. **The App's own requests.** Through the account's WebKit data store to the instance host, and so through the listener and either backend: the runtime page and its child windows, the rendezvous request, the session check and the cached-page capture (run in the runtime's WebView), the share sheet's reads, and the voice capture client's WebView. On the data store but never routed, because the host is the control plane's: sign-in, account, and push management (`MobileAuthTransport`), and the account pages (`AvibePage`). On their own `URLSession`, and so never on the direct path: the Health device client and its background uploader. The public backend itself is an `NWConnection` with no proxy.

**The hand-over** (decision, 2026-11-19, after the owner's trial; revised 2026-11-20). With an App that reports its path (`direct` version 3) the page hands over only while the App's path is a direct address, never through the relay: a stream moved onto the relay loses the race to the public origin, and nothing moved it again when the direct address came. When the App's path goes from the relay to a direct address the page is armed once more for that usable stretch, and that one hand-over is not held to the 30 s spacing; every other trigger is. A path that becomes usable does not move a page by itself: WebKit keeps reusing the pooled public connection the page loaded on (section 2), so a rebuilt stream stays on it. So the page asks. Once per usable stretch, and at most every 30 s, the page checks that nothing of it uses the network: no same-origin `fetch` waiting for its answer or still delivering its body (bodies idle for 10 s no longer count), no WebSocket open, and no same-origin iframe (a Show Page runs its own requests). If that is not true within 10 s it skips, and the next resume or change tries again. It also hands over a stream whose handshake says it did not come in directly (`via`) while the App prefers direct, as after a first load. Then it closes its live stream, holds its new same-origin requests, and calls `direct.handover()`; the held requests are sent once the App has answered (at most a few seconds). WebKit's cancel of that stream crosses the relay right after, so the App waits, at most 3 s, for that host's committed public relays to have carried nothing for 1 s, closes those that have, keeps busy ones, and answers `{closed, kept}`. The page then opens its stream again, over a new connection that takes the direct backend; it re-arms when the App skipped or kept something. **It comes back by itself** (decision, 2026-11-20): one public win gives WebKit an HTTP/2 connection it reuses for everything, so a page that fell back would otherwise stay there. An attempt held off by the spacing or by a busy page is made again when the spacing ends, but only while the live stream itself says it came in another way (`via`); a stream that has not said is left alone. With an App on a direct address, such a stream also arms the hand-over again. The bounds, so a page cannot flap: never sooner than the 30 s spacing (the one upgrade aside); at most 6 hand-overs on one path of the App; and none after 3 in a row whose stream still came in another way. The counts start again when the App's kind of path changes (the relay, IPv4, IPv6), not when its preference only goes to the public path and comes back on the same kind, and after 10 minutes without a hand-over. A stream that says it came in directly is never handed over. Limits: the App acts only for the sole live document of the store with that host loaded, and skips when another WebView of the store has it, or when it cannot tell; subresources the browser loads by itself (images, scripts, styles) are not counted by the page, and a relay carrying one of them is kept only while it moves bytes; a relay busy for another reason is kept, and the stream may then stay public until the next try. Nothing is replayed. The loss path is unchanged: a closure rebuilds the stream onto the public path.

Closing a relay closes WebKit's connection to the listener. Over HTTP/2 one connection carries many requests, so every request on a lost direct connection fails with it. That is the cost of a lost path, not of the closing. Nothing replays them.

**Detection is not a measured bound.** How fast "lost" arrives is the transport's (section 12). The 8 to 17 s measured with a desktop iroh client, against the instance's 6 s idle timeout and 1.5 s keep-alives, is not a device bound and not an end-to-end failover time (cell Q7).

## 8. Failover by WebKit (candidate safety net)

`allowFailover = true` is the candidate (it was `false` in the earlier design). It is a safety net under the native public backend, not a replacement for it and not a guarantee.

- **When it can engage.** The listener never refuses an allowed host while it can dial the public origin itself. So WebKit's own failover can engage only when the listener cannot be reached (for example while the port is closed for a re-bind), or when both backends failed.
- **What is measured:** only HTTP/1.1 fetch on the 26.5 simulator, with nothing listening or with a refusing proxy (section 2). A stalling proxy did not fail over within the window.
- **What is not measured** (cell Q6): navigation, HTTP/2, WebSocket, iOS 17, networks that allow only an HTTP proxy, and networks that block raw TCP.
- **Stickiness is measured, not assumed.** Q6 measures whether WebKit, after failing over, keeps bypassing the route, and for how long. In the field it is unobserved (section 5). A bypass by WebKit is outside the listener's sight and is never counted as a `public` relay.
- **Fallback position.** If Q6 shows failover is harmful or sticky, the value becomes `false`. A listener that cannot be reached then fails those loads until it is re-bound (section 9).

## 9. Lifetime: launch, background, resume, and the listener's recovery

**Cold launch.** With a transport registered and a stored selection, the App first binds and checks the listener, then writes the route for that host. Only then does it let the store's first load (the bootstrap) begin. The wait has a bound, set from measurement. On a bind or check failure, or past the bound, no route is written in this launch, everything uses WebKit's normal path, and diagnostics record it.

**Background.** Two candidates, decided by device cell Q5:

- **B1: keep P bound while suspended.** Apple's archived TN2277 advises against it: a suspended App's listening socket can keep accepting in the kernel with nobody serving it, and its resources can be reclaimed.
- **B2: close the listening socket on background, and re-bind exactly P on `willEnterForeground`,** exclusively, with the ownership check. Closing the listening socket does not close accepted connections. This is the default candidate, following TN2277. Q5 is still a device gate before it is relied on.
  - While the port is closed, WebKit's connections to P are refused. With failover (section 8) they may take WebKit's normal path.
  - App-owned loads of a routed host (an open, the shell's Retry, the WebView's own first load or reload after its content process ended) are held while the listener is closed or being bound again, by a synchronous gate, not a timed wait. They are made once the return's first attempt has ended: with the listener serving, or, after a failed attempt, as the recovery below says.
  - WebKit's own resumed connections cannot be ordered.

Either way, the background closes every committed direct relay and stops the transport (section 7). If that closed any relay for a host, one closure for that host is held as pending and published after the listener is ready again on the foreground (section 10). The App keeps committed public relays, though the system may end them while the App is suspended.

**An initial bind failure leaves that host on WebKit's normal path** (decision, 2026-11-24, replacing "ends direct for the process"). If the first bind or its ownership check fails, no route is written: that host loads on the public origin with no interruption, and is excluded on that store, like a host whose listener was not ready in time. No route points at that listener, so it is stopped, and nothing is recovering. The failure is on record (below). The next host opened in the foreground makes a new listener.

**The listener recovers by itself** (decision, 2026-11-24, replacing the terminal listener fault of 2026-11-12; owner report "Connection lost, reopen the app", TestFlight 54). No failure of the listener is terminal. A failure is anything that leaves a bind neither serving nor closed on purpose: a bind error, a bind the system leaves `waiting`, an ownership check this listener did not answer, the attempt's bound (2 s), the foreground watchdog's deadline, or the listening socket failing while it serves. A deliberate close, a stale generation, and a replaced attempt are never failures.

- **Why the same port, and why that is enough.** A route cannot be moved or cleared reliably (section 2, cell Q4), so each store keeps sending its hosts to P. But if the App holds P again and the ownership check proves the listener there is ours, every route is correct again and nothing else has to change. So the recovery binds exactly P, exclusively, and checks it: attempt after attempt, with 0.25, 0.5, 1 and 2 s between them and then every 5 s, for as long as the App is in the foreground. Each attempt closes the socket first. Nothing is bound in the background; the return to the foreground starts a new stay with its own first attempt.
- **What is authorized meanwhile.** Nothing: uncommitted work is revoked in the step that records the failure, no plan offers the direct backend, no route is written, and `direct.status()` says `tunnel`. Relays granted before the failure follow section 7: a failure closes none, and is not a closure. A failure in the foreground keeps the selection, so the transport keeps its connection and direct is preferred again as soon as the listener serves.
- **Loads of a routed host, by what the port may be** (the load gate).
  - *During a return's first attempt* (2 s for the bind and its check; up to the watchdog's 5 s when the background's relay closes are still being issued): `held`, as on every return. In the foreground `held` is always bounded, so a caller may wait it out.
  - *After a failed attempt, the port closed or refusing* (every cause but the one below): `open`. WebKit fails over to its normal path by itself (section 8, measured for a closed and for a refusing port), and the port is bound again underneath.
  - *After a failed attempt, the port possibly someone else's* (the bind found it in use, `EADDRINUSE`, and nothing else: a check that fails after the App's own bind is the App's own listener, and that attempt's failure closes its socket): `fault`, the refusing value. Something accepts there, so WebKit does not fail over, and the host's traffic would go to that process: TLS stays end to end, but the host name and the timing would leak, and the stranger could stall or drop loads. No caller may time out of this value into a load (the voice take's client waits five seconds on `held` and then loads; on `fault` it loads nothing). It is not terminal: this is the one cause a new attempt cannot cure by itself, it is cured when the stranger lets the port go, and the attempts go on until then.
  - *What is known of the port stays known* until an attempt says otherwise: in the background, across a return (whose first attempt is then `fault`, not `held`), and after an attempt that only ran out of time, which found nothing.
- **The background's closure waits for a port that can be reached.** After a failed first attempt on a closed port the pending closure is published at once: a page that reconnects for it fails over. While the port may be someone else's it stays pending, because the page's reconnect would go to the port's holder and a terminal does not try twice; it is published, with the event, by the attempt that serves or finds the port closed.
- **What the shell is told.** `listenerRecovery` is set while a listener that a route points at does not serve in the foreground: when it began (the return, or the failure if the App was in front; a new stay restarts it), the last failure of this stay, and whether the port may be someone else's (the gate is then `fault`). Every change is announced. `retryListenerNow()` starts an attempt at once unless one is running. What the shell shows from these (the page kept, the existing "can't reach" row after 10 s, Retry) is the App side's.
- **The record.** Each failure is kept as its step (`bind`, `waiting`, `ownership`, `deadline`, `socket`; the instance also accepts `foreign`, which the App never sends), the error number if there is one, and how many seconds the App had been away before that stay; no wall time, address, port or identifier. The last five that differ are in `direct.status()` as `listener_faults`, for a host that has a route. The same entries go to the instance once, as `listener_faults` on the next rendezvous request that is answered (remote-direct-rendezvous.md section 2), so its log has them without anyone asking. There each entry carries all three keys, as the instance requires: `code` 0 when there is no number, and 0 seconds when no background came before. That request is made only after the listener serves again, because nothing is selected while it does not.

**The foreground watchdog** (decision, 2026-11-13; a failure like any other since 2026-11-24). One stay in the foreground has one fixed deadline, 5 s on the continuous clock (a conservative proposal, not a measurement), from the start of the first bind or from a return with the listener closed. If the listener is then still neither serving nor failed, it has failed, for the deadline. Attempts inside the stay do not move the deadline; the background ends the stay and is never itself a failure. The deadline is applied by the listener's own authority whenever anything is decided, so nothing that comes after it (a confirmation, a load, a status read) can find the listener still on its first attempt, and a bind that finds the deadline come does not start. The attempts after a failure are bounded each by their own 2 s, not by this deadline. It is a backstop for a running process only: it cannot run while the App is suspended or killed.

**No marker, and no launch without a listener.** The terminal fault wrote a marker that made the next launch run without the path controller. With recovery nothing writes or reads it, and every launch with a transport uses its listener; such a launch removes a marker left by an earlier build, unread.

**Not measured.** The recovery is exercised on the simulator and on macOS with real loopback sockets: a stranger on the port, a serving socket failed as Network reports it, a failed attempt followed by a good one. A bind the system leaves `waiting` could not be produced on loopback: the tests tell the lifecycle what Network tells it for such a bind, while the real bind is under way. What iOS does to a suspended App's port on a device (the case behind the owner's report) is the field check of the next TestFlight build, read from `listener_faults`.

**NetworkProcess restart.** With a constant route and a live listener, a restarted NetworkProcess receives the same route again (WebKit [24046](https://github.com/WebKit/WebKit/pull/24046)). On releases without that fix, the route can be lost, and the store then uses WebKit's normal path. Restarting the NetworkProcess is a test instrument only, never a shipping mechanism.

## 10. Bridge

The contract is in [`ios-app.md`](ios-app.md) section 4. Two kinds of information cross it, and they are kept apart because they are used differently.

- **`preferred`** is the backend that the calling document's **new** connections will try first: `"direct"` or `"tunnel"`. It is never a claim about where an existing connection runs, and only the latest value matters.
- **`closure`** is a counter of closures, a non-negative integer kept per instance host. It starts at 0 and never decreases while the shell process lives.
  - **What counts.** It grows by one for each section 7 event in which the App closed at least one committed relay for that host: a transport `lost`, a failed validation, a rendezvous generation change, or the background. A failure of the listener (section 9) is not a closure.
  - **What does not need counting.** A selection end or an account transition destroys the documents that could act on it, and later documents take a new baseline.
  - **Background closures are published on return.** A closure caused by the background is held as pending. It is added to `closure` only on the return to the foreground, after the listener is ready again (re-bound and checked, section 9). So no document acts on it while new connections cannot reach the listener.
  - **Why it is separate.** Each increase asks the Web UI to act, so an increase must never be lost behind a later preference change.
- **`sequence`** grows with every change of either value in the shell process, and orders `preferred`.

`direct.status()` answers `{preferred, sequence, closure}` for the calling document's origin. `direct.transportChanged({preferred, sequence, closure})` carries the same three fields. It is emitted after the change, whenever `preferred` changes or `closure` grows, to the current runtime's instance-origin documents.

**Resume reconciliation, independent of events.** A suspended document may miss every event, including the state the shell emits once more after the foreground listener is ready. So reconciliation does not depend on any event:

- **Native side.** From the return to the foreground until the listener is ready again, and the pending background closure is published, the shell holds every `direct.status()` answer. If the return's first attempt fails instead, the held answers are given then, as `tunnel`, with the closure published unless the port may be someone else's (section 9); later answers are given at once. A status answer received after a resume therefore already reflects the published closure.
- **Web UI side.** Each document reads `direct.status()` once whenever it becomes visible again (`visibilitychange` to `visible`, and `pageshow` when restored from the back-forward cache). It applies the answer to its existing values: a higher `sequence` updates the preference, and a higher `closure` runs the reconnect action once. It never takes a new baseline on resume; the baseline is taken only once per document, before its first SSE stream or terminal.

This is one read per resume, not polling.

**What the Web UI keeps, per document:**

- **The latest preference.** Apply `preferred` from any status or event whose `sequence` is higher than the highest seen; ignore anything older.
- **The last handled closure.** Before it opens its SSE stream or a terminal, the document reads `direct.status()`, and that `closure` is its baseline. Earlier closures concern connections this document never had. Afterwards, any status or event whose `closure` is higher than the last handled value triggers the reconnect action once, and the new value becomes the last handled one. The comparison is with the closure, never with `sequence`. So a closure is acted on even if its event arrives after a newer preference, arrives twice, or is first seen through a status read.

**The reconnect action.** The closure count is aggregate. Neither the shell nor the Web UI can tell which of the Web UI's streams ran on the closed path: over HTTP/2 a stream does not know its connection, and the listener does not know the Web UI's streams. So the action reconnects, through their existing owners, only what can be reconnected without repeating an action, even if it was healthy on the public backend:

- **The SSE stream** reconnects and catches up.
- **Terminals whose server-side session persists** (tmux-backed) reattach to that session. The client-side view does not survive: scrollback, viewport, search, and selection reset. Input typed while the terminal reconnects is dropped, not buffered or replayed.

It does not touch anything that could lose server-side state or repeat an action: plain shells (whose process survival is not promised), uploads, and requests in flight. Those react only to their own connection's error. The action adds no retry of any request. A change of `preferred` alone triggers nothing. Old and new backends coexist.

### 10a. 「线路测速」: the measurement's own path (`DirectMeasure.swift`)

`direct.measure()` (`ios-app.md` section 4, capability `directMeasure`) is a
diagnosis and nothing else: one warm-up request and five timed HTTPS
`GET /health` round trips on one connection, for the path in use and for the
Cloudflare tunnel, both within 30 s, one measurement at a time in the App.
It makes no plan, takes part in no race, and changes no path, choice,
stickiness, closure or count, under 「仅直连和中继」 too.

- **The path in use** is measured through a loopback port of its own, open
  for that measurement only. The port serves `CONNECT <the instance's
  host>:443` and nothing else, over the App's admitted direct or relay link
  (`DirectPathController.measurementLink`) and over nothing else: it has no
  way to the public origin. An ephemeral URLSession of its own is routed to
  it with no failover and one connection per host; TLS is the system's, end
  to end to the instance, with the system's check of the instance's name.
  With no admitted link there is no such leg (`current: null`).
- **The port is bounded** apart from the App's listener, since anything on
  the device can reach it while a measurement runs. It holds at most four
  connections at once, whatever they have sent; one beyond that is closed
  unread, with no task of its own. It opens one stream over the link in its
  whole life, for the first request for its host that reaches it. The port
  cannot tell who asks: another process on the device that asks first gets
  that stream, to the instance's ingress, where the App's own listener
  would have taken it too (section 4), and the measurement's request is
  then refused and its leg fails. A second request is always refused and
  counted, which ends the leg. The link is asked whether it is still current
  before the stream is opened and again after the instance has answered,
  before the client is told `200` or any byte it sent early is sent on.
- **The tunnel** is measured by a second ephemeral URLSession with no route,
  to the instance's public address. The two sessions share nothing.
- **A timed sample counts** only if it ran on the connection the warm-up
  made (the task's metrics say the connection was used again, with one
  attempt), through the port (the metrics say a proxy, and the port has
  had exactly one request for its host), on the path the measurement began
  on. Anything else ends that leg as `failed` with what was timed before
  it. Measured on macOS: URLSession sends a request again on a new
  connection without an error when its connection is cut, which is why the
  metrics are read and not only the result.
- **The path the measurement began on** is asked before every request and
  after its body: the selection, lease, listener and path generation a
  plan's direct candidate needs, and the transport's mark of the path
  (`DirectPathMark`). The mark changes when the transport's connection
  does, and whenever an observation finds another path selected than the
  observation before: another relay, or another address of the same
  family, is another path.
  - An observation is always a read of the connection made at that moment:
    when the mark is read, and whenever the binding's watch of the
    connection's paths reports, on the binding's thread and not a turn of
    the main actor later. What a report carries is not compared: it is the
    state as the binding found it, and may arrive after the path has moved
    on. Reads are made one at a time, so the paths noted are in the order
    they were selected.
  - A report after which the selected path is the one it was changes
    nothing: while it runs through a relay the connection keeps opening
    and closing other paths, looking for a direct one, and that is not a
    change of the path being measured.
  - The leg's labels (kind, local or global, the relay's region) are read
    in the same observation as the mark it begins on, not from what the
    controller has been told: the transport may have found a move a turn
    before the controller hears of it. For the same reason the status's
    `sequence` is no part of this: it advances when that news arrives, and
    for closures and preferences, none of which is a change of the path.
  - There is no mark from the moment the rendezvous answer the path was
    admitted under has run out. That is read from the transport's clock at
    the observation, not taken from the timer that ends the path having
    run: streams opened before the deadline go on carrying until it has,
    and a request answered in between must not count. The read changes
    nothing; ending the path stays the timer's work.
  - Only counts and labels leave the transport; which path it is stays
    there.
  - What it does not see: a path that changed and changed back between two
    observations. The binding's watch reports once for changes that follow
    each other closely, so the read a report causes may find the path
    there was before. Changes reported one by one are each counted. The
    binding's per-event watch (`watchPathEvents`) would close this; it is
    not used yet.
- **Time** is the continuous clock's. A request is not begun at or after its
  bound or the measurement's, however late its task is first run, and a
  result that arrives at or after it is `timeout`, whichever of the request
  and the timer the scheduler ran first. The session's own request timeout is `timeout` as well; any other
  failure of the connection, the name or the certificate is `failed`.
- **Labels** are the link's own: `relay`, `ipv4` or `ipv6`; `local` or
  `global` from the selected path's remote address class; and for the relay
  its region, read from the selected path's own relay URL and only when
  that is the URL of one of the four public relays a dial may use (`use1`,
  `usw1`, `euc1`, `aps1`), letter case, the root dot and a written default
  port aside. Another port or path on such a host, and any other relay, has
  no region; none is taken from what was configured.
- **Not covered by a test in CI:** the whole path through URLSession (TLS to
  an instance through the port). The test target has no TLS server, and the
  system sends a request for a loopback address round any route.

## 11. Tests and qualification

**Automated, in `ios-shell` on the simulator, with stand-ins.** These are App-logic tests; they do not qualify WebKit:

- the listener table in section 4, against real loopback sockets;
- exclusive bind, the ownership check, and a stand-in that occupies P;
- the listener's recovery: every failure the same recoverable state with its cause, left only by the next bind; a stranger on the port (refused throughout, the same recovery across attempts, one record, the closure kept pending, served again when it lets go); a bind left waiting, as Network reports it (not held, the closure published, bound again); an attempt whose check is not answered on the App's own port (not a stranger: not held, recorded as `ownership`); an attempt that only ran out of time; the serving socket failing as Network reports it (not held, served again); `retryListenerNow()`; a failure whose completion arrives in the background; leaving and returning while recovering; a failure before any route (no listener kept, nothing recovering, the next open makes another);
- the failure record: what `direct.status()` and the rendezvous request carry, within the instance's limits, and each failure reported once;
- the foreground watchdog, through the lifecycle's own deadline: nothing while it is ahead, the failure once it has come, and the bind after it starting outside it; a late confirmation; attempts that do not move it; the background ending it; grants kept; and, in the controller, a first bind past a zero bound leaving no listener and no route while a listener that serves in time is never failed;
- bind exits and resources: the bind's answer and the phase as one decision in both orders; a cancelled bind never left binding or verifying; retirement of a listener no route points at, failed or not; a cancelled relay keeping its slot until its task ends;
- the grant: against a failure or close in both orders, a revoked candidate never granted or answered `200`, a granted relay kept and never planned again;
- the load gate: the open, Retry, and the WebView's automatic loads for no route, a serving listener, a held listener, and a listener that failed (refused, as `fault`, only while the port may be someone else's, and then also for the voice take's client), with runtime replacement and sign-out;
- backend choice: the direct stand-in failing at open, refusing, or stalling before `200`; the hedge; early bytes held and never forwarded to the loser; the early-byte bound; a backend failing after `200` closes the relay with no redial;
- path generations: each row of section 7 closes exactly the relays it marks Closed and no others; advancing a generation cancels candidates without closing committed relays; a relay's own EOF or failure ends it whatever its generation; a late report for an older selection or generation changes nothing;
- authorization: an ended selection's tags and late reports never authorize direct; a host that is not the current selection's gets only the public backend; committed public relays close only for their own reasons (section 7); an account transition closes relays of both backends. Store or caller attribution is not tested; a CONNECT cannot carry it (section 4);
- the route owner: written once per host and never cleared, counted at WebKit's real setter, and nothing without a transport;
- the bridge: emission rules; `preferred` ordered by `sequence`; `closure` per host and monotonic; delivery only to current instance-origin documents. Web UI cases interleave a preference change, a closure, and a status read in every order. They also cover:

- duplicate and late events;
- a status read that shows a closure before its event arrives;
- a closure during page load, runtime creation, or a new child window, before and after the document's baseline read;
- background, then resume on the same port: the pending closure is published only after the listener is ready, and a document that was destroyed does nothing;
- the same with both closure emissions dropped (the one at publication and the re-emitted state). The document's own resume lifecycle, `visibilitychange` to `visible`, must issue the status read; the test does not call `direct.status()` itself. The read is held until readiness and then shows the higher `closure`. The document reconnects once, keeps its earlier baseline and watermark, and does not reconnect again when a dropped event is later delivered;
- a status read during the resume window is answered only after readiness, or after the recovery path has published its state;

**WebKit behaviour.** It is qualified by the measurement harness in `packaging/webkit-proxy-change/`, not by App tests. The cells this design needs:

| Cell | Question | Where |
| --- | --- | --- |
| Q1 | A new named store, routed before any load, after a relaunch with the App's real store: HTTP/1.1, HTTP/2, navigation, `fetch`, SSE, WebSocket | Simulator 17.0 and newest; device |
| Q2 | A store that has loaded the control plane: a route for the instance host added before its first load, used? | Simulator 17.0 and newest; device |
| Q3 | Within that store, a second host added (union) before its first load, with the first host's pool alive: used, and the first host unaffected? | Same |
| Q4 | A route changed from P to P2, on two live stores with distinct hosts: does every store and host route through P2? Three variants: cooperative closure of P, an independent process holding P that accepts and stalls new connections, and one holding P with a warm pool surviving on it. Probe order: `fetch`, unique navigation, `fetch`. Measured (section 2): the occupied variant with a warm pool was negative, so the App never moves a route (section 9). Kept as history; nothing depends on it now. | Same |
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

- **The binary.** Link only the gated `IrohLib` artifact whose pin in `packaging/iroh-apple/release.json` is `published`, with a matching checksum. The pin is now published (tag `iroh-apple-v1.2.0-1`, iroh 1.4.1, deployment target 17.0); A1 stages the iOS-only `Iroh.xcframework` and matching generated wrapper, but the App still does not register a transport. The pin belongs to the gate lane.
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

### Residual: 「仅直连和中继」 and Action Button takes

A voice take is sent from a hidden WebView on the account's store, which shares WebKit's connections with the page. A take made while the App was not opened in this process, or while its listener is closed for the background, leaves on the public path (in the second case the host's route is opened to failover for it, `DirectPathController.noteVoiceLoad`). The store then holds public connections the listener does not control, so 「仅直连和中继」 is not enforced again until the next launch (`pendingRelaunch`); the stored choice is unchanged. Someone who mostly uses the Action Button may therefore rarely have the choice in effect. Keeping it in effect and delivering the take needs the take to go through the listener in the background (binding it for the take) or over a connection of its own; neither is built.

## 14. Pull requests

| PR | Content | Status |
| --- | --- | --- |
| #3530 | This document, the bridge contract, and the Phase 1 and parent-plan rows that describe the App | Merged |
| #3537 | Swift: listener, backends, routes, controller, bridge. Inactive without a transport. | Draft; reworked against this contract |
| #3532 | Web UI: the closure watermark and the reconnect action (section 10) | Merged |
| Transport (A1, A2) | iroh, rendezvous, probe | After the iroh pin is `published` and the owner approves the first transport build |
| Device validation (A5) | Section 11 | The iOS owner or the owner |
