# Model Hub inference deadlines

## Problem and contract

The September 8, 2026 investigation found that Model Hub reused its 60-second
transport budget for response headers, the first response bytes, first model
output, and completion of buffered inference. A connected, healthy provider can
take longer at any of these stages. Retrying discards its work and can exhaust
the caller's retries without ever allowing the model to finish.

An admitted inference request must remain live until protocol completion, a
concrete transport failure, cancellation by its owner, or, for the streams the
[stream idle bound](#stream-idle-bound-2026-10-13) covers, silence past that
bound. Elapsed model time alone is not failure. This property applies to every
supported protocol and to both streaming and buffered responses. Cancellation
must still release the connection, temporary storage, and source ownership, and
a waiting request must not block unrelated requests.

## Scope

- Remove inference wall-clock deadlines in the engine client.
- Retain bounded connection establishment, management/discovery calls, reading
  an already-failed HTTP response, and resource cleanup.
- Start the finite local buffered-response parsing budget after the complete
  response has arrived, so model execution cannot consume it.
- Audit backend idle reclamation, gateway cancellation, and native caller
  timeouts before closing the change. Record evidence and scope decisions here.
- Treat temporary Source failures and cooldowns as delayed admission under one
  service-owned 120-second recovery window per pending model request. Re-read
  configuration after cancellable waits and share the window across fallback
  passes. Retry pre-output failures only; never replay model output.
- Treat cooldown expiry as eligibility, not success. One pending real request
  owns half-open admission; only recognized output or verified protocol success
  records recovery. Window expiry never cancels admitted inference.
- Pass all-temporary Hub supply through native preflight immediately, so native
  acceptance and the first model request do not own independent wait windows.
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
| Source cooldown | A waiting route returns 409; Codex does not honor HTTP Retry-After | One shared recovery window covers pre-output retries and delayed admission; terminal compatibility maps only recovery exhaustion to Codex 400 or Claude/OpenCode 424, with no Retry-After |
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

### Bounded recovery compatibility amendment — September 9, 2026

The approved binary audit in `model-hub-retry-experience.md` supersedes the
earlier once-only gateway cooldown wait. Codex 0.153.2 issued six requests for
424/422 despite provider retries being zero and unbounded connection retries
being disabled; 400 produced one. Claude 2.1.263 and OpenCode 1.18.18 produced
one for 424. All three accepted headers delayed by 121 seconds. These are
orchestrator-owned binary audit results, not a claim that core unit tests ran
the native binaries.

The single exhausted domain type/code is `model_hub_recovery_exhausted` with
neutral wire text, mapped by backend (not upstream protocol). Codex's internal
400/InvalidRequest transport interpretation is intentional compatibility, not
the public `invalid_request_error` classification. Original HTTP status and
Source failure reasons remain in provenance. Only exhausted automatic recovery
uses this branch; auth, request incompatibility, engine errors and post-output
failure retain their established behavior.

Hub-only launch injection sets Codex provider `request_max_retries=0` and
`features.unbounded_connection_retries=false`, plus Claude environment and
settings `CLAUDE_CODE_MAX_RETRIES=0`. Codex `stream_max_retries`, Claude watchdog,
and normal continuation remain unchanged. The OpenCode native poll-loop
consumer and all native terminal/rendering integration are orchestrator-owned.

The received-terminal audit does not prove exactly-once behavior if the
terminal response is lost. Existing request identity does not identify every
native invocation across arrivals. No prompt-based dedupe, Turn blackout or
Source blackout is introduced to hide that transport residual.

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

## Local projection budget follow-up (2026-09-27)

The audit decision "give local parsing its own budget" was only half applied:
the buffered success path started its parse deadline after the body arrived but
sized it with the client's connection budget, and the failed-response path
shared one deadline between the error-body read and its projection. The
inference-wait regression uses a 20 ms connection budget, so any scheduling or
GC pause of that length while parsing an already-received body became a
`TIMEOUT`, a 1-second source cooldown, and a gateway 503. That is the recurring
`gateway-complete-buffered_body-openai_responses` CI failure on master.

Both projections now start their own `_LOCAL_PROJECTION_TIMEOUT_SECONDS` budget
after the body has arrived. The connection bound and the bounded read of an
already-failed response are unchanged, and production timing is unchanged
because that budget equals the 60-second transport default. A regression drives
a projection that outlasts the connection budget and asserts success; the
projection-budget test asserts the budget is independent of the client timeout.

## Stream idle bound (2026-10-13)

### Problem

The engine sets no timeout once an upstream connection is established, not
even an opt-in HTTP/2 health check. Its maintainers closed
router-for-me/CLIProxyAPI#6534 as not planned: timeouts and cancellation belong
to the downstream client. For the engine that client is this gateway.

Engine patches 4 and 5 (`patches/model-hub-cpa-v8.0.30/README.md`) are carried
by design and turn a dead network path into an error in about 45 s. They cannot
see a relay that stays alive while its own upstream is stuck: the relay answers
PINGs and keeps writing `: keep-alive` comments, and the engine holds
comment-only frames until the next event. Such a stream sent the gateway nothing
until the caller gave up: Vibey after 600 s, Codex after 300 s between events,
and Claude Code after its own byte-silence watchdog. The rc3 turn
`trn_98d57e9e6ea0` waited the full 600 s on the API-key relay; whether its path
was dead or its upstream stuck was not recorded.

### Contract

- A streamed request to a Source in the table below ends when the engine sends
  the gateway no byte for **120 s**. Every byte restarts the bound: data events,
  pings and comments alike. Native callers apply it from the moment the
  request body is written, so it covers the wait for response headers too.
- When the bound runs out, the gateway closes the engine connection. The engine
  cancels its upstream request through its request context; measured on the
  shipped v8.0.23-3 engine, the upstream saw the disconnect within 0.02 s.
- The attempt ends as a `timeout` outcome, classified as the `network` fallback
  reason: Source recovery delays of 1, 2, 4 ... s, and no persisted health write.
  - **Before model output** the caller has seen nothing. Model Hub retries
    inside its 120 s recovery window, which opens at this failure, not at the
    request. The caller receives the retried stream, or recovery exhaustion
    with cause `network`.
  - **After model output** nothing is replayed. The stream ends with the
    protocol's `stream_interrupted` terminal, as for any transport failure after
    output, and the caller's own policy applies.
- Buffered responses and every Source outside the table keep the original
  contract: elapsed time and silence alone are not failure.

| Source vendor | Protocol, untranslated | Bounded | Longest healthy silence measured |
| --- | --- | --- | --- |
| `anthropic` | `anthropic` | yes | 10.0 s: a `ping` every 10 s through 336 s of buffered thinking, a 157K-token prompt, and a 12K-token tool input that the relay buffered |
| `openai` | `openai_responses` | yes | 35.8 s, before the first event of a cold 102K-token prompt; at most 29.2 s between events at `max` effort, with or without a reasoning summary; tool-call arguments streamed as deltas |
| `xai` | `openai_responses` | no | 188 s: grok-4.6 sent a 15K-token tool call in one delta, and the engine held the relay's comments meanwhile |
| any | translated by the engine | no | 72.8 s (Responses to Claude) and 141.8 s (Chat to Claude): the translators drop the Anthropic pings |
| any other | `openai_chat`, `google`, or another vendor | no | not measured |

Measured on 2026-10-13 through the local engine (v8.0.23-3, darwin-arm64) with
long-reasoning prompts at each model's highest accepted effort. The streams
were 33 to 900 s long. Only timing metadata was kept, outside this repository.
The ChatGPT subscription was capped on that day. Its streams,
measured on 2026-10-12 directly over HTTP/2, had at most 21 s between events,
which agrees with the `openai` row.

### Why the gateway's engine read

- Every Agent's Hub traffic (Claude Code, Codex, OpenCode, Vibey) reaches the
  upstream through `EngineClient.invoke`. The bound lives there, on the engine
  connection's read (`sock_read`), so all of them inherit it.
- Before model output the gateway holds the response, so only the gateway
  sees the upstream's bytes, and only the gateway can retry without the caller
  seeing a failure. Vibey's own bound sees nothing until the gateway releases
  headers, and it covers only Vibey.

### Interactions

- **Model Hub recovery.** The 120 s window opens at the first failure. A request
  whose upstream stays stuck therefore makes about two attempts and ends in
  recovery exhaustion at roughly 241 s. That is before Vibey's 600 s bound, and
  for Claude Code shortly after the 240 s early commit, which then carries it
  in-stream.
- **Vibey's retry budget** (`max_elapsed_s = 120`, measured from the request's
  first attempt). A stall before output never reaches Vibey's loop. A stall
  after thinking alone reaches it as `interrupted` with `after_thinking`, and
  is retried once only if 120 s have not passed since the first attempt
  started. With the bound at 120 s that does not happen, because the error
  comes at least 120 s after the last byte. After text or a tool call the error
  is terminal (C-2). It now comes 120 s after the last byte instead of 600 s.
