# iOS direct interoperability host

This is a test-only, hosted interoperability gate, not direct-transport
installation or activation. `ios/scripts/direct_interop_host.py` owns the
loopback fixtures, sidecar children and independent host verdicts;
`ios/scripts/run_direct_interop.py` consumes the merged published pins and
supervises XCTest. The App test harness and its Xcode graph are separate inputs.

## Execution boundary

The `ios-direct-interop` workflow runs source-only Python contract tests on
relevant pull requests. Those tests include real loopback TCP and a deliberately
non-native subprocess protocol fixture; they are **not** Apple/iroh evidence.

The native job is opt-in through `workflow_dispatch` on **master** with
`run_interop: true`. It requires the exact freshly fetched merged master commit
and a clean index/working copy with no untracked source inputs. This binds the
entire consuming source tree, including every App/runtime file and build helper,
rather than trusting a partial filename list. The Apple pin, sidecar pin, App
adapter, interoperability XCTest source and Xcode graph are additional named
readiness checks, not the limit of that source fence.
A pending sidecar pin or missing App test source is a dependency failure, not a
successful or skipped interoperability run. There is no candidate-pin or binary
path override.

Actual release assets are downloaded and verified in `RUNNER_TEMP`. The sidecar
validator checks the complete five-platform published pin, version/tag/source URL,
archive names/paths/sizes/checksums and qualification provenance, then verifies
the selected archive's binary identity, source, version and qualification.
Only the verified Apple build input is copied to the App graph's staging directory. The
sidecar is executed from the temporary workspace, never from an installed
runtime. The host records the child PIDs and each per-case fixture, drains both
sidecar pipes, closes listeners/connections, and reaps its children on completion,
control failure, timeout or cancellation. Cleanup is idempotent and independently
owned: cancellation of a serving task or repeated cancellation of `close()` cannot
abandon its bounded stop/TERM/KILL/reap sequence. The caller joins that cleanup
before propagating cancellation; shielding without joining is not sufficient.
The runner also tracks its recorded fresh-session process group's liveness
independently of the xcodebuild leader. Even an already-exited leader does not
skip group cleanup: surviving members receive bounded TERM/KILL escalation, and
success requires the group to be gone and the owned leader to be reaped.
It does not install anything, enable
transport, modify pairing, use the owner's live runtime, provision certificates,
perform public DNS/backend calls, or authorize device/TestFlight qualification.

## Frozen v2 control contract

A test method opens one loopback control TCP connection. Every NDJSON record,
including its terminating LF, is at most 4 KiB. Duplicate JSON keys, truncated
lines, unknown messages and non-finite numbers are errors. Message types are
only `hello`, `next`, `case`, `setup_done`, `armed`, `triggered`, `result`,
`verdict`, and `done`.

The fixture is an absolute JSON file passed as
`TEST_RUNNER_AVIBE_DIRECT_INTEROP_FIXTURE`:

```json
{"schema":2,"control":"127.0.0.1:<port>","result_dir":"<absolute directory>","case_deadline_seconds":20,"setup_deadline_seconds":10}
```

The App sends `hello` with `schema: 2` and `tier: engine|transport`. For each
case it sends `next` with a fresh 64-hex client endpoint ID. The host returns
`case` with `name`, `sidecar_endpoint_id`, `addresses`, `admissible_addresses`,
`alpn: avibe-direct/1`, `hostname: interop.invalid`,
`instance_id: interop-fixture`, `generation: interop-v2-1` (a **string**), and
`lifetime_seconds: 300`. The hostname is a CONNECT authority, not a DNS lookup.

### Setup barrier and bytes

Admitted cases first establish a successful setup probe, with retries bounded by
the separate setup deadline. Setup streams never count as case streams. There
is no sidecar `admit` acknowledgment or production test hook.

After retiring setup, the App sends:

```json
{"type":"setup_done","schema":2,"name":"<case>"}
```

The host drains the retired setup streams, switches its answer/count phase, then
returns the **same** `setup_done` envelope. The App waits for this acknowledgment
before starting its case dial. A write completing on the control socket does
not establish ordering on the separate ingress socket; this barrier does.
Draining and acknowledgment remain bounded by the setup/case deadlines.

Every probe writes exactly:

```text
CONNECT <hostname>:443 HTTP/1.1\r\nHost: <hostname>:443\r\n\r\n
```

The setup and accepted case answer is exactly
`HTTP/1.1 200 Connection Established\r\n\r\n`. The refused case answer is exactly
`HTTP/1.1 403 Forbidden\r\n\r\n`.

