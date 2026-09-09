# Model Hub retry, recovery, and failure experience

Status: Approved for implementation on September 9, 2026. Runtime changes are
not deployed by approval. The defaults below are the implementation target,
not claims about deployed behavior.

## Start with the reported case

`gpt-6-astra` had one configured supplier, `relay-gpt`, and that relay was
unreachable during the incident. Avibe eventually reported a Codex-wrapped
local 503 and promised automatic recovery at an absolute UTC timestamp.
The promise described a cooldown expiring, not a successful connection and
not a guarantee that the failed conversation would resume.

The intended experience is:

1. Try another runnable supplier immediately, if the configured chain has one.
2. If the only options are temporarily unavailable, keep the pending request
   alive within a bounded recovery window. Show “waiting to retry,” not a
   terminal failure or a promise that the provider will recover.
3. When a supplier becomes eligible, let one waiting real request test it.
   Other requests may use backups or wait; they do not all hit the relay.
4. If that request produces valid model output, let work continue. If it
   fails again, increase the delay. Timer expiry alone proves nothing.
5. If automatic recovery ends, say that this request has ended and offer
   the existing safe Retry action where supported. Later supplier recovery
   must not silently revive a terminal Turn.

The incident's observed `server_error` classification does not itself prove
a socket failure: a relay can return 503 because its own upstream is down.
User-facing specificity must follow the recorded wire evidence.

## Evidence and existing contracts

The incident was investigated on Avibe `3.0.15rc7`. This proposal was checked
against source baseline `49813f73471720e2e00d38fdbff1d82e8f4d58a7`.
An in-flight engine-intent change is not assumed to be deployed or required.

| Boundary | Current implementation / recorded incident evidence |
| --- | --- |
| Source selection | A fallback-class failure excludes that Source for the current resolution and tries the next eligible configured hop. No candidate yields `no_candidate`; a failed walk yields `exhausted`. |
| Cooldown | Server/overload failures use 30 seconds, rate limits 60 seconds, and recoverable quota failures 300 seconds. Delays are fixed, without jitter or consecutive-failure growth. Actionable balance/auth failures retain their separate rules. |
| Unclassified connection failure | Classified for fallback, without a durable Source health write. The complete live network-backoff behavior described in the contract is not implemented in these inspected paths. |
| Cooldown expiry | The Source can return to active/standby and emit `recover` without an inference succeeding. |
| Gateway admission | Waits once for a known future deadline only after `no_candidate`, then resolves again. It does not wait-and-retry `exhausted` inside the same gateway request. Remaining temporary unavailability returns 503 with `Retry-After`. |
| New Turn admission | Runtime routing can reject unavailable supply before reaching the gateway's waiting path. |
| Engine and native caller | CPA request retry is disabled and Hub owns cooling. In the inspected Codex version, native retries still apply and do not honor `Retry-After`; retrying notifications can be hidden until the final failure. |
| Failure recovery button | A separate, user-initiated Delivery action exists. It is not permission for automatic Turn replay. |

The incident's 29 failed attempts were an accumulated result, not one setting
named “29 retries.” Diagnosis should expose counts by layer and request.

Existing authorities to reuse:

- [Model Hub](model-hub.md) and its [API contract](model-hub-contracts/api.md):
  classification, routing, blocker precedence, live connection backoff,
  settlement freshness, event semantics, and the closed terminal-copy matrix.
- [Inference deadlines](model-hub-inference-deadlines.md): connected inference
  ends on completion, concrete failure, or owner cancellation, not elapsed time.
- [Failed-Turn Retry](backend-failure-retry.md): accepted versus `not_written`
  retry, durable ownership, and stale/duplicate-action guards.
- [Delivery foundation](session-delivery-foundation.md): retained input,
  admission, and native acceptance ownership.

This proposal deliberately revises three contracts, rather than treating them
as implementation omissions:

- Permit bounded retries of retryable, pre-output failures within one gateway
  request; the inference-deadlines contract currently permits only delayed
  admission after `no_candidate`.
- Replace timer-based recovery claims with eligibility plus observed recovery;
  add single-request recovery admission and increasing shaped-error cooldowns.
