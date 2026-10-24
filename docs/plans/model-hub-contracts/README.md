# Model Hub contracts

Status: **contract_version 21**, 2026-10-24: keyless Exa activation (KEX-3).
`gh-v3.2.3rc3` through `rc7` carry v20, so the unreleased v21 event now includes
its coordinated readers, producer, and default-on activation. The versioned
mirrors advance together; persisted TurnProvenance retains v5–v20 and adds v21.
KEX-1 and KEX-2 supplied the optional built-in catalog/read metadata, synthetic
`src_exakeyless` Source identity (`exa-mcp` / `exa/mcp`), keyless MCP producer,
cooldown classification, and compatibility-safe readers. KEX-3 sets
`KEYLESS_EXA_ACTIVE = true` for the reader and producer together and ships the
public disclosure; no live Exa call is used in tests or CI. An older release must
ignore the additive fields while retaining ordinary service settings and orders.
The localized hosted-activity vocabulary adds `service_rate_limited` as a fallback
reason; it never creates a credential health blocker for the synthetic Source.

It builds on v20 (2026-10-22): active web-search services (S2b-2).
`gh-v3.2.3rc1` and `gh-v3.2.3rc2` carried v19, so activation opened v20 rather than
changing the released event. `SEARCH_SERVICES_ACTIVE = True` activates service candidates,
`service_vendors`, frozen turn settings and the mixed search chain together with S6 disclosure.
The versioned mirrors advance together; persisted TurnProvenance retains v5–v19 and adds v20.
Relay diagnostics in this v20 shape may retain only bounded, credential-free `request-id` and
`x-request-id` response facts on the exact owning attempt; arbitrary headers, bodies and
upstream identity inference remain outside the contract.
The downgrade warning in `../web-search-services.md` §12 remains required.

It builds on v19 (2026-10-21): dormant web-search-service readers and producer
(S2a/S2b-1, `../web-search-services.md` §3–5, §6.5, §8). The RC tag `gh-v3.2.2rc13`
carries v18, so those additions opened a new event rather than changing v18.
- C-1 (`../agent-core-contracts/message.schema.json`) accepts `HostedRecord.attempts[]`
  (`HostedAttempt`), `HostedCost.estimate`, and a `ServiceExecutorOrigin`
  (`{provider, api: "search_api", model}`) beside the released `Origin` in
  `ProxyExecution.executor_origin`. `HOSTED_ERROR_CODES` gains the six `service_*`
  attempt codes, `search_query_too_long` and `search_sources_blocked`. `HostedSource`
  and the assistant's four-protocol `Origin` are unchanged, and released records load
  and serialize unchanged.
- Capability evidence (`capability-evidence.schema.json`) and the Tools read's
  `status.latest_ok.mode` and `status.verified[].mode`
  (`hosted-tools-status.schema.json`) accept `mode: "service"`.
- Usage (`usage-summary.schema.json`, `api.md` Search-service usage): service ledger
  rows keyed by Source and catalog `service_id`, the required `services[]`,
  `kind: "service"` bucket rows, `service_reported_usd` and `excluded_service_requests`
  on totals and days, and `pricing.service_price_date`. Model counts never include
  service requests.
- S2b-1 added the gated unified service/model chain: frozen preference/settings, bounded mapping,
  independently fenced service cooldowns and Source health, re-enable trials, sent-request metering
  and positive evidence. The flag-off runtime/read stayed unchanged in v19; S2b-2 activates in v20.
- `POST /api/models/hosted-tools/web_search/services/<source_id>/retry` clears the controller's
  service cooldown and advances its generation, with a plain success response. It is available
  regardless of activation and follows the existing Member management boundary.
- C-6's search-call contract and the turn's `hosted_tools` add the mixed chain, skip/terminal
  rules, native preference, service billing and classified-only Source health writes.
  Re-enable is the one non-attempt action advancing Source settlement generation.
- The versioned mirrors advance together; persisted TurnProvenance retains v5–v18
  and adds v19.

It builds on v18 (2026-10-20, carried by `gh-v3.2.2rc13`): web-search settings and
dormant Tools read (S1b, `../web-search-services.md` §3.2/§3.5). The RC tag
`gh-v3.2.2rc12` contains S1a and v17, so these additions opened a new event rather
than changing v17.
- V2 adds `prefer_native`, partial Source-keyed `services` overrides (`results` and
  `enabled`) with catalog result-count clamping, and server-owned `executor_order_kinds`
  saved with each web-search order.
