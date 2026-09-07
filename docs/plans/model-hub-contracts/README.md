# Model Hub contracts

Status: **contract_version 10**, 2026-09-06: sparse manual overrides, shared effective
planning and guarded Restore/default membership. The approved routing contract is
`../model-hub-routing-modes.md`, including the owner-approved Empty Route Inheritance
correction `352486374`; API-key-only scope remains unchanged.

Supported persisted configuration shapes remain readable under the repository's
persisted-shape rule. Existing nonempty arrays, including stale and dormant OpenCode
routes, retain their exact intent. Validate supported original shapes before normalizing
valid empty values to absence; invalid values never silently become inherited. Canonical
maps omit empty values without dropping catalog identities or other configuration.
Reads/preview are pure; normal saves persist normalization. Do not infer old authorship
from array contents or Source order. Ephemeral envelopes use the terminal version; persisted
TurnProvenance accepts historical versions through the terminal one without new
required route fields. Existing persistence readers normalize retired reason values
before current-schema validation without changing the writer generation; raw historical
bytes need not already use today's reason vocabulary. Generation names in older implementation history are not a
current version declaration.

The `minItems: 1` constraints on `AgentSupply.routes` values and
`AgentChain.manual_override.hops` are canonical-output constraints only. Empty PUT
and preview arrays remain accepted Restore inputs, and supported legacy config must
validate its original shape before empty-value normalization; it must not be
rejected against the output-only restriction.

The contracts and their consumers must coexist on one tested PR head before Model Hub
can be enabled. CI evaluates that head, not individual commits. A green intermediate
commit is not evidence that the complete final protocol has landed.

## Product model

- Sources are upstream assets. A Source never owns ordering.
- Gateway stores one default Source membership/order per backend and sparse manual
  overrides per model. Absent and valid empty values inherit; only nonempty arrays override.
- The shared pure planner returns nonempty manual hops verbatim, otherwise all accepted matches
  in default order, otherwise unchanged-id passthrough on eligible non-retired Hub API-key defaults.
- Reads, previews, summaries, guards, adoption, probes, launches and execution use that
  effective plan. Health and process readiness annotate it without changing its origin.
- New catalog rows have no override; Source creation appends defaults but never writes
  manual hops. Restoring automatic or empty PUT deletes the key with identical exact
  guards; saving equal nonempty hops remains manual. Removing the final hop previews
  inherited routing through the same Restore/Undo/Save flow, never empty Manual.
- A hop whose upstream `model_id` differs from its menu model is an explicit configured
  mapping. There is no separate mapping object or runtime mapping event.
- The protocol vocabulary is exactly `anthropic | openai_responses | openai_chat`.
  `base_url` already distinguishes official endpoints from relays and self-hosted
  gateways; no fourth protocol value is defined.
- Source health is global and changes only from shaped explicit upstream classifications.
  Native CLI availability, exact-hop integrity, and bounded pre-first-byte connection
  backoff are live execution facts and never silently rewrite stored configuration.
- Supply state is pull-oriented. Successful fallback is silent; Gateway and Usage are
  the user-visible inspection surfaces.

Normative routing behavior lives only in `docs/plans/model-hub.md`: §4.2 owns effective planning and default placement, §4.3 owns effective-chain execution and credential failures, §4.5 owns state,
turn copy, and guarded mutation envelopes, and §6 owns native-config import actions.
Contract prose points to those authorities and does not add branches.

API-key passthrough is the delivery scope. Subscriptions continue automatic matching and
known-model manual routes; an unmatched subscription-only default set is Unconfigured.
No underlying engine expansion or OAuth alias substitution is part of this change.

## Invariants

1. Plaintext upstream credentials never appear in config, API payloads, events, logs,
   or Agent runtime configuration. Hub-held material is referenced by an opaque engine
   credential id; native credentials remain in the sanctioned CLI store.
2. Every persisted Source has a protocol with a named owner before commit: a shipped
   api-key vendor catalog pin, a user declaration on `custom`, or a matching
   protocol-shaped upstream response. `POST /api/models/sources/observe` is the
   non-persisting API-key observation surface; API-key `POST /api/models/sources`
   normally repeats observation before committed credential provisioning. Explicit
   `save_unverified: true` permits a catalog pin or declaration without observation;
   custom Auto still cannot be guessed. Completed Hub OAuth consent can retain its
   bound credential under the fixed vendor protocol. These Sources carry the
   independent `verification_pending` marker until an actual current-credential
   model call succeeds; model discovery does not clear it. A typed Base URL never
   supplies protocol ownership. Model-free validation, altered-credential controls
   and public model lists do not prove authentication. See the 2026-09-07 ruling
   in `docs/plans/model-hub.md` and the create/Source schemas for the complete policy.
3. Every Source/model reference is canonical and referentially valid at write time.
   Unchanged stale Route hops may be retained or reordered, but new or changed pairs
   must validate Source existence/eligibility, canonical identifiers and explicit retirement.
   Missing API-key inventory alone is not an invocation or manual-write rejection.
   Subscription Sources retain existing model admission and stale-hop retention.
   Normalized persisted catalog/target identities do not lose edit, preview, restore
   or history-read capability when a later new-identifier length bound is shorter.
4. Source deletion atomically removes its id from every backend Source order and every
   exact Route chain, preserving nonempty survivor order. Removing the final manual
   hop drops its override key and recomputes inherited routing before exact removal,
   supply-gap and transport-target planning. Serialization and reload reject a
   dangling Source id before empty-value normalization.
5. Manual Route membership and order survive unrelated state changes. Automatic routes
   follow current defaults and matching evidence. For the same planning inputs, health,
   quota and process readiness may change runnability/current but never tier or origin.