- **Vibey's 600 s silence bound** covers each chunk after the response
  headers. The wait for the headers belongs to the engine read; see
  [Vibey's wait before output](#vibeys-wait-before-output-2026-10-18).
- **Native callers.** Their own bounds (Codex 300 s between events, Claude
  Code's first-byte and idle watchdogs) are longer and remain unchanged.
- **Engine patches 4 and 5 stay.** They bound a dead path in about 45 s on
  every path, the unbounded and translated ones included, and they do so
  without depending on any upstream cadence.

### Known-by-design residuals

- A stuck upstream behind a relay that keeps writing data frames (Anthropic
  `ping`, or an `event: keepalive` frame) resets the bound and is not caught. For
  the Anthropic relay measured here, the pings may come from the relay itself.
- Unbounded Sources keep the caller's bound and patches 4 and 5. Where a healthy
  stream sends no data for minutes, no byte bound can tell a busy upstream from
  a stuck one. Forwarding the relay's comments would not help either: a stuck
  upstream behind a live relay sends those comments too. Holding them back is
  what lets this bound see such a stall on the bounded Sources.
- The vendor is the Source's declared vendor. A relay declared `openai` that
  serves a model which buffers tool calls, as xAI does, would be cut at 120 s
  on a tool call longer than that.

## Vibey's wait before output (2026-10-18)

### Problem

On 2026-10-17 a Vibey turn on Claude Opus 5.5 at `xhigh` effort, asked for a
long piece of writing through an `anthropic` API-key relay, failed after
20 minutes as `stalled`: two engine requests, each cut by Vibey at 600 s. The
gateway's 120 s bound never fired, so the engine kept receiving bytes, the
Anthropic pings. From Claude Opus 4.7 on, `thinking.display` defaults to
`omitted`, so a thinking phase streams no thinking deltas. The gateway withholds
the response headers and every byte until the first content block, and Vibey's
own bound also covered the wait for those headers, so it counted the model's
whole thinking time as silence. Claude Code is not affected: its streams commit
early and carry keepalives (`_EARLY_STREAM_COMMIT_SECONDS`).

Two alternatives were rejected. Requesting `display: "summarized"` would make
thinking count as output, but would move the upstream request away from Claude
Code's default shape (owner decision, 2026-10-18). Committing Vibey's streams
early, as Claude Code's are, would drop the `x-avibe-served-hop` header Vibey
reads with the headers to verify the origin of replayed signatures, and the
HTTP status its error handling keys on.

### Contract

- Through the gateway, Vibey does not bound the wait for response headers.
  Once they arrive, its 600 s bound covers each next chunk as before.
- The engine read holds the bound for every attempt of a Vibey stream: the
  120 s idle bound on the measured Sources above, and Vibey's 600 s silence
  bound on every other stream. Every byte restarts either one, so a model that
  thinks behind a pinging upstream is never cut, and a silent stream on an
  unmeasured Source is cut at 600 s and recovered like any other timeout.
- A failure that reaches Vibey before any model output, at least 600 s after
  its request was sent, ends the turn: it is neither replayed nor continued.
  Sent again, the request would most likely wait as long again, and Model Hub
  has already retried what its recovery window allows.

### Residuals

- A stuck upstream behind a relay that keeps pinging holds a Vibey turn until
  Stop, as it holds a Claude Code turn. Elapsed time alone is not failure.
- On an unmeasured Source, after output, the engine read's 600 s bound and
  Vibey's own coincide, and the engine read usually ends first, so such a stall
  reaches Vibey as `interrupted` rather than `stalled`.
- A persistently silent unmeasured Source still costs about two 600 s attempts
  (Model Hub recovery), as before (Vibey's own stall and one continuation).

## Remote relay request-body prelude (2026-10-31)

The 2026-10-31 relay investigation found Anthropic requests that spent nearly
120 s in the relay's request-body read before the caller cancelled the
connection. For Vibey, the engine now uses its existing 600 s caller-owned
budget for the request/response prelude when it exceeds the measured 120 s
bound. Once the first response byte arrives, measured Anthropic streams retain
the 120 s per-read idle bound. Native callers retain the 120 s prelude bound.

This is a client-side resilience margin, not proof that a relay is healthy. The
relay still needs its own request-body idle timeout, phase diagnostics, and
attempt-ID propagation; those changes are tracked in the relay project.
