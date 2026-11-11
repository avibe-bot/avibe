# Remote direct paths: instance transport contract

Status: **Contract for Phase 1 row I1.** Nothing here is built yet. The owner decided on 2026-11-11 that the instance runs its iroh endpoint in a small Rust sidecar, shipped as a managed runtime ([`remote-direct-phase1.md`](remote-direct-phase1.md), decision D1). This document fixes what the sidecar is, how the instance talks to it, and how it is built and shipped, so the sidecar and the instance-side code can be written in parallel.

## 1. What the sidecar is

`avibe-direct-transport` is one native program. It owns an iroh endpoint and does three things:

1. accepts iroh connections for the application protocol `avibe-direct/1`;
2. accepts only peers the instance has told it are admitted, and drops every other peer at the end of the transport handshake;
3. for each stream an admitted peer opens, connects to the instance over loopback and copies bytes both ways.

It does nothing else. It does not parse HTTP or TLS, does not know hostnames, certificates, users, or sessions, and holds no credential of the instance. Everything it forwards enters the strict ingress ([`remote-direct-ingress.md`](remote-direct-ingress.md)) and meets the ordinary remote authentication.

```text
App (iroh)  ->  sidecar  ->  loopback stream listener  ->  DirectIngress.handle_stream  ->  TLS listener  ->  Web UI
                   |  control channel (stdin/stdout)  ^
                   +----------------------------------+   Web UI server process
```

## 2. Process model

