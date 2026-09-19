# Codex request-scoped Hub routing

## Outcome

Changing a Hub model or overlapping Sessions in the same working directory must
not replace a healthy shared Codex app-server. Real process/channel changes retain
the existing durable-owner and generation guards. No deployment or local service
restart is part of this change.

## Boundary contract

- The gateway issues one authentication token per live Codex process scope.
  A route change does not change the process fingerprint.
- A Hub launch supplies `gateway_request_metadata`, a string-to-string mapping.
  The Codex adapter passes it unchanged as `responsesapiClientMetadata` on
  `turn/start`, including a collaboration-mode compatibility retry.
- The gateway issues `avibe_route_id`, an opaque process-scoped route handle, and
  `avibe_turn_id`, the existing Avibe runtime turn ID. Neither field is a user
  model name. Existing route registrations own their lifecycle; no per-turn
  tombstone cache, second scheduler, or native Codex patch is introduced.
- Native Codex serializes these entries into the JSON string at
  `client_metadata["x-codex-turn-metadata"]`. The gateway validates the supplied
  route within the authenticated scope, checks its accepted wire model, and
  correlates only the explicitly named, still-admitted turn with that route.
- Route identity remains usable after its turn settles, for native continuations
  and retries, but can never be attributed to a newer turn. Process retirement
  revokes both authentication and request routing. Retention is bounded by the
  routes used by a process, not the number of completed turns.
- The gateway removes Avibe-private metadata before upstream forwarding while
  preserving unrelated native metadata. Missing, malformed, foreign-scope, or
  unknown identity fails closed without poisoning another turn.
- Non-Codex credential routing and native/direct channel behavior are unchanged.
  Older Codex clients that cannot carry the required metadata must not silently
  use active-turn inference.

## Acceptance evidence

1. Real isolated Codex proves request metadata through two concurrent threads,
   distinct models, tool continuations, transport retries, and automatic
   compaction where supported. Use test-owned HOME, CODEX_HOME, XDG and loopback
   mock upstreams only; include non-ASCII input.
2. Gateway/registry tests cover alias collisions, concurrency, late requests,
   invalid identity, body-validation failure, cancellation/draining, settlement,
   retirement, and bounded retention.
3. Adapter tests prove model changes reuse the same transport and that genuine
   channel/process changes still respect durable ownership.
4. Focused tests, changed-file lint, independent review, exact-head Codex PR
   review, and the complete expected CI set pass before handoff.

## Known by design

- Shared processes remain keyed by working directory.
- Genuine direct/native/Hub channel changes may still require process replacement.
- No broad catch, notification suppression, or extra delivery retry is added.
