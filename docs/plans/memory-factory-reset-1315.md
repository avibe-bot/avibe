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
- Scenario catalog IDs `MEMORY-FACTORY-001`, `002`, `101`, and `201` with
  hermetic frontend contract checks.
- English and Chinese Memory docs describe deletion scope, retained state,
  partial outcomes, and crash recovery.
