# Model Hub inference deadlines

## Problem and contract

The September 8, 2026 investigation found that Model Hub reused its 60-second
transport budget for response headers, the first response bytes, first model
output, and completion of buffered inference. A connected, healthy provider can
take longer at any of these stages. Retrying discards its work and can exhaust
the caller's retries without ever allowing the model to finish.

An admitted inference request must remain live until protocol completion, a
concrete transport failure, or cancellation by its owner. Elapsed model time
alone is not failure. This property applies to every supported protocol and to
both streaming and buffered responses. Cancellation must still release the
connection, temporary storage, and source ownership, and a waiting request must
not block unrelated requests.

## Scope

- Remove inference wall-clock deadlines in the engine client.
- Retain bounded connection establishment, management/discovery calls, reading
  an already-failed HTTP response, and resource cleanup.
- Start the finite local buffered-response parsing budget after the complete
  response has arrived, so model execution cannot consume it.
- Audit backend idle reclamation, gateway cancellation, and native caller
  timeouts before closing the change. Record evidence and scope decisions here.
- Treat a known source cooldown as delayed admission, with one cancellable wait
  and a fresh resolution at its existing recovery time. Only retry a request
  that had no runnable candidate; never replay visible output or restart a
  failed attempt inside the same gateway request.
- Recognize Responses reasoning-text and compatible Chat reasoning-content
  deltas as model output using the existing protocol taxonomy.
- Keep production configuration and the running service unchanged during
  development; verify with hermetic tests and local Incus where applicable.

## Validation

Exercise delayed inference at each wire boundary beyond the connection budget,
then assert successful completion or owner-driven cancellation. Preserve error
classification, usage, response replay, source isolation, and shutdown tests.

## Audit inventory

| Boundary | Initial evidence | Decision |
| --- | --- | --- |
| Engine response headers / first bytes / first output | Uses transport timeout | Remove inference deadline |
| Buffered inference completion | Uses transport timeout | Remove inference deadline |
| Buffered local parsing | Shares deadline with body download | Give local parsing its own budget |
| Gateway teardown and cancellation drain | Runs after completion/cancellation | Retain cleanup bounds |
| Codex and Claude active-session reclamation | Age backstop overrides durable active ownership | Require the existing ownership veto at both reclamation checks; retain repair of orphaned local flags |
| Codex SSE idle | Native default is 300,000 ms, after HTTP headers | Retain the native policy; zero expires immediately, not disabled. No arbitrary huge override and no early gateway headers |
| Source cooldown | A waiting route returns 409; Codex does not honor HTTP Retry-After | Delay no-candidate admission until the known retry time once; re-resolve current configuration; use 503 and Retry-After for remaining time-recoverable errors |
| Prelude output classification | Responses reasoning text and Chat reasoning content were treated as metadata | Extend the existing taxonomy and acceptance fixtures |
| Prelude storage | Memory spills at 256 KiB; replay has deliberately no total byte ceiling | Preserve lossless replay and cancellation cleanup, not a new response-size rejection policy |
| Codex local control RPC | 120-second acknowledgement deadline; turns run via notifications | Retain, not an inference deadline; resetting initialization on one slow RPC is a separate transport issue |
| OpenCode active turn | Default duration cap already disabled | No change |
| OpenCode admission and stale overlay cleanup | 120-second acceptance check and 30-second old-process drain | Retain; neither limits an accepted live turn |
| HTTP request body | 16 MiB input cap; oversized requests use aiohttp's 413 | Retain size protection; protocol-shaped 413 rendering is a separate issue |

Base inspected: `6bad0ca137ca9a72678511a094d366aa7e48af36`.

## Ownership decision

The ownership snapshot proves an owner still exists, not that its process is
healthy. The old age backstop has no discriminator between a silent valid turn
and a live-but-deadlocked backend. Narrowing the veto to ignore the resource's
own active Turn would therefore restore the same demonstrated false eviction.
This change deliberately prioritizes owner-controlled termination. Explicit
Stop, native terminal/EOF handling, and reclamation of unowned local flags remain.
A live deadlock with no terminal evidence can remain until explicit Stop; do not
claim that this change introduces automatic deadlock detection. Such detection
needs independent backend evidence, not another elapsed-inference threshold.

## Incident evidence