- Add jitter and valid empty-completion recovery evidence to the existing
  network-backoff design. Its documented 30-second maximum remains intact.

These revisions are not authoritative until their canonical contracts,
production enums/interfaces, localization keys, and mirror fixtures change
together during implementation.

## Ownership and safety boundaries

| Owner | Responsibility |
| --- | --- |
| Existing Turn / Delivery manager | Own input, native acceptance, queue order, cancellation, final settlement, and explicit user Retry. A waiting subphase is not a new Turn or Delivery. |
| Existing Model Hub service and resolver | Own classification, configured fallback, per-Source eligibility, backoff, and a shared in-memory recovery admission coordinator. |
| Gateway and runtime router | Consume that one policy for pending Hub requests and pre-native admission respectively. Do not maintain independent retry counters or sleep loops. |
| CPA | Execute the selected attempt. Keep inference request retries disabled; preserve the existing bounded credential-refresh exception. |
| Native backend | Execute the agent and any supported continuation. For `channel=hub`, redundant whole-HTTP-request retry must be reconciled with Hub ownership. For `native_cli`, Hub cannot control unobserved provider requests. |
| Web / IM | Render controller-owned progress and terminal facts. Never retry model requests from a view timer. |

No new persistent task state machine, retry table, standalone probe worker,
or notification scheduler is needed. The coordinator belongs to the existing
Model Hub service lifecycle; existing controller dispatch/IPC carries progress.

The retry unit is one pending model inference request, not an Agent Turn.
Retries stop once the first user-visible model-output byte is committed,
including recognized reasoning and tool-call output. Headers, SSE comments,
and protocol metadata are not output. Preserve lossless prelude handling.

Pre-output inference retry does not guarantee exactly-once upstream billing:
a dropped connection may hide completed inference. Keep every observed usage
report exactly once locally. Do not extend this policy to replaying upstream
side-effecting operations without a verified idempotency contract.

Successful fallback uses only the current configured chain and its existing
mapping semantics. It does not rewrite route order, select an unrelated
model, restart a backend, or change credentials implicitly.

## Recovery policy

### One bounded automatic recovery window

Recommended initial default: **120 seconds per pending model request**, starting
at its first retryable failure or first blocked admission. The same window
covers fallback passes, cooldown waits, and recovery admission; it never resets
because another Source was tried. Preserve its identity across preflight and
the first gateway request so startup cannot receive a second full window.
Preflight can wait for eligibility but cannot claim recovery or reserve a slot
that the eventual gateway request must wait behind.

This is an admission limit, not an inference timeout:

- Start another attempt only while the window remains open. An attempt already
  connected may finish after it expires, however slowly the model thinks.
- Do not cancel a live attempt on the window timer. If it subsequently fails
  and the window has expired, do not admit another automatic attempt.
- A terminal classification ends recovery immediately. A runnable backup can
  be used immediately; there is no mandatory sleep between different Sources.
- When nothing is runnable, wait interruptibly for eligibility, relevant
  configuration change, cancellation, or window expiry. Re-resolve current
  configuration on wake; never cache a candidate list across the wait.
- If the next permitted attempt is beyond the remaining window, end automatic
  recovery now and show the next eligible time as advisory. Do not spend two
  minutes waiting when the recorded quota reset is five minutes away.
- Only an all-temporary blocked chain is an automatic waiting case. Preserve
  `interrupted` and its remedies if any blocker needs user action; do not hide
  it behind a cooling Source.

Two minutes allows two recovery opportunities after typical 30-second and
60-second transient delays while bounding unattended outage waits. It is a
proposed starting policy, not a measured optimum. Do not add a user-facing
timeout control in the first release.

Native transport compatibility is a release gate, not an assumption. Validate
the full window through each supported backend before enabling this loop.
Retain legitimate native stream continuation and native idle policies; do not
send fake output/early success headers, set enormous timeouts, or disable all
stream retries to keep a waiting connection alive. If a backend cannot support
the contract, resolve that ownership boundary before rollout and document the
limitation; do not claim uniform automatic recovery.