- The Web UI server process starts the sidecar as a child when direct access is on and usable (the setting, the control plane's permission, and a valid certificate; lifecycle is row I3) and stops it when any of those ends. That process already owns the ingress and the rendezvous state.
- The sidecar exits when its standard input reaches end of file. If the Web UI server dies, no orphan remains. The instance stops it by closing the control channel, and kills it by its recorded process ID only if it has not exited in a few seconds.
- The endpoint key is generated in the sidecar's memory at each start. It is never written, logged, or sent to the instance; only the endpoint ID (the public key) leaves the process. A restart is a new endpoint ID and a new rendezvous generation, and every admission ends.
- The endpoint uses iroh's minimal preset: no relay, no public or local discovery, no port mapping, no telemetry. It binds UDP on the unspecified IPv4 and IPv6 addresses with ports chosen by the system.
- A crash costs only the direct path. The instance withdraws the rendezvous at once, so clients fall back to Cloudflare, and restarts the sidecar with backoff.

## 3. Control channel

Newline-delimited JSON on the sidecar's standard input (instance to sidecar) and standard output (sidecar to instance), UTF-8, one object per line, at most 4 KiB per line. Standard error carries human-readable logs and never a secret. An unknown `type`, an oversized line, or invalid JSON from the instance makes the sidecar exit with an error; from the sidecar it makes the instance stop it.

### Instance to sidecar

| Message | Meaning |
| --- | --- |
| `{"type":"start","protocol":1,"alpn":"avibe-direct/1","stream_addr":"127.0.0.1:<port>","token":"<64 hex>","max_connections":64,"max_streams_per_connection":64}` | Exactly once, as the first line. `stream_addr` is the instance's stream listener; `token` proves a data connection comes from this sidecar. The token travels here, never in arguments or the environment. |
| `{"type":"admission","endpoint_id":"<64 hex>","ttl_seconds":<0 to 300>}` | The client endpoint is admitted for that many seconds from now; zero ends its admission. Sent whenever the rendezvous admits a client, extends its admission, or drops it. |
| `{"type":"stop"}` | Close every connection and exit. |

### Sidecar to instance

| Message | Meaning |
| --- | --- |
| `{"type":"ready","protocol":1,"endpoint_id":"<64 hex>","addresses":["192.168.1.20:51234","[fd12::1]:51234"]}` | The endpoint is bound. `addresses` are the socket addresses a peer could dial, as concrete interface addresses, not unspecified ones. The instance filters them (rendezvous contract) and publishes. |
| `{"type":"addresses","addresses":[…]}` | The set changed (an interface came or went, an address changed). |
| `{"type":"peer_closed","endpoint_id":"<64 hex>","reason":"<code>"}` | A connection ended. For counters and diagnostics only. |
| `{"type":"fatal","error":"<code>"}` | The sidecar cannot continue and is exiting. |

### Admission

The instance decides who is admitted: a client endpoint is admitted when a signed-in user's App asks the rendezvous, for as long as the rendezvous answer says. The sidecar enforces that decision without asking back, so a peer the instance has never heard of costs the instance nothing.

- The instance sends `admission` to the sidecar before it answers the rendezvous request, so the notice is on its way before the client can dial. It sends a time to live, not a clock time, so the two processes never compare clocks. When the rendezvous drops an admission early, the instance sends zero.
- The sidecar keeps the admitted endpoint IDs in memory, each with a deadline on its monotonic clock: at most 256, dropping expired ones first and then the oldest; a time to live above 300 seconds is treated as 300. The set starts empty and is authoritative from the start: there is no request back to the instance and no fallback.
- At the end of the transport handshake the sidecar accepts a peer only if its endpoint ID is in the set and unexpired, and closes every other connection at once.
- A live connection is closed when its admission expires or is ended. An App that keeps using the direct path refreshes its rendezvous before expiry, which extends the admission.
- A restart of the sidecar is a new endpoint ID. The rendezvous then holds no admissions, and neither does the sidecar.
- If a notice loses the race with a very fast dial, the client is refused once, stays on Cloudflare, and succeeds on its next attempt.
- Admission is not authorization. It keeps devices that never signed in away from the ingress; the remote session check still decides every request.

## 4. Data path

For each bidirectional stream an admitted peer opens, the sidecar opens one TCP connection to `stream_addr` and first writes one ASCII line:

```text
AVDT1 <token: 64 hex> <peer endpoint ID: 64 hex>\n
```

Then it copies bytes in both directions until either side ends.

- The instance's stream listener binds loopback on a port the system chooses. It reads the line (at most 160 bytes, within 2 seconds), compares the token in constant time, checks the endpoint ID's form, and passes the rest of the connection to `DirectIngress.handle_stream(stream, peer=<endpoint ID>)`. Anything else is closed without an answer.
- Half-close is preserved: a finished iroh send side becomes a TCP write shutdown, and the reverse. A reset on one side closes the other.
- Copies are bounded and apply backpressure; neither side buffers a stream in memory.
- The token is not an authorization boundary. Without it, another local process could only reach the ingress, which grants nothing. It exists so the per-peer limits and the rate-limit key cannot be spoofed by a process that picks its own peer label.
- Loopback TCP with a token is used on every platform, so the instance has one code path.

## 5. Limits

- At most `max_connections` peers and `max_streams_per_connection` streams per peer; excess is refused by the transport's own flow control or closed. The ingress keeps its own limits as a second line.
- A handshake that does not complete in a few seconds, and a connection with no stream for several minutes, are closed. Exact defaults are set in the implementation and recorded here when it lands.
- The sidecar opens no application connection except to `stream_addr`, and contacts no public service. The transport itself exchanges UDP packets with peers; what it does for a peer before that peer is refused is described in section 8.

## 6. Build and supply chain

The sidecar is our own source in this repository, built by a workflow, published as a release, and pinned by a manifest, the way the Git and tmux runtimes are (`docs/plans/git-runtime.md`).

- **Pinned iroh.** The same iroh core release as the iOS gate (`packaging/iroh-apple/versions.env`), with `default-features = false` and only the features the sidecar needs, built with the committed `Cargo.lock`. The workflow asserts on the resolved feature graph for every target that the optional pieces are absent: the private Apple datapath, port mapping, the mDNS and mainline discovery crates, metrics push, and n0's services client. iroh's built-in DNS and PKARR address-lookup code, with its DNS and HTTP client dependencies, cannot be compiled out with a feature; it is compiled in and never configured, because the minimal preset installs no address-lookup service and the sidecar adds none. The README and the manifest list it, and a test asserts on captured traffic that the sidecar sends nothing but UDP to its peer and TCP to the stream listener.
- **Targets:** macOS arm64 and x86_64, Linux x86_64 and aarch64 (statically linked), Windows x86_64 (Windows on ARM runs it under emulation, as it does `cloudflared`).
- **Tests in the workflow:** a second endpoint in the test dials the sidecar over loopback. An admitted peer's stream reaches a stand-in stream listener with the right header, and bytes and half-close pass both ways. A peer the sidecar was not told about is closed at the end of the handshake, and a live connection is closed when its admission expires. A wrong protocol is refused. End of input makes the sidecar exit.
- **Publication:** a release per build (`direct-transport-v<version>-<n>`) with one archive per platform and checksums; a packaged manifest pins the release URL, size, archive hash, and binary hash; installation goes through the shared managed-runtime core into `~/.avibe/runtime/direct-transport/`; the release mirror and a release guard cover it as they cover the other runtimes.
- **Approval.** The source, each release, and each manifest pin add or change a native binary that users run. Each of those pull requests waits for the owner's approval.

## 7. Work split

| PR | Content |
| --- | --- |
| I1a | The sidecar's source, its tests, and the build workflow with the feature-graph assertions. Produces workflow artifacts only. |
| I1b | Publication: the release, the manifest pin, the runtime manager, the mirror and release guard entries. |
| I1c | Instance side: the stream listener and header check, the supervisor (start, stop, restart with backoff, protocol check), admission notices, address publication to the rendezvous. No caller until I3. |
| I3 | The setting and the lifecycle that starts and stops everything (Phase 1 plan). |

I1a and I1c depend only on this document and can proceed in parallel. I1b needs I1a's artifacts.

## 8. Open risks to verify early

- **macOS Local Network privacy.** On macOS 15 and later, a process that exchanges packets with devices on the local network can trigger the Local Network prompt, attributed to the app or service that started it. Verify with a second device how the prompt appears for the sidecar when Avibe runs as the desktop app, as a login service, and from a terminal, and what denial looks like. Denial must leave Cloudflare working and be reported in diagnostics.
- **Host firewalls.** Windows Defender Firewall asks before a program receives inbound traffic, and Linux hosts may run their own firewall. Record what the user sees and what the direct path does when inbound UDP is blocked: the rendezvous still lists addresses, the App's probe fails, and it stays on Cloudflare.
- **Signing.** macOS needs at least an ad-hoc signature on arm64. An unsigned Windows executable downloaded at run time may be flagged; it should be signed through the same path as the desktop app.
- **Addresses.** From the pinned source, the endpoint reports concrete interface addresses and chosen ports, and republishes them when interfaces change. A Linux test that adds and removes an interface confirms it.
- **Before admission.** In the pinned iroh, the hook that can refuse a peer runs only after the transport has registered the connection. By then the transport has told the peer the endpoint's interface candidates, and until the connection is closed it honors the peer's requests to send path probes to addresses the peer names. Stable API can neither refuse a peer earlier nor switch NAT traversal off. So a device that can reach the UDP port, which on a LAN is every device, can learn the endpoint's interface addresses and cause a bounded number of probe packets, without ever being admitted. The exact bounds are being measured. The mitigations under evaluation are: refusing in the hook from the local admitted set, so the window is as short as the transport allows; limits on handshakes per source and in total; a filter on source addresses before the handshake; and a vendored patch that switches NAT traversal off, which Phase 1 does not need. Which of these ship is the owner's decision, because it weighs a stated limit against carrying a patch on a network-facing dependency. Until it is made, the rendezvous contract's statement that addresses go to signed-in users only holds for the rendezvous answer, not for the transport.