The payload is UTF-8 `avibe-direct-interop-v2\n`: **24 bytes**, including LF.
The host echoes it and independently measures byte counts, SHA-256 and FIN/reset
behavior. It records counts/hash rather than payload content. A probe is a
separate stream, which the App may abandon/reset after receiving its answer;
that must not be mistaken for a payload FIN.

### Required engine inventory

Every case runs once, in this order, using a fresh client key/endpoint and fresh
host sidecar:

| Case | Setup | Case and independent host evidence |
| --- | --- | --- |
| `admitted` | 200 probe | One 200 probe plus one payload stream; authenticated AVDT1 client identity, exact bytes/hash, FIN in both directions, no payload reset. |
| `not_admitted` | None; no retry | No admission; actual App peer/handshake rejection before its deadline; zero ingress connections, including malformed ones. |
| `wrong_identity` | None; no retry | Wrong valid server identity, distinct from client and actual sidecar identities; App handshake failure and zero ingress. |
| `refused_connect` | 200 probe | After the barrier, exactly one 403 probe, no payload; App `probe_refused`, not local timeout. |
| `admission_ends` | 200 probe | Case 200 probe and payload; alive at `armed`, then TTL 0; actual `admission_ended` event and stream closure after the host trigger. |
| `sidecar_stops` | 200 probe | Case 200 probe and payload; alive at `armed`, then `stop`; actual clean recorded-child exit and stream closure after the host trigger. |

The fixed wrong public identity is the Ed25519 basepoint encoding `58` followed
by 31 bytes `66`; no private key is supplied. It is not the client's own ID,
which the real rendezvous validator would reject before the handshake.

For trigger cases, the App sends `armed` only after payload exchange while the
stream is alive. The host independently checks that state, records the trigger,
performs it, and sends `triggered`. Closure may reach the App before the
`triggered` control line; the App asserts alive-at-armed and closed-after-armed,
not an ordering between different network paths or host/App clocks.

Negative evidence contains only sidecar events actually emitted. The pinned
sidecar may reject a peer before any client-specific `peer_closed` event. Zero
ingress is mandatory; a nonexistent rejection event must never be invented.
Observed reason strings may include `admission_ended`, `idle_timeout`, `stopped`,
or `connection_closed`. A local deadline is not a successful negative result.

## Evidence and reconciliation

The App sends a `result` with `schema`, `name`, `outcome`, `observed`, and
nullable `detail`. The host independently answers exactly:

```json
{"type":"verdict","schema":2,"name":"<case>","host":"confirmed","detail":null}
```

A failure uses `host: contradicted`. No setup count is sent in the verdict: the
App counts its own setup streams and the host counts its own accepted ingress
streams. They must agree. The host never accepts an App `passed` as sufficient
evidence for its own `confirmed`. Case assertions must hold before cleanup, so
cleanup cannot manufacture their initial proof, and must still hold after the
sidecar and ingress fixture have fully closed. The final artifact records that
terminal snapshot: a valid late shutdown probe cannot disappear behind an
earlier zero-ingress verdict.

`engine.json` and `transport.json` belong to the App. `host.json` has the six
engine cases at the top level and conditional transport evidence under
`transport`. All use `schema: 2`, `tier`, `status`, and ordered `cases`. Host
records include the independent observations and a canonical SHA-256 of the
App's control-channel case record. Reconciliation verifies that the App's final
file contains that same record, not different observations written afterward.
Host closure times are relative monotonic milliseconds (`armed_ms`,
`trigger_ms`, `closed_ms`), never wall-clock comparisons with the App.

The engine tier is mandatory. Transport uses only actual RFC1918/ULA addresses
advertised by the sidecar. If none exist, the host's first `admitted` offer has
`admissible_addresses: []`; the App sends `result: not_covered`, receives a
confirmed verdict and `done`, and writes `transport.json` with
`status: not_covered` and `cases: []`. This is the sole allowed coverage skip.
An offered address that fails validation or dial is a failure, not a skip.
If a later offer loses its admissible addresses after coverage has begun, the
tier fails and retains its earlier case records; it cannot become `not_covered`.

Success requires xcodebuild exit 0, host exit 0, and both independent artifacts
agreeing on all six engine cases as passed/confirmed. Missing files, early
`done`, duplicate/reused cases/keys, mismatched byte counts, false FIN, bad
AVDT1, child failures, changed pins, and any required tier's missing evidence
fail closed. The workflow uploads result files, PID records, provenance,
xcodebuild log and xcresult on failure too. Native success does not qualify the
WebKit/listener path, Local Network privacy, installed state, public-CA access,
registration, device behavior or transport enablement.
