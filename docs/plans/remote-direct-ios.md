# Remote direct paths: the iOS App side

Status: **Proposal, prepared for the iOS owner's review.** On 2026-11-11 the owner asked that the App side of remote direct paths be prepared as branches and pull requests instead of waiting for an answer on [#3222](https://github.com/avibe-bot/avibe/issues/3222). Nothing here merges without the iOS owner's review, and he can change or reject any of it. This work does not raise the App's minimum iOS version, adds no capability or entitlement, and does not touch signing, TestFlight, or the notification service extension.

Parent plans: [`remote-direct-phase1.md`](remote-direct-phase1.md) (rows A2 to A5 and I4), [`remote-direct-ingress.md`](remote-direct-ingress.md) (stream framing), [`remote-direct-rendezvous.md`](remote-direct-rendezvous.md) (what a client must do). Bridge contract: [`ios-app.md`](ios-app.md) section 4.

## 1. What the App does

The App keeps loading `https://<slug>-app.avibe.bot`. When a direct path to the selected instance is usable, WebKit's connections for that one host go through a loopback proxy inside the App, which carries them over the direct transport to the instance. When it is not, they go through Cloudflare Tunnel exactly as today. The origin, cookies, cache, and passkeys are the same on both paths. TLS runs between WebKit and the instance on both paths: the App relays ciphertext and adds no trust override.

```text
WKWebView on the account's data store (runtime, child windows, hidden transports)
  -> proxyConfigurations: HTTP CONNECT proxy 127.0.0.1:<port>, matchDomains [<instance host>]
  -> CONNECT listener        answers CONNECT <instance host>:443 only
  -> transport stream        canonical CONNECT head out, the instance's status back, then bytes
  -> instance strict ingress TLS for <instance host>; ordinary remote authentication
```

`WKWebsiteDataStore.proxyConfigurations` and `ProxyConfiguration.matchDomains` are public API from iOS 17.0. Measured on macOS WebKit in the feasibility spike: the route carries main navigation, subresources, `fetch`, SSE, and WebSocket for the matched host; the match is a **suffix** match; changing the configuration does not move connections that are already open. None of this is measured on an iPhone yet (section 8).

## 2. Components and who owns what

| Component | Where | Owns | Plan row |
| --- | --- | --- | --- |
| Transport | `DirectTransporting`, a protocol with no implementation yet | The iroh endpoint, the native rendezvous fetch, probing, and network-change handling. Reports when the selected instance has a usable path, and opens byte streams over it. It is told when the App is on the tunnel although it reported a path usable, and decides when to probe again. | A2 |
| CONNECT listener | `DirectConnectListener` | One loopback socket on a port the system chooses. Refuses everything but `CONNECT <instance host>:443`, then relays bytes. | A3 |
| Path controller | `DirectPathController` | The transport state (tunnel or direct), the tags, and every decision to install or remove the route. | A3 |
| Route owner | `DirectRouteOwner` | The only code that writes `proxyConfigurations` on an account data store. | A3 |
| Store lifecycle | `AccountWebsiteDataStore.rotate()` | Removes the route with the store it was installed on. No second lifecycle. | A3 |
| Bridge | `NativeBridge.swift` | `direct.status()` and the `direct.transportChanged` event. | A4 |
| Web UI | `ui/src/lib/native.ts` and the existing reconnect owners | Rebuilds the SSE stream and terminal WebSockets when the transport changes. | I4 |

**With no transport registered, nothing activates:** no socket is bound, `proxyConfigurations` is never written, the status is always `tunnel`, and no event fires. The first App pull request ships exactly that state and tests it.

## 3. Transport state and tags

The state is `tunnel` by default. It is `direct` only while all of these hold: a transport reported a usable path for the current tags, the listener is bound, and the route is installed. Any failure returns to `tunnel` at once.

A **selection** is one opened runtime, between the App's `openRuntime` and `stopRuntime`. Every route carries the tags of the selection it was made for:

| Tag | Source | Changes when |
| --- | --- | --- |
| Selection number | The path controller; advances at every selection and at every return from the background | A runtime opens or stops, including the same instance again |
| Account and instance | `MobileRuntimeContext`, the lease `MobileSessionCoordinator` issues | Account switch, instance switch |
| Account generation | Inside the same lease | Any sign-in, refresh, sign-out, or store rotation |
| Data-store identifier | `AccountWebsiteDataStore.identifier` when the runtime opened | The store rotates |
| Instance authority | The instance URL: its exact host, port 443 | Instance switch |
| Rendezvous generation | The rendezvous answer the transport used | The instance's endpoint key or address set changes |

