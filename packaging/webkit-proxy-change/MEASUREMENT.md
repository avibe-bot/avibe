# Disposable WebKit route measurement

This is a branch-only experiment, not product code. All Apple builds and simulator
execution run in GitHub Actions. Never run the runner on the owner's Mac.

## Observation contract

The matrix has **27 cells**: four cold H1/H2 controls, two idle-pool installs,
one active H2 stream install, four removal variants, four in-flight variants,
four NetworkProcess restart variants, six failure-listener controls
(absent/refuse/stall × allowFailover false/true), and two constant-proxy controls.
The experiment also has exactly **three lifecycle cells** on the newest installed
iOS runtime: reload, same-data-store WebView recreation, and App kill/relaunch.
Each lifecycle cell warms a TLS H1 connection through route A (direct), verifies a
same-identity GET remains open at the lifecycle boundary, installs route B (the
CONNECT proxy) before the lifecycle load, and correlates the new page/fetch
requests with their authoritative route and proxy CONNECT identity. Lifecycle
cases answer whether new traffic uses route B; true and false are both valid
measured outcomes, while missing evidence is unmeasured. They do not claim
old-stream survival.
`evidence.py` checks the 27-cell matrix independently of completion markers.

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
one host-launch XCTest → exactly three newest-runtime lifecycle cases → one direct
TLS XCTest → one cold CONNECT XCTest → one matrix XCTest. Simulator migration can overlap
compilation, but readiness still must pass before installation; overlap is not a
proven cure for migration stalls. Every XCTest gate requires exactly one expected
passing test. The lifecycle stage is an app-launch/terminate orchestration gate,
not an XCTest count gate. The early H1 controls prove the basic path; each H2 cell
must independently prove its protocol/reuse.

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

## Pending Q2/Q3/Q4 extension (not executed)

`instance-cases.json` adds ten separate cells after the unchanged matrix:
Q2 (control-plane-used store, fresh instance host) and Q3 (warm A, exact host
union adds fresh B), each H1/H2; Q4 port replacement on two live stores, each
H1/H2, with cooperative closure, occupied old port/new sockets, and occupied
old port/existing sockets. `InstanceRouteTests.testInstanceRoutes` is a separate
one-test stage; `instance_evidence.py` writes `instance-observations.json` even
when that stage is skipped or interrupted. This extension has no measured result.

Each store is named and recorded by UUID/handle; config writes have per-store
sequence, exact sorted hosts, port, start/end, and explicit `allowFailover=false`
for isolation (not a shipping-default decision). P/P2 use epochs 1/2; stand-ins
have no epoch. Each new-host/recovery probe is immediate uncached fetch, unique
navigation, then fetch, without harness retry. Fresh-host fetch is a cross-origin
GET from the established control/A document, allowed by cell-scoped fixture CORS;
only correlated server/CONNECT evidence qualifies it. Q3 keeps A's WebView alive
and reports A route and connection preservation separately from B admission.

Every listener in these cells runs in a child process; stand-ins are independent
of the recovery-owned relays. Recovery can stop only `listener(1)` workers. A
stand-in first forwards (existing-pool case), then pauses without closing its
sockets, or exclusively binds the released old port and stalls new CONNECTs.
Test teardown happens after observation-end, not as part of recovery. A completed
uncached origin request can prove bypass; absence of a new CONNECT cannot.
H2 cross-host reuse is retained with request host, TLS SNI and CONNECT authority;
it is not silently credited as a fresh intended-host CONNECT.

GET failures and mixed routes are measured false; absent preconditions or
unsettled/uncorrelated operations yield null/unmeasured, with partial operations
preserved. Fetch aborts after 8 s and settles before proceeding; its native bridge
has a 12 s watchdog plus 2 s cancellation grace. Navigation has 15 s plus 2 s
cancellation grace; callbacks are identity-fenced. If cancellation cannot be
acknowledged, the view is explicitly unmeasured, never reset to obtain success.
Whole-cell work is capped at 240 s; the separate XCTest wrapper at 2700 s.
These are fixture bounds, not product recovery latency promises.

No App restart, private NetworkProcess restart, TLS termination, arbitrary target,
POST replay, or old-stream-survival assertion is introduced. All targets are
allowlisted runner-loopback TLS fixtures. The original 27-cell and lifecycle
sources/outcomes remain separate and unchanged. Q4 cooperative success alone
cannot qualify occupied-port recovery, and true failover/device behavior remains
outside these ten isolation cells. No Actions round is authorized merely by
adding this source.

Pure additions (no listeners or child processes):

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s packaging/webkit-proxy-change -p 'test_instance_evidence.py' -v
node packaging/webkit-proxy-change/test-instance-pure.mjs
```

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
