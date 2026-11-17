# Model Hub actionable failure context

## Contract

Provider 401/403 errors describe invalid credentials or missing permission, without asserting revocation. Existing persisted reason keys remain compatible. Model-list refresh failures preserve the upstream HTTP status and a bounded, credential-redacted provider message through runtime, service, API and UI. A failed request to a target its Source does not list explains that the model is absent from that provider's inventory and suggests another model; it does not claim absence proves the cause of the upstream refusal.

The unlisted-model chat summary is one actionable sentence; source names, attempt statuses, and safe upstream text stay in the existing “Show details” provenance view. Setup cards keep the wording from #3569 (“Key rejected (401) · re-enter”); longer explanations distinguish invalid credentials from missing permission without asserting revocation.

Part 3 (request-private 503 wait) is superseded by the 2026-11-17 amendment in `model-hub.md` §4.3: an upstream HTTP 5xx classified as `server_error` for a model absent from the admitted Hub Source inventory ends that request at once as `request_nonfallback` (downstream 400), with no fallback, retry or wait, and does not write shared Source health or cooldown. Listed models, named auth/quota/rate-limit codes, transport failures, global blockers, and HTTP 200 buffered overload keep their existing behavior. There is no model-keyed shared state or persistence change.

## Validation

Before implementation, fresh local Incus worktree `--seed none`: wrong key rejected in setup (401); unlisted passthrough chat failed after 3m47s with HTTP503 and made other listed models unavailable; failed list refresh showed only generic copy. Real regression credentials entered solely through product forms.

Protect these boundaries with runtime error transport/redaction tests, bilingual API/UI refresh tests and bilingual terminal projection tests. Capture updated EN/ZH setup, refresh and chat surfaces in the same environment. Product review precedes Merge-ready; Avibe lead owns merge.
