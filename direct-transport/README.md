# Avibe direct-transport sidecar

`avibe-direct-transport` is the instance's native iroh endpoint. It forwards admitted bidirectional streams to one loopback TCP listener; it never parses HTTP/TLS or authorizes a user. Every forwarded byte still enters the instance's strict ingress and normal remote-session checks.

The crate is top-level because it is an independently built, managed native runtime, not part of the Python package, desktop app, or iOS binding. Its wire contract belongs to [`remote-direct-transport.md`](../docs/plans/remote-direct-transport.md). This PR follows the authoritative-admission amendment in avibe#3538; the initial request/answer admission design is intentionally not supported.

## Process and protocol

- New in-memory endpoint key on each start; only its public endpoint ID leaves the process. No key file or credential argument/environment variable.
- NDJSON on stdin/stdout, at most 4096 bytes **including LF**. First message is `start`; later messages are `admission` and `stop`. Invalid/unknown/oversized input fails closed with an error exit. No tracing subscriber is installed; stderr contains only fixed error codes, never tokens, keys, or network addresses.
- `admission` carries `endpoint_id` (64 lowercase hex digits) and `ttl_seconds` (nonnegative integer). Zero revokes; values above 300 clamp to 300. The empty initial set is authoritative. There are no admission requests, fallback, barrier, or periodic round trips.
- At most 256 admissions, with local monotonic deadlines. Expired entries are removed first, then the oldest insertion; refresh preserves insertion order. Revocation/expiry/eviction closes live connections through weak handles. Re-admitting the same ID cannot rescue a connection from a revoked admission.
- The after-handshake hook rejects unknown peers immediately. QUIC stream credit starts at zero and is granted only after the hook accepts. Each stream opens one loopback TCP connection, writes `AVDT1 <token> <peer-id>\n`, then copies bounded chunks with independent half-close. Transfer/listener failures reset the stream rather than pretending successful EOF.
- EOF and `stop` end connections and exit; explicit process exit follows bounded endpoint cleanup because Tokio's standard I/O can otherwise keep runtime shutdown waiting on an open pipe. No child process is started by the program.

## Limits

| Resource | Limit |
| --- | --- |
| Connections / streams per connection | Configurable 1–64 each; defaults provided by the instance are 64 |
| Pending handshakes | 16 total, 4 per source IPv4 /32 or IPv6 /64 |
| Handshake attempt token buckets | Global burst 32, refill 8/s; source burst 8, refill 1/s |
| Source accounting | 256 records; inactive records age out after 60 s; saturation rejects new sources rather than resetting recent budgets |
| Handshake deadline | 5 s |
| QUIC connection idle / keepalive | 6 s negotiated idle; 1.5 s connection and per-path keepalive; not a hard failure deadline |
| Connection with no open stream | 5 min; an active long-lived stream does not trigger this timer |
| TCP connect/header and control output | 2 s each |
| QUIC windows | 64 KiB/stream receive, 4 MiB/connection receive, 4 MiB/connection send |
| Copy buffers | Tokio copy's fixed 8 KiB per direction; destination backpressure |
| Unidirectional streams / datagrams | Disabled |
| Stock remote NAT address cap | 8 per round (not a per-second or per-connection probe cap) |

Over-budget incoming handshakes are silently ignored before application handshake work. Stateless QUIC Retry validates the source before it can consume a bucket or occupy a pending-handshake slot; normal native tests exercise this path. Source addresses are accounting keys only; they are not an identity/admission prefilter. The counter does not claim to bound noq's internal incoming queue or frames processed within a handshake.

## Transport liveness and interruption tolerance

The sidecar sets a **6 s connection idle timeout and 1.5 s connection/per-path keepalive**. The stock client is unchanged and negotiates the smaller idle timeout. The separate 300 s no-open-stream policy and admission refresh are unchanged. There is no receive-silence watchdog or ten-second failure deadline.

