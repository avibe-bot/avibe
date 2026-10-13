# Backend-reported Model Hub terminal failures

## Change contract (2026-10-13)

A source-origin Vibey `stalled` failure reached the Hub failure bridge as text
only. `fail_hub_attempt` wrote `protocol_error` and `stream_started: true`, even
when neither fact was established. This change corrects recorded diagnostics;
it does not change retries, timeouts, route selection, Source health, or Stop.

- The existing BaseAgent -> ModelHubRuntimeRouter -> correlation failure bridge
  accepts an optional structured `error_kind`. Vibey supplies its existing
  `RunEnded.cause.kind` only for source-origin failures. Do not classify by
  diagnostic substrings, infer phase from retryability, or use a prior response.
- Backend-only fallback records use `terminal_error.reason = backend_error`,
  optional `error_kind`, and optional `backend_error_detail`. A `stalled` kind
  remains `stalled`, including in copied JSON and localized detail presentation.
  Other backends without typed facts use the neutral backend-error reason.
- `backend_error_detail` is a credential-redacted, control/bidi-stripped,
  whitespace-collapsed, at-most-400-character diagnostic, not an upstream API
  answer. Reuse the existing Model Hub diagnostic sanitizer by moving it to
  an appropriate shared owner rather than maintaining a weaker second policy.
- `stream_started` may be null on a backend-error record: the callback has no
  exact failing-request phase evidence. Unknown is neither false nor true.
  All gateway-owned terminal reasons keep their existing boolean phase contract.
  Propagating additional provider phase telemetry is not required for this fix.
- An already authoritative gateway failure (terminal projection OR concrete
  terminal_error, even when that reason has no copy projection) must survive a
  backend notification. Keep Stop/frozen-outcome and late-success protections.
- Existing released provenance records remain readable without the new fields.
  Do not rewrite old history by guessing what really happened. Update the owning
  JSON schema, TypeScript mirror, API contract, and English/Chinese UI copy.
- Failure details label the backend diagnostic distinctly, never claim it is
  upstream response text, and show the typed kind (a localized stall label for
  stalled; other kinds may be displayed as diagnostic machine identifiers).

## Validation obligations

The primary regression must exercise the real Vibey adapter and shared failure
bridge into persisted provenance: existing coverage stubs out that bridge and
only checks notice copy. A scripted stalled provider must not become a protocol
error, and the old implementation must fail the new assertion.

Extend the existing correlation tests for authoritative gateway errors without
copy projections, generic native-backend notifications, preservation of late
success/Stop rules, and historical reads. Exercise non-ASCII diagnostics and
redaction. Add UI consumption coverage distinguishing backend diagnostics from
upstream API facts; run focused tests, changed-file Ruff, UI build, exact-head
Codex review and full CI. No production-state writes, deployment, release,
service restart, or merge is authorized.