6. Every Source model id is unique within that Source. The final model item shape is
   `{id, origin, reasoning_efforts, reasoning_efforts_source, retired?, display_name?,
   discovered_at?}`. The tier source is `upstream | catalog | user | null`; only `user`
   and `null` declarations are editable. Older persisted rows without the field load as
   `user` when their list is non-empty and `null` when it is empty. Effort lists have no
   selected/default item. `retired: true` is a persistent discovered-model tombstone and
   never supplies.
7. A backend has at most one `native_cli` Source. Additional accounts are Hub-held
   Sources.
8. Direct mode and a Native hop are distinct. Direct bypasses Gateway for the backend;
   Native is one configured hop inside Gateway mode.

## Authority and mirror guard

`mirror-registry.json` is the executable index for closed vocabularies and decision
tables. The generated contract test reads the live schemas, specification, adapter
copies, and locale/type consumers in the same run. It enforces both directions:

- every registered contract or copy enum resolves to exactly one authority;
- every authority row has at least one registered consumer;
- cross-file vocabulary projections and mappings remain total;
- orphan values and orphan branches fail.

The gate does not accept a missing external snapshot. If a future consumer must use one,
the snapshot carries the current artifact fingerprint and a mismatch fails before any
comparison. A gate may not report success by comparing stale input with itself.

## Version closure

`contract_version` 10 must coexist in all registered version locations on the same tested head:

- `mirror-registry.json`
- `agent-chain.schema.json`
- `probe-result.schema.json`
- `observation-result.schema.json`
- `runtime-dependency.schema.json`
- `guard-refusal.schema.json`
- `turn-provenance.schema.json`
- `api-response.schema.json`
- `api.md`
- `core/handlers/model_hub/service.py`
- `core/handlers/model_hub/provenance.py`
- `ui/src/components/settings/models/types.ts`
- `tests/test_model_hub_config.py`
- `tests/test_model_hub_api.py`
- `tests/test_model_hub_l3.py`
- `tests/test_model_hub_resolution.py`
- `tests/test_model_hub_runtime.py`
- `ui/src/components/settings/models/*.test.*`

`tests/test_model_hub_config.py::test_every_versioned_object_ends_at_the_terminal_version_the_code_writes`
enforces the closure over whatever files this directory holds rather than over this list
— versioned objects by their shape, and every `contract_version` a document writes as
text — so an object or declaration added later is covered without an edit here. This
list stays as the reader's map of where the value is published.

One object diverges, and only in what it accepts. Every versioned object except
`TurnProvenance` is an envelope built and consumed inside one request, so it is pinned
to the terminal value alone. `TurnProvenance` is written to disk, where records a
released build persisted outlive the bump that republished the shape — the same
persisted-shape rule that governs config files. It therefore accepts the released values
as a set ending at the terminal one, and nothing branches on which member a record
carries.

The three-value protocol closure includes `source.schema.json`, `adapter-interface.py`,
and the byte-identical `core/handlers/model_hub/adapter.py` on the same tested head.

After implementation lands, these contracts are read-only for downstream lanes. An
implementation-discovered mismatch is reported to the orchestrator for a targeted
revision; the discovering lane does not reinterpret or edit the contract in place.

## Files

| File | Authority and consumer role |
| --- | --- |
| `source.schema.json` | Source identity, channel, three protocols, state, usage, inventory, credential reference, and audit metadata. |
| `source-create.schema.json` | API-key Source creation request, transient credential boundary, optional single-protocol constraint, and lost-response correlation. |
| `agent-supply.schema.json` | Backend mode, default Source membership/order and sparse manual intent, configuration eligibility, model-supply and backend-health projections. |
| `backend-model.schema.json` | Backend Agent model identity, editable capability metadata, and server-owned lock/routeability projection. |
| `agent-chain.schema.json` | Effective route, manual override and origin projection plus current execution position, runnability, blockers, live connection backoff, retry metadata, and model supply state. |
| `probe-result.schema.json` | Saved recovery probes and route probes over the shared effective plan, including the live connection-backoff reason without persistent network health. |
| `observation-result.schema.json` | Non-persisting Add-time connectivity, authentication, protocol-establishment, and inventory observation. |
| `turn-provenance.schema.json` | Exactly attributed turn attempts and terminal outcome; no policy or mapping discriminator. The one versioned object persisted to disk, so it accepts every released version. |
| `usage-summary.schema.json` | Metered token usage over a trailing local-day window, aggregated from proxied turns. A report only: no consumer may feed it back into resolution, admission, or cooldown. |
| `resolution-event.schema.json` | Pull-feed Source/resolution records and their closed reason/detail vocabulary. |
| `oauth-flow.schema.json` | Subscription creation and re-auth presentation without secret material. |
| `migration-scan.schema.json` | Copy-only import of existing native CLI/provider configuration; not an internal contract migration. |
| `runtime-dependency.schema.json` | Managed local Gateway asset, persisted enablement intent, lifecycle, and health. |
| `guard-refusal.schema.json` | Shared guarded-mutation refusal whose two arrays are the exact plan echoed by a confirmed retry. |
| `api.md` | Routes, envelopes, default Source order and manual Route writes/Restore/preview, guards, OAuth/import results, provenance, usage, and runtime status. |
| `api-response.schema.json` | Machine-readable response contract and real-response exercise for every route in `api.md`, at exact route-table parity. |
| `opencode-overlay.md` | Stable OpenCode provider/model identifiers and effective-hop overlay behavior. |
| `adapter-interface.py` | Adapter protocol, observation, credential, discovery, invocation, cleanup, and classification boundary. |
| `mirror-registry.json` | Executable authority/mirror registry and terminal contract version. |
| `README.md` | This ownership, version-closure, and contract-index document. |
