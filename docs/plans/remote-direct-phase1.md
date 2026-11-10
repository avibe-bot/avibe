# Remote direct paths: Phase 1 plan

Status: **Plan.** The instance and backend work is ready to start; the App work is a proposal until the iOS owner agrees (section 3). Phase 1 delivers LAN and opportunistic IPv6 direct paths for the iOS App with Cloudflare fallback. Parent plan: [`remote-direct-paths.md`](remote-direct-paths.md).

Phase 0 delivered the foundations this plan builds on:

- the strict ingress and its contract, [`remote-direct-ingress.md`](remote-direct-ingress.md) (#3494);
- the rendezvous endpoint and its contract, [`remote-direct-rendezvous.md`](remote-direct-rendezvous.md) (#3500);
- the iOS build gate, `packaging/iroh-apple/` (#3493): iroh-ffi for iOS 17.5 without the private Apple datapath, checked in the binary;
- the certificate issuance design and prototype in the backend repository, `docs/direct-certificates.md` (avibe-backend#325).

The iOS App is another team's area. Its PRs below are a proposal; the split is settled with the iOS owner on [#3222](https://github.com/avibe-bot/avibe/issues/3222) before any App work starts.

## 1. What Phase 1 delivers

For an instance on the direct-access allowlist that has direct access turned on, with a TestFlight build of the App:

- on the same network as the instance, or where the instance has a reachable global IPv6 address, the Workbench's assets, API calls, SSE, and WebSockets travel a direct iroh path;
- everywhere else, and whenever the direct path fails or is refused, they travel Cloudflare Tunnel exactly as today, within a bounded time and without losing the session;
- the origin, login, cookies, cache, and passkeys are the same on both paths, and the direct path grants nothing the tunnel does not.

Out of scope: IPv4 hole punching and the relay (Phase 2), desktop shell and Android (Phase 3), a public App Store release, and any change for browsers.

## 2. Exit criteria

1. **Auth parity.** For anonymous, Viewer, Editor, Owner, revoked, and control-plane-unreachable sessions, the direct path and the tunnel return the same answers, on a real instance with a real certificate.
2. **No local-owner path.** The Phase 0 ingress tests pass against the wired transport, not only the TCP stand-in.
3. **Bounded fallback.** No usable direct path within the probe budget means the tunnel is used; the App never shows an indefinite loading state. Target: tunnel content within 2 seconds of a failed probe.
4. **Recoverable transport change.** After a switch in either direction, SSE resumes with catch-up and terminals reconnect; no POST, upload, or terminal input is replayed.
5. **Generation safety.** A late probe or answer for an old account, instance, or generation never installs a route.
6. **Device matrix** on iOS 17.5 and a current release (section 7).
7. **Kill switches work**: per instance, and from the control plane (section 8).

## 3. Decisions needed before work starts

| # | Decision | Recommendation | Who |
| --- | --- | --- | --- |
| D1 | How the instance runs its iroh endpoint | A small Rust sidecar, shipped as a managed runtime like `cloudflared`, built from the same pinned iroh and feature set as the iOS gate. It forwards each accepted stream to the ingress over loopback. A crash then costs only the direct path, the endpoint key never enters Python, port mapping and public discovery are compiled out, and Intel Macs are covered. The stock Python wheel cannot turn port mapping off, has no Intel macOS build, and puts native code inside the Web UI process. | Owner |
| D2 | Where integration runs | Production `avibe.bot`, with certificate issuance enabled only for an allowlist of internal instances. WebKit trusts only public certificates, so a staging CA cannot serve a TestFlight build. The alternative is the staging zone, or another test zone the owner controls, with real certificates and an App build that targets it. | Owner |
| D3 | How the App fetches the rendezvous | Native fetches it, over the same-origin transport on the account data store that push management uses, so the answer never crosses the bridge. Show Pages are same-origin and can reach the bridge; an answer handed over by the Web UI could be forged by an Agent-authored page, and native would then probe addresses of that page's choosing under the user's Local Network grant. | iOS owner |
| D4 | Local Network permission copy and timing | Ask only when a LAN candidate is first probed; denial is silent and stays on the tunnel. | iOS owner |
| D5 | iOS 17.5 as the App's minimum | The owner approved it in the parent plan; the App's minimum is the iOS owner's call and the project builds 17.0 today. It drops testers on 17.0 to 17.4, the external TestFlight group included. | iOS owner |

## 4. Hard rules

- **A certificate is issued only for an instance whose owner turned direct paths on.** Never for the fleet, never by default, never as a side effect of pairing or updating. Each certificate publishes the instance's hostname in public Certificate Transparency logs; the owner accepted that consequence on this condition (2026-11-10).
- **Nothing in production without the owner's approval:** DNS writes, Cloudflare changes, backend deploys, the ACME account, the allowlist.
- **The direct path never grants local-owner authority** and never weakens remote authentication. The Phase 0 ingress tests are the floor.
- **Every iOS build links only the gated iroh artifact.** TestFlight builds can reach external testers.

## 5. Work by repository and owner

"Ours" is the remote-access side. Each PR passes the normal gates. PRs marked **S** wait for the owner's approval: they touch authentication, credentials, or the trust boundary, or they add a native dependency that users will run (the supply-chain boundary).

### avibe: instance runtime (ours)

| PR | Content | Depends on |
| --- | --- | --- |
| I1 **S** | Transport runtime (per D1): accept iroh connections for `avibe-direct/1` with the minimal preset, no relay, an ephemeral in-memory key; close peers that are not admitted; forward streams to `DirectIngress.handle_stream` with the peer's endpoint ID; report the endpoint ID and bound sockets. Build, pin, and manifest for every supported platform. | Phase 0 ingress, rendezvous, build gate |
| I2 **S** | Certificate client: generate the P-256 key on the instance, store it owner-only with the tunnel credential's redaction and lifecycle rules, send the CSR, poll the order, install and renew by the backend's renewal time, discard on unpair. | Backend B2 |
| I3 **S** | Lifecycle: a `remote_access.vibe_cloud.direct` setting (default off, additive); start and stop transport and ingress from certificate and setting state; publish to the rendezvous; honor the control-plane switch; status and diagnostics without addresses or key material. | I1, I2 |
| I4 | Web UI half of the bridge: `direct` capability in `ui/src/lib/native.ts`, and explicit SSE, WebSocket, and terminal reconnect on a transport-change event, reusing the existing reconnect owners. No replay of ambiguous mutations. The rendezvous answer does not pass through the Web UI (D3). | Bridge contract (A4 spec) |
| I5 | Setting surface: design frame, Remote Access setting, copy in both locales, documentation. | I3 |
| I6 | Regression: an Incus scenario with a headless iroh client standing in for the App (rendezvous, dial, CONNECT, TLS, auth matrix, stale candidate, blackhole, restart and generation change). | I1–I3 |

### avibe-backend: certificates (ours)

| PR | Content | Depends on |
| --- | --- | --- |
| B1 **S** | Phase 0 prototype: ACME client, CSR validation, DNS-01 provider, step machine, Pebble proof, design document. | — |
| B2 **S** | Persistence (additive migration), the instance routes, per-instance and weekly budgets, the queued state, the sweeper. | B1 |
| B3 **S** | Retirement: revoke and cancel on device revocation, instance deletion, and hostname release; audit events; the control-plane direct switch in the runtime-status answer. | B2 |
| B4 **S** | Enablement: environment configuration, the ACME account, the allowlist (D2), the test-zone trial on Let's Encrypt staging, and the Let's Encrypt adjustment request ahead of any wider rollout. Production steps each need the owner's approval. | B2, B3 |

### avibe `ios/`: the App (iOS owner; proposed)

| PR | Content | Depends on |
| --- | --- | --- |
| A1 **S** | Deployment target 17.5 on all six entries, the notification service extension included (D5); consume the gated `IrohLib` xcframework; add the `sendmsg_x`/`recvmsg_x` check to the archive verification; `NSLocalNetworkUsageDescription` only, with no new capability or entitlement, so signing profiles and the archive's entitlement checks do not change. | Phase 0 build gate |
| A2 | Direct transport core: endpoint per launch with the minimal preset and no public or multicast discovery; native rendezvous fetch (D3); dial the answer's endpoint ID at addresses the App has checked for itself (private, unique-local, global IPv6 unicast; at most eight); bounded probe; fallback; foreground, background, and network-change handling. | A1, rendezvous contract |
| A3 | Loopback CONNECT listener and the exact-host `proxyConfigurations` route on the account's data store, with one owner across the runtime and hidden transport WebViews; forward the CONNECT head and report the instance's answer. The route is tagged with the store identifier, account, instance, and generation; it is reinstalled when the store rotates and torn down with the old store, and a late probe never installs one on a store that is going away. `matchDomains` is a suffix match, so the listener itself must refuse every authority but the selected instance's. | A2, ingress contract |
| A4 | Bridge: the `direct` capability (the transport-change event and a read-only status), specified first in `ios-app.md` section 4. | Agreed with I4 |
| A5 | Device validation (section 7) and the TestFlight build. | A1–A4, I1–I4, B2–B4 |

### Order

1. Contracts: ingress framing, rendezvous, the `direct` bridge capability, the certificate wire contract. Phase 0 delivers the first two and the last; the bridge capability is agreed first in Phase 1.
2. In parallel: I1–I3, B2–B3, A1–A3. I4 and A4 follow the bridge contract.
3. Integration on the allowlisted instances (B4, I6, A5), then the exit criteria.

## 6. Risks carried into Phase 1

- **Local Network permission.** WKWebView traffic is exempt, but the App's own UDP to a LAN address is not. Denial must fail fast and silently to the tunnel.
- **`proxyConfigurations` changes do not move open connections.** SSE and WebSockets must be rebuilt explicitly; a browser WebSocket does not reconnect by itself.
- **Certificate lifetime on sleeping machines.** An expired certificate means no direct path until renewal completes; the tunnel carries the session meanwhile.
- **IPv6 is opportunistic.** The spike measured 250 of 250 mainland probes failing to reach one home connection's IPv6 listener. It is not an availability promise.
- **Let's Encrypt budget.** 50 new certificates a week for all of `avibe.bot` fits an allowlisted cohort and nothing larger without an adjustment.
- **TestFlight reaches external testers.** A public external group exists, and any archive can be added to it by hand. The private-datapath exclusion therefore holds for every iOS build, and the allowlist, not the App's audience, bounds who gets a direct path.
- **The App owner's pace.** App work starts only on decisions the iOS owner states on the issue. The instance and backend work does not wait for it; integration does.

## 7. Device matrix (A5)

On a physical iPhone with iOS 17.5 and one with a current release, against a test instance:

- Local Network permission undetermined, allowed, denied, and later revoked;
- same Wi-Fi, AP isolation, different Wi-Fi, Wi-Fi to cellular and back, sleep and resume, DHCP change, stale candidate, blackholed candidate;
- wrong certificate name, expired certificate, another instance's endpoint: refused, no downgrade;
- no cookie, Viewer, Editor, Owner, revoked member, control plane unreachable: same answers as the tunnel; CSRF refusals intact;
- cookies, cache, and passkeys preserved across path changes;
- an SSE stream, a terminal, and an upload in flight during a path change;
- account switch, instance switch, and sign-out with a probe still running.

## 8. Rollout and switches

- **Per instance:** the direct setting is off by default. Turning it off stops the transport and the ingress and withdraws the rendezvous; the App falls back on its next refresh or failure.
- **Control plane:** the runtime-status answer carries whether direct access is allowed; an instance stops serving it when it is not. Issuance is limited to the allowlist.
- **App:** TestFlight only, no App Store release. A rendezvous answer of `unavailable` is always a valid state, so an App build never depends on the direct path, and an instance off the allowlist always answers that way.

## 9. Estimate

Engineer-days after Phase 0, excluding approval waits and the Let's Encrypt adjustment lead time. The ranges in the parent plan stand; this splits them.

| Area | Days |
| --- | ---: |
| Instance I1–I3, I6 | 8–14 |
| Web UI I4, I5 | 2–4 |
| Backend B2–B4 | 5–9 |
| App A1–A5 | 8–14 |
| Integration and security regression | 6–10 |
