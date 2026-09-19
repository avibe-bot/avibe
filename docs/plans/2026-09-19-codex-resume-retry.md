# Codex resume and bounded pre-write recovery

Issue: #2049.

## Problem and evidence

A large `thread/resume` response exceeds the stdio reader's 128 MiB line
limit. The reader loses the oversized-response cause, all pending requests
receive an ordinary connection error, and adapter retries restart a shared
per-working-directory process. The shared Delivery owner then repeatedly
requeues the same unwritten input.

A hermetic probe with Codex 0.154.0, the production Avibe transport, and a
loopback Responses fixture produced 17 real turns and a 427,901,977-byte
rollout. Default resume failed at the existing line limit. Resuming the same
thread with `excludeTurns: true` returned 1,937 bytes. Separate checks proved
that the next model request retained the old context, configuration refresh
still worked, stored history remained readable, and another thread could run
on the same process.

## Contract

1. Both Codex resume producers request `excludeTurns: true`. Avibe consumes
   the response identity and configuration, not its historical `thread.turns`.
   This does not truncate stored or model-visible history.
2. A stdio line-limit failure retains a typed oversized-response cause. It is
   not classified as a reconnectable failure. A definitive pre-write resume
   failure uses the existing `requires_explicit_retry` receipt contract.
3. The shared Delivery/Turn settlement owner permits at most three automatic
   startup attempts ending in `definitive_prewrite_failure/no_terminal_result`
   per Delivery before requiring explicit recovery. Batch membership never
   transfers an older input's exhausted budget to newer inputs. Concurrency
   refusal and native acceptance/unknown-start recovery keep their existing policies.
4. Inputs requiring recovery remain queued with the same Delivery ID,
   snapshot, attachments, and FIFO position. Timer drains and restart recovery
   cannot replay them. The existing failure-notice Retry action and Send now
   can retry them. Validated failure-notice Retry resets the bounded budget;
   Send now grants one attempt and does not replenish an exhausted budget.
   A concurrency refusal after Send now preserves the previous hold and reason.
   The budget derives from server-owned delivery history, not process memory
   or caller metadata.
   The shared read payload projects `requires_explicit_retry` and `retry_reason`
   from this receipt, for CLI queue inspection and the existing Web queue strip.
   These fields never authorize dispatch; the claim owner rereads the history.
   Requeuing publishes the existing `queue.updated` event after commit so live
   clients receive this projection without a reload.
5. Failure-triggered Codex transport replacement uses the existing durable
   ownership and live-turn checks. A still-live shared process cannot be
   replaced while another accepted turn or protected activity owns it.
   Replacement of a definitively dead process retains the existing narrower
   ownership policy. A blocked replacement preserves session bookkeeping.
   The adapter registers the complete durable Session binding before transport
   acquisition or resume, including on the first turn after controller restart.

## Boundaries and intentional non-changes

- `modules/agents/codex/transport.py` owns framing and transport error identity.
- `modules/agents/codex/agent.py` owns Codex resume and process replacement.
- `core/native_dispatch_phase.py`, `core/session_turns.py`, and
  `storage/message_deliveries.py` own proof of no write, durable settlement,
  and the retry budget.
- Existing Retry/Send now admission remains the recovery interface. No new
  queue state, schema migration, retry scheduler, or parallel queue is added.
  The existing recovery cadence remains; this change bounds attempts rather
  than introducing a second exponential-backoff scheduler.
- The 128 MiB reader limit remains a resource boundary. Other oversized
  protocol messages fail visibly; this change does not add an unbounded parser.
- Accepted or ambiguous native work is not blindly replayed after process
  death: tools may already have produced side effects. Adapter-local recovery
  requires explicit pre-write evidence, not merely a reconnectable exception.
- No workstation service restart, remote regression, deployment, or merge is
  part of this implementation authorization.

## Validation

- Codex adapter tests cover both resume paths, permanent failure identity,
  bounded reconnect behavior, and shared-process ownership.
- Transport tests reproduce a line overrun and distinguish it from normal EOF.
- Real-storage Delivery tests cover budget exhaustion across manager restarts,
  explicit recovery with original inputs, batch/FIFO preservation, and
  unchanged transient refusal/accepted/unknown policies.
- Reuse the isolated native Codex/loopback model harness to verify that the
  production adapter's excluded history still reaches the next model request.
- Run focused Python suites and changed-file Ruff checks; require current-head
  Codex review, zero unresolved threads, and the complete GitHub CI workflow.

### Local results

- 611 focused Python tests and 50 subtests passed, including the retry FSM,
  shared runtime ownership, CLI, UI stream, and message-delivery scenarios.
- All 9 isolated native Codex contracts passed with 0.154.0. The new excluded
  resume contract also passed against the official 0.153.2 binary: both resume
  paths returned the expected model/effort, omitted turns, retained the old
  non-ASCII input in the next model request, and preserved readable history.
- 4,661 UI tests passed; UI production build, lint baseline, theme validation,
  test typechecking, and changed-file Ruff passed.
- No real model service, user Codex home, or credentials were used by the native
  contracts. Full browser/IM operation against a deployed local Incus instance
  remains a post-merge opt-in; the running workstation service was untouched.

### Review follow-up

The review of `805cfbc3b` is the first findings-bearing head (four threads,
three root-cause classes: durable retry bookkeeping, runtime enrollment, and
live projection). The orchestrator verified the full inventory and
approved narrow contract-preserving corrections:

- Per-input accounting: independently exhaust each Delivery in a mixed-age batch.
- Runtime enrollment: produce the durable binding before any ownership-gated replacement.
- Hold transition closure: a non-spending concurrency refusal cannot release an existing hold.
- Live projection: publish the committed requeue through the existing queue event.

Each was reproduced by a failing regression before correction. The tests use
the real session-manager ownership target, mixed-age SQLite delivery batches,
repeated manual retries/refusals, and a separate database connection observing
the queue event after commit. The failing CI prompt-baseline fixture also now
supplies real prewrite evidence and an explicit successful-replacement result.
After these corrections, the expanded run passed 758 Python tests and 52
subtests, including all 9 native Codex contracts. Changed-file Ruff also passed.