- The PUT merges Source/field patches and supports null override resets.
- The Tools read adds `prefer_native`, saved `services`, read-only `service_order`,
  model/service candidate discriminants, service settings/health/trial/cooldown
  fields, and service vendor facts.
- S1b kept service candidates and `service_vendors` absent behind the single internal
  activation gate, with no UI, service execution, placement rule, turn freeze or native-preference
  routing change. S2b-2 activates them together in v20.
- The 2026-10-20 owner decision makes phase-1 web search snippet-only. This event
  withdraws v17's bundled page-content request/result mappings, `content_max_chars`
  and per-page price extras; Exa's included `contents.highlights`, reported-cost
  mapping and estimate flag remain. Page reading belongs to phase-2 `web_fetch`.
  S1b's unshipped `content` setting and vendor content flags are removed from this
  v18 event. A dev build's saved `services.<id>.content` is ignored with the normal
  unknown-field warning, preserving the tool switch, search opt-outs and orders.
- The versioned mirrors advanced together; persisted TurnProvenance retained v5–v17
  and added v18. The rc12 hosted-tools loader ignores new optional config fields
  and retains the whole-tool switch and both orders, not per-Source service
  opt-outs. An rc12 config write drops those overrides; re-upgrading then uses
  service defaults, so DeepSeek's opt-out must be reapplied before execution.

It builds on v17 (2026-10-19): search-service foundations
(`../web-search-services.md`). Tool-only Sources use `protocol: search`; the catalog,
credential custody, free checks, re-enable response and service-order persistence
shipped before search execution in `gh-v3.2.2rc12`. Its page-content metadata is
withdrawn by v18 as recorded above. Later lanes extend the current version event
only while it is unshipped; an RC freezes its meaning. S2b-2 activates the service
read and snippet-only search chain.