For Hub-managed Codex traffic, use the supported per-provider request-retry
setting to remove duplicate HTTP retries, after exact-version verification.
Audit Claude and OpenCode similarly. An unchanged terminal native retry must
not reopen a completed recovery window or create another batch of upstream
attempts. The existing request/Turn correlation must enforce this boundary.
Distinguish a native retry from a legitimate later model request in the same
Turn; if the backend cannot provide that distinction, treat it as an ownership
gate rather than deduplicating by prompt text or blocking the whole Turn.
Direct `native_cli` traffic retains native retry ownership; only its observable
preflight availability and terminal rendering participate in this proposal.

### Backoff and cooldown

Count consecutive eligible failed attempts for the same Source identity.
Do not count waiters, native error notifications, elapsed timers, or failures
whose settlement generation is stale.

| Classified failure | Proposed local delay, before upstream advice | Storage / action |
| --- | --- | --- |
| Unclassified pre-output connection failure | 1, 2, 4, 8, 16, 30 seconds; cap 30 | Reuse contracted live Source-scoped backoff; no Source/config write. |
| Retryable server/overload | 30, 60, 120 seconds; cap 120 | Keep existing shaped-error Source cooldown; streak is live state. |
| Recoverable rate limit | 60, 120, 240, 300 seconds; cap 300 | Keep existing cooldown classification. |
| Recoverable quota exhaustion | 300 seconds | Keep existing cooldown; normally beyond the automatic window. |
| Refreshable credential rejection | Existing same-Source retry once | No independent retry loop; a second rejection follows the existing terminal rule. |
| Bad key, revoked access, balance requiring action, request/schema/context/model errors, local engine failure | No new automatic retry | Keep existing classification/remedy; `engine_down` never blames or cools an upstream Source. |

Apply bounded positive jitter to the local base: a sampled delay in
`[base, min(cap, base * 1.2)]`. This preserves minimum delays and hard caps;
at the cap, source-scoped admission still prevents a waiting-client stampede.
Do not sample a separate delay per waiter. Shared state owns one deadline.

Carry upstream retry advice through the existing `RawCallOutcome` boundary.
The adapter preserves a bounded, allowlisted `Retry-After` value; the Hub
parses nonnegative delay-seconds or a valid HTTP date against one captured
response time. The effective deadline is the later of local backoff and valid
upstream advice. A Source deadline must not move earlier due to another failure.
Invalid, overflowing, or past values cannot cause a tight loop; fall back to
the local policy and record only a sanitized diagnostic.

Do not clamp a valid long upstream delay down to the local cap and retry early.
If it is outside this request's window, terminate this request truthfully while
keeping the advisory deadline. Compare aware timestamps using the existing
shared parser; use monotonic elapsed time for in-process scheduling.

### Recovery admission, not timer-based health

Cooldown expiry means **eligible to try**, not **recovered**. Represent
eligibility and a recovery request using the existing runtime annotation
boundary, not new persistent Source statuses.

For each affected Source, one existing pending request may hold recovery
admission. It sends its own inference; there is no synthetic probe traffic.
Other waiters use eligible fallback Sources or await that admission's result.
Recheck admission under the service's existing synchronization before invoking
the engine; never hold the configuration lock while sleeping or doing I/O.

Valid recovery evidence is the same Source and identity generation producing
recognized model output, or completing a valid successful buffered/empty
response. A TCP connection, HTTP 200 headers, heartbeat, or elapsed timer is
insufficient. Output proves service for this attempt, not permanent health.

On that evidence, clear the affected streak/deadline, release recovery
admission, and record a recovery observation. Ordinary traffic may then resume.
A later shaped stream failure follows its existing terminal/health rule; it
cannot make the already-consumed request retryable. Success on a backup never
clears the failed primary's streak. No traffic means no recovery claim.

Reuse the existing attempt-start settlement generation and Source identity
fences; do not add a second freshness authority. At most one failed recovery
admission advances the streak. Older concurrent outcomes may retain their
history/usage but cannot overwrite newer Source facts.

If the recovery request is canceled, release its admission after connection
cleanup without asserting success or inventing a failure. Wake another live
waiter when eligible. A slow connected recovery request is not killed to free
the slot: other waiters can use backups or exhaust their own recovery windows.
Release must check ownership so late cleanup cannot unlock a newer request.