Local service logs and durable Turn rows were compared for the two reported
Sessions. Times below are UTC+08:00 on September 8, 2026.

- `sesssbbj4mt2y`: forced progress-timeout settlement at 00:22:37, 01:21:07,
  09:00:12, and 09:50:27, with native reconnect messages before settlement.
- `sesvmgbdub2gp`: the same forced settlement at 01:31:15. A late native 503
  followed at 01:34:35; both durable rows name the same native Turn.
- `sesssbbj4mt2y` likewise recorded a late 503 at 09:57:16 against the native
  Turn forcibly settled at 09:50:27. These rows are not independent upstream
  outages and must not be counted as such.
- Another observed call cooled its sole source at 09:14:52 until 09:15:22,
  while the caller exhausted reconnects by 09:14:56 and failed with 409.
- The generic 409 at 09:58:14 in `sesvmgbdub2gp` has no attributable gateway
  provenance. It must not be asserted to be the cooldown case without evidence.
  Route/configuration conflicts remain terminal; their safeguards are unchanged.

## Native caller evidence

Verified against the installed Codex 0.153.2 and its exact official source tag:

- [HTTP retry implementation](https://github.com/openai/codex/blob/rust-v0.153.2/codex-rs/codex-client/src/retry.rs)
  uses local backoff and does not read Retry-After.
- [Native regression test](https://github.com/openai/codex/blob/rust-v0.153.2/codex-rs/core/tests/suite/retry_after.rs)
  explicitly asserts local backoff despite a Retry-After header.
- [SSE consumer](https://github.com/openai/codex/blob/rust-v0.153.2/codex-rs/codex-api/src/sse/responses.rs)
  applies its idle deadline to the next parsed SSE event, after headers.
  SSE comment heartbeats do not necessarily reset that event-level deadline.
  The gateway still defers headers until model output or a protocol terminal,
  preserving pre-output fallback and HTTP error semantics.

The native SSE cap and a provider's own limits remain outside the removed
Avibe inference deadlines; this PR does not claim end-to-end infinite patience.

## Validation evidence

- Real loopback HTTP tests cover all three protocols, five delayed inference
  boundaries, and completion/cancellation. They assert no timeout or cooldown
  classification for slow successful inference and closed client resources.
- Gateway HTTP tests cover known cooldown, repeated failure, configuration
  changes during the wait, and cancellation for every backend.
- Existing protocol classification, byte replay, usage, teardown, native
  termination, and stale-local-flag repair suites remain required.
- Focused combined local validation: 1,223 passed, 30 subtests passed, one
  previously expected xfail. Changed Python files pass Ruff.
- Production configuration, credentials, and installed service are unchanged.

## Review inventory and scope decisions

The orchestrator fetched every review thread, review verdict, and matching lint
run before this revision. Review `5136689855` examined
`2d0700c64c849ba97e71ec7b4cbc7f9cf9522492` and raised three findings:

| Root-cause class | Finding | Complete correction |
| --- | --- | --- |
| Timestamp compatibility | Valid UTC `Z` strings fail on Python 3.10 | Normalize the offset before parsing; test both accepted UTC forms through source resolution and the gateway |
| Cancellation ownership | Transport admission shields an unbounded upstream wait | Propagate cancellation until a handle is acquired; retain shielding for finite settlement; carry already-observed wire facts on cancellation for usage accounting |
| Retry provenance | A previous no-candidate marker wins over a later admitted attempt | Clear obsolete supply facts and their terminal projection at admission; cover later success, failure, and cancellation |

This is the first findings-bearing head for all three classes; no repeated-class
circuit breaker has tripped. No architecture or persistent data-model rewrite is
required. The adapter's cancellation exception extends `CancelledError` with
already-observed wire facts only; it introduces no source failure, retry, or new
terminal outcome. Its canonical interface and mirror change together with both
consumers. Unbounded inference remains cancellable; finite ledger/resource work
retains its existing owner and limits.

The expanded local regression passed 1,410 tests and 30 subtests, with one
pre-existing xfail; changed Python files pass Ruff and whitespace checks.

The new complete gateway-to-service-to-adapter-to-HTTP test reproduced the
original leak: downstream cancellation returned while the loopback provider
request remained open. It now verifies every wire wait and protocol, transport
lease release, and exactly-once accounting of pre-output usage. Existing tests
for a response beating cancellation now synchronize on actual handle availability,
not mere transport admission.

Local Incus installation, startup, and health verification completed on the first
head, with 803 passing tests, 30 passing subtests, and one existing xfail. Only
the task-specific `avr-wt-model-hub-inference-deadlines` environment was used.
The first lint run's migration-fixture failure is supplied by merged dependency
#1933 (`a58644528deb8365876915904d11fb89df48db11`); integrate it normally before
requesting the next exact-head review and CI.

### Second-head circuit breaker and orchestrator decision

The full paginated inventory contains five findings across two reviewed heads:
`2d0700c64c849ba97e71ec7b4cbc7f9cf9522492` (review `5136689855`) and
`bb0ba11f056084884231c89814084b23c07594ad` (review `5136913971`). The first
head's three findings are listed above. The second head repeats two classes:

First-head threads: `PRRT_kwDOPbFPYs6gEzJI` (timestamp),
`PRRT_kwDOPbFPYs6gEzJM` (cancellation), and `PRRT_kwDOPbFPYs6gEzJN` (provenance).

| Root-cause class | Second-head finding | Full-chain diagnosis |
| --- | --- | --- |
| Timestamp semantics | Thread `PRRT_kwDOPbFPYs6gFJW0`: string ordering selects the wrong cooldown deadline | Validation preserves offsets. Parsing at the gateway alone does not fix selection in `turn_supply_facts`; the probe API has the same string minimum. Resolver readiness, service comparisons, and gateway delay must share timestamp semantics |
| Retry provenance | Thread `PRRT_kwDOPbFPYs6gFJW2`: an earlier exhausted projection overrides Stop after readmission | Admission clears only no-candidate projections and only when a supply marker exists. Exhaustion has no such marker. All supply-level retry projections need the same admission boundary, independently of that marker |

Both repeated classes reached the two-head threshold. The orchestrator paused
editing, inspected all five threads and their consuming paths/tests, and chose
the following smallest complete correction before resuming:

- Move the existing service timestamp parser to the existing resolver module.
  Reuse it for readiness, source/OAuth time comparisons, both earliest-deadline
  selections, and gateway delay. Compare aware instants, preserving the selected
  original string for rendering and compatibility. Keep Python 3.10 `Z` support
  and the existing interpretation of old naive timestamps as UTC.
- On actual attempt admission, retire both `no_candidate` and `exhausted`
  projections independently of supply-marker presence. Preserve failed-attempt
  history, request-specific identities, committed served/nonretryable facts,
  and the Stop/frozen-outcome boundary. No persistent schema or lifecycle rewrite.
- Enumerate every supply-level outcome/variant from the production authority in
  the retry regression, followed by success, exhaustion, terminal failure, or
  Stop. Exercise exhaustion, delayed recovery, readmission, and Stop through real
  gateway HTTP as well. Exercise mixed offsets, fractional timestamps, and route
  permutations through resolution, probe, and gateway admission.
- Keep the resolver-consuming retry test in the existing L3 ownership scope.
  Current-head lint run `34182012889` failed only because its prior placement in
  `test_model_hub_provenance.py` added an unregistered resolver importer (O1).
  Move the test to L3; do not weaken the authority guard or expand lane ownership.

The first lint run `34179699562` belongs to the old head and its dependency
failure is resolved by the already-integrated #1933. The current head has 15
successful checks and the O1 failure plus its failing unit-test aggregate.
Cancellation ownership did not recur on the second reviewed head. The durable
watch and cursor remain unchanged. Production, merge, and deployment stay out
of scope; fresh exact-head review, complete lint, and task-local Incus validation
are still required after this correction.

Before the correction, the added consuming tests reproduced 12 failures across
chronological selection, old naive timestamps, and exhaustion followed by Stop
(including Codex and Claude HTTP requests). Afterward all 75 focused cases pass.
The combined local regression passes 1,492 tests and 30 subtests with one existing
xfail, including the previously failing config/authority closure test. The
authority checker reports no findings; changed Python files pass Ruff and
whitespace checks. The orchestrator inspected the parser/minimum diff and its
probe-to-gateway consumer, and the admission diff and its HTTP Stop consumer.
