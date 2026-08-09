# Model Hub contracts

Status: **FINAL v5 shape, implementation-gated (2026-08-09)**.

These files describe the terminal contract for Model Hub before first release. There is
no v4-to-v5 data migration, compatibility reader, conversion transaction, or version
discriminator: Model Hub has not shipped. `contract_version` is 5 wherever a versioned
object exists.

The contracts and their consumers must coexist on one tested PR head before Model Hub
can be enabled. CI evaluates that head, not individual commits. A green intermediate
commit is not evidence that the complete final protocol has landed.

## Product model

- Sources are upstream assets. A Source never owns ordering.
- Gateway stores one explicit Source order per backend and one exact ordered Route chain
  per menu model. Neither has a `follow | custom` or other policy discriminator.
- Add Source runs the sole placement policy once and persists its positions. Runtime
  never repeats placement, model matching, vendor matching, or substitution.
- Runtime walks the stored Route hops verbatim, rechecking only whether each exact hop is
  runnable now and whether a failure permits fallthrough.
- A hop whose upstream `model_id` differs from its menu model is an explicit configured
  mapping. There is no separate mapping object or runtime mapping event.
- The protocol vocabulary is exactly `anthropic | openai_responses | openai_chat`.
  `base_url` already distinguishes official endpoints from relays and self-hosted
  gateways; no fourth protocol value is defined.
- Source health is global. Native CLI process availability and exact-hop integrity are
  live execution facts and never silently rewrite stored configuration.
- Supply state is pull-oriented. Successful fallback is silent; Gateway and Usage are
  the user-visible inspection surfaces.

Normative routing behavior lives only in `docs/plans/model-hub.md`: §4.2 owns Add-time
placement, §4.3 owns exact-chain execution and credential failures, §4.5 owns state,
turn copy, and guarded mutation envelopes, and §6 owns native-config import actions.
Contract prose points to those authorities and does not add branches.

## Invariants

1. Plaintext upstream credentials never appear in config, API payloads, events, logs,
   or Agent runtime configuration. Hub-held material is referenced by an opaque engine
   credential id; native credentials remain in the sanctioned CLI store.
2. Every persisted Source has a protocol proved by a real upstream response before
   commit. `POST /api/models/sources/observe` is the non-persisting API-key observation
   surface; API-key `POST /api/models/sources` performs the same observation before
   its independent committed credential provisioning, while subscription OAuth uses
   its vendor-specific observation flow. Vendor names, Base URLs, and manual hints may
   order probes but cannot create a saved protocol value.
3. Every Source/model reference is canonical and referentially valid at write time.
   Unchanged stale Route hops may be retained or reordered, but new or changed pairs
   must validate.
4. Source deletion atomically removes its id from every backend Source order and every
   exact Route chain, preserving survivor order. Serialization and reload reject a
   dangling Source id.
5. Given the same stored configuration and request, the selected Route hops are stable
   across time, quota, and health changes. Live runnability and current execution
   position may change; Route membership and order may not.
6. Every Source model id is unique within that Source. The final model item shape is
   `{id, origin, reasoning_efforts, display_name?, discovered_at?}`; effort lists are
   editable capability declarations, may be empty, and have no selected/default item.
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

The terminal value 5 must coexist in all registered version locations on the same tested head:

- `mirror-registry.json`
- `agent-chain.schema.json`
- `probe-result.schema.json`
- `observation-result.schema.json`
- `runtime-dependency.schema.json`
- `turn-provenance.schema.json`
- `core/handlers/model_hub/service.py`
- `core/handlers/model_hub/provenance.py`
- `tests/test_model_hub_config.py`
- `tests/test_model_hub_api.py`
- `tests/test_model_hub_l3.py`

The three-value protocol closure includes `source.schema.json`, `adapter-interface.py`,
and the byte-identical `core/handlers/model_hub/adapter.py` on the same tested head.

After implementation lands, these contracts are read-only for downstream lanes. An
implementation-discovered mismatch is reported to the orchestrator for a targeted
revision; the discovering lane does not reinterpret or edit the contract in place.

## Files

| File | Authority and consumer role |
| --- | --- |
| `source.schema.json` | Source identity, channel, three protocols, state, usage, inventory, credential reference, and audit metadata. |
| `agent-supply.schema.json` | Backend mode, explicit policy-free Source order, configuration eligibility, model-supply and backend-health projections. |
| `agent-chain.schema.json` | Read projection of exact stored hops plus current runnability, blockers, retry metadata, and model supply state. |
| `probe-result.schema.json` | Saved recovery probes and route probes over exact configured hops. |
| `observation-result.schema.json` | Non-persisting Add-time connectivity, authentication, response-backed protocol, and inventory observation. |
| `turn-provenance.schema.json` | Exactly attributed turn attempts and terminal outcome; no policy or mapping discriminator. |
| `resolution-event.schema.json` | Pull-feed Source/resolution records and their closed reason/detail vocabulary. |
| `oauth-flow.schema.json` | Subscription creation and re-auth presentation without secret material. |
| `migration-scan.schema.json` | Copy-only import of existing native CLI/provider configuration; not an internal contract migration. |
| `runtime-dependency.schema.json` | Managed local Gateway asset, lifecycle, and health. |
| `api.md` | Routes, envelopes, exact Source order and Route-chain writes, guards, OAuth/import results, provenance, and runtime status. |
| `opencode-overlay.md` | Stable OpenCode provider/model identifiers and exact configured-hop overlay behavior. |
| `adapter-interface.py` | Adapter protocol, observation, credential, discovery, invocation, cleanup, and classification boundary. |
| `mirror-registry.json` | Executable authority/mirror registry and terminal contract version. |
| `README.md` | This ownership, version-closure, and contract-index document. |