Use Source scope for connection failures and the existing generic server
classification. Explicit request/model incompatibility must remain local to
that request and must not poison other models. Do not infer model scope from
a free-text 503 or add a persistent model-health table. A future transient
model-scoped policy requires explicit classifier evidence and corresponding
exact-hop contract changes; it is not silently bundled into this revision.

## Turn lifecycle and user actions

- **Before native acceptance:** retain the same original Delivery, prompt,
  attachments, and queue ownership. Waiting does not mean “native accepted.”
  Do not resubmit input or reopen a backend merely to check supply.
- **After acceptance:** waiting is a progress subphase of the existing live
  Turn, not a reversal to pre-admission state. A gateway request retry must
  not rerun earlier tools or restart the Agent.
- **Stop / disconnect:** propagate authoritative cancellation through waits,
  recovery admission, the active transport, and bounded cleanup. A browser
  disconnect alone follows the existing conversation ownership policy; a
  canceled gateway caller cannot leave detached inference running.
- **New input:** retain existing queue/steer/interrupt rules. A later message
  does not silently replace the request being retried or reset its budget.
- **Model selection:** use existing selection semantics; changing the default
  is not consent to rewrite an accepted request. Users may Stop and submit a
  new request with another model. No special “switch and replay” action.
- **Source edit/removal:** wake affected waiters and re-resolve. Endpoint or
  credential replacement invalidates live backoff and recovery admission for
  the old identity. Preserve existing revocation/transport lifecycle behavior;
  old results cannot mutate the new identity or claim recovery for it.
- **Controller restart:** rebuild live throttle state through the existing
  lifecycle and reconcile durable Turns/Deliveries normally. Do not persist
  timers as new jobs or auto-resubmit an ambiguously accepted native start.
- **Explicit Retry after failure:** reuse the
  [existing action](backend-failure-retry.md). Accepted failed Turns use the
  existing `continue` admission; proven `not_written` reuses retained input.
  Unknown acceptance never licenses replay. Keep double-click/stale-Turn
  guards, drafts, and attachments unchanged.

## Presentation and evidence

Two independent facts must be visible: **is this request still live?** and
**when may this Source be tried again?** A terminal Turn can coexist with a
Source that is temporarily unavailable; `supply_state=waiting` alone cannot
justify telling that user “we are still retrying.”

| Situation | Example Chinese copy | English equivalent |
| --- | --- | --- |
| Live wait, connection failure proved | `relay-gpt 暂时无法连接，正在等待重试。预计 30 秒后再次尝试。` | `Cannot connect to relay-gpt. Waiting to retry in about 30 seconds.` |
| Live wait, only upstream 503 known | `relay-gpt 暂时无法处理请求，正在等待重试。` | `relay-gpt is temporarily unable to handle the request. Waiting to retry.` |
| Deadline elapsed, recovery attempt active | `正在尝试重新连接 relay-gpt…` | `Trying relay-gpt again…` |
| Automatic recovery ended, no usable backup | `本次请求未完成：relay-gpt 持续不可用，且没有可用备用源。自动重试已结束，你可以重试或切换模型。` | `This request could not complete: relay-gpt remains unavailable and no backup is usable. Automatic retry has ended. You can retry or choose another model.` |
| Provider asks for a longer wait | `本次请求未完成，自动重试已结束。relay-gpt 预计 5 分钟后可再次尝试。` | `This request could not complete; automatic retry has ended. relay-gpt may be tried again in about 5 minutes.` |
| Output interrupted | `本次输出已中断，自动重试已停止。你可以重试，让 Agent 继续处理。` | `Output was interrupted; automatic retry has stopped. You can retry to let the agent continue.` |
| Eligibility on Models page, no recovery evidence yet | `冷却已结束，可再次尝试；尚未验证恢复。` | `Cooldown ended; eligible to retry. Recovery has not been verified.` |

These are semantic examples, not a second string authority. Preserve existing
actionable auth/configuration remedies and the no-fallback model distinctions.
Terminal rendering must come from the canonical closed matrix, with its enum,
key, backend projection, and fixture consumers updated together.

Default copy omits the backend wrapper, local gateway URL, and raw UTC
timestamp. Use relative time, with local absolute time and timezone in details
when useful. A frontend countdown reaching zero changes to “retry eligible”
or “checking status,” never “recovered” without a server fact.

