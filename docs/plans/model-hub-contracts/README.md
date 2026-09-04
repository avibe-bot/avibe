# Model Hub contracts

Status: **FINAL shape, implementation-gated. `contract_version` is 7 (2026-09-03
backend-catalog composition and reasoning-tier provenance); 6 published 2026-08-19
usage metering; 5 published the 2026-08-11 contract completion.**

These files describe the terminal contract for Model Hub before first release. No bump
carries a data migration, compatibility reader, conversion transaction, or version
discriminator: Model Hub has not shipped, so republishing the shape converts nothing.
`contract_version` is 7 wherever a versioned object exists. The owner-approved
pre-release corrections add server-owned candidate composition, one-time matching
points, and reasoning-tier provenance while removing the earlier persistent
network/timeout cooldown spelling, without adding compatibility paths.

`v5` is still written throughout these files and stays: it names the contract-completion
generation they were authored in, and sentences such as "Minimum v5 set" or
`adapter-interface.py`'s dated header describe what that generation said. Only
`contract_version` — the value a consumer reads and the closure below guards — moves.

Which of the two a sentence means is not left to the reader. A claim about the current
value spells `contract_version`, which is the token the closure guard matches, so a bare
`vN` is a generation name by construction. Prose that restated the number in its own
words is exactly how `api.md` came to advertise one value in its envelopes and another
in its own declarations.

The contracts and their consumers must coexist on one tested PR head before Model Hub
can be enabled. CI evaluates that head, not individual commits. A green intermediate
commit is not evidence that the complete final protocol has landed.

## Product model

- Sources are upstream assets. A Source never owns ordering.
- Gateway stores one explicit Source order per backend and one exact ordered Route chain
  per menu model. Neither has a `follow | custom` or other policy discriminator.
- Add Source, a menu-model add, and a built-in reconcile add each run the same matching
  and placement policies once and persist the accepted hops. Runtime never repeats
  placement, model matching, vendor matching, or substitution.
- Runtime walks the stored Route hops verbatim, rechecking only whether each exact hop is
  runnable now and whether a failure permits fallthrough.
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

Normative routing behavior lives only in `docs/plans/model-hub.md`: §4.2 owns one-time
matching and placement, §4.3 owns exact-chain execution and credential failures, §4.5 owns state,
turn copy, and guarded mutation envelopes, and §6 owns native-config import actions.
Contract prose points to those authorities and does not add branches.

## Invariants

1. Plaintext upstream credentials never appear in config, API payloads, events, logs,
   or Agent runtime configuration. Hub-held material is referenced by an opaque engine
   credential id; native credentials remain in the sanctioned CLI store.
2. Every persisted Source has a protocol with a named owner before commit: a shipped
   api-key vendor catalog pin, a user declaration on `custom`, or a matching
   protocol-shaped upstream response. `POST /api/models/sources/observe` is the
   non-persisting API-key observation surface; API-key `POST /api/models/sources`
   performs the same observation before its independent committed credential
   provisioning, while subscription OAuth uses its vendor-specific observation flow.
   A typed Base URL never creates a saved protocol value. Catalog pin and declaration
   still require reachability and authentication; they never bypass those failures.
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

`contract_version` 7 must coexist in all registered version locations on the same tested head:

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
| `agent-supply.schema.json` | Backend mode, explicit policy-free Source order, configuration eligibility, model-supply and backend-health projections. |
| `backend-model.schema.json` | Backend Agent model identity, editable capability metadata, and server-owned lock/routeability projection. |
| `agent-chain.schema.json` | Read projection of exact stored hops plus current execution position, runnability, blockers, live connection backoff, retry metadata, and model supply state. |
| `probe-result.schema.json` | Saved recovery probes and route probes over exact configured hops, including the live connection-backoff reason without persistent network health. |
| `observation-result.schema.json` | Non-persisting Add-time connectivity, authentication, protocol-establishment, and inventory observation. |
| `turn-provenance.schema.json` | Exactly attributed turn attempts and terminal outcome; no policy or mapping discriminator. The one versioned object persisted to disk, so it accepts every released version. |
| `usage-summary.schema.json` | Metered token usage over a trailing local-day window, aggregated from proxied turns. A report only: no consumer may feed it back into resolution, admission, or cooldown. |
| `resolution-event.schema.json` | Pull-feed Source/resolution records and their closed reason/detail vocabulary. |
| `oauth-flow.schema.json` | Subscription creation and re-auth presentation without secret material. |
| `migration-scan.schema.json` | Copy-only import of existing native CLI/provider configuration; not an internal contract migration. |
| `runtime-dependency.schema.json` | Managed local Gateway asset, persisted enablement intent, lifecycle, and health. |
| `guard-refusal.schema.json` | Shared guarded-mutation refusal whose two arrays are the exact plan echoed by a confirmed retry. |
| `api.md` | Routes, envelopes, exact Source order and Route-chain writes, guards, OAuth/import results, provenance, usage, and runtime status. |
| `api-response.schema.json` | Machine-readable response contract and real-response exercise for every route in `api.md`, at exact route-table parity. |
| `opencode-overlay.md` | Stable OpenCode provider/model identifiers and exact configured-hop overlay behavior. |
| `adapter-interface.py` | Adapter protocol, observation, credential, discovery, invocation, cleanup, and classification boundary. |
| `mirror-registry.json` | Executable authority/mirror registry and terminal contract version. |
| `README.md` | This ownership, version-closure, and contract-index document. |
