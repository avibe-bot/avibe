# Memory write deadline and recovery observations (#2020)

## Goal and evidence

A valid native add can take longer than Avibe's 30-second response wait.
The adapter then reports an ambiguous result and the existing recovery closes
both the native child and volatile writer generation. Health after recovery
does not establish whether that write completed.

The released EverOS 1.2.3 archive was verified against SHA-256
`296f320ec9acc55fc01f5c4f6ff2a4a338710a33b73e41615f957ce0569e3e07`.
Both HTTP write routes await `memorize()`, whose outer `asyncio.timeout` covers
lock acquisition and processing. Its default is 360 seconds.

## Smallest complete change

1. Generate an explicit native memorize deadline of 360 seconds from one
   constant. Both add and flush clients allow 370 seconds (10-second response
   margin). Keep connection setup at three seconds. HTTPX bounds I/O phases;
   this is not claimed to be an independently enforced total HTTP wall-clock
   deadline. The native invocation owns the processing deadline.
2. Preserve the existing bounded recovery and no-replay policy. Successful
   receipts still use the current success path. Terminal errors now retain
   sanitized observations; server errors do not prove that no write occurred.
3. Keep at most 50 recent anomalies in the existing writer object. Expose them
   through the existing Processing Record and failure API. A native child
   restart does not clear them; a controller restart or explicit data clear
   does. The source remains `partial`, never a complete durable history.
4. Distinguish an in-flight `unknown` result from queued `not_submitted`
   captures. Consecutive queued drops aggregate in `affected_count` so a full
   queue does not evict the unknown observation. Barriers are not captures.
   Emit content-free events to ordinary service logs for longer-lived evidence.

### Deliberate scope choice

The initial design offered either preserving unsent captures or exposing their
best-effort disposal. This implementation chooses explicit disposal: normal
Wake closes the writer in multiple authority transitions. Retaining queued
payloads across it would require a second recovery lifecycle and attachment /
identity fencing changes. That cost is unnecessary to repair premature timeout
and silent disposal. No queue, schema migration, receipt-reconciliation service,
or automatic replay is introduced.

Tracking discarded pending flush sessions is logged separately; these sessions
already have add receipts and are not reported as lost messages. A native 5xx
response ends the call but does not establish absence of partial writes. It is
reported as unknown without restarting a responsive native child solely for
that HTTP response.

## Acceptance invariants and validation

- A native operation within its processing budget can return before the adapter
  abandons its response, for both add and flush.
- Unknown submissions are never replayed. Recovery cannot erase their recent
  observations or make unsubmitted captures indistinguishable from them.
- Queue disposal counts actual captures; barrier count is never a loss count.
- Subsequent successful work does not erase earlier anomalies.
- Diagnostic records are bounded and contain no payload or credential text.
- Authority-changing close/reset retains its existing semantics.

Fast tests cover adapter timeout propagation, unknown outcome/no replay,
worker-drain and close-drain disposal, bounded/coalesced observations, successful
continuation and Processing Record projection. Existing cancellation, attachment,
process and lifecycle tests remain applicable.

An opt-in released-artifact test runs the real native HTTP routes and memorize
lock/deadline over a real Unix socket with a synthetic local pipeline. A
31-second call succeeds; response loss after a synthetic write is ambiguous;
a later write succeeds. All HOME/XDG/provider paths are disposable and no keys
or external providers are used. This verifies the actual bundled synchronous
contract; it does not measure an external LLM or claim end-to-end extraction
quality. Unified Incus/IM acceptance remains an integration check before release.

## Delivery checklist

- [x] Red regressions before implementation.
- [x] Timeout and observation changes without a persistent workflow.
- [x] Isolated released-artifact contract trial.
- [ ] Focused validation, PR review and CI.

## Review boundary audit

Review of `abce9c5` identified three diagnostic projection gaps (count, source
scope and attempt count). Review of `9da60f3` identified two classification gaps
(supervisor-crash disposal and non-ambiguous flush exceptions). Before further
edits the orchestrator audited the whole boundary: ambiguous transport failures
and supervisor crashes both fence queued provider calls; intentional authority
changes retain their prior semantics. Non-ambiguous exceptions retain their
original error and failed classification rather than becoming synthetic HTTP
server failures. These changes use existing writer state only.

Review of `2427c87` exposed the remaining diagnostic boundary class: captures
already dequeued but still preparing attachments, coalescing across distinct
recoveries, and loss of provider rejection codes. The orchestrator chose one
cleanup accounting point guarded by the existing reservation lifetime. A capture
records whether provider invocation began; only unavailable, never-submitted
captures count as disposal. Provider replacement closes the coalescing incident.
Native rejection codes remain diagnostic-only; persisted `last_error` retains
its released generic code. No workflow or storage state is added.