The choice prioritizes surviving a three-second interruption. In the isolated comparison at source `8583a5918456986bff12a317ba6cb91fe96c36d7` (Actions run `42983601155`), each outage had three keepalive phases, fresh endpoints, and quiet and in-flight cases. **Both stock and experimental NAT-zero servers gave these counts**, per traffic mode:

| Two-way outage | 4 s / 1.25 s: quiet, in-flight | 6 s / 1.5 s: quiet, in-flight |
| --- | --- | --- |
| 1.5 s | 3/3, 3/3 | 3/3, 3/3 |
| 2.5 s | 3/3, 3/3 | 3/3, 3/3 |
| 3 s | 2/3, 3/3 | 3/3, 3/3 |
| 3.5 s | 1/3, 2/3 | 3/3, 3/3 |
| 5 s | 0/3, 0/3 | 3/3, 1/3 |

Four seconds did pass all exact three-second in-flight cases; it was rejected because a quiet connection failed at three seconds and an in-flight case failed at 3.5 seconds. At the selected pair, three-second in-flight transfers completed without replay 379–601 ms after restoration. The fixture measures a known byte through the stand-in ingress, not a mobile HTTP workload.

Client-observed closure after a permanent silent blackhole, at the selected pair:

| Dropped direction | Stock observed | NAT-zero observed | Low-RTT single-path source envelope | CI ceiling (envelope + 3 s) |
| --- | --- | --- | --- | --- |
| Both | 8.008–10.376 s | 9.614–10.815 s | `2I = 12 s` | 15 s |
| Client → server | 11.438–16.637 s | 15.868–17.079 s | `3I + K = 19.5 s` | 22.5 s |
| Server → client | 8.011–10.378 s | 9.590–10.817 s | `2I = 12 s` | 15 s |

These are observations and conditional source envelopes, **not universal upper bounds**. In pinned noq-proto 1.4.0, `connection/mod.rs::reset_idle_timeout` applies a three-PTO floor; `packet_builder.rs` permits one ack-eliciting send-side reset after a receive, per path. The stock client's five-second heartbeat or another eligible send can extend its timer. Idle expiry is silent, so one-way loss can stack the server's expiry and the client's later expiry. Additional paths, high RTT/PTO and scheduling require separate qualification. The default per-path idle timer does not close the last path. Do not derive a hard deadline from the configured six seconds.

Both builds survived a 305-second open stream without application data and 120 seconds of periodic one-in-20 packet loss. Healthy no-stream connections explicitly closed at the existing 300-second application deadline. Over five full quiet-minute windows, NAT-zero averaged 39.2 UDP datagrams/minute each way (1,470 client→server and 1,213 server→client UDP-payload bytes/minute); stock averaged 43.0/41.8 datagrams and 3,593/2,768 payload bytes, including probes. These exclude network/link headers and say nothing about battery cost.

Both Linux artifact targets run the [idle/outage harness](scripts/idle-qualification/README.md) against an unchanged, hash-checked stock client. CI requires quiet survival and completion without replay for all tested outages through three seconds, plus the per-direction ceilings above. Longer interruptions remain measurements, not promised survivals. Evidence includes actual fault counters, raw case logs and client/server hashes.

## Dependencies and network surface

All direct dependencies are exact pins; `Cargo.lock` pins their transitive graph. Rust is pinned by `rust-toolchain.toml`. An iroh bump is excluded from automatic Dependabot updates and must be reviewed together with the Apple qualification pin.

| Direct dependency | Purpose |
| --- | --- |
| `iroh = 1.4.1`, defaults off, `tls-ring` only | Endpoint identity, authenticated QUIC streams, address watcher; same core/noq pins as `packaging/iroh-apple/versions.env` |
| `tokio = 1.54.1` | Runtime, TCP/stdin/stdout I/O, bounded copies, timers, synchronization; process support only for tests |
| `serde = 1.0.229` | Typed strict control-message encoding/decoding |
| `serde_json = 1.0.154` | NDJSON serialization |

