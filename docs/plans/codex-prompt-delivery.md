# Codex Prompt Delivery

## Contract

Avibe-owned instructions must enter model-visible developer messages independently
of model-owned collaboration text. Unchanged instructions must not accumulate on
each turn or process restart. Prompt delivery must not change model routing or
reasoning effort, and ambiguous native mutations must retain at-most-once recovery.

## Cause and Delivery

Codex 0.153.2 prefers a model catalog's collaboration instructions over
`collaborationMode.settings.developer_instructions`. A successful
`collaborationMode/list` probe therefore proves API support, not prompt delivery.
Both quick-reply and session-title rules can be present in the turn parameters
while absent from the model request.

Avibe now uses `thread/inject_items` with explicit developer messages for its
instructions. The existing durable `fallback` marker name and write-ahead states
remain unchanged. Legacy `collaboration` threads migrate through the existing
pending-clear path, even when their model and prompt fingerprint are unchanged.
Model and reasoning overrides remain top-level turn parameters.

`features.retain_client_developer_messages=true` preserves these messages through
Codex remote compaction v2. This replaces the collaboration-based delivery
described in the earlier managed-Skills implementation plan; Skill discovery and
prompt composition are unchanged.

## Evidence and Limits

- Unit tests cover injection deduplication, changed instructions, model routing,
  legacy migration, persistence failures, ambiguous RPCs, and restart recovery.
- `tests/test_codex_prompt_delivery_contract.py` drives the real Codex executable
  through Avibe's agent and transport into a loopback Responses server. It uses
  the actual quick-reply and session-title templates and a model catalog that
  overrides collaboration text. It covers new and legacy threads, prompt changes,
  process restart, and remote compaction v2.
- Run the native contract with `CODEX_PROMPT_CONTRACT_BINARY` set to the selected
  executable and pytest targeting that file. It is opt-in, requires no model
  credentials, and was verified with Codex 0.153.2. Ordinary CI runs the unit
  contract without installing a native backend.
- The retention flag only protects native remote compaction v2. Older Codex
  compaction implementations and third-party providers' local summarization may
  still summarize injected messages away. They are not a retention guarantee.
- This verifies transport delivery, not whether a real model follows every rule.
  Live Workbench button rendering and automatic title changes require the local
  Incus integration pass; no running developer service is restarted by this fix.

No existing scenario catalog owns prompt delivery. This change adds a native
contract rather than assigning an unrelated capability's scenario ID.