Rules:

- A transport report is bound to the selection it was requested for. A report that arrives after that selection ended is dropped.
- Before a route is installed, the controller asks `MobileSessionCoordinator.isCurrentRuntime` for the lease, as the App's route and WebView registration already do, and compares the tag's store identifier with the current one. A late result therefore never installs a route on a store that is going away or has gone.
- `isCurrentRuntime` is also false while the coordinator runs a request of its own with no account transition: `pushManagementRequest` holds `phase = .operation` until its request ends. A route that becomes ready in that window is not installed either. The App stays on the tunnel, the transport is told, and its next report is admitted. This is the conservative reading; section 9 asks whether it should be otherwise.
- Within a selection, a rendezvous generation that a newer one replaced is never accepted again. Generations never repeat, so this costs a correct transport nothing.
- A route for a new generation is a new route: the old listener and its relays are closed first.

## 4. What happens when

| Event | Result |
| --- | --- |
| A runtime opens (cold restore or instance selected) | New selection, state `tunnel`. The transport is told the target and starts its own work. |
| The transport reports a usable path | Tags checked, listener bound, route installed, state `direct`, event emitted. |
| The transport reports the path lost; a stream cannot be opened; the instance refuses a CONNECT; the listener fails or reaches its connection limit | Route removed, listener and its relays closed, state `tunnel`, event emitted. Requests in flight on the direct path fail as network errors. Nothing replays them. |
| The App enters the background | Returns to `tunnel` and deselects the transport. A listening socket does not survive suspension reliably ([TN2277](https://developer.apple.com/library/archive/technotes/tn2277/_index.html)). |
| The App returns to the foreground | A new selection for the open runtime. Whatever the transport reported before is void; it probes again. |
| The network changes | The transport's to detect (A2): it reports the path lost at once and probes again. The controller needs no network monitor of its own. |
| Account covers the runtime | Nothing. The runtime and its route stay. |
| Instance switch, instance picker | The runtime stops: state `tunnel`, transport deselected. The next runtime is a new selection with a new authority. |
| Sign-in, refresh, sign-out, account switch | `MobileSessionCoordinator` releases the runtime first, as today, so the state is already `tunnel` when anything else happens. |
| The data store rotates | `rotate()` removes any route from the outgoing store before replacing it. The new store starts with none, and only a later selection, whose tags carry the new identifier, can install one. |
| The rendezvous generation changes | The old generation is replaced: back to `tunnel`, then `direct` again on the new one. |
| The web content process is terminated | The primary reloads, as today. The route belongs to the data store and is unaffected. |

## 5. The listener

It binds `127.0.0.1` only, on a port the system picks, and exists only while a route is being installed or is installed.

| Request from WebKit | Answer |
| --- | --- |
| `CONNECT <instance host>:443 HTTP/1.1` | Relayed, as below |
| Another host, a subdomain of the instance host, another port, no port, userinfo, an IP literal | `403` |
| Any method but `CONNECT` | `405` |
| Not HTTP/1.1, a body, a repeated `Host`, a `Host` that disagrees with the target, a malformed head, a head over 4 KiB | `400` |
| Head not complete within 5 seconds | Closed |
| More than 64 connections at once | `503`, and the path returns to the tunnel |

The subdomain row is the reason the listener checks the authority itself: `matchDomains` would route `x.<instance host>` to it too.

For an accepted request the listener opens one transport stream (2-second bound) and writes the canonical head from the ingress contract, `CONNECT <host>:443 HTTP/1.1` with `Host: <host>:443`, not the bytes WebKit sent: the instance requires that exact `Host`, and WebKit's other proxy headers are of no use to it. It reads the instance's status (4 KiB, 3 seconds). `200` becomes `200 Connection Established` to WebKit and the relay starts. Anything else reaches WebKit as a failed proxy connection with the instance's status, and the path returns to the tunnel.

The relay copies in both directions with backpressure: each direction reads the next chunk (64 KiB at most) only after the previous one was accepted. A finished side is passed on as a half-close, and the other direction then has 30 seconds to finish, the ingress's own bound. There is no idle timeout: a healthy SSE stream is quiet between heartbeats and a terminal can be idle for hours.

The listener does not detect a silently dead path under established relays. The transport must (A2): it keeps the connection alive, notices loss within the probe budget, and reports it, which closes every relay.

Any process on the phone can connect to a loopback port. It gains a byte pipe to the instance's strict ingress and nothing else: no TLS key, no session. It can at most use up the 64 connections, which sends the App back to the tunnel.

## 6. Fallback bounds

- A failure is handled in the same main-actor turn it is reported in: the route is removed before the event is emitted, so every connection WebKit opens afterwards uses the tunnel.
- A CONNECT that cannot be served fails within 2 seconds (stream open) or 3 seconds (the instance's answer).
- `allowFailover` stays `false`, as measured in the spike: the App decides when the tunnel is used, and a refused CONNECT is visible instead of silently retried elsewhere.
- Phase 1's target, tunnel content within 2 seconds of a failed probe, also needs the transport's probe and loss detection (A2) and device measurements (A5).

## 7. Bridge and Web UI

The contract is in [`ios-app.md`](ios-app.md) section 4: capability `direct`, the read-only call `direct.status()`, and the event `direct.transportChanged`. No call lets web content choose addresses, start a probe, or supply a rendezvous answer, because Show Pages are same-origin with the Workbench and can reach the bridge.

On the event the Web UI closes and reopens its SSE stream through the existing reconnect loop, whose handshake already triggers a catch-up read, and reconnects each terminal that can reattach without loss. It does not retry any request. Without the capability the Web UI behaves as it does today.

## 8. Tests

Automated, in `ios-shell` on the simulator:

- the CONNECT refusal table above, one case per row, against the real listener over real loopback sockets;
- relay, early data, backpressure, and half-close in both directions against a stand-in stream;
- the tag rules: late reports, a replaced generation, instance switch, account switch, sign-out, store rotation, background and foreground;
- that nothing binds a socket or writes `proxyConfigurations` without a transport;
- the bridge: `direct.status()` and the event's payload, in an instance main frame only;
- one WebKit-level test: a `WKWebView` on a real data store loads through the installed route into a stand-in instance. It proves that the simulator's WebKit sends its CONNECT for the matched host to the listener and that a document loads through the relay. The page is plain HTTP on port 443 of an unqualified host name, which needs no certificate and is outside App Transport Security, so the test proves nothing about TLS, certificates, or a real network. Subdomain refusal and route removal are tested at the listener and the controller, not through WebKit.

On devices, in the Phase 1 matrix (A5): Local Network permission states, suspension and resume, Wi-Fi and cellular changes, a blackholed path, and whether WebKit on iOS applies a route change to new connections as promptly as on macOS.

## 9. Open for the iOS owner

1. **Where native fetches the rendezvous (D3).** It needs a native same-origin request to the instance on the account's data store. `MobileAuthTransport` cannot make it: it is pinned to the control-plane origin. Two ways fit the App's rules: a fixed script in the runtime primary WebView's `WKContentWorld.defaultClient`, which needs no new WebView and no new release barrier; or a second hidden transport on the instance origin. We would start with the first.
2. **Local Network permission (D4)** and **the minimum iOS version (D5)**: unchanged from the Phase 1 plan. Neither is touched by the listener, the route, or the bridge.
3. **Background policy.** Returning to the tunnel on every background is the conservative reading of TN2277. If a brief app switch should keep the route, that needs device evidence first.
4. **The capability's name and payload**, before the Web UI half spreads to instances.
5. **Admission while the coordinator is busy** (section 3). Reusing `isCurrentRuntime` unchanged means a route that is ready during a push management request is refused once. Admitting on the lease alone needs a coordinator check without the `phase` term, which is the iOS owner's to add or refuse.

## 10. Pull requests

| PR | Content | Needs |
| --- | --- | --- |
| #3530: this document and the bridge spec | Documentation only | iOS owner's review |
| #3532: Web UI half (I4) | `ui/` only; inert without the capability | Ours; it waits for the capability's name to be accepted here |
| #3537: listener, route, state, bridge (A3, A4) | Swift, inactive without a transport | iOS owner's review |
| Transport (A1, A2) | iroh, rendezvous fetch, probing | The iOS 17.0 build question, then the iOS owner's review |
| Device validation (A5) | The Phase 1 matrix and a TestFlight build | The iOS owner or the owner |
