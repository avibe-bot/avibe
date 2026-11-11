# Model Hub actionable failure context

## Contract

Provider 401/403 errors describe invalid credentials or missing permission, without asserting revocation. Existing persisted reason keys remain compatible. Model-list refresh failures preserve the upstream HTTP status and a bounded, credential-redacted provider message through runtime, service, API and UI. A failed passthrough request explains that the requested model is absent from provider inventories and suggests another model; it does not claim absence proves the cause of the upstream refusal.

The unlisted-model chat summary is one actionable sentence; source names, attempt statuses, and safe upstream text stay in the existing “Show details” provenance view. Setup cards keep the wording from #3569 (“Key rejected (401) · re-enter”); longer explanations distinguish invalid credentials from missing permission without asserting revocation.

Part 3 supersedes the earlier admission/cooldown deferral: a literal upstream HTTP 503 classified as `server_error` for a model absent from the admitted Hub Source inventory does not write shared Source health or cooldown. That request still owes the existing request window, 503 backoff, and validated `Retry-After` timing in request-private Source-id state. Listed-model 503s, other statuses, global blockers, and HTTP 200 buffered overload keep their existing shared behavior. There is no model-keyed shared state or persistence change.

## Validation

Before implementation, fresh local Incus worktree `--seed none`: wrong key rejected in setup (401); unlisted passthrough chat failed after 3m47s with HTTP503 and made other listed models unavailable; failed list refresh showed only generic copy. Real regression credentials entered solely through product forms.

Protect these boundaries with runtime error transport/redaction tests, bilingual API/UI refresh tests and bilingual terminal projection tests. Capture updated EN/ZH setup, refresh and chat surfaces in the same environment. Product review precedes Merge-ready; Avibe lead owns merge.
