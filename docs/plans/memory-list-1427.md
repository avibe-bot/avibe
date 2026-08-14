# Memory episode listing via EverOS `/memory/get`

Issue: [#1427](https://github.com/avibe-bot/avibe/issues/1427)

## Goal

Add a closed, principal-scoped listing path for processed EverOS episodes, then
expose it through `vibe memory list`. The provider boundary must not leak raw
EverOS dictionaries or broaden the existing profile lookup.

## Locked decisions

- v1 lists only active processed `episode` memories. Profiles remain on the
  existing profile surface; atomic facts, unprocessed messages, agent cases,
  and agent skills are excluded.
- Single-project listing uses EverOS's 1-based `page` and `page_size` contract.
  CLI rejects `--project all`; only the verified UI/controller path may request
  an Avibe-aggregated `all` listing.
- The `all` result uses a versioned, bounded Avibe cursor that records per-project
  consumed offsets and a project-catalog fingerprint. It is not an EverOS cursor.
- `vibe memory list` is not added to the injected Personal Memory prompt. Parser
  contracts must prove the injected CLI surface remains unchanged.
- JSON output includes the provider-neutral opaque episode `id` for future item
  management. The id has no management semantics in this change.
- No config key, persisted schema, or EverOS source changes are required.

## Contract

The provider port accepts only an Avibe-derived principal, one stored project id,
`page >= 1`, and `1 <= page_size <= 20`. It sends the exact EverOS episode shape
to `POST /api/v2/memory/get` and returns a provider-neutral page:

- item: `id`, `kind=episode`, `subject`, `summary`, `body`, RFC3339 `timestamp`,
  and `project`
- page: `items`, `page`, `page_size`, current-page `count`, pre-page
  `total_count`, and closed warnings

The adapter validates the complete EverOS envelope, all four kind arrays, owner,
app/project scope, counts, and bounded item fields. Any malformed or cross-scope
response fails closed as `memory_provider_response_invalid`. A provider
`total_count` above EverOS's 20,000-row exact-ordering window adds the
transport-only `memory_list_truncated` warning.

The sidecar keeps the existing profile request as an independent exact shape.
Episode requests additionally allow only the exact eight fields used by Avibe:
`user_id`, `app_id`, `project_id`, `memory_type`, `page`, `page_size`, and the
fixed `sort_order=desc`/`sort_by=timestamp` pair. Unknown keys, filters,
`agent_id`, legacy/reserved projects, and values outside the bounds are rejected.

## CLI and aggregation

`vibe memory list [--project <slug>] [--page N] [--limit 1..20] [--json]`
defaults to `default`, page 1, and limit 20. The internal route derives the
principal from the caller-session binding exactly like search; no owner id is
accepted on the wire.

Single-project ordering is fixed to `timestamp desc`. This is the smallest
closed v1 contract because the EverOS episode response exposes `timestamp` but
not `updated_at`, so Avibe cannot correctly merge an `updated_at`-sorted `all`
result. Sorting controls can be added later only when the provider response has
the corresponding merge key.

For UI-only `all`, the runtime enumerates the same principal's current Memory
project catalog as search, fetches bounded provider pages under one 20-second
deadline, and performs a deterministic merge by timestamp then project/id.
Failures produce `memory_list_partial`; deadline/provider-window limits produce
`memory_list_truncated`. `total_count` is null if any project failed.

## Delivery and evidence

1. PR1: value types, EverOS port/fake, exact sidecar expansion, module/runtime
   single-project listing, and focused unit/contract tests.
2. PR2 (after PR1 review pass): scoped internal route/client, CLI/i18n/docs,
   UI-only aggregate cursor, parser contracts, and closed-loop scenarios.
3. Browse UI remains out of scope until its design mockup is approved.

Scenario IDs are catalogued under `tests/scenarios/memory_list/`; the closed-loop
evidence captures processed episodes in one scoped project, lists them through
the product boundary, and verifies an exact pagination boundary.
