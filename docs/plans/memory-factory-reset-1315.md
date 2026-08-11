# Memory Factory Reset (#1315)

## Background

Rebuild can recreate projections from Markdown but cannot recover a corrupt
Memory tree, queue database, or durable state. Factory reset is the supported
replacement path for that corruption floor.

## Frontend contract

- Settings > Memory exposes Factory reset beside Clear all.
- The shared destructive confirmation waits five seconds and names exactly
  `memory` and `state/memory` as deleted while retaining settings and the
  installed pinned artifact.
- The request is awaited at `POST /api/memory/runtime/factory-reset` with
  `{"confirm": true}`; there is no polling or generic operation projection.
- The final payload's `roots` entries are rendered independently so partial
  deletion remains truthful.
- A projected `recovery_intent: factory_reset` changes the action to Retry;
  invalid or unknown `memory-runtime` artifact state disables it and links to
  Dependencies Repair.

## Evidence

- Vitest coverage for confirmation, awaited request, pending Retry, and the
  existing Memory settings/recovery controls.
- Scenario catalog IDs `MEMORY-FACTORY-001`, `002`, `003`, `101`, `201`, `202`,
  and `301` with executable test docstrings carrying each ID. The backend
  service-boundary scenarios exercise the Controller replacement gate, old
  runtime worker/process quiescence, stale old-module rejection, fresh runtime
  claim resumption, truthful two-root deletion, and durable
  `recovery_intent: factory_reset` after post-delete activation failure.
- Focused route contract coverage proves the public adapter preserves the
  exact internal `memory_operation_in_progress` closed envelope as HTTP 409
  while rejecting malformed envelopes and omitting backend-only fields.
- English and Chinese Memory docs describe deletion scope, retained state,
  partial outcomes, and crash recovery.
