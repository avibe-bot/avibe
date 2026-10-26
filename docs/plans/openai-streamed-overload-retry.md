# OpenAI streamed overload replay

## Background and goal

The engine hands a Responses stream to the gateway at first model output.
Ordinary recovery therefore cannot replay a later overload without duplicating
text or tool calls. The gateway must hold that output before deciding to replay.
The change belongs to the shared Model Hub, not a Desktop or Agent adapter.

## Change contract

With automatic recovery enabled, the gateway buffers the first streamed
`openai_responses` body before committing response headers or bytes. Once fully
consumed, only this conjunction permits one buffered-output replay:

- an HTTP 200 stream with recognized model output;
- its classified failure is `server_error`, and its wire type/code candidates
  name only `server_error` (excluding the root `type:error` envelope selector);
- its bounded wire message, before display sanitization or whitespace folding,
  is exactly
  `Our servers are currently overloaded. Please try again later.`

This is protocol-based, including OpenAI-compatible custom Sources; it is not a
vendor-label or Desktop switch. Other errors and protocols keep their original
outcomes. Successful Responses bodies are also delayed: the error cannot be
known before reading the terminal. No non-streaming request changes.

The failed body is discarded, and normal configured routing/recovery chooses
the next attempt. A ready backup proceeds immediately; a sole cooling Source
waits for its normal eligibility. This remains one pending inference request:
the original 120-second admission window and half-open ownership never reset.
An already admitted inference may finish beyond that window.

Only one **post-output replay** is allowed per gateway request. The next
resolved stream is forwarded normally, including its partial output if it
fails; it is not buffered/replayed again. Ordinary pre-output retries retain
their existing budget, so total upstream attempts need not be limited to two.
If no retry can be admitted, only the resolver's terminal error is delivered.

## Ownership and implementation

- `resolve_with_recovery` retains its `RecoveryRequest` while a gateway callback
  prepares the resolved response. A callback may return a settled retry outcome
  to resume that same loop; final completion clears progress once.
- The gateway owns the adopted handle and spool before any awaited operation.
  Spool I/O and cleanup run off the controller loop with cancellation-safe
  ownership. RAM is capped at 256 KiB; larger bodies spill to a temporary file.
  The complete replay buffer is capped at 128 MiB; exceeding it is a local
  delivery failure, never permission for another upstream request.
- Service settlement records the discarded overload as a failed fallback
  attempt and applies its normal Source cooldown, but does not project a final
  Turn failure. The actual upstream `stream_started` fact remains true. Hidden
  output from this discarded overload does not clear an existing recovery
  streak or emit a recovery-success event.
- Each upstream attempt is metered once. Only after settlement/metering and
  discard does the gateway clear its attempt-owned state. Cancellation while
  buffering, waiting, or sending the next body still owns cleanup.
- Only the final admitted producer supplies response headers. A pinned Vibey
  continuation is never replayed on another Source: existing `pin_unavailable`
  handback asks its caller to re-render portable content instead.
- A spool failure is a local delivery failure, not replay permission or evidence
  against the provider. Upstream usage is retained.

## Known by design

- Initial Responses output, reasoning, and client-executed tool calls are not
  visible until the first stream ends. This first-byte latency is the cost of
  preventing duplicate downstream effects; other protocols still stream live.
- Provider-side hosted tools may already have run, and the provider may bill
  both attempts. This does not provide exactly-once upstream execution.
- Each spool uses at most 128 MiB of temporary storage. A full/unreadable
  temporary disk or a response exceeding that cap ends the request locally;
  temporary files are closed on every owned exit.
- No route/config/credential changes, schema migrations, deployment, service
  restart, or live upstream experiment is included.

## Validation

The owner-boundary tests use real loopback HTTP, the production engine client,
and the gateway; all credentials and state are synthetic and test-owned.

- `MH-RETRY-OVERLOAD-001`: exact root/nested error matrix, custom and OpenAI
  Sources, retry success/failure, lossless nonmatches, distinct usage and
  provenance, real Vibey fallback origin and pin handback, other protocols.
- `MH-RETRY-OVERLOAD-002`: withholding output, disk rollover, disconnect during
  buffering/recovery wait, local spool write/read failures, and the size cap.
- `MH-RETRY-OVERLOAD-003`: earlier pre-output refusal and streamed overload share
  one recovery clock and failure streak; expired admission exposes no discarded
  bytes and an overload cannot announce recovery merely by producing hidden text.

The positive regression was run before the fix and failed because the original
partial text/tool calls and overload were returned instead of the retry body.
Adjacent recovery, inference-wait, L3, and Vibey consumer tests are also run.
Exact-head Codex review and CI remain delivery gates.
