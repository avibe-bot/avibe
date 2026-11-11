# Disposable WebKit route measurement

This is a branch-only experiment, not product code. All Apple builds and simulator
execution run in GitHub Actions. Never run the runner on the owner's Mac.

## Observation contract

The matrix has **27 cells**: four cold H1/H2 controls, two idle-pool installs,
one active H2 stream install, four removal variants, four in-flight variants,
four NetworkProcess restart variants, six failure-listener controls
(absent/refuse/stall × allowFailover false/true), and two constant-proxy controls.
`evidence.py` checks that inventory independently of completion markers.

The matrix XCTest passing means execution/observation coverage, **not transport
qualification**. An unchanged route or an explicit fetch/SSE/WebSocket error can
be a valid negative observation. Missing setup, unknown protocol, failed H2
reuse, missing listener activity, or unavailable process restart is unmeasured.

- Every operation retains its client result, including `ok:false` and errors.
- Successful responses must match server request/response events, TLS socket ID,
  HTTP connection/session ID, and H2 stream ID where applicable.
- Proxied requests reference their CONNECT ID through the proxy's upstream
  socket tuple. Existing CONNECT sessions are valid; no new CONNECT is required
  for each reused request. An origin response through the constant proxy is
  reported as `origin-via-proxy`, not unproxied fallback.
- Failover claims require the initial origin socket to be positively closed and
  the new origin response to use another socket. Refusal/stall require a
  host/port-correlated listener event; absent-port controls require a refused
  connection check. A timeout records bounded failure with no successful route,
  never an inferred origin or proxy route.
- Constant-proxy controls establish an SSE on the warmed H2 session first.
  Closure names the actual CONNECT socket being destroyed. SSE snapshots retain
  the original connection/stream and later identities; automatic EventSource
  reconnect is not survival of the original stream.
- Every operation uses GET. **Non-idempotent replay safety is not measured** and
  remains a future App acceptance gate. GET attempt counts are retained;
  multiple attempts are not hidden or promoted into a no-replay claim.

## Gates and artifacts

Build → actual-product/loader/xctestrun validation → simulator ready → install →
one host-launch XCTest → one direct TLS XCTest → one cold CONNECT XCTest → one
matrix XCTest. Simulator migration can overlap compilation, but readiness still
must pass before installation; overlap is not a proven cure for migration stalls.
Every test gate requires exactly one expected passing test. The early H1 controls
prove the basic path; each H2 cell must independently prove its protocol/reuse.

`matrix-observations.json` contains per-case and per-operation verdicts, raw client
results, authoritative routes, attempted routes, connection IDs, and old-stream
identity/survival information. It is written even when the matrix XCTest fails,
so partial observations survive. `case_complete` is only an execution marker;
completion without operation coverage or alongside `case_unmeasured` is rejected.
`*-observations.json` records the minimal gates. Raw `cert/fixture.jsonl`, XCTest
logs/results, runtime inventory/build, exact commit, Xcode version/build, and
WebKit diagnostics remain the underlying evidence. Skipped stages have null exit
codes and their own blocking cause.

Commands have bounded runtime. In particular, each restart cell gets one 8-second
navigation attempt, not ten 20-second retries; JS evaluation has a 12-second
bound; browser cleanup does not await an unbounded JS promise. The whole matrix
has a 900-second ceiling. Timeouts remain explicit unmeasured/failed observations.

## Cheap local checks (no Apple execution)

```sh
python3 -m unittest discover -s packaging/webkit-proxy-change -p 'test_evidence.py' -v
python3 packaging/webkit-proxy-change/validate-stage.py bundle \
  <downloaded-artifact>/derived-data/Build/Products/Debug-iphonesimulator/ProbeHost.app \
  <downloaded-artifact>/build-for-testing.log
node --check packaging/webkit-proxy-change/fixture.mjs
bash -n packaging/webkit-proxy-change/run-measurement.sh
```

Synthetic evidence tests exercise the interpreter, not WebKit. Simulator results
cannot qualify real-device Local Network permission, suspension, radio changes,
real Cloudflare behavior, or future OS transports. No iOS version is qualified by
API availability, compilation, or this harness's synthetic tests.
