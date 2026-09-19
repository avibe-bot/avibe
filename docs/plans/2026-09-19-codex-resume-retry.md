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
   before requiring explicit recovery. Concurrency refusal and native
   acceptance/unknown-start recovery keep their existing policies.
4. Inputs requiring recovery remain queued with the same Delivery ID,
   snapshot, attachments, and FIFO position. Timer drains and restart recovery
   cannot replay them. The existing failure-notice Retry action and Send now
   can retry them, and a new explicit attempt begins a new bounded budget.
   The budget derives from server-owned delivery history, not process memory
   or caller metadata.
5. Failure-triggered Codex transport replacement uses the existing durable
   ownership and live-turn checks. A still-live shared process cannot be
   replaced while another accepted turn or protected activity owns it.
   Replacement of a definitively dead process retains the existing narrower
   ownership policy. A blocked replacement preserves session bookkeeping.

## Boundaries and intentional non-changes

- `modules/agents/codex/transport.py` owns framing and transport error identity.
- `modules/agents/codex/agent.py` owns Codex resume and process replacement.
- `core/native_dispatch_phase.py`, `core/session_turns.py`, and
  `storage/message_deliveries.py` own proof of no write, durable settlement,
  the retry budget, and explicit claim provenance.
- Existing Retry/Send now admission remains the recovery interface. No new
  queue state, schema migration, retry scheduler, or parallel queue is added.
- The 128 MiB reader limit remains a resource boundary. Other oversized
  protocol messages fail visibly; this change does not add an unbounded parser.
- Accepted or ambiguous native work is not blindly replayed after process
  death: tools may already have produced side effects.
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