`presets::Minimal` installs no relay or address lookup. Port mapping is disabled both in features and configuration. The resolved graph must exclude private Apple datapath, port mapping, optional mDNS/mainline discovery, metrics push, and n0 services clients. `iroh-metrics`' no-op types remain, with no features enabled.

**Compiled is not configured:** iroh's built-in DNS and PKARR lookup modules, including `iroh-dns` and `reqwest`, are unconditional in this release. They remain supply-chain/code surface, but this program never installs or calls an address-lookup service. The artifact manifest records those resolved dependencies. A feature check alone is not evidence of no egress; Linux captured-traffic qualification is separate.

### Before admission: stated security limit

The endpoint binds unspecified IPv4/IPv6 addresses (all eligible interfaces), with OS-chosen UDP ports at startup. Any host able to exchange UDP with a bound port can attempt a handshake—not just a paired device or a device on the same LAN. This can include the internet when the machine has a public address or a forwarded port and no intervening firewall. No automatic port mapping is enabled, but that does not establish a firewall boundary.

A device **never admitted by the instance** can complete transport authentication under its own endpoint identity. Before the application hook refuses it, stock iroh registers the connection and queues interface candidates, which can include eligible VPN/bridge/temporary IPv6 addresses. It can also process that peer's REACH_OUT request and send a QUIC path probe to a peer-named IP address and UDP port, including loopback or a non-loopback destination reachable from the instance. There is no address-class filter. The eight-address cap is per round; higher rounds reset it, so it is not a per-second or lifetime probe bound.

The probe is a transport-generated encrypted QUIC packet, not arbitrary UDP application data. The peer supplies the destination connection ID, up to 20 bytes; that CID is visible outside header protection. Pinned source constructs the PATH_CHALLENGE and cryptographic protection in the transport. The advisory capture described below tests the chosen CID bytes and a fixed non-loopback destination. It does **not** decrypt the body or prove the stronger claim that no other packet property can depend on peer input (for example negotiated parameters, timing or packet length).

That unadmitted device gets no application stream credit, no forwarded connection to the TCP ingress and no remote-session authorization. It does not receive the endpoint private key or ingress token. Admission protects application access, **not** pre-refusal candidate confidentiality or peer-directed UDP probing.

Stateless QUIC Retry first checks return reachability at the handshake's source address; then the global/per-source budgets and five-second handshake deadline above limit attempts. This check does not validate a different destination named by REACH_OUT and is not an identity allowlist. Immediate application refusal was observed at roughly 30 ms in the initial isolated run, but scheduling can change the window: it is not a 30 ms guarantee. Five initial immediate-refusal stock trials each received two ADD_ADDRESS frames and caused 1–2 35-byte UDP probes at the owned loopback receiver; a deliberately one-second delayed refusal produced seven probes/trial. These are observed counts with the original client CID, not amplification/rate bounds. Handshake limits and prompt refusal reduce exposure; neither removes it.

## Qualification and artifacts

The `avibe-direct-transport` workflow runs only on dispatch or PR changes to this crate/workflow. It builds and tests natively on macOS arm64/x86_64, Linux arm64/x86_64 musl, and Windows x86_64, with `--locked` and a resolved graph check per target. Linux binaries must have no ELF interpreter/shared-library dependency. macOS binaries are ad-hoc signed and passed through the shared private-datapath scanner plus a positive control.

Each platform produces one archive, SHA-256 file, and manifest recording binary size/hash, pins, resolved features, toolchain, source revision, and release build time. These are **workflow artifacts only**: no release, publication, runtime manifest pin, installation, or update integration in this lane. A successful platform build does not imply cross-device/privacy/firewall qualification.

Native tests exercise exact header identity/token, both half-close orders, refusal without a round trip, expiry, refresh, revoke-then-readd, stream credit, listener errors, wrong ALPN, malformed/oversized input, `stop` with an open stdin, and EOF/socket cleanup. Admission and handshake accounting also have deterministic unit tests.

