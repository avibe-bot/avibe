# Unlisted-model 503: request-owned backoff

## Contract (product approval, 2026-11-12)

A literal upstream HTTP 503 classified as `server_error` for an actual sent model
absent from that Source's inventory at transport admission does not change shared
Source health or cooldown. The pending request still uses the existing recovery
window, 503 delay sequence, jitter, and validated Retry-After advice. Its final
message remains the one-sentence inventory guidance delivered in #3581.

Private deadlines belong to the existing RecoveryRequest, indexed by existing
Source id, with the admitted Source identity as a value. There is no model-keyed
shared state and nothing persisted. Distinct deadlines are necessary: if Source A
says Retry-After=300 and Source B can retry after30, a single max timer would end
the request earlier than the shipped fallback behavior. Each request keeps its
own Source streaks; concurrent independent tries intentionally do not coalesce.

Global admission remains authoritative: listed-model 503, 429, auth failures,
existing provider-wide blocks, half-open owners and identity changes keep their
current behavior. Private waiting combines with global deadlines, never replaces
a stronger block or revives a Source. Inventory membership is frozen at admission;
manual aliases are judged by the actual upstream model, not the requested name.

No retry after output, no new wait notice, no copy or persistence-schema changes.
HTTP200 buffered overload remains under its existing policy. Probe failures get
the same health exemption but no new retry loop. Downstream cancellation releases
owned admission and discards request-local state.

## Acceptance and regression ownership

The existing recovery tests lack the distinction between global cooldown and
request-private waiting. Add service-boundary regressions proving fast503 waits
instead of spinning, a concurrent listed-model call remains usable, listed503 and
existing shared blocks still apply, mixed Retry-After preserves fallback, Source
identity/inventory races keep the correct scope, and cancel cannot leak waiting
or a half-open slot. Extend actual gateway/stream tests for admission/settlement
facts that direct service tests cannot reach. New assertions must fail before the
fix. Run focused Python/scenario tests, changed-file lint, then exact-head Codex
and CI on a new PR from master. Lead owns merge; notify item7 Session
`sesdn8zt5wc9e` only after GitHub confirms MERGED.