For short interruptions, use the existing working indicator. Reveal a waiting
detail after five seconds of continuing recovery; this is display debounce,
not a new retry timer. Web updates one live working/activity surface, including
before the first assistant/tool activity row. The existing Activity model has
no waiting row type today: extend its live status projection deliberately,
rather than faking assistant text or recording a failed terminal notification.

IM uses the current Turn's progress delivery path: edit a supported status
message, or emit at most one waiting notice when editing is unavailable.
Do not send each backoff step or a per-second countdown as new messages.
Terminal failure is emitted once through existing delivery. Successful
fallback adds no standalone chat notification; normal output resumes and
Models/Usage retain the supplier history. IM Retry buttons are not assumed
to exist; preserve existing platform actions.

### One producer of structured facts

Extend the existing request correlation and provenance instead of parsing
native exception strings in each UI. The minimum facts and their consumers are:

| Fact | Producer / consumer |
| --- | --- |
| Exact Turn/Delivery, gateway request, and attempt identities | Existing ownership/correlation; controller progress and terminal settlement reject stale updates. No new cross-session correlation key. |
| Live recovery phase, attempt count, next eligible time, window end | Hub coordinator; runtime router/gateway publish through existing controller dispatch/IPC. Views render a non-authoritative snapshot. |
| Source/model, failure layer, reason, upstream status, gateway status | Existing adapter/classifier/provenance; terminal renderer and expandable diagnostics distinguish relay failure from local engine failure. |
| Sanitized upstream error code/message/request ID and retry advice | Adapter allowlist and redaction boundary; bounded diagnostic details only. Never forward arbitrary headers or error bodies. |
| Observed recovery versus eligibility expiry | Service settlement; Source read projection and event feed. Historical timer-based `recover` entries must not become proof of successful inference. |

Wire names belong in the existing canonical interfaces during implementation;
this table defines responsibilities, not another schema. The source resolution
event feed remains a pull/debug record, not an outbox. Do not add recipient or
audience fields to it.

Only the controller's existing Turn owner may finalize the conversation. Clear
obsolete no-candidate/exhausted projections when a new attempt is admitted;
late failure or progress cannot overwrite success, Stop, or a newer Turn.
Reload must reconcile the live snapshot with durable terminal state. Historical
records without new facts remain readable and make no inferred retry promise.

## Delivery sequence

1. **Truthful state and copy.** Separate eligibility from observed recovery;
   distinguish live waiting from terminal failure; preserve diagnostic layer
   evidence and reuse existing failure actions. Correct current text even when
   no automatic retry remains. Amend canonical contracts and mirrors with the
   affected implementation. Do not advertise the new loop before it exists.
2. **One recovery policy.** Complete contracted live network backoff, add
   Retry-After propagation, the bounded admission window, shared recovery
   admission, and runtime-router/gateway parity. Reconcile native retry
   ownership and wire live Web/IM progress in the same complete behavior slice.
3. **Validate and tune.** Run the acceptance matrix below, including native
   callers and restart/cancellation. Use retry counts, recovery delay, terminal
   outcomes, and Stop latency from existing observability to evaluate the
   defaults. Add no background probing or automatic production tuning.

These slices share resolver, settlement, provenance, and gateway contracts.
Keep one owner for those changes; do not parallelize incompatible core edits.
Rebase against merged engine/runtime work without treating an open PR as a
runtime fact. UI implementation must reuse the design system and update the
authoritative design when its visible structure changes.

## Acceptance matrix

The labels below are local to this proposal. Allocate canonical scenario IDs
in `tests/scenarios/model_hub/catalog.yaml` during implementation.