On both Linux targets, a disposable network namespace captures every interface without a capture filter. A separate empty sink namespace provides default routes so unexpected public-service traffic is observable rather than failing before a send. The checker correlates source ports with process-owned sockets, reassembles the exact loopback header and payload, rejects unrelated UDP/TCP (including DNS), and verifies capture canaries, complete records, and zero kernel drops. It adds/removes synthetic private IPv4/global IPv6 addresses and dials newly added addresses through the still-running endpoint. Offline positive/negative tests exercise the capture policy.

### Advisory NAT-disabled experiment

The separate, non-gating/non-shipping Linux job first builds a stock-client test executable. It then vendors pinned dependencies in disposable runner storage, applies [`iroh-1.4.1-allow-nat-zero.patch`](experiments/iroh-1.4.1-allow-nat-zero.patch), updates that vendored file's checksum, and builds only a separate experimental server with NAT cap zero. The prebuilt stock client (hash checked unchanged) runs the same admission/stream tests and dead/live address-family candidate tests against it in an isolated network namespace. The job uploads evidence only, never a binary.

The patch only allows zero through iroh's wrapper; noq already interprets zero as extension disabled. It is **not used by any packaged binary**. Owner approval is required before any proposal to carry it in a shipping dependency.

The advisory job also measures path blackholing through a test-owned loopback proxy; runs a separate encrypted hostile-REACH_OUT test with a NAT-enabled positive control; and compares candidate/probe counts for unadmitted peers under immediate and deliberately delayed rejection. The probe client is explicitly distinct from the unmodified interoperability client. Only that process opts into a deterministic 20-byte CID generator: 16 ASCII bytes `AVIBE-PROBE-CID-` followed by a unique four-byte sequence. Predictable CIDs are deliberately test-only; all separately spawned servers keep their default generator.

Five trials per server/refusal variant target each of two owned UDP sockets: `127.0.0.1` and `192.168.245.2`, the latter explicitly assigned inside the route-less namespace. The fixture verifies it is outside the host namespace, permits only these target classes, checks the sender's port/address, records full UDP payload hex and all issued CIDs, and asserts bytes 1–20 (zero-based offset 1, excluding the first header byte) equal a complete client-issued CID. Positive controls require observed probes at **both** destination classes; NAT-zero requires no probes or candidate frames. Every trial separately checks that no unadmitted TCP ingress connection occurs. See `pre-admission-probes.jsonl` in the advisory evidence for actual counts/bytes; do not describe an unrun capture as a measured result.

The delayed hook, deterministic client generator, wrappers and hostile-frame injection are applied only in disposable CI source copies. None changes the production crate or its dependency lock. Results are evidence, not a release decision; owner approval of the stock exposure and supply chain is still required.

On a pin bump, rerun the unchanged-client directional blackholes, quiet/in-flight three-second outage gates and quiet/loss costs, and re-derive the per-direction source envelopes (including PTO floors, send-side reset and last-path behavior); do not copy the current ceilings without rechecking. Also recheck wrapper/negotiation semantics, candidate fan-out and CID/destination exposure, hostile unnegotiated frames, native graph/binary checks, admission ordering, address changes, and migration/roaming. Without NAT traversal a new dial can use supplied alternate addresses, but an existing connection cannot assume it will switch to another candidate. Ordinary QUIC source-address validation remains a distinct surface, including a source-read concern about off-path padded responses; this patch is not a claim of zero UDP reflection.

## Not yet proven by this lane

Real-device LAN/global IPv6 reachability, mobile roaming/fallback timing, macOS Local Network prompts and denial in app/service/terminal launch modes, host firewalls, production signing/distribution, and sustained adversarial network behavior require separate qualification. Source reading says unspecified binds report concrete interface addresses and update on interface changes; the Linux add/remove probe provides runtime evidence, not proof for every host OS/network.
