# Remote direct paths plan

Status: **Decision accepted. Phase 0 delivered; Phase 1 is planned in [`remote-direct-phase1.md`](remote-direct-phase1.md).** This document answers [avibe-bot/avibe#3406](https://github.com/avibe-bot/avibe/issues/3406), which asked for iOS LAN-first access with bounded fallback while preserving authentication and existing public access.

## 1. Decisions

1. The iOS App owns an in-process direct layer built on **iroh**. It tries the available LAN, opportunistic public IPv6, iroh IPv4 hole-punch, and our own authenticated relay paths. iroh uses its stable default path policy: direct-primary with RTT bias; we do not implement a custom strict ordering.
2. `WKWebView` continues to use the selected instance's existing `https://<instance>-app.avibe.bot` origin. `WKWebView.proxyConfigurations` sends only that host through an App-owned loopback CONNECT listener, which picks the direct path or the instance's public origin for each new connection; the route itself is not changed to switch paths ([`remote-direct-ios.md`](remote-direct-ios.md)). Browsers continue to use Cloudflare Tunnel normally.
3. Cloudflare Tunnel remains the always-available public path, bootstrap, and authenticated rendezvous/signaling channel. The channel distributes the authorized instance identity, current endpoint information, relay configuration, expiry, and generation. iroh still performs its own authenticated QUIC path establishment and relay/direct upgrade; Cloudflare is not treated as a replacement iroh control protocol.
4. The App and instance require no user-installed Tailscale application and no second user account.
5. The origin, login, cookies, existing remote authentication, Web cache, and Vault passkeys remain unchanged. Direct transport never grants local-owner authority.
6. The instance has a strict direct-ingress boundary: preserve the instance hostname in SNI and HTTP `Host`, use a fixed upstream, strip client-supplied forwarding headers, synthesize only trusted metadata, and enter the existing remote-auth/role/CSRF/session path. A legal CONNECT request with a local-looking HTTP host must not reach the local-owner path.
7. When the direct path is lost, the connections it carried are closed; SSE reconnects with catch-up and terminals with a persistent server-side session reattach (their client-side view resets, and input during the reconnection is dropped). Existing long-lived connections are never assumed to migrate, and uncertain mutations are never replayed automatically.
8. The instance iroh endpoint key is **ephemeral**: generate it at each service start, never persist it, and publish only its public identity through the authenticated Cloudflare channel. It is not the authorization boundary; inner public TLS and existing Avibe authentication are.
9. The instance TLS private key for its `*-app.avibe.bot` certificate is generated on the instance and handled with the same sensitive-credential treatment as the current remote-access tunnel credential. Today, the tunnel token is persisted in the local `remote_access.vibe_cloud.tunnel_token` configuration field, omitted from normal API/status payloads, represented only by a hash in diagnostics, and injected into `cloudflared` through its child-process environment. The certificate key must follow the same redaction, permission, lifecycle, and no-log/no-WebView-exposure rules; this plan does not add a new avault custody API.
10. The iOS minimum was set to **17.5** because upstream packages iroh-ffi for 17.5. It is open again: the App owner decides the App's minimum, and we are testing a 17.0 build of the gated artifact (Phase 1 plan, decision D5). The App ships through TestFlight only for this work. TestFlight now has an external group as well as the internal one, so every iOS build, not only an App Store build, excludes iroh's private Apple datapath feature and links only the artifact qualified by the build gate (`packaging/iroh-apple/`). The future macOS desktop shell is distributed directly, not through the Mac App Store, so it may retain that datapath; if the private symbols are unavailable on a future macOS, its fallback must be verified then. iOS builds contain no datapath code, and no runtime switch re-enables it.

## 2. Architecture

```text
Browser or iOS WKWebView
  https://<instance>-app.avibe.bot              (unchanged origin)
        |
        | iOS only: selective CONNECT proxy for this exact host
        v
App loopback CONNECT listener
        |
        | selected iroh stream: LAN / IPv6 / hole-punch / HK relay
        v
Instance direct-ingress listener
        |
        | strict SNI + Host check; fixed upstream; trusted headers only
        v
Inner TLS to <instance>-app.avibe.bot
        |
        v
Existing remote authentication, authorization, cookies, CSRF, SSE, WSS
```

The App obtains the instance's public iroh identity and endpoint candidates only from the authenticated Cloudflare channel for the currently selected account, instance, and generation. A LAN address, iroh identity, relay token, or successful transport handshake is never a browser login or local-owner signal.

The inner TLS stream remains opaque to the App transport and relay. The instance presents the public certificate for its own hostname, so the WebKit origin and Vault WebAuthn RP ID do not change.

## 3. Why this closes #3406

The issue's original Bonjour/backend-candidate options are replaced by one transport contract that covers LAN and later cross-network direct paths without introducing a second origin:

- LAN candidates are handled by the same App-owned CONNECT path as all other direct candidates.
- Public access remains available while a bounded direct probe runs; no indefinite loading state is allowed.
- Permission denial, client isolation, stale candidates, certificate/identity mismatch, DHCP changes, network changes, or relay failure return to Cloudflare without clearing account state.
- The instance still applies its normal remote authentication and authorization. Private-network location is not an access grant.
- Instance switching, account switching, logout, and network changes use generation checks so late probes cannot authorize the direct path for an old account, instance, or store.
- WebKit assets, API requests, SSE, and WSS use the same selected origin; no cookie copying, alternate passkey RP, or second login flow is introduced.

## 4. Transport and rendezvous contract

Phase 0 defines an authenticated rendezvous payload, versioned and scoped to one account, device pairing, instance, and generation. It contains only what the App needs to connect:

- instance ID and `*-app.avibe.bot` hostname;
- ephemeral iroh EndpointId and public endpoint candidates;
- authenticated relay URL/configuration and short-lived relay capability;
- expiry, generation, and revocation state;
- the iroh application ALPN/version expected by the instance.

The payload never contains an endpoint private key, Cloudflare token, TLS private key, browser cookie, or Avibe session secret. The App rejects stale generations, wrong instance identities, expired capabilities, and unexpected peer identities.

Use iroh's stable default path selector. It may race direct paths and prefer a lower-RTT direct path rather than enforce LAN > IPv6 > IPv4. This is an accepted product decision. The application still bounds candidate probing and falls back quickly to Cloudflare when no usable authenticated path is ready.

## 5. Security and recovery invariants

### Direct ingress

- Accept CONNECT only for the exact selected instance authority and expected port.
- Terminate the transport at a fixed instance ingress; never dial an arbitrary client-supplied host.
- Establish inner TLS with the instance hostname as SNI and verify the request's HTTP `Host` remains that hostname.
- Strip `Forwarded`, `X-Forwarded-*`, Cloudflare identity headers, and other client-supplied trust metadata before synthesizing the minimal trusted ingress metadata.
- Preserve the existing remote auth, role, session-cookie, CSRF, Origin, SSE, and WebSocket behavior.
- Never classify a request as local merely because its transport peer or upstream socket is loopback.

### Key and credential handling

- Generate the iroh endpoint key per instance-service start; keep it in memory only and publish only its public identity through the authenticated channel.
- Generate and renew the instance hostname TLS key on the instance. Store and redact it using the existing remote-access sensitive-credential handling; never place it in Web content, rendezvous payloads, logs, or ordinary status responses.
- Treat relay capabilities as short-lived, instance-scoped credentials. Relay admission must fail closed and must not be based solely on a known URL or LAN location.
- The simplified ephemeral-key decision removes the separate avault implementation workstream, but does not weaken inner TLS or existing remote authentication.

### Transport changes

- Every account, instance, candidate set, and transport decision carries a generation.
- When the direct path is lost, close the connections it carried, reconnect SSE and reattach terminals with a persistent server-side session, and preserve the existing browser data store.
- Reuse existing SSE catch-up/Last-Event-ID behavior. Do not blindly replay POSTs, terminal input, uploads, or other mutations whose prior result is ambiguous.
- On foreground, resume, Wi-Fi/cellular changes, sleep, DHCP changes, or process restart, revalidate the direct path and the listener's port before new connections use direct; use the public origin while direct state is unavailable. The App's lifecycle rules, and which of them are still unmeasured, are in [`remote-direct-ios.md`](remote-direct-ios.md).

## 6. Phases

### Phase 0 — gates before implementation

- Pin and qualify iroh core/FFI versions; current evidence is core 1.4.1 and FFI 1.2.0.
- Build the Swift package for iOS 17.5 with iroh's private Apple datapath excluded. Run the internal TestFlight archive/signing and artifact checks; no public App Store claim is made. Gate: `packaging/iroh-apple/README.md`; the archive check is Phase 1 work.
- Define and test the authenticated rendezvous payload, generation/revocation semantics, and explicit peer identity verification over the existing Cloudflare path. Contract: [`remote-direct-rendezvous.md`](remote-direct-rendezvous.md).
- Define the instance TLS certificate lifecycle: hostname-scoped DNS-01, local key generation/storage, renewal, rotation, hostname retirement, and failure-safe fallback. Design: `docs/direct-certificates.md` in the backend repository.
- Resolve Let's Encrypt scale limits before choosing an issuance fleet model. The current evidence is a shared `avibe.bot` registered-domain limit of 50 new certificates per 7 days, with renewal/ARI behavior and quota increase requests to be evaluated. Do not change the Public Suffix List as an incidental quota workaround.
- Write wire-level tests for strict SNI/Host ingress, forwarding-header stripping, remote-auth parity, CONNECT authority checks, and no local-owner escalation. Contract and tests: [`remote-direct-ingress.md`](remote-direct-ingress.md).

### Phase 1 — LAN + opportunistic IPv6 + Cloudflare fallback

The work split, owners, decisions, and exit criteria are in [`remote-direct-phase1.md`](remote-direct-phase1.md).

- Add the App loopback CONNECT listener, with a direct backend and a public-origin backend, and exact-host `proxyConfigurations` routes that are never changed to switch paths.
- Discover or receive LAN and IPv6 candidates through the authenticated contract; verify the instance identity before routing WebKit traffic.
- Keep Cloudflare as the public path during every probe and on every direct failure.
- Add explicit SSE/WSS recovery and generation-safe account/instance/network switching.
- Validate on a physical iPhone at the App's minimum iOS version (17.0 today) and one at a current release: Local Network permission allow/deny, same-LAN/AP isolation, Wi-Fi↔cellular, sleep/resume, stale candidates, certificate mismatch, auth roles, cookies/cache, WebAuthn, and ambiguous mutation recovery.

The feasibility spike found global IPv6 on one Mac but 250/250 mainland TCP probes to its temporary high-port listener timed out, while the same probe service reached 238/249 positive-control nodes. IPv6 therefore remains opportunistic, not a Phase 1 availability promise.

### Phase 2 — iroh hole-punching + authenticated relay

- Carry the same CONNECT byte stream over iroh direct paths and the authenticated relay.
- Test LAN, public IPv6, dual-CGNAT, UDP blackhole/throttling, relay-only, direct↔relay path changes, and Cloudflare fallback on authorized networks.
- Operate the relay with default-deny admission, short-lived capabilities or an authenticated callout, connection/resource limits, health metrics, certificate renewal, abuse controls, and measured egress.
- Keep the relay near China, but do not treat geography or a provider marketing route as proof of mainland availability.

### Phase 3 — desktop shell and Android

- Reuse the transport/rendezvous contract in the future macOS shell and desktop instance runtime.
- Prefer a small native sidecar or approved native boundary for macOS, Windows, and Linux; keep Python as orchestration rather than a raw long-term key holder.
- Android follows after Phase 2. Current iroh evidence includes an Android AAR with minSdk 24 and four native ABIs; foreground/background endpoint shutdown and rebind, foreground-service policy, and device testing remain required.

## 7. Relay provider work

The relay provider is intentionally **not decided in this plan**. The current off-peak comparison and pending peak measurements live outside this repository at:

- `_tmp/relay-providers/relay-provider-comparison-2026-11-09.md`
- `_tmp/relay-providers/peak-followup-message.md`
- `_tmp/relay-providers/offpeak-2026-11-09/`

The comparison separates carrier probes from end-to-end TLS/relay evidence and records policy/KYC constraints. No provider purchase, signup, payment, deployment, DNS change, or Cloudflare change is part of this plan. The implementation phase must choose only after peak results, written provider permission for shared Avibe relay use, and a real SNI-preserving relay trial.

## 8. Estimate

The estimate is for implementation after 3.2.5 stable and excludes external approval waits, provider charges, and Android's full release work:

| Component | Estimate | Scope |
| --- | ---: | --- |
| iOS App | 8–14 engineer-days | iroh integration, loopback CONNECT, candidate/generation state, fallback, SSE/WSS recovery, lifecycle and device validation |
| Instance runtime | 8–14 engineer-days | ephemeral endpoint, fixed strict ingress, TLS key/certificate integration, lifecycle, diagnostics and desktop packaging groundwork |
| `avibe.bot` certificate issuance | 5–9 engineer-days | DNS-01 lease/accounting, per-host issuance, renewal/retirement, quota and audit handling |
| Authenticated relay | 3–6 engineer-days initially | relay packaging, admission, TLS, limits, health/metrics and measured egress; excludes host/provider charges |
| Integration and security regression | 6–10 engineer-days | auth parity, WebAuthn/cookie/cache preservation, network matrix, failover, revocation and recovery |
| **Total** | **30–53 engineer-days** | Parallelizable; implementation starts after 3.2.5 stable |

There is no separate avault-custody estimate under the accepted ephemeral-key decision. The certificate key handling is included in the instance and certificate-issuance work and must mirror the existing remote-access credential controls.

## 9. Evidence and non-goals

Evidence is condensed from the read-only spike in `_tmp/lan-direct-spike/`:

- `report.md`: macOS same-origin WKWebView/CONNECT, cookies/cache, SSE/WSS, strict-ingress and reconnect findings.
- `backend-report.md`: remote-auth/local-owner boundary, certificate issuance, DNS-01 and Cloudflare evidence.
- `p2p-iroh-report.md`: iroh versions, mobile bindings, relay/control behavior, custody findings, and direct/relay/upgrade prototype.
- `p2p-tailscale-report.md`: embedded Tailscale comparison and the Headscale/Cloudflare control-plane conflict.
- `ipv6-report.md`: temporary listener and mainland positive-control measurements.

This plan does not implement the transport, change the Cloudflare path, deploy a relay, issue production certificates, alter the existing Web UI origin, or promise iOS/public-network availability before the phase gates pass.