| Case | Required evidence |
| --- | --- |
| Sole relay fails, then succeeds | Pre-output recovery stays within one pending request, delays grow, real output resumes, and no terminal failure is emitted. |
| Sole relay keeps failing | Admission window is not reset by fallback/native retries; no attempts start after expiry; exactly one final failure says automatic retry ended. |
| Delay exceeds remaining window | No pointless full-window sleep or early retry; preserve the provider's later advisory time. |
| Startup versus active Turn | Same temporary-supply policy; original startup Delivery and attachments survive; waiting never fabricates native acceptance. |
| Concurrent waiting Turns/backends | One recovery admission per affected Source; backups proceed; waiters do not each increment the streak or allocate a separate probe. |
| Recovery owner canceled or slow | No lock/transport leaks; canceled owner releases only its own admission; another waiter can proceed; slow connected inference is not killed. |
| Delayed headers / first output / buffered completion | Success beyond the former transport timeout and beyond the admission window; no health penalty, retry, or duplicate usage caused by elapsed inference time. |
| Stream boundary | Heartbeats/headers alone do not prove recovery; reasoning and tool output do. No Hub replay after output, including engine/transport failure and cancellation races. Valid empty completion also releases recovery admission. |
| Advice and clocks | Missing/malformed/past/large Retry-After, differing UTC offsets, clock shifts, and deterministic jitter; no stale serialized deadline or busy loop. |
| Stronger blockers and fault scope | Bad credentials, quota requiring action, local engine failure, mixed blockers, and explicit model errors retain correct remedies; waiting cannot hide them or corrupt unrelated Source/model state. |
| Source replacement / stale settlement | Old success/failure/cleanup cannot mutate the replacement or release its recovery admission; backup success cannot clear primary failure history. |
| Restart / unknown native acceptance | No revived terminal Turn, duplicate original prompt, or fabricated acceptance; live snapshot and durable owner reconcile through existing recovery. |
| Retry / new input / model change | Existing double-click, stale-Turn, queued-input, `not_written`, and accepted-continue tests stay green; no draft loss or automatic tool replay. |
| Web and IM | One live waiting presentation, no retry-message storm, readable local timing, redacted details, reload consistency, and one truthful terminal. Successful fallback remains quiet. |
| Native retry ownership | Exact-version Codex, Claude, and OpenCode callers cannot multiply a Hub recovery episode; legitimate native continuation remains intact. Direct-native scope is explicitly distinguished. |

Use clock-controlled resolver/service tests, real loopback HTTP for Responses,
Messages, and Chat Completions (streaming and buffered), existing usage and
provenance tests, and UI projection tests. Verify native integration and at
least one IM delivery path in isolated developer-local Incus with a controllable
mock upstream. Do not use real relay credentials, paid inference, the running
workstation service, or remote tenant environments.

Proposal validation only: check document links and whitespace. Runtime tests,
native compatibility, UI/design verification, and rollout remain implementation
gates; this document does not claim they have passed.

## Implementation ownership and boundary contract

The implementation orchestrator owns integration and all contract revisions.
The first shared contract commit establishes these additive boundaries before
delegation:

- `RawCallOutcome.retry_after: str | None = None`: the engine client supplies
  only the bounded ASCII `Retry-After` HTTP header on failed responses (maximum
  128 characters). No arbitrary headers, credentials, or body excerpts are
  added. The Model Hub retry policy consumes and validates this advice.
  Existing constructors remain valid; the adapter-interface mirror changes
  with the production dataclass.
- `RawCallOutcome.response_received_at: datetime | None = None`: the client
  captures an aware UTC instant on receipt of failed HTTP response headers.
  The Hub resolves delay-seconds and HTTP dates against that instant, not the
  later error-body read or settlement time. Missing evidence falls back to
  the Hub's captured classification time.
- `RawCallOutcome.recovery_verified: bool = False`: recovery-only evidence,
  supplied by recognized model output or a recognized successful protocol
  terminal/body. It does not tighten the existing permissive response-admission
  policy. A generic HTTP 200, unrecognized JSON, or malformed body may still be
  passed through under that policy but cannot produce a recovery claim.
  `ProtocolObservation` carries the same additive boolean from the existing
  protocol projector to the adapter. The adapter lane owns this evidence
  projection and its mirror/tests; core only consumes it.
- The core lane owns the single in-memory retry coordinator, resolver live
  annotations, Source settlement, gateway retry loop, runtime-router admission,
  their canonical contracts, and tests. Recovery waits are cancelable and a
  request retry never becomes a Delivery replay.
- The adapter lane owns only the two outcome members, HTTP propagation, their
  mirror, and focused wire tests. It does not classify, sleep, retry, or change
  engine retry configuration.
