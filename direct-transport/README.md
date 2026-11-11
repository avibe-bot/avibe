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
| Connection with no open stream | 5 min; an active long-lived stream does not trigger this timer |
| TCP connect/header and control output | 2 s each |
| QUIC windows | 64 KiB/stream receive, 4 MiB/connection receive, 4 MiB/connection send |
| Copy buffers | Tokio copy's fixed 8 KiB per direction; destination backpressure |
| Unidirectional streams / datagrams | Disabled |
| Stock remote NAT address cap | 8 per round (not a per-second or per-connection probe cap) |

Over-budget incoming handshakes are silently ignored before application handshake work. Stateless QUIC Retry validates the source before it can consume a bucket or occupy a pending-handshake slot; normal native tests exercise this path. Source addresses are accounting keys only; they are not an identity/admission prefilter. The counter does not claim to bound noq's internal incoming queue or frames processed within a handshake.

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

**Before admission remains a security decision.** Stock iroh registers a connection and queues interface candidates before invoking the hook. It can process peer-named REACH_OUT destinations below that hook. There is no address-class filter, and higher rounds reset the per-round cap. Immediate rejection and handshake caps shorten exposure, not remove it. Unspecified binds can advertise eligible VPN/bridge/temporary IPv6 addresses. Do not interpret the hook as a pre-transport confidentiality boundary.

## Qualification and artifacts

The `avibe-direct-transport` workflow runs only on dispatch or PR changes to this crate/workflow. It builds and tests natively on macOS arm64/x86_64, Linux arm64/x86_64 musl, and Windows x86_64, with `--locked` and a resolved graph check per target. Linux binaries must have no ELF interpreter/shared-library dependency. macOS binaries are ad-hoc signed and passed through the shared private-datapath scanner plus a positive control.

Each platform produces one archive, SHA-256 file, and manifest recording binary size/hash, pins, resolved features, toolchain, source revision, and release build time. These are **workflow artifacts only**: no release, publication, runtime manifest pin, installation, or update integration in this lane. A successful platform build does not imply cross-device/privacy/firewall qualification.

Native tests exercise exact header identity/token, both half-close orders, refusal without a round trip, expiry, refresh, revoke-then-readd, stream credit, listener errors, wrong ALPN, malformed/oversized input, `stop` with an open stdin, and EOF/socket cleanup. Admission and handshake accounting also have deterministic unit tests.

On both Linux targets, a disposable network namespace captures every interface without a capture filter. A separate empty sink namespace provides default routes so unexpected public-service traffic is observable rather than failing before a send. The checker correlates source ports with process-owned sockets, reassembles the exact loopback header and payload, rejects unrelated UDP/TCP (including DNS), and verifies capture canaries, complete records, and zero kernel drops. It adds/removes synthetic private IPv4/global IPv6 addresses and dials newly added addresses through the still-running endpoint. Offline positive/negative tests exercise the capture policy.

### Advisory NAT-disabled experiment

The separate, non-gating/non-shipping Linux job first builds a stock-client test executable. It then vendors pinned dependencies in disposable runner storage, applies [`iroh-1.4.1-allow-nat-zero.patch`](experiments/iroh-1.4.1-allow-nat-zero.patch), updates that vendored file's checksum, and builds only a separate experimental server with NAT cap zero. The prebuilt stock client (hash checked unchanged) runs the same admission/stream tests and dead/live address-family candidate tests against it in an isolated network namespace. The job uploads evidence only, never a binary.

The patch only allows zero through iroh's wrapper; noq already interprets zero as extension disabled. It is **not used by any packaged binary**. Owner approval is required before any proposal to carry it in a shipping dependency.

The advisory job also measures path blackholing through a test-owned loopback proxy; runs a separate encrypted hostile-REACH_OUT test with a NAT-enabled positive control; and compares candidate/probe counts for unadmitted peers under immediate and deliberately delayed rejection. That last probe client uses test-only wrappers around existing noq APIs, names only its own loopback listener, and is explicitly distinct from the unmodified interoperability client. The delayed hook, wrappers, and hostile-frame injection are generated/applied only in disposable CI source copies. None changes the production crate or its dependency lock. Results are evidence, not a release decision; the PR is not ready for approval until the remaining security choice is made.

On a pin bump, recheck wrapper/negotiation semantics, candidate fan-out, hostile unnegotiated frames, native graph/binary checks, admission ordering, address changes, and migration/roaming. Without NAT traversal a new dial can use supplied alternate addresses, but an existing connection cannot assume it will switch to another candidate. Ordinary QUIC source-address validation remains a distinct surface, including a source-read concern about off-path padded responses; this patch is not a claim of zero UDP reflection.

## Not yet proven by this lane

Real-device LAN/global IPv6 reachability, mobile roaming/fallback timing, macOS Local Network prompts and denial in app/service/terminal launch modes, host firewalls, production signing/distribution, and sustained adversarial network behavior require separate qualification. Source reading says unspecified binds report concrete interface addresses and update on interface changes; the Linux add/remove probe provides runtime evidence, not proof for every host OS/network.
