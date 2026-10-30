# Search-history compatibility recovery

## Change contract

Implement an Avibe-only recovery for Anthropic native web-search history rejected by an upstream or pooled relay. Do not change relay routing, credentials, or production services, or retry the affected conversation as part of implementation.

The failure shape contains completed historical native searches, a later mixed server/client tool call, and a user message containing saved client tool results. A prefill rejection of that shape is a recovery candidate, not proof of a search defect. Synthetic regression coverage validates recovery behavior, not the original production cause.

### Admission and request changes

- Read only normalized provider rejection details, on `invalid_request` with HTTP 400 or an SSE error without HTTP status. Other errors retain their existing policy.
- Match explicit invalid search `encrypted_content` / citation `encrypted_index` errors against the submitted request's native search call/result pairs containing ciphertext. A `messages.N.content.M` path narrows the match when it resolves to such a pair; otherwise it is only a hint, since an intermediary's indices need not match this wire. Do not answer an error about absent ciphertext with an unchanged request.
- The exact assistant-prefill rejection permits one compatibility trial only if canonical context ends in a real client result, the wire ends in a tool-result-only user message answering every preceding client call, that assistant also has a pending native search, and there is completed native search history that can be demoted. Genuine assistant prefill, unrelated 400s, and malformed continuations are not search diagnoses.
- Reuse `transform_messages` with the web-search route's `replay=False`. Its existing deferred-exchange ownership identifies historical versus in-flight content. Historical exchanges become portable observations; current native exchanges, declarations, client call/result identities, and unrelated thinking signatures stay intact. Canonical history is never changed.
- An explicit refusal of ciphertext still in the current exchange retains the existing `opaque_refused(hosted)` fallback, including after a history trial. This converts that identity's hosted payloads to their portable form, with completed observations readable in place and a pending search carrying no invented result. It does not remove thinking signatures.

### Ownership, bounds, and compatibility

- Both a no-output rejection and a rejection after partial text/thinking/tool/search output are handled in the existing `_model` changed-request loop. Discard the failed, uncommitted attempt, audit its usage, and recompile from committed context on the same hop. No failed narration/search card is committed or republished. Vibey displays committed rows, not speculative deltas.
- No client call from a failed attempt executes. Previously completed client tool results remain context and are not run again. This is neither generic same-request replay nor an outer `Agent.resume` continuation.
- Allow one history-only trial per Source identity per run. An explicit current-ciphertext fallback is bounded by the existing one-per-(identity, payload kind) refusal handling. Already-portable requests and a repeated refusal cannot obtain another unchanged retry. RunScope still owns cancellation and normal tool/steer admission.
- Keep history-only decisions in memory, not as a new persisted `replay_refusal.payloads` value. A later turn may probe native history again; a guarded prefill error is not a permanent capability verdict. Existing explicit opaque refusals still use the released `hosted` / `signature` format, readable on rollback and by forks. No projection/schema migration is needed.
- No Model Hub evidence rule is broadened. History-replay compatibility and the ability to execute native searches remain separate.

## Validation

`tests/agent_core/ai/test_search_history_recovery.py` exercises the real adapter/compiler/Agent boundary with a deterministic mock upstream:

- direct/wrapped HTTP rejection and SSE rejection after partial output;
- initial native search, later mixed continuation, and a single corrected historical request;
- preserved current native calls, signatures, declarations, saved client results, and one client execution;
- repeated failures terminate; an explicit current ciphertext refusal still recovers after the historical trial, including after partial output;
- a steer accepted during rejection stays out of the pending native exchange; Stop prevents another request without losing the saved client result;
- genuine assistant prefill, missing history, invalid result pairing, unrelated/signature errors, and already-portable history do not gain history recovery; stale diagnostic indices do not remove recovery when ciphertext is present, while a resolvable path selects its own exchange.

`tests/test_vibey_agent.py` covers the distinct SQLite/delivery boundary: partial text, thinking, incomplete and completed client arguments, and a completed deferred search result, each with prefill or explicit search rejection, followed by either recovery or another refusal. The tests verify unchanged original rows, no failed partial/narration/search-card duplication, no repeated client execution, no new persisted refusal vocabulary, audited attempt usage, and the final success/error notice.

Run focused pytest and changed-file Ruff locally, independent read-only review, then exact-head Codex review and CI through the PR delivery loop. No live affected-session retry or deployment is part of validation.

## Known-by-design

- Portable history retains already-readable queries, sources, and citations, not decrypted page bodies.
- A retried provider attempt can repeat provider-owned read-only searches. The client cannot guarantee exactly-once upstream work; recovery must not repeat previously executed client tools.
- A successful compatibility trial does not establish the production root cause or prove that a hidden relay account changed.
- History-only trials are run-local. This intentionally avoids a permanent capability assertion and a rollback-incompatible on-disk marker.
- Generic retry, transcript-continuation budgets, gateway evidence, routing policy, and other protocols are unchanged except that explicit invalid-request SSE codes are classified as permanent errors rather than unknown failures.