- Native retry settings, Turn correlation, progress, and terminal presentation
  are integrated only after the native-boundary audit. No lane may deduplicate
  distinct model requests using prompt text or implement a second retry owner.

Live presentation consumes the core lane's public registry methods:

- `terminal_projection(turn_id: str, *, backend: str)` returns an exact,
  unambiguous, non-frozen terminal failure projection with no pending peer
  request, or `None`. Shared failure presentation uses it instead of parsing
  native error wrappers. History replay never queries live state.
- `recovery_snapshot(turn_id: str) -> list[dict]` returns only live unambiguous
  request progress: `request_id: str`, `phase: waiting | attempting`,
  `attempt_count: int`, `started_at: str`, `window_end: str`,
  `source_id: str | None`, `reason: str | None`,
  `next_eligible_at: str | None`. Timestamps are aware UTC ISO strings.
- The registry's optional `on_recovery_changed: Callable[[str], None]`
  callback reports material changes after releasing its lock. The controller
  maps the exact live Turn to its Session and publishes the existing
  `session.activity` invalidation with `event=model_recovery`. The existing
  `turn-state` response adds optional `model_recovery` with that snapshot.
- Web reuses the existing working/Activity label after a five-second debounce,
  with no new notification or view-owned retry. IM concise status rendering
  reads the same snapshot on its existing heartbeat and edits its existing
  bubble; platforms or preferences without progress retain existing behavior
  instead of receiving a new unsolicited waiting notice.

No subtask may modify another lane's files or open/merge a PR independently.
All work is integrated into one implementation PR and validated at its exact
head. Any missing cross-boundary behavior is reported to the orchestrator, not
silently omitted from the accepted contract.

### Native terminal compatibility decision

The domain result for an exhausted automatic recovery episode is
`model_hub_recovery_exhausted`, in both `error.type` and `error.code`. Its wire
message is exactly:

> Automatic recovery has ended. Try again or choose another model.

The JSON envelope has top-level `type: error`. It includes no upstream body,
provider diagnostic, absolute retry time, or `Retry-After`. Provenance retains
the actual failure and supply facts; localized user presentation reads those
facts rather than interpreting the transport status as a user mistake.

Native compatibility testing on 2026-09-09 found that a uniform 424 or 422 is
not terminal for Codex 0.153.2: its outer loop made six requests even with
provider HTTP retries disabled. The same closed error at 400 made one request.
Claude 2.1.263 and OpenCode 1.18.18 made one request at 424. Therefore the
gateway maps this domain result to HTTP 400 for Codex and HTTP 424 for Claude
and OpenCode, by backend identity, not by the selected wire protocol. This is
an intentional Codex transport-compatibility mapping: Codex internally calls
400 `InvalidRequest`, but the Hub neither emits `invalid_request_error` nor
claims the user's input is invalid. Do not remap unrelated authentication,
request-validation, post-output, or local-engine failures to this result.

Hub launches set Codex provider `request_max_retries=0` and
`features.unbounded_connection_retries=false`; `stream_max_retries` remains
unchanged. Claude Hub launches fix `CLAUDE_CODE_MAX_RETRIES=0` in both process
environment and launch settings. Keep its independent normal stream
continuation and fallback behavior; the native audit also verified that 424
is terminal with its retry watchdog enabled. Direct-native launches and
user-owned configuration are unchanged.

OpenCode's Avibe-side automatic `continue` is bypassed only for a current
assistant error already admitted by the existing baseline/liveness checks,
with a bound Hub launch, `APIError`, integer `statusCode=424`, and exact
`error.type` and `error.code` in parsed `responseBody`. Never parse the
truncated diagnostic text or disable the global retry setting. Explicit
Retry and ordinary native continuation keep their existing ownership.

The isolated native audit also held response headers for 121 seconds before
the selected terminal response; all three main calls remained single requests.
This is native caller evidence, not integrated controller or Incus acceptance.
A lost terminal response still cannot be distinguished from a later legitimate
model call using today's routing/Turn identifiers. No exactly-once guarantee,
prompt hash, Turn blacklist, or Source blacklist is introduced to hide that
transport-delivery limit.