Catalog metadata verified on **2026-10-21**:
- DeepSeek maps the model-facing `summary` and bills only non-empty 200 results; its
  declared unbilled timeouts follow the design's §4.3 rule. The official reference is
  [Search](https://api-docs.deepseek.com/api/search) and the [guide](https://api-docs.deepseek.com/guides/search).
- The catalog pins Tavily basic search and Exa Auto with highlights only. Exact status/tag
  overrides distinguish Exa key/team budgets from balance and moderation refusals from
  unavailable endpoints; Tavily 422 is query-specific. See
  [Exa errors](https://exa.ai/docs/admin/error-codes) and
  [Tavily OpenAPI](https://docs.tavily.com/documentation/api-reference/openapi.json).
- `search.auto_enroll` is explicit for model vendors with search; only DeepSeek is
  enabled automatically. S1b and S2 own the resulting settings defaults.
- Exa's `search.result.reported_cost` names `costDollars.total` with `estimate: true`,
  following the [Exa Search reference](https://exa.ai/docs/reference/search).
  Its always-requested `contents.highlights` supplies snippets and is included in
  the search price. The catalog has no page-content request or result mapping.
- The adapter's hard 8 MiB response cap remains; an over-cap body is unreadable.
  S2 applies the design's billing, cooldown and failover rules to that outcome.
- Tavily's usage response may omit account pairs. A valid key pair with headroom
  (including `limit: null`) then passes the free credential check; it proves key
  authentication, while the first search confirms payment. A key at its limit
  still reports `limit`; malformed or half-present pairs report `unknown`.
- The query bound is 128 characters for DeepSeek only. Tavily and Exa document no
  query-length maximum, so their catalog `query_max_chars` remains null.

It builds on v16 (2026-10-16): two non-self-healing Source causes,
`plan_excludes_product` (`models.source.needs_action.plan_excludes_product`), for a plan that does not include the
product, and `spend_limit_reached` (`models.source.needs_action.spend_limit_reached`), for an account past a spend or
usage limit only raising it lifts, join the resolution-event, provenance, Source, agent-chain and probe vocabularies,
and the mirror registry pairs them. The
terminal diagnostic's `upstream_error_code` gains `usage_not_included`, `credit_balance_exhausted`,
`organization_spend_limit_exceeded`, `project_spend_limit_exceeded` and `organization_usage_limit_exceeded`. Each
is a closed value a v15 consumer rejects.

It builds on v15 (2026-10-15): hosted tools design v2 (`../vibey-hosted-tools.md`), passive and
default-on, with no probes. This bump publishes the Model Hub side; the hop-resolution and executor changes join it
before it ships.
- Capability evidence is passive (`capability-evidence.schema.json`, version 2): `basis` is `observed` only; a record is
  keyed by status, so a `verified` success (with its `mode`) and an `unsupported` refusal coexist; refusals have no
  time limit and read as unknown once the Source identity or the `wire` stamp (engine release, Avibe release, the
  model's rendered fields) differs; `shape` and `engine` are gone.
- Refused efforts lose `changes_at` and their `probe` basis (`agent-chain.schema.json`, `source.schema.json`).
- The Tools read is rewritten (`hosted-tools-status.schema.json`): per tool `enabled`, passive `status` (`latest_ok`,
  `refused[]`, `verified[]`), the search executor order (saved and effective, with candidates), and the image models
  with their default.
- The probe endpoint and `hosted-tool-probe-result.schema.json` are removed. `POST /api/models/hosted-tools/<tool>/clear`
  and `POST /api/models/sources/<source_id>/evidence/clear` clear refusals; the PUT is per field.
- The provider overlay gains a vendor's `executor_models[]` and `image_models[]`, and loses the native `image_generation`
  declaration.
- `HopResolution.capabilities` gains `context_price_steps` (`hop-resolution.schema.json`): where the target model's
  list price rises with its context, from the price table, which Vibey's compaction threshold stays below.

It builds on v14 (2026-10-14): opaque history is bound to the account that issued it, the Source
identity, not only to the origin (#2620): a provider refuses another account's encrypted reasoning.
- `HopResolution.source_identity` (`hop-resolution.schema.json`) names the primary hop Source's credential-free
  identity, which Vibey records on each response its own Source served and matches before replaying opaque payloads.
- The gateway strips an unpinned request's opaque history for another origin, Source, or Source identity, read on the
  final admission snapshot. A request pinned to the primary hop whose identity changed since the launch answers
  `pin_unavailable` (`vibey-consumer.md`). The identity stays in memory: never in the served-hop header or provenance.
- `source.schema.json` gains the optional `credential_generation`; Source identity follows it instead of
  `verification_pending`, so verification changes no identity and an identity never reverts after a credential
  change.

It builds on v13 (2026-10-14), which published the hosted-tools Phase 1 additions
(`../vibey-hosted-tools.md`) as one version event:
- the capability evidence the engine's 400s teach (`UpstreamRejection` in `adapter-interface.py`) and refused efforts,
  on agent-chain hops and Source models, with when they change (`changes_at`);
- the Tools tab's read (`hosted-tools-status.schema.json`): executor binding, candidate billing, price and probe
  estimate, and evidence expiry;
- the user-started hosted-tool probe (`POST /api/models/hosted-tools/<tool>/probe`,
  `hosted-tool-probe-result.schema.json`): call and replay, and a call probe for image generation;
- the picker read's `effort_menu` (`backend-model.schema.json`);
- `clean_api_keys` on migration apply, which migrates saved API keys without upstream validation;
- that the engine no longer adds its own `image_generation` tool (`opencode-overlay.md`).

It builds on v12 (2026-10-13): the served-hop response header also names the serving Source
id (`served-hop.schema.json`), because two Sources can share an origin. v12 builds on v11
(2026-10-02): the Vibey Hub-only consumer, primary-hop resolution and response origin linked to the served attempt, plus the Google protocol
vocabulary and Gemini gateway frontend, with API/config-only Source admission. See
[`vibey-consumer.md`](vibey-consumer.md) and [`google-protocol.md`](google-protocol.md).
The approved routing contract is
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
- The Source protocol vocabulary is `anthropic | openai_responses | openai_chat | google | search`.
`MODEL_PROTOCOLS` is the four invocable model-wire protocols (`anthropic | openai_responses |
openai_chat | google`); `SOURCE_PROTOCOLS` is that tuple plus the tool-only `search` protocol.
Model observation, probes, discovery, auto-detection, provider overlays, and hop schemas use
`MODEL_PROTOCOLS`; only persisted Source and Source-create shapes use `SOURCE_PROTOCOLS`.
  `base_url` distinguishes official endpoints from relays and self-hosted gateways.
  Google is explicitly declared through API/config; the three-option UI picker and
  custom Auto-detection set remain unchanged.
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
   credential id; a retained `native_cli` Source remains in the sanctioned CLI store,
   while an approved native migration explicitly transfers selected custody into Hub.
2. Every persisted Source has a protocol with a named owner before commit: a shipped
   api-key vendor catalog pin, a user declaration on `custom`, or a matching
   protocol-shaped upstream response. `POST /api/models/sources/observe` is the
   non-persisting API-key observation surface; API-key `POST /api/models/sources`
   normally repeats observation before committed credential provisioning. Explicit
   `save_unverified: true` permits a catalog pin or declaration without observation;
   custom Auto still cannot be guessed. Completed Hub OAuth consent can retain its
   bound credential under the fixed vendor protocol. A newly stored Hub credential
   that no add-time observation authenticated — an explicit unverified save, a
   native-config import, Hub OAuth admission, any credential or endpoint replacement —
   carries an opaque `verification_pending` identity. Model Sources keep the
   matching-credential model-call rule: a successful model call with the same current
   credential and marker may clear it. A tool-only `search` Source never makes a model call.
   For every search-capable Source, including an official DeepSeek model Source
   saved unverified, the marker clears on an authenticated search outcome with the matching credential
   (any 200, a 402, or a catalog-named limit), or a free-check `pass`, `balance` or
   `limit` for that credential. A 200 proves authentication even when its results
   are empty or its body is unreadable. Clearing requires the same current credential
   and marker and is a fresh cross-process field mutation; `credential_generation`
   does not change. Free-check `unknown` and `invalid` leave it unchanged.
   Model discovery never clears it.
   A typed Base URL never supplies protocol ownership. For model observation,
   model-free validation, altered-credential controls and publicly readable model
   lists do not prove authentication; on a protocol that
   already has an owner, a model listing that answers the credential while refusing
   the identical uncredentialed request does. See the 2026-09-07 rulings
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
9. The `vibey` backend is Hub-only and never consumes a native CLI Source or launch
   overlay. Its empty catalog added on upgrade changes no existing backend intent.
   Response origin is captured at admission and shared with the recorded attempt.

### Takeover producer/consumer closure

Source identity reuse retains the current credential proof and Source/ref/state.
If the consenting backend references that Source in neither its default order nor
an explicit Route, append it only to that backend's defaults before withdrawal.
Do not rewrite existing Routes or other backends' orders. This also applies when
an existing native subscription Source changes custody in place.

OpenCode candidates sharing vendor, protocol, normalized target and credential
are one logical import even when several native config layers reference them.
Union their manual model IDs (later layer labels win for a repeated ID), and bind
the consent identity to every contributing candidate. Distinct credentials or
targets and all blockers remain separate. Physical cleanup still inventories and
journals every supported config/auth path; logical deduplication cannot remove
file compare guards or unrelated settings.

Persisted native authentication and live launch configuration must converge
before custody can complete or native admission can reopen. The internal,
synchronous seam is:

- `ModelHubService._reconcile_native_auth(backends: tuple[str, ...]) -> None`
  reads the store's current `native_auth_snapshot`, not a guessed Hub-mode
  projection, and supplies it to optional `migration_reconcile_auth`.
- The Controller binds that callback to
  `BackendRestartCoordinator.reconcile_migration_auth(snapshot) -> None`.
  It is valid only inside that coordinator's existing migration guard with
  request locks, native lease and both admission gates still owned.
- The coordinator calls
  `AgentAuthService.reconcile_native_auth_snapshot(snapshot) -> None`.
  Only the existing native-auth fields are mirrored to live compatibility/raw
  config consumers: Controller, handlers, agents, Claude control client and
  Codex config aliases. Update existing objects/fields so shared aliases stay
  coherent; do not register agents, call native auth or HTTP, launch processes,
  release turns, restart services, or change unrelated runtime configuration.
- After a successful V2 takeover save, mirror before the first possible
  exposure. On exposed recovery mirror even when the Hub payload is unchanged.
  Success and terminal completion mirror before writing a completed receipt;
  reversal mirrors the restored persistent direction before forgetting its
  journal. A mirror failure retains a recoverable pending transaction and closed
  admission. Exposure remains forward-only; a pre-exposure failure may use the
  existing reversal path. Empty-only/native-retained state is not blindly cleared.

Standalone fixture services may omit the callback because they have no live
Controller cache. Production cannot acquire the migration guard without its
Controller owner. This is a memory-consumer correction inside the existing
lifecycle, not another journal, marker, lock, phase or public API field.

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

`contract_version` 21 must coexist in all registered version locations on the same tested head:

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

`scripts/check_model_hub_authorities.py`, run by
`tests/test_model_hub_config.py::test_model_hub_authority_closure_is_generated_from_live_files`,
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

The five-value Source protocol closure includes `source.schema.json`, `source-create.schema.json`,
and the storage/admission constants. The model-only closure is the four-value
`source-probe-result.schema.json` protocol and its `MODEL_PROTOCOLS` consumers; search is not
admitted by observation, probes, discovery, auto-detection, provider overlays, or hop schemas.

After implementation lands, these contracts are read-only for downstream lanes. An
implementation-discovered mismatch is reported to the orchestrator for a targeted
revision; the discovering lane does not reinterpret or edit the contract in place.

## Files

| File | Authority and consumer role |
| --- | --- |
| `source.schema.json` | Source identity, channel, five protocols, state, usage, inventory, credential reference, and audit metadata. |
| `source-create.schema.json` | API-key Source creation request, transient credential boundary, optional single-protocol constraint, and lost-response correlation. |
| `agent-supply.schema.json` | Backend mode, default Source membership/order and sparse manual intent, configuration eligibility, model-supply and backend-health projections. |
| `backend-model.schema.json` | Backend Agent model identity, editable capability metadata, and server-owned lock/routeability projection. |
| `agent-chain.schema.json` | Effective route, manual override and origin projection plus current execution position, runnability, blockers, live connection backoff, retry metadata, and model supply state. |
| `probe-result.schema.json` | Saved recovery probes and route probes over the shared effective plan, including the live connection-backoff reason without persistent network health. |
| `source-probe-result.schema.json` | Explicit API-key Source/model test, without Agent routing, fallback or provider-wide health mutation. |
| `observation-result.schema.json` | Non-persisting Add-time connectivity, authentication, protocol-establishment, and inventory observation. |
| `turn-provenance.schema.json` | Exactly attributed turn attempts and terminal outcome; no policy or mapping discriminator. The one versioned object persisted to disk, so it accepts every released version. |
| `usage-summary.schema.json` | Metered token usage over a trailing local-day window, aggregated from proxied turns, with its list-API-price valuation. A report only: no consumer may feed it back into resolution, admission, or cooldown. |
| `quota-summary.schema.json` | Rate-limit windows of hub-held subscriptions, parsed from each vendor's own usage report, and the API-price value of each subscription's metered usage against its monthly fee. A report only; carries no grant or raw body. |
| `hosted-tools-status.schema.json` | The Tools read: each hosted tool's switch, its passive status (latest success, refusals, per-pair successes), the search executor order (saved, effective, candidates with their unit rates) and the image models with their default. A report only; hop resolution reads config and evidence itself. |
| `resolution-event.schema.json` | Pull-feed Source/resolution records and their closed reason/detail vocabulary. |
| `oauth-flow.schema.json` | Subscription creation and re-auth presentation without secret material. |
| `migration-scan.schema.json` | Server-owned custody-takeover scan of existing native CLI/provider configuration; not an internal contract migration. |
| `runtime-dependency.schema.json` | Managed local Gateway asset, persisted enablement intent, lifecycle, and health. |
| `guard-refusal.schema.json` | Shared guarded-mutation refusal whose two arrays are the exact plan echoed by a confirmed retry. |
| `api.md` | Routes, envelopes, default Source order and manual Route writes/Restore/preview, guards, OAuth/import results, provenance, usage, and runtime status. |
| `api-response.schema.json` | Machine-readable response contract and real-response exercise for every route in `api.md`, at exact route-table parity. |
| `opencode-overlay.md` | Stable OpenCode provider/model identifiers and effective-hop overlay behavior. |
| `adapter-interface.py` | Adapter protocol, observation, credential, discovery, invocation, cleanup, and classification boundary. |
| `mirror-registry.json` | Executable authority/mirror registry and terminal contract version. |
| `vibey-consumer.md` | C-6 launch projection, capability authority, served-hop response headers, and delivery boundaries. |
| `google-protocol.md` | Google API/config admission, native gateway paths, discovery, stream facts, and pinned-engine translation boundary. |
| `vibey-boundary-matrix.json` | Executable Vibey backend/channel audit: all declared schema shapes, runtime admission owners, and Hub-only refusal cases. |
| `hop-resolution.schema.json` | Ephemeral in-process Vibey consumer result, including gateway-only credentials, nullable capabilities, and each enabled hosted tool's mode, declaration, bound executor, and replay authorization. |
| `hop-origin.schema.json` | Non-secret upstream origin persisted on the provenance attempt, beside its `source_id`. |
| `served-hop.schema.json` | Exact `x-avibe-served-hop` response header value: the admitted origin plus the serving Source id. |
| `capability-evidence.schema.json` | One persisted (Source, model, feature, status) evidence record, observed from real traffic only: a refusal (routing state, no time limit) or a success (display history, with its mode), with the wire stamp and the Source identity it follows. |
| `provider-overlay.schema.json` | The `providers` section of `vibe/data/backend_models.json`: per-vendor model families with wire efforts and hosted-tool declarations, includes, and list prices. |
| `README.md` | This ownership, version-closure, and contract-index document. |
