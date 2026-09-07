# Model Hub — REST API contract

Status: **Normative, `contract_version` 10** — Model Hub implementations must conform, and the response conformance guard enumerates this route table and validates one real server response for every route.

Success envelope: `{ok: true, contract_version: 10, ...}`.
Failure envelope:
`{ok: false, contract_version: 10, error: <machine_code>, detail?: <i18n_key>}`.
`detail` is always a string. Structured error data lives in a named sibling.
Guarded mutation refusals specialize that envelope through
`guard-refusal.schema.json`; both report arrays are required and together form the plan
that a confirmed retry echoes unchanged.
Authentication and CSRF rules are the existing UI-server rules.

`api-response.schema.json` is the machine-readable response registry for this route
table. The contract guard requires the two endpoint sets to be identical, requires an
exercised HTTP response for every registry entry, and validates that response against
the route's named schema.

The shared envelope and every versioned nested contract carry `contract_version` 10, the
terminal value. Supported persisted config shapes and historical TurnProvenance
remain readable; ephemeral envelopes use only the terminal version.

## Route table

| Method and path | Request → response | Normative notes |
| --- | --- | --- |
| GET `/api/models/sources` | → `{sources: Source[]}` | Unordered asset inventory. Every Source carries server-derived `adopted_by` and any persisted `client_nonce`; array order is never a spend order. |
| POST `/api/models/sources/observe` | `{vendor, base_url?, key, protocol?}` → `{observation: SourceObservation}` | Non-persisting connectivity/authentication/protocol/inventory observation. On `custom`, omitted `protocol` auto-detects and still requires matching response proof. A shipped vendor catalog pin collapses omission to that one protocol. A supplied value restricts observation to one interface and is established when authentication succeeds and either `vendor` has a shipped catalog pin, the client declared the protocol on `custom`, or a matching protocol-shaped response proves it. No credential reference is returned. |
| POST `/api/models/sources` | `source-create.schema.json` → `{source: Source, added_to: AddedTo[], adopted_by: AdoptedBy[]}` | The server assigns `id` and `created_at`; plaintext keys are transient. Default placement is committed before effective adoption is projected; manual overrides are unchanged. Optional `accept_unavailable_inventory` is the sole explicit consent for a repeated observation that established a protocol owner but whose inventory discovery fails. An optional `client_nonce` is reserved only in process before work and persisted only on the committed Source for list-based lost-response reconciliation. |
| PATCH `/api/models/sources/<id>` | `{display_name?, base_url?, force?: boolean, would_remove_hops?: RouteHopRef[], would_interrupt?: SupplyGap[]}` → guarded Source-mutation envelope | Metadata/Base-URL mutation from the authoritative matrix in `model-hub.md` §4.5. A forced retry confirms only an exact echo of the refusal plan. |
| PUT `/api/models/sources/<id>/credential` | `{key, force?: boolean, would_remove_hops?: RouteHopRef[], would_interrupt?: SupplyGap[]}` → guarded Source-mutation envelope | API-key replacement. Confirmation fields are JSON body fields. Success is exactly `{source, removed_hops, interrupted}`; the OAuth-only repair tail never appears here. |
| POST `/api/models/sources/<id>/reauth` | `{acknowledge_irreversible?: true}` → `{flow: OAuthFlow}` | Both Hub OAuth and `native_cli` Sources require the acknowledgement before OAuth starts. Missing or false acknowledgement returns `reauth_confirmation_required` before any adapter call. See repair rules. |
| DELETE `/api/models/sources/<id>?force=<bool>` | `{would_remove_hops?: RouteHopRef[], would_interrupt?: SupplyGap[]}` → guarded `409` or `{removed_hops, interrupted}` | A confirmed delete removes the Source from every backend Source order and Route chain in one transaction. A nonempty destructive plan commits only when the body exactly echoes the current refusal plan. |
| POST `/api/models/sources/<id>/refresh` | `{force?: boolean, would_remove_hops?: RouteHopRef[], would_interrupt?: SupplyGap[]}` → guarded Source-mutation envelope | The sole saved connectivity/discovery/recovery mutation. |
| POST `/api/models/sources/<source_id>/models` | `{model_id, display_name?, reasoning_efforts}` → `{source: Source}` | Creates one user-authored model entry. The Source identity comes only from the path. |
| PATCH `/api/models/sources/<source_id>/models/<model_id>` | `{reasoning_efforts}` → `{source: Source}` | Replaces the complete capability list only when `reasoning_efforts_source` is `user` or null, without changing identity, origin, or Routes. An `upstream` or `catalog` declaration returns HTTP 409 `source_model_tiers_managed` with its provenance in the `reasoning_efforts_source` sibling. |
| DELETE `/api/models/sources/<source_id>/models/<model_id>` | `{force?: boolean, would_remove_hops?: RouteHopRef[], would_interrupt?: SupplyGap[]}` → guarded `409` or Source-mutation success | Deletes a manual entry; for a discovered entry it persists `retired: true` without deleting the row. Both outcomes use the same exact-hop and supply guards. |
| GET `/api/models/agents` | → `{agents: AgentSupply[]}` | Backend records include server-authoritative `cli_present` and `named_agents`, the enabled named-Agent live projection. The default read returns the current presence snapshot; `?refresh_cli_presence=1` first refreshes deep npm-only discovery and returns the resulting snapshot. |
| GET `/api/models/agents/<backend>/sources` | → `{agent: AgentSupply}` | Returns the authoritative effective order and eligibility. |
| PUT `/api/models/agents/<backend>/sources` | `{order: string[], force?: boolean, would_remove_hops?: RouteHopRef[], would_interrupt?: SupplyGap[]}` → guarded `409` or `{agent: AgentSupply}` | Atomically replaces default membership/order, preserves manual overrides, and guards effective hop removal or protected-supply loss. Pure reordering needs no guard. |
| POST `/api/models/agents/<backend>/chains/reorder` | `{order?: string[], force?: boolean, would_remove_hops?: RouteHopRef[], would_interrupt?: SupplyGap[]}` → guarded `409` or `{agent: AgentSupply}` | Compatibility default-order entry point with the same guards as Sources PUT. Never reorders manual arrays. Without order, returns the current projection. New UI uses Sources PUT. |
| PATCH `/api/models/agents/<backend>/mode` | `{mode}` → `{agent: AgentSupply}` | Explicit `hub` / `direct` switch. A qualifying Direct → Gateway switch atomically adopts the recognized CLI login as the first native Source; other switches create nothing. |
| GET `/api/models/agents/<backend>/models` | → `{agent: {backend, mode, catalog_models}}` | Picker-safe catalog read. It exposes no Source order, Route, or credential-bearing supplier data. Every OpenCode row carries required `native_protocol`; Claude and Codex rows omit it. |
| GET `/api/models/agents/<backend>/models/candidates` | → `{candidates: {builtin: Candidate[], providers: Candidate[], in_list: Candidate[]}}` | Server-owned picker projection. It returns addable built-ins, deduplicated ordered-provider inventory, and every current menu row with the same exact supplier projection; it is independent of backend mode and contains no credentials. Every OpenCode Candidate carries server-derived `native_protocol`; other backends omit it. Only `in_list` candidates may carry optional `group_if_removed: "builtin" | "providers" | null`, naming the group where that id would be offered after removal. |
| PUT `/api/models/agents/<backend>/models` | `{baseline: BackendModel[], models: BackendModel[], expected_suppliers?: {<id>: [{source_id, model_id}]}, force?: boolean, would_remove_hops?: RouteHopRef[], would_interrupt?: SupplyGap[]}` → guarded `409`, stale-candidate `409`, `{agent: AgentSupply}`, or `{agent: AgentSupply, removed_hops: RouteHopRef[], interrupted: SupplyGap[]}` | Applies one full-list backend catalog edit with optimistic merge. OpenCode rows require a literal `native_protocol: openai_responses | anthropic`; rows for other backends forbid it. Each caller addition still absent from the latest list starts automatic without a route key. A listed supplier-projection mismatch refuses atomically with the separate exact shape `{ok: false, contract_version, error: "candidate_suppliers_changed", detail, changed}`; a concurrently added row keeps its existing Route. A routeful removal uses the exact echoed-plan guard and then atomically removes its Route; empty-route removal is ordinary success. Supplier inventory remains unchanged. |
| GET `/api/models/catalog/models-dev?query=<text>` | → `{matches: ModelsDevMatch[]}` | Read-only metadata lookup through the server-owned conditional cache. Results keep the shipped provider fields, deduplicate aggregator copies by model id, rank first-party matches first, add `first_party`, derive `native_protocol` from the model id's last path segment through the vendor map, cap results at 8, and never persist automatically. |
| GET `/api/models/agents/<backend>/chains` | → `{chains: AgentChain[]}` | Hub only. Returns the complete Overview model set in menu order, followed by a selected model or configured Route not already present. All members share one config snapshot and observation time. Direct returns the documented `direct_mode` error. |
| GET `/api/models/agents/<backend>/chain?model=<id>` | → `{chain: AgentChain}` | Hub only. Direct returns the documented `direct_mode` error. |
| PUT `/api/models/agents/<backend>/chain?model=<id>` | `{hops: RouteHop[], force?: boolean, would_remove_hops?: RouteHopRef[], would_interrupt?: SupplyGap[]}` → guarded `409` or `{chain, removed_hops, interrupted}` | Nonempty hops save an exact manual override, including equal-to-automatic arrays. Empty hops remain accepted and use DELETE Restore semantics with identical exact effective-removal/supply guards. New pairs validate canonical ids, Source eligibility/existence and retirement; API-key pairs do not require inventory membership, while subscriptions retain existing membership admission/stale-hop retention. Only nonempty replacement uses the protected-supply-only `mutation.route_replace` guard. |
| DELETE `/api/models/agents/<backend>/chain?model=<id>` | optional `{force?: boolean, would_remove_hops?: RouteHopRef[], would_interrupt?: SupplyGap[]}` → guarded `409` or `{chain, removed_hops, interrupted}` | `mutation.route_restore` removes the key and recomputes defaults; absent key is idempotent. Guard actual effective removals and supply loss. |
| POST `/api/models/agents/<backend>/chain/preview?model=<id>` | `{manual_override: null \| {hops: RouteHop[]}}` → `{chain: AgentChain}` | Accepts empty hops and normalizes them to null before the shared planner evaluates isolated draft config. Output manual_override is nonempty or null; effective chain/origin follows inheritance. No persistence, events, engine startup/sync, credentials or egress, including when runtime is stopped. |
| POST `/api/models/agents/<backend>/probe` | `{model?}` → `{probe: ProbeResult}` | Hub only. Direct returns the same `direct_mode` error. |
| GET `/api/models/agents/<backend>/provenance?model=<id>` | → `{provenance: TurnProvenance \| null}` | On-demand read of the most recently persisted retained record for this exact backend and canonical catalog model, regardless of outcome. Validates backend/model; absent history is null. Uses only the existing bounded store and never starts or syncs the engine. No history field is added to AgentChain. |
| GET `/api/models/events?limit=<n>&before=<id>` | → `{events: ResolutionEvent[]}` | Bounded source-resolution feed. |
| GET `/api/models/usage?days=<n>` | → `{usage: UsageSummary}` | Bounded metered token report over a trailing local-day window. `days` is clamped to the retained window; a Source with no metered call is absent rather than reported as zero. |
| POST `/api/models/oauth/start` | `{vendor, channel, client_nonce?}` → `{flow: OAuthFlow}` | Starts creation of a new subscription source. Before provider work, the optional exact `(client_nonce, vendor, channel)` tuple is atomically claimed; concurrent retries coalesce to its one pending start and terminal result. |
| GET `/api/models/oauth/status/<flow_id>` | → OAuth result | Terminal create and reauth shapes are below. |
| POST `/api/models/oauth/submit` | `{flow_id, value}` → OAuth result | Same terminal shape as status. |
| POST `/api/models/oauth/cancel` | `{flow_id}` → `{ok}` | Cancels provider work. A committed flow with `client_nonce` remains the same bounded terminal `OAuthFlow` with `state: "cancelled"` until its existing `expires_at`; a flow without a nonce is forgotten. |
| POST `/api/models/migration/scan` | → `{scan: MigrationScan}` | Read-only. |
| POST `/api/models/migration/apply` | `{item_ids: string[]}` → `{applied, sources, added_to}` | Each accepted import runs the same default placement and effective adoption as Add Source; original files remain byte-identical. |
| GET `/api/models/turns/<turn_id>/provenance` | → `{provenance: TurnProvenance}` or documented absence error | Debug read for exactly attributed Hub turns. |
| GET `/api/models/runtime/status` | → `{runtime: RuntimeDependency}` | Read-only managed engine status. The nested object carries `contract_version` 10 and persisted user intent in `enabled`; `not_started` is installed lazy-start idleness, not an alarm. |
| POST `/api/models/runtime/install` | → `{runtime: RuntimeDependency}` | Idempotently starts server-owned installation. It returns and persists `installing`; reload reads the same state. Uses the existing mutation authentication and CSRF guards. |
| POST `/api/models/runtime/start` | → `{runtime: RuntimeDependency}` | Persists `enabled: true` and explicitly starts the managed engine. Service startup restores this intent. Uses the existing mutation authentication and CSRF guards; status reads never start it. |
| POST `/api/models/runtime/stop` | → `{runtime: RuntimeDependency}` | Explicitly stops the managed engine, persists `enabled: false`, and returns it to `not_started`. The mutation is rejected with `runtime_in_use` while any Agent backend is configured for Hub mode, so disabling the shared runtime cannot strand a configured route. |

The removed product-global route `PUT /api/models/priority` has no replacement. Sources
PUT and compatibility chains/reorder share backend-default semantics and effective guards,
preserving all manual arrays. Exact manual order is saved through the chain resource;
DELETE restores automatic and POST preview evaluates the draft without a write.

## Unsaved Source observation

`POST /api/models/sources/observe` accepts only the observation subset of a Source
create request:

```json
{
  "vendor": "custom",
  "base_url": "https://relay.example/v1",
  "key": "<transient plaintext key>",
  "protocol": "openai_chat"
}
```

`base_url` may be null for an official vendor endpoint. `protocol`, when present,
restricts observation to exactly that interface; omitting it selects the shipped vendor
pin when one exists, otherwise on `custom` it probes the authoritative three-value
order. Auto-detect on `custom` still requires a matching protocol-shaped upstream
response. A supplied protocol is established when authentication succeeds and either
`vendor` has a shipped catalog pin, the client declared that protocol on `custom`, or
a matching protocol-shaped response proves it. The protocol probe is deliberately
schema-invalid and names no synthetic model, so a relay can authenticate and classify it
without selecting or invoking an upstream model. Schema validation alone does not
prove authentication: the model-free protocol path must reject a control credential,
or the existing model-list path must reject the control and accept the candidate.
Public inventory and `accept_unavailable_inventory` never supply that proof.
A bare-origin Base URL uses the
standard `/v1` endpoint paths, while a URL with a path is treated as the complete API
root. The endpoint provisions an unbound engine credential only for this operation,
returns `observation-result.schema.json`, then revokes the transient reference before
settling. A revoke failure remains in the existing pending-revocation journal. The
response never contains that reference or any persisted Source.

For an API-key `POST /api/models/sources`, the server performs the same
server-owned observation internally before its independent committed credential
provisioning. It accepts the create fields and optional protocol constraint, but never
accepts protocol proof or inventory results from the caller. A null protocol
produces no Source. A repeated observation that establishes a protocol owner but ends
with failed inventory discovery produces no Source unless this request explicitly carries
`accept_unavailable_inventory: true`; the accepted Source has `models: []` and the
existing uncertain health projection. Subscription OAuth creation follows its
vendor-specific observation flow before commit. Saved Sources use the stored protocol for
every later operation.

`source-create.schema.json` is the complete `SourceCreate` request. Its field table is
authoritative; no Source response field may be inferred backwards into the request:

| Field | Required | Producer → consumer | Rule |
| --- | --- | --- | --- |
| `vendor` | yes | Add Source client → observation adapter | Same normalized vendor id as the unsaved observation request. |
| `display_name` | no | Add Source client → Source metadata | Omission uses the server's canonical locale-neutral vendor label; it never carries credential material. |
| `base_url` | no | Add Source client → observation adapter | null or omission selects the official endpoint; a custom URL is validated before provisioning. |
| `key` | yes | Add Source client → transient and committed credential provisioning | Plaintext is write-only and never appears in a response, Source record, event, or log. |
| `protocol` | no | Add Source client → observation adapter | One supported interface. It restricts the probe to that type. Omission auto-detects only on `custom`; a shipped vendor catalog pin collapses omission to the pinned protocol. Persistence requires authentication plus a shipped catalog pin, a `custom` declaration, or matching response proof. |
| `client_nonce` | no | Add Source client → process-local create reservation and persisted Source read projection | Client-generated before send and atomically reserved in the live process before observation or credential work; only the successful commit persists and echoes it unchanged so an ordinary list read can reconcile a lost response. |
| `accept_unavailable_inventory` | no | Add Source state ⑤ client → Source-create commit gate | Boolean; omission is `false`. It consents only to the server's repeated observation returning an established protocol owner with `discovery: failed`; it never supplies or overrides observation evidence. |

The request has no `id`, `created_at`, `state`, `usage`, protocol evidence, discovered-model,
credential-ref, billing, or supply-channel field. The server assigns or observes all of
them. A successfully observed empty inventory is represented by the returned
`source.models: []`; the client never submits a discovered inventory as authority.

### Source-create unavailable-inventory consent

`POST /api/models/sources` always repeats response-backed observation; it never trusts the
earlier unsaved result that led the client to this request. The boolean is inert outside
the one inventory-failure cell, so a stale state ⑤ decision cannot weaken any other
create precondition:

| Repeated server observation | `accept_unavailable_inventory` | Server result |
| --- | --- | --- |
| Protocol established; `discovery: succeeded` | omitted, `false`, or `true` | Ordinary create from the newly observed inventory; a legitimately empty result may commit as `models: []`. |
| Protocol established; `discovery: failed` | omitted or `false` | Existing classified `discovery_failed`; no Source or committed credential is written, and AC-26 cleanup settles before return. |
| Protocol established; `discovery: failed` | `true` | Commit exactly one Source with the established protocol, `models: []`, and the existing uncertain health projection; matching has no inventory candidates. |
| Protocol not established, or an earlier reachability/authentication failure | omitted, `false`, or `true` | Existing classified failure; no Source is committed. The flag cannot authorize this result. |

When `client_nonce` is present, the server atomically reserves it in the live process
before observation, transient-ref creation, or committed credential provisioning. The
reservation is not a Source row and has no durable representation. Only successful
commit persists the unchanged value as `Source.client_nonce`; neither representation
stores a request digest, terminal envelope, or plaintext credential. After a lost
response, the client first reads
`GET /api/models/sources`: an exact nonce match reconciles the committed Source, while a
miss permits a same-nonce retry. A retry that races unfinished work receives an
in-progress conflict and waits; a retry that races a newly committed Source receives a
committed conflict and repeats the list read. The server applies this total state/action
table:

| Decision | Live state at retry | Retry relation | Server action and HTTP/API result | Upstream work |
| --- | --- | --- | --- | --- |
| `nonce.in_flight` | this process holds the nonce reservation for unfinished create work | same nonce with any otherwise-valid request | retain the reservation; HTTP 409 `source_create_in_progress` | none |
| `nonce.released` | no live-process reservation and no live Source owns the nonce, including after process restart | same nonce with any otherwise-valid request | atomically reserve the nonce in process and run a fresh create | exactly one new attempt under the reservation |
| `nonce.committed` | one live Source carries the nonce | same nonce with any otherwise-valid request | HTTP 409 `source_nonce_conflict`; client reads the ordinary Source list and finds the exact nonce | none |

The successful Source commit atomically removes the process reservation while writing the
visible `Source.client_nonce`; there is no live-process overlap or unclaimed interval.
Pre-commit failure or cancellation releases the reservation only after every transient or
uncommitted credential ref has been revoked or entered the durable pending-revocation
journal under AC-26. Process termination ends both the in-flight work and its process-local
reservation; reconstruction reconciles any durable pending-revocation entry before a
same-nonce retry begins as a fresh attempt. No ownerless claim is reconstructed.
Uniqueness covers only live-process reservations and live Sources. Source deletion releases its nonce; after the required
list read observes no matching live Source, a later same-nonce request is definitionally
a fresh create and may create one new Source. Protecting a deleted Source from a stale
nonconforming caller is outside this single-user client's D-36 recovery protocol. No
receipt, digest, terminal-envelope snapshot, released-state record, or separate
reconciliation endpoint exists.

Cancellation has one commit boundary. Before the durable Source commit, AC-26 applies:
the transient or uncommitted ref is revoked, or is named by the durable pending-
revocation journal, before cancellation settles. After that commit, cancellation ends
only the caller's wait. The transaction completes normally, the Source and all accepted
placements remain committed, and the next Source/Agent read returns a coherent state
even if no client received the create response. There is no server-side abort path after
the commit point.

The observation result has six terminal outcomes: `observed`, `ambiguous`,
`unreachable`, `authentication_failed`, `adapter_error`, and `timeout`. Its
`protocol` is non-null only when authentication succeeds and one rung establishes the
transport contract for the attempted path: a shipped vendor catalog pin, an explicit
`custom` declaration, or a matching upstream response shape. Authentication is accepted only by a
shaped success or a shaped request-level error that occurs after authentication;
shaped authentication errors are rejected. Shaped server and rate-limit errors
prove reachability but not authentication, so they settle as `adapter_error` with
`reachable: true`, `authenticated: unknown`, and `protocol: null`. A local adapter
failure may use the same outcome with `reachable: null`. A bare HTTP status proves
reachability, but proves neither protocol nor authentication. Consequently,
`ambiguous` always has `reachable: true` and `protocol: null`.
`authentication_failed` also has `protocol: null`, because a rejected credential
does not establish a persistable protocol. After ambiguous Auto detection, the client
must select one concrete protocol before retrying. Only `observed` attempts inventory discovery and
therefore reports `succeeded` or `failed`; every other terminal reports
`not_attempted` with an empty model list and empty metadata list. On successful
discovery, `models` remains the compatibility list of unique ids and `model_metadata`
contains one record for each id in the same order. v1 retains only the OpenRouter-shape
`supported_parameters` array. An absent or malformed member becomes null without losing
the model id; an empty array remains an explicit valid value.

## Source-model reasoning tier provenance

Each Source model carries `reasoning_efforts_source`, the first applicable rung in this
ordered ladder: recognizable live `supported_parameters` metadata (`upstream`), an exact
row in the bundled backend catalog (`catalog`), a non-empty user declaration (`user`),
or null with an empty undeclared list. Live metadata is recognizable only when a valid
array contains `reasoning` or `reasoning_effort`; it applies the protocol-family default
from the backend catalog module. Catalog backfill applies that model row's exact tier
list verbatim. It never substitutes a family default or filters the row through the
shared ordered vocabulary.

Refresh re-applies the first two rungs and otherwise preserves user declarations. OAuth
materialization and native-config import use the same ladder at creation time. A v8 API
payload always includes the field, while the persisted-config loader accepts older rows
that omit it: a non-empty list becomes `user` and an empty list becomes null. The first
post-upgrade refresh performs any catalog backfill; there is no offline tier migration.

Managed `upstream` and `catalog` declarations are read-only. User and null declarations
remain editable, including free-form values. A successful refresh that replaces a
non-empty user list with a managed declaration emits exactly one redacted
`reasoning_efforts_override` event after commit.

## Identifier rules

- `Source.id` and every non-null source reference match
  `^src_[a-z0-9]{8,}$`.
- The API boundary also verifies referential existence where a new reference is
  emitted or accepted. JSON Schema can validate format, not cross-document existence.
- Claude and Codex requests use fixed-menu built-in ids.
- OpenCode requests use the bare canonical menu id (spec §4.8 v4). The overlay provider
  is a function of the row's `native_protocol`; the resolver sends the exact effective hop's
  model id upstream.
- Add-time matching may suggest a sanctioned alias or mapping, but it materializes the
  accepted result as an exact `{source_id, model_id}` hop before commit. Runtime neither
  resolves aliases nor substitutes a model.
- Source event references are checked when emitted. Retained feed entries remain
  valid after a later legal source deletion.

## K4 v5 field registry

These rows are authoritative for the new payload members only. They do not classify
every K4 route change as read-only or compatibility-neutral. G-3's discovered-model
DELETE behavior and G-14's mode-switch transaction are separately specified pre-release
route corrections. The owner-approved 2026-08-11 19:44–19:56 pre-release correction
also removes persistent network/timeout cooldown causes in favor of live `backoff`;
every other existing field and enum meaning remains unchanged.

| Field | Producer | First consumer | Invariant |
| --- | --- | --- | --- |
| `Source.client_nonce` | Source create commit | lost-create reconciliation | Exact optional echo of `SourceCreate.client_nonce`, written only at commit; unique across live Sources and live-process reservations, and never used for routing. |
| `Source.models[].retired` | discovered-model DELETE | Source detail inventory | Omission means false; only a discovered row may be true; true rows remain readable and never supply. |
| `Source.adopted_by` | Source read assembler | Source cards and Source detail status | Complete unique persisted-reference projection for backends currently in Hub mode, sorted by backend then menu model; clients do not derive it from `hops`. Routes retained by Direct-mode backends are excluded because they bypass the gateway. |
| `AgentSupply.cli_present` | backend CLI detector | zero-installed-backend state | Boolean installation fact only; it does not imply login or process readiness. |
| `AgentSupply.model_supply[].has_runnable_hop` | exact-chain live annotator | backend-group collapse predicate | Uses the AgentChain runnability axiom rather than inferring liveness from configured membership. `chain_length: 0` forces false; a nonzero length may carry either value. |
| `RuntimeDependency.enabled` | explicit runtime start/stop mutation | Gateway switch and service-start recovery | Persisted user intent, independent of observed process health. Missing in older config defaults to false; an older response without the field falls back to observed health in the UI. |
| `RuntimeDependency.host_platform` | server host detector | unsupported-host runtime pill | Names the Avibe host, not the browser; exact membership in `manifest.assets[].platform` decides install support. |
| `RuntimeDependency.status.error_key` | runtime installer | install-failed runtime state | Closed persisted i18n key: `settings.models.install.fail.detail` after installation fails; null after a new attempt begins and in every non-failure state. |
| `RouteHopRef.position` | guarded mutation planner | guarded-change hop row | One-based position in the named Route before the attempted mutation. |
| `OAuthStart.client_nonce` | OAuth client before send | OAuth start idempotency | Optional client-generated correlation; the server claims its exact tuple with vendor and channel before provider work, coalesces an in-flight retry, releases after failure or task cancellation before a flow exists, and converts success atomically to the flow. Explicitly canceling that committed flow retains its terminal `cancelled` correlation until the existing expiry. |
| `OAuthFlow.client_nonce` | OAuth start echo | lost-start reconciliation | When the request supplied the nonce, every flow response echoes it unchanged and carries a non-null date-time `expires_at`; flows without a nonce retain the existing nullable expiry branch. |
| `AgentChain.chain[].health: backoff` | configured-chain live annotator | chain/AgentSupply health reads | Source-scoped in-memory connection throttle before the first user-visible model-output byte, with `retry_at` strictly later than the assembler's captured read time, never persisted in Source/config. It overlays only an otherwise healthy hop whose exact Source/model capability is present. Cooldown, durable Source health, missing Source, or unsupported model suppresses it and keeps the stronger blocker's established projection; simultaneous native-process unavailability is the sole exception and takes the reason slot without erasing the deadline. Before serialization, an expired overlay is normalized to the underlying non-backoff facts. Ordinary backoff rolls up `waiting`; every durable/capability/process blocker rolls up `interrupted`. |
| `TurnOutcomeProjectionInput.source_transition_persisted` | streamed-attempt settlement | backend terminal-copy projection | Optional internal fact present only for `turn.streamed_fallback`; true means the Source transition committed, while false preserves attempt history but forbids switch/current-change claims and selects the generic interrupted copy. UI consumers deliberately do not consume the member. |

## AgentSupply read projection

Each backend entry on `GET /api/models/agents` carries the v5 API-boundary keys:

```json
{
  "backend": "claude",
  "cli_present": true,
  "mode": "hub",
  "selected_by_agent": "pm",
  "selected_model_id": "claude-opus-4-6",
  "selected_model_explicit": true,
  "sources": {
    "order": ["src_claudepro1", "src_anthkey01"],
    "eligibility": [
      {
        "source_id": "src_claudepro1",
        "eligible": true,
        "reason_key": null,
        "in_current_model_chain": true,
        "process_availability_reason": "native_cli_unavailable"
      },
      {
        "source_id": "src_anthkey01",
        "eligible": true,
        "reason_key": null,
        "in_current_model_chain": true,
        "process_availability_reason": null
      },
      {
        "source_id": "src_otherkey1",
        "eligible": true,
        "reason_key": null,
        "in_current_model_chain": false,
        "process_availability_reason": null
      }
    ]
  },
  "supply_status": "degraded",
  "model_supply": [
    {"model_id": "claude-opus-4-6", "chain_length": 2, "has_runnable_hop": true}
  ],
  "named_agents": [
    {
      "name": "pm",
      "effective_model_id": "claude-opus-4-6",
      "supply_status": "degraded",
      "route_reason": null
    },
    {
      "name": "reviewer",
      "effective_model_id": "claude-haiku-4-5",
      "supply_status": "interrupted",
      "route_reason": "route_unconfigured"
    }
  ]
}
```

`named_agents` lists every enabled named Vibe Agent whose backend matches this
record. `effective_model_id` is the Agent's explicit model. `supply_status` is
derived independently for that effective model. `route_reason` distinguishes a
missing route from a configured route whose Sources are unavailable. The
list name and object shape are intentionally distinct from `SupplyGap.agents`,
which is a list of bare names inside a mutation result.

`selected_model_explicit` is TRUE iff `selected_model_id` originates from the
user's explicit configuration request. FALSE means no explicit model selection,
including a resolver-picked value or no selected model.

AgentSupply does not project a backend-level serving head. Chain and probe responses
carry the effective upstream model and source for the named model request; the
Agents payload stays at per-named-Agent selection and supply-status grain.

Disabled Agents are absent. In Direct mode each named Agent may still have an
effective model, but its Hub `supply_status` is null.

Every backend record remains present even when its executable is absent.
`cli_present` is server-authoritative, and the zero-installed-backend state is exactly
the payload where every record carries false. Login recognition is a separate fact used
only by the Direct → Gateway adoption transaction; neither login nor process readiness
may be inferred from `cli_present`.

Each `model_supply` row carries both configured `chain_length` and live
`has_runnable_hop`. A nonzero length with false is an all-stale Route; zero with false is
structurally empty, and zero with true is invalid. The boolean is computed from the
complete exact chain with the same source-health AND process-availability rule as
`AgentChain`, so the page never issues a chain read per row and never treats configured
membership as live supply.

The `sources.eligibility` inventory is also the one complete source-signal
projection. Every existing source remains represented, including a source outside
the selected model's capability chain. `in_current_model_chain` is true or false
when `selected_model_id` is non-null and is null when no selection exists.
`process_availability_reason` is `native_cli_unavailable` or null independently of
membership and source-global health; a Hub source always carries null. This one
mechanism therefore serves both the Agent row and the all-sources drawer.

The rejected alternative was to embed only the current `AgentChain`: an in-order
source that does not supply the selected model is absent from that chain, so its
process-availability fact vanishes at exactly the drawer grain that still renders
the source. Evidence at master `05f72ae5`: `service.py:1843-1891` computes the
per-backend unavailable set beside the complete eligibility inventory, while
`model_supply` carries both configured `chain_length` and live
`has_runnable_hop`; it still carries no Route body or serving head.

### Honest null selection

An isolated Model Hub service that is not connected to the Vibe Agent catalog may
still have no projected selection:

```json
{
  "mode": "hub",
  "selected_by_agent": null,
  "selected_model_id": null,
  "selected_model_explicit": false,
  "supply_status": null
}
```

This means “no projected Agent selection.” Each turn still resolves against the
model carried by that request. `sources`, `model_supply`, and
`named_agents` remain present and non-null in Hub mode. Each eligibility row keeps
its process availability but carries `in_current_model_chain: null`.

## Per-backend source order

Sources PUT requires `order`; compatibility chains/reorder POST accepts optional order.
Both accept optional `force`, `would_remove_hops`, and `would_interrupt` fields.

```json
{"order": ["src_anthkey01", "src_relay9c1x"]}
```

The order is the complete desired default subset, including an empty subset. Every id
is unique, existing and configuration-eligible; invalid input returns
`invalid_source_order`. Manual routes can name other eligible Sources and are never
rewritten by either operation. Source creation/native import append eligible defaults;
refresh, health and turns do not rewrite order.

`mutation.default_sources` stages the order under the mutation lock and compares
before/after effective plans. Existing exact-plan guards cover effective hop removal
or protected-supply loss; pure reordering without removal has no guard. A confirmed
retry must echo both refusal arrays exactly. Success remains `{agent: AgentSupply}`.
An omitted POST order returns the current projection without a write.

Eligibility remains server-authoritative. Ineligible rows carry the closed reason key;
eligible rows carry null. The frontend never matches inventory or infers intent from
array equality. Default changes affect only the chosen backend.

### Direct-to-Gateway native adoption

For `PATCH /api/models/agents/<backend>/mode`, a transition from `direct` to `hub`
reuses the same sanctioned native-login recognition and response-backed observation
boundary as native import. If that backend has a recognized CLI login and has no
`native_cli` Source, the mode change, creation of the backend's singleton native Source,
default Source-order insertion commit in
one transaction. The returned `AgentSupply` is assembled after that commit and exposes
the resulting order, eligibility, sparse manual Routes, and effective supply summaries.

An existing native Source, an unrecognized or absent CLI login, or any transition other
than `direct` → `hub` creates nothing. The mode switch itself remains legal in those
cases. Repeating the request never creates a second native Source. `cli_present` alone
does not satisfy the recognition predicate.

### Manual Route configuration and preview

Owner decision `c1d398d5f` scopes unknown-model passthrough and inventory-independent
manual invocation to Hub API-key Sources. Subscriptions retain known-model admission and
automatic matching; unmatched subscription-only defaults yield an empty/null-origin plan.

`AgentSupply.routes` is the canonical sparse map of nonempty `{hops: RouteHop[]}`
overrides. Missing and valid empty values both inherit. Nonempty PUT creates a manual
key even when hops equal the generated result; empty PUT and DELETE remove it. Canonical
output omits empty map values and reports `manual_override: null` for inherited intent.
Output-only `minItems: 1` is not an input validation rule: empty PUT/preview remains
accepted, and supported raw config is strictly validated before normalization. Invalid
records never become automatic accidentally. Normal saves persist the normalized map
without discarding nonempty/stale/dormant routes, catalog identities or unrelated fields;
read/preview performs no write. Nonempty manual arrays are exact and may use an eligible
Source outside default membership. New or changed
pairs require canonical nonempty ids, Source existence/eligibility and no explicit
retirement. API-key targets do not require inventory membership; subscription targets
retain existing known-model admission and stale-hop retention. Retained stale hops remain
editable without inventing inventory.

The new-identifier length bound is not a read or mutation bound for persisted
identities. Existing normalized catalog ids and retained exact target/source-inventory
identities remain usable across PUT, preview, DELETE and model-provenance reads even
when a later admission bound is shorter. Newly introduced identifiers still pass
admission; padding aliases remain rejected, and existing source, channel, retirement
and stale-hop policies are unchanged.

Nonempty PUT uses `mutation.route_replace`: visible noninterrupting removals are ordinary
success; protected-supply interruption returns the existing exact-plan refusal. Empty
PUT and DELETE use the same `mutation.route_restore` operation: guard actual effective removals and supply loss, then
remove the key and recompute. Both return `{chain, removed_hops, interrupted}` and
preserve the existing error family and confirmation arrays. Both restore entry points
share idempotency, admission, leases, synchronization and rollback. All final-hop
removals, including Source deletion/catalog reconciliation, normalize before calculating
effective removal, interruption and transport registration.

Preview requires `manual_override`, whose value is null for automatic or an object
with the draft hops, including an accepted empty array normalized to null. It uses the
same pure planner with isolated draft configuration,
returns the existing chain success envelope, and performs no persistent mutation,
event, runtime startup/sync, credential access or upstream request. The returned
`manual_override` describes normalized draft intent, not persisted intent. Empty input
may therefore return Automatic or Passthrough; only an empty inherited plan has null
origin. Removing the final manual hop uses this Restore preview and its existing
Undo/Save/guard/Done flow; the old nonempty override stays saved until success. Preview is available
while runtime is stopped. Save responses remain authoritative, and failed/ambiguous
writes follow existing reconciliation using intent as well as effective hops.

<!-- authority-consumer: route_plan.manual route_plan.automatic route_plan.passthrough route_plan.empty -->

## Source creation outcome

`AddedTo` is:

```json
{"backend": "claude", "menu_model": "claude-opus-4-6", "source_id": "src_anthkey01", "model_id": "claude-opus-4-6", "position": 2}
```

`position` is one-based in the effective Route chain after commit. `AdoptedBy` is the
Source-card projection of effective references:

```json
{"backend": "claude", "menu_model": "claude-opus-4-6"}
```

Neither array carries a policy field. A Source with no effective adoption returns
empty arrays and remains available for explicit Route editing; there is no skipped-order
reason or “not enabled” state.

The terminal result of both ordinary API-key creation and OAuth creation is:

```json
{
  "ok": true,
  "contract_version": 10,
  "source": {
    "id": "src_anthkey01",
    "kind": "api_key",
    "client_nonce": "scn_01j5w8z7p4n6q2rt",
    "adopted_by": [
      {"backend": "claude", "menu_model": "claude-opus-4-6"}
    ]
  },
  "added_to": [
    {"backend": "claude", "menu_model": "claude-opus-4-6", "source_id": "src_anthkey01", "model_id": "claude-opus-4-6", "position": 2}
  ],
  "adopted_by": [
    {"backend": "claude", "menu_model": "claude-opus-4-6"}
  ]
}
```

The public HTTP wrapper, RPC layer, and client pass this shape through directly; no
extra `source` or `flow` nesting is added.
The top-level `adopted_by` remains the creation-result projection and is byte-equal to
`source.adopted_by` in that response. Later `GET /api/models/sources` reads carry the
same Source field recomputed from current persisted references; `client_nonce`, when
present, is the exact persisted create correlation.

## Supply guard

`RouteHopRef` is the immutable pre-mutation reference used by both
`would_remove_hops` and `removed_hops`:

```json
{
  "backend": "claude",
  "menu_model": "claude-opus-4-6",
  "source_id": "src_anthkey01",
  "model_id": "claude-opus-4-6",
  "position": 2
}
```

`position` is one-based in the named persisted Route before the attempted mutation.
It is not the entry's index in the cross-Route reporting array. Reporting order is
backend id, then menu-model id, then this pre-mutation position; a forced success
returns the same references and positions as the corresponding refusal.

`SupplyGap` is:

```json
{
  "backend": "claude",
  "model_id": "claude-opus-4-6",
  "agents": ["pm"]
}
```

`agents` is the set of enabled named Vibe Agents whose explicit model is the
menu-side `model_id`. It is present and may be empty. The planner emits
`would_interrupt` in ascending `(backend, model_id)` order and each `agents` array in
ascending stable Agent-id lexicographic order. This is the canonical JSON order used
by exact plan echo; enumeration or insertion order never changes plan identity.

The protected model set for a backend is the union of:

1. explicit models of enabled named Vibe Agents;
2. checked open-menu models;
3. every model with a retained nonempty manual Route row, including dormant routes;
   legacy empty keys normalize away without removing their catalog identities.

The guard evaluates each protected `(backend, model)` against the post-mutation
state. It counts only runnable exact hops in that model's effective Route chain, never
eligible inventory or the backend Source order by itself. A pair with no runnable hop
appears once in `would_interrupt` or `interrupted`.

Every guarded Source/inventory mutation uses the §4.5 envelope matrix and the complete
`guard-refusal.schema.json` shape. The first refusal is:

```json
{
  "ok": false,
  "contract_version": 10,
  "error": "source_last_supplier",
  "would_remove_hops": [],
  "would_interrupt": [
    {
      "backend": "claude",
      "model_id": "claude-opus-4-6",
      "agents": ["pm"]
    }
  ]
}
```

The lead error and its evidence array are inseparable: `source_in_route_chain` and
`source_model_in_route_chain` require nonempty `would_remove_hops`, while
`source_last_supplier` requires nonempty `would_interrupt`. The other array remains a
complete projection and may independently be empty or nonempty.

The shared guard planner stages the complete post-mutation Source/Route result and its
ordered guard-report arrays. On confirmation the client resends the same substantive
mutation with `force: true` and byte-for-byte equivalent JSON values for the refusal's
`would_remove_hops` and `would_interrupt` arrays. No token, digest, version receipt, or
server-side confirmation state exists.

The shared layer recomputes the current guarded-impact plan and then applies this total
decision matrix. For Source, inventory, default-membership and Restore mutations, a plan is nonempty when the staged
mutation has at least one `would_remove_hops` or `would_interrupt` item. For
nonempty `mutation.route_replace`, only a nonempty `would_interrupt` activates the plan; its
refusal also reports every submitted removal in `would_remove_hops`, while a
noninterrupting removal skips refusal and appears only in successful `removed_hops`.
Empty PUT shares `mutation.route_restore` with DELETE and cannot bypass the exact
effective-removal guard.
Recalculation, exact-array comparison, and any commit share one atomic boundary. Source DELETE carries `force` in the query and the
two echoed arrays in its JSON body; every other guarded mutation carries all confirmation
fields in the JSON body.

| Decision | `force` | Recomputed plan | Echoed refusal plan | HTTP/API result |
| --- | --- | --- | --- | --- |
| `guard_decision.unforced_no_impact` | false | empty, including visible noninterrupting nonempty-manual `route_replace` removals | absent or supplied; echo is inert | ordinary mutation success |
| `guard_decision.unforced_confirmation` | false | nonempty | absent or supplied; echo is inert | HTTP 409 `GuardRefusal` with the current plan |
| `guard_decision.forced_no_impact` | true | empty | absent, exact, or stale | ordinary mutation success; `force` and any echo are inert |
| `guard_decision.forced_confirmed` | true | nonempty | both arrays exactly equal the recomputed plan | commit once and return the row's success envelope |
| `guard_decision.forced_unconfirmed` | true | nonempty | either array absent or either array differs | HTTP 409 `GuardRefusal` with the newly recomputed plan; remove nothing |

Thus every destructive guarded impact that commits is the exact plan the user confirmed.
If a previously refused request recomputes to an empty plan, including a request carrying
an old nonempty echo, there is no destructive referent left to confirm: the ordinary path
commits without inventing a guard error or request-validation variant. Every 409 plan is
nonempty, and no refused branch removes a hop.

Success returns the exact envelope selected by the authoritative matrix and the
byte-identical `RouteHopRef` array from the accepted plan.

## Credential replacement and reauth

The commit point is the single durable write that makes the replacement source state
live. Hub OAuth has an earlier credential-irreversibility boundary when new grant
material is written under its identity-bound stable ref; that boundary does not make
the replacement source state live. Implementations may sequence adapter calls
differently only if all invariants remain true:

1. **Atomic source state.** Before the commit point, readers see the old source.
   After it, readers see the replacement credential reference, mask, discovered
   models, and derived state together.
2. **Rollback before commit, scoped to what is preservable.** API-key replacement and
   any repair that receives a distinct replacement ref stay strict: every failure
   before commit preserves the old source and revokes replacement engine material
   created by that attempt. Hub-OAuth re-auth is the deliberate exception because the
   engine binds OAuth identity to a stable ref. Failures before new grant material is
   written under that ref still preserve everything. Once the write occurs, prior
   material is unrecoverable through the ref: a later failure MUST fail the request,
   clear discovered supply, and persist `state.status: needs_action` with
   `state.detail_key: models.source.needs_action.oauth_expired`. The key means the
   convergent remedy is OAuth re-auth; this state is never silently routed.
3. **Guard before commit.** Elective API-key replacement evaluates the supply guard
   against the discovered replacement model set before the write.
4. **Per-channel truth.** Hub API-key material is engine-owned and transactional.
   Hub OAuth material is engine-owned but identity-bound to a stable ref, so replacing
   its grant is irreversible once the new material is written. Native credentials are
   CLI-owned and replacement is likewise irreversible once login starts.
5. **Server-enforced OAuth re-auth acknowledgement.** For both Hub OAuth and
   `native_cli` Sources, `POST …/reauth` requires
   `{"acknowledge_irreversible": true}`. Missing or false returns
   `reauth_confirmation_required` before any OAuth adapter call. This is unconditional
   across both supply channels and does not claim that a pre-login supply prediction
   exists. Transactional API-key repair through `PUT …/credential` remains outside
   this acknowledgement rule.
6. **Repair-result ownership.** API-key replacement returns only the standard guarded
   Source-mutation success envelope `{source, removed_hops, interrupted}`. Its repair
   effect is read from the returned `Source.state`, while `removed_hops` and
   `interrupted` report the exact Route impact. `recovered` and `interrupted_pairs`
   belong only to a terminal OAuth flow with `intent: "reauth"`; they never appear on
   `PUT …/credential`.
7. **Durable failed-revocation reconciliation.** If old engine material cannot be
   revoked after commit, the service persists a pending-revocation record before
   returning success. A reconstructed service reads the same journal and retries it.
   The source remains usable; no state is allowed where the old handle is live and no
   durable record names it. Successful reconciliation removes the record.
8. **Channel-aware OAuth retained-material partition.** A failed Hub flow reports
   one total `retained_material_disposition` from the adapter seam. `none` preserves
   the prior source strictly. `flow_source_ref` is the irreversible retained-material case:
   persist `state.status: needs_action` with
   `state.detail_key: models.source.needs_action.oauth_expired`, clear discovered
   supply, and never route silently. `orphan_ref` preserves the prior source and
   records a ref-keyed invariant-7 retry through `retained_credential_ref` and
   `EngineAdapter.cleanup_orphaned_oauth_material`: the operation returns true
   only after both auth-file deletions are confirmed and the retained ref is
   revoked; only then may the journal entry be cleared. `foreign_source_ref`
   preserves the prior source and
   schedules neither revocation nor a journal entry; the same-account refresh
   belongs to the other source's record, whose health reports at its own grain,
   while this flow retains `models.oauth.binding_failed`. `unknown` treats the flow
   source like the irreversible case but touches no refs: no handle can be named
   safely, so loud re-auth is the convergent remedy.

   A failed create flow has no prior `Source` to mark `needs_action`.
   `flow_source_ref` is durably revoked by its retained ref, `orphan_ref` uses
   the cleanup retry above, and `none` / `foreign_source_ref` / `unknown` touch
   no refs; the producer can emit both known-ref and unknown create failures
   because intent is a service concern outside the adapter seam.

   The consumer does not infer this partition by comparing refs. Hub success pins
   `flow_source_ref` and equality of `credential_ref` and
   `retained_credential_ref`. Native CLI success pins `credential_ref: null`,
   disposition `none`, and a null retained ref because CLI-owned material never
   enters the engine seam. A two-ref transactional OAuth swap remains a deliberate
   v2 non-goal: the owner accepted one additional re-auth for this rare, already
   interactive failure window; revisit only with field evidence.

API-key success:

```json
{
  "ok": true,
  "contract_version": 10,
  "source": {
    "id": "src_relay9c1x",
    "kind": "api_key",
    "state": {"status": "standby"}
  },
  "removed_hops": [],
  "interrupted": []
}
```

## Source refresh and blocked-source recovery

`POST /api/models/sources/<id>/refresh` is an explicit source-scoped operation. It may
refresh a source whose global state is `needs_action` or `error`, even though normal
turn resolution and the chain probe exclude that source as non-runnable.

On usable discovery it updates the discovered model set, clears the blocker, and
sets the source to `standby`. The success response is the guarded Source-mutation
envelope `{source, removed_hops, interrupted}`; clients render the complete updated
Source and the exact route impact from that envelope. `last_discovered_at` advances
only on this successful replacement. A classified failure updates the source-global
state, preserves the last successful model list and timestamp, and returns the normal
safe error. This route is the only refresh/recovery operation; there is no parallel
“test” or “recover” endpoint.

Discovered-model retirement is a persistent exception to replacement. DELETE on a
discovered model stages `retired: true` on that row instead of deleting it, then runs
the same exact-hop and protected-supply guards. Forced success removes only invalidated
Route references and keeps the retired inventory row. Every later refresh preserves
that row, its edited metadata, and `retired: true` whether the upstream still advertises
the id or no longer does. A retired row is excluded from add-time matching, model-
capability eligibility, new Route validation, live runnability, and invocation. There is
no automatic or refresh-driven unretire path. DELETE on a manual row retains its
existing remove-row semantics.

## OAuth completion

`POST /api/models/oauth/start` optionally accepts a client-generated `client_nonce`
matching `^ofn_[a-z0-9]{16,64}$`. The claim key is the exact
`(client_nonce, vendor, channel)` tuple; a different vendor or channel is a different key
and never resolves to another tuple's flow. Every flow produced from a nonce-bearing
request carries a non-null date-time `expires_at` from its first response through every
terminal replay; an ordinary or presentation-only flow without a nonce may retain
`expires_at: null`. Before invoking the provider, the server
atomically claims the tuple and applies this total state/action table:

| Decision | Tuple state at start | Server action and HTTP/API result | Provider starts |
| --- | --- | --- | --- |
| `oauth_nonce.released` | no claim or unexpired flow exists, including after a shared pending-start failure/task cancellation or after a retained canceled flow reaches its existing `expires_at` | atomically claim, start once, and make every coalesced caller await the same terminal result | exactly one under the new claim |
| `oauth_nonce.in_flight` | a provider start owns the claim but has not produced a flow | coalesce with that pending start and return its same terminal result; never create a parallel flow | none for the retry |
| `oauth_nonce.committed` | provider success atomically converted the claim into one unexpired `OAuthFlow`, including one explicitly canceled afterward | return that same `flow_id`, current state, and presentation; explicit cancellation returns the retained `state: "cancelled"` flow | none |

A shared provider-start failure or task cancellation before a flow exists returns the
same terminal failure to all coalesced callers and releases the claim only after cleanup
settles; the next exact-tuple retry therefore enters `oauth_nonce.released`. Provider
success converts the claim to `OAuthFlow` atomically, with no unclaimed interval. If the
user then explicitly cancels a nonce-bearing committed flow, the provider work is
canceled but that same flow remains as bounded terminal `state: "cancelled"`; a delayed
exact-tuple retry returns it without another provider start. Its existing `expires_at`
ends the reconciliation window and releases the tuple, so a later retry is a fresh
start. Canceling a flow created without a nonce forgets it because no D-36 correlation
promise exists. Every returned flow echoes the nonce. A new user action generates a new
nonce. Omitting it otherwise preserves ordinary one-action/one-start behavior.

`OAuthFlow.intent` and local materialization make the terminal shape total. A
materialization error is an error raised only after the upstream flow reached terminal
success while the service was committing the created or repaired Source. Its
`interrupted_pairs` uses the existing `SupplyGap` shape and reports persisted impact that
already happened; it never aliases the future-tense guard field `would_interrupt`.

| Decision | Flow/service condition | HTTP/API result | `interrupted_pairs` |
| --- | --- | --- | --- |
| `oauth_terminal.flow_only` | non-terminal flow, or adapter terminal `failed`/`cancelled` without local materialization error | successful `{flow}` | absent |
| `oauth_terminal.create_success` | terminal create and Source materialization succeed | `{flow, source, added_to, adopted_by}` | absent |
| `oauth_terminal.reauth_success` | terminal re-auth and existing-Source materialization succeed | `{flow, source, recovered, interrupted_pairs}` | present as the complete report and may be empty |
| `oauth_terminal.materialization_interrupted` | local terminal materialization fails after an acquisition-stage Source mutation has already produced at least one exact supply gap | standard `{ok: false, contract_version, error, detail?}` envelope plus the exact report; `flow` is absent | present and nonempty |
| `oauth_terminal.materialization_plain_error` | local terminal materialization fails before such an interruption or its exact report is empty | standard error envelope; `flow` is absent | absent, never an empty placeholder |

The same existing materialization error code may enter either error row. The server adds
`interrupted_pairs` if and only if the acquisition-stage mutation produced a nonempty
report; every other error envelope omits the field. Native re-auth `discovery_failed`
after clearing/marking the Source is the positive fixture. The same error before an
interruption, and any materialization failure whose computed report is empty, are
negative fixtures. A later Source read cannot recreate the historical report.

Status and submit return the same terminal shape:

```json
{
  "ok": true,
  "contract_version": 10,
  "flow": {
    "flow_id": "oaf_claude01",
    "client_nonce": "ofn_01j5w8z7p4n6q2rt",
    "intent": "create",
    "state": "success",
    "source_id": "src_claudepro1"
  },
  "source": {
    "id": "src_claudepro1",
    "kind": "subscription",
    "adopted_by": [
      {"backend": "claude", "menu_model": "claude-opus-4-6"}
    ]
  },
  "added_to": [
    {"backend": "claude", "menu_model": "claude-opus-4-6", "source_id": "src_claudepro1", "model_id": "claude-opus-4-6", "position": 1}
  ],
  "adopted_by": [
    {"backend": "claude", "menu_model": "claude-opus-4-6"}
  ]
}
```

`added_to` and `adopted_by` are absent for reauth because no new Source or Route
reference is created.

## Chain and probe

In Hub mode, `AgentChain.chain` is the shared effective per-model route in planner
order. `manual_override` reports a normalized nonempty manual override or null and
`route_origin` reports `automatic | manual | passthrough | null`, independently of live
health. Valid empty input inherits; an empty effective chain therefore has both null
manual_override and null origin. Live inspection annotates it: cooling, missing, model-unsupported,
source-blocked, live connection-backoff, and process-unavailable native CLI hops stay at their effective-plan
positions with live annotations.
`AgentChain.current` is either null or the exact `{source_id, model_id}` identity of
the hop that is current for the next execution. Recovery changes `current` on the next
turn without changing the effective plan or origin.
Each item carries `channel`, Source-global health or the distinct live `backoff`
overlay, process-aware `runnable`, and nullable `reason`. The complete axiom is:

`runnable = source-health-permits AND no-live-backoff AND process-available`.

Process availability is definitionally true for `channel: "hub"` in v2; there is no
configuration knob for it. For `native_cli`, `reason: "native_cli_unavailable"` is an
orthogonal process fact legal at every health and always forces `runnable: false`. The
item stays visible and dimmed, and makes a fully blocked chain `interrupted`, even when
its health is `cooldown` or `backoff`. A short connection throttle overlays only an
otherwise healthy, eligible non-retired Hub or process-available native hop. Source
cooldown, `needs_action`, `error`, `source_missing`, and `model_unsupported` suppress
that overlay and retain their established health/reason/retry projection. An eligible
throttled hop carries `health: backoff`,
`reason: models.source.backoff.connection_failed`, and the future deadline in
`retry_at`; this blocker rolls up as `waiting`. If the native CLI also becomes
unavailable, the actionable process fact takes the single `reason` slot, while
`health: backoff` and the same `retry_at` remain visible; the chain is `interrupted`.
Outside those blockers, `reason: null` means process-available. An empty Hub chain remains
a valid `interrupted` chain.

```json
{
  "ok": true,
  "contract_version": 10,
  "chain": {
    "contract_version": 10,
    "backend": "codex",
    "model_id": "gpt-5.6",
    "manual_override": null,
    "route_origin": "automatic",
    "chain": [
      {
        "source_id": "src_chatgptplus",
        "model_id": "gpt-5.6",
        "channel": "native_cli",
        "health": "healthy",
        "runnable": false,
        "reason": "native_cli_unavailable",
        "retry_at": null
      }
    ],
    "current": null,
    "supply_state": "interrupted"
  }
}
```

In Direct mode both chain and probe refuse with:

```json
{
  "ok": false,
  "contract_version": 10,
  "error": "direct_mode",
  "detail": "models.hub.direct_mode"
}
```

They never return `chain: []` or fabricate a `source_id` to represent native CLI
supply.

A successful probe nests its result:

```json
{
  "ok": true,
  "contract_version": 10,
  "probe": {
    "contract_version": 10,
    "backend": "claude",
    "channel": "hub",
    "reachable": false,
    "source_id": "src_relay9c1x",
    "model_id": "glm-5.2",
    "latency_ms": 287,
    "error": "models.source.needs_action.balance_exhausted"
  }
}
```

The probe walks the same §4.3 chain order and selects the first runnable item;
items already marked unavailable are never probed. For `channel: "hub"`, that
candidate keeps v5's total request-result truth table verbatim. For
`channel: "native_cli"`, the probe re-verifies process readiness after selection;
the fact may have gone stale, so an available candidate can honestly return
not-ready. `reachable` is READINESS, not completion evidence: no upstream call is
timed, so `latency_ms` is null in both directions. Ready carries `error: null`;
not-ready carries the closed i18n key `models.probe.native_cli_unavailable`.

```json
{
  "contract_version": 10,
  "backend": "codex",
  "channel": "native_cli",
  "reachable": true,
  "source_id": "src_chatgptplus",
  "model_id": "gpt-5.6",
  "latency_ms": null,
  "error": null
}
```

```json
{
  "contract_version": 10,
  "backend": "codex",
  "channel": "native_cli",
  "reachable": false,
  "source_id": "src_chatgptplus",
  "model_id": "gpt-5.6",
  "latency_ms": null,
  "error": "models.probe.native_cli_unavailable"
}
```

No candidate is an API error with a typed model-scoped state:

```json
{
  "ok": false,
  "contract_version": 10,
  "error": "probe_no_candidate",
  "detail": "models.probe.no_candidate.waiting",
  "supply": {
    "supply_state": "waiting",
    "retry_at": "2026-07-29T09:15:00Z"
  }
}
```

The §4.3 network-failure matrix is mirrored here in full. The retained `*_first_byte`
decision ids refer specifically to the existing `stream_started` boundary: the first
user-visible model-output byte, not HTTP status, headers, or another response byte. The
server transport supplies that fact; neither the client nor an HTTP timeout label may
infer it. A shaped result is one of the existing explicit closed classifiers, while
transport means there is no explicit code:

| Decision | Failure shape | Phase | Source/config write | Backoff/read projection | Execution result |
| --- | --- | --- | --- | --- | --- |
| `network_failure.shaped_before_first_byte` | explicit closed quota/rate/auth/server classification | `stream_started: false`; before first user-visible model output | existing non-permanent family only | none | existing retry/fallback and redacted event |
| `network_failure.transport_before_first_byte` | no explicit code; connection failure | `stream_started: false`; before first user-visible model output | none | Source-scoped in-memory `backoff`, delays 1/2/4/8/16/30 seconds, reason `models.source.backoff.connection_failed`, future `retry_at` | continue to next runnable hop; redacted `network` event |
| `network_failure.shaped_after_first_byte` | explicit closed classification after output started | `stream_started: true`; after first user-visible model output | existing non-permanent family only, with its unchanged recovery rule | none | terminal, no replay; existing redacted event only |
| `network_failure.transport_after_first_byte` | no explicit code; stream interruption | `stream_started: true`; after first user-visible model output | none | none | terminal, no replay; redacted `network` event only |

The backoff deadline expiring makes the hop runnable without persistence. Before any
chain, AgentSupply, or probe response is validated and serialized, the read assembler
captures one assembly time and normalizes every expired live overlay to that Source's
underlying non-backoff health and runnability; it never emits a stale `backoff` or expired
live `retry_at`. The first later user-visible model-output byte produced by that same
affected Source, Source endpoint/credential replacement, or process reconstruction
clears its deadline and consecutive-failure streak. Output from a different fallback
Source does not clear the affected Source's streak. The delay is capped at 30 seconds.
For a native hop whose process is simultaneously unavailable, the live deadline remains
visible but `native_cli_unavailable` takes the single reason slot and the chain remains
`interrupted`; restoring the process reveals any still-live connection backoff.
Projection precedence while a deadline is live is total:

| Underlying hop fact | Backoff overlay | Emitted facts | Fully blocked rollup |
| --- | --- | --- | --- |
| healthy + exact admitted non-retired pair + process available | apply | `backoff`, `connection_failed`, future deadline | `waiting` when all hops are cooldown/ordinary backoff |
| cooldown | suppress | existing cooldown health/reason/deadline | `waiting` when all hops are cooldown/ordinary backoff |
| `needs_action` or `error` | suppress | existing Source health/reason/retry facts | `interrupted` |
| `source_missing` or `model_unsupported` | suppress | existing capability-blocker health/reason/retry facts | `interrupted` |
| healthy + exact capability present + native process unavailable | apply with process-reason exception | `backoff`, `native_cli_unavailable`, same future deadline | `interrupted` |

`Source.state`, Source serialization, and every `models.source.cooldown.*` key remain
untouched by unclassified transport failure. Probe connection failure uses the same
closed live key and validates only as `channel: hub`, `reachable: false`, and
`latency_ms: null`; native probes and Hub probes with measured latency cannot carry it.
It never reports the removed persistent network/timeout cooldown keys.

## Latest recorded turn

The Recorded Error Detail Closure in `model-hub-routing-modes.md` (`0de3d2f47`,
envelope/diagnostic correction `9cb9ebb53`)
binds GET `/api/models/agents/<backend>/provenance?model=<id>` to the existing
`BoundedProvenanceStore`. Select the latest persisted retained record matching both
`agent` and `requested_model_id`, not the latest failure or the current route's Source.
The store retains at most 500 exactly attributed settled turns with its existing atomic
writes. Ambiguous attribution remains excluded; this is not a log of every upstream
request. Null means no matching retained record, including eviction. A newer served,
canceled or other record without a terminal error supersedes older error display.
Success uses exactly `{ok: true, contract_version: 10, provenance: TurnProvenance | null}`;
there is no `success` field or envelope exception.

New `terminal_error` records may contain `http_status` (integer 100-599 or null) and
`upstream_error_code` (a recognized upstream machine code or null). The existing
`UPSTREAM_MACHINE_ERROR_CODES` authority owns code membership. When `model_not_found`
was observed and classification returned `upstream_request_invalid`, retain it over a
co-occurring generic `invalid_request_error` type; otherwise use existing specificity
order. This is diagnostic selection, not a classifier rank/decision change. Never infer
`model_not_found` from `invalid_parameter`, the requested model, or today's route.
Unknown upstream strings, raw bodies, messages, headers and credentials are not
retained. These optional observations do not change classification, fallback or Source
health. Historical records may omit both fields and keep their existing read behavior.

The dialog independently reads this projection on demand and labels it "Latest recorded
turn" / "最近已记录回合". Its error panel and details action use the same structured record,
recorded timestamp and exact historical Source/model identifiers. Deleted Sources keep
their historical ids. Missing machine codes use generic reason-based copy. Retrieval
failure has an explicit retry state; model changes and dialog close invalidate pending
reads. Cancel, Restore and Save never rewrite history. The pure effective planner and
chain-list response do not acquire history reads or fields.

Diagnostic selection follows the Recorded-error diagnostic selection authority in
`model-hub.md`. D28 mirrors its decisions; the schema code enum is independently
compared with production `UPSTREAM_MACHINE_ERROR_CODES` by
`tests/test_model_hub_provenance.py::test_terminal_diagnostic_schema_uses_the_production_machine_code_authority`.

<!-- authority-consumer: recorded_error.specific_model_not_found recorded_error.known_code recorded_error.no_safe_code -->

## Turn provenance absence

This section applies only to the existing turn-id endpoint; the backend/model latest
projection above returns successful null for absent retained history.

`TurnProvenance` is returned only for an exactly attributed Hub turn. Direct and
ambiguous absence are explicit and distinguishable from an unknown turn:

```json
{
  "ok": false,
  "contract_version": 10,
  "error": "provenance_unavailable",
  "detail": "models.provenance.direct_mode"
}
```

```json
{
  "ok": false,
  "contract_version": 10,
  "error": "provenance_unavailable",
  "detail": "models.provenance.attribution_ambiguous"
}
```

An unknown `turn_id` returns `turn_not_found`. The server derives ambiguous absence
live from “known turn, no exact record”; it does not persist a placeholder and never
guesses which attempt belonged to the turn.

When exact-match forwarding removes a requested reasoning effort, that exact attempted
hop carries both `stripped_reasoning_efforts` and the declaration consulted in
`declared_reasoning_efforts`. The paired fields appear only on the failed, served,
terminal, or canceled attempt where a strip actually occurred; they never leak onto a
fallback hop or another turn. The same redacted source/model, stripped effort, and
declared-tier facts are written to the application logger without changing chat copy.

## Resolution events

Events are state/feed records, not recorded fan-out. `severity` is presentation
metadata (`info` or `action_required`). Consumers derive current impact from live
source orders and named-Agent effective models.

`model_id: null` means a source-scoped system event is about the source as a whole.
Model-scoped kinds require a string. `agent: system` is invalid on backend-scoped
`supply_interrupted`. Source endpoints use canonical ids and are checked for existence
when emitted.

`reasoning_efforts_override` is a source/model-scoped system event with info severity,
`to_source: null`, and reason `upstream_tiers` or `catalog_tiers`. It records that a
successful refresh replaced a non-empty user declaration; it contains neither the old
tiers, new tiers, nor credential material.

## Usage metering

Usage is a report, never a control input. Nothing in resolution, admission, or
cooldown reads these numbers, so an upstream that misreports usage cannot change
which Source serves the next turn.

The metered unit is one upstream call that reached the model, not one turn: a turn
that failed over billed every hop that reported tokens, and each hop is counted
against the Source it was made against. A call counts when the hub forwarded its
output downstream or when upstream reported tokens for it, so a stream that died
after its terminal frame and a buffered error that still billed us are both
metered. A call that never reached the model is not, because the resolution feed
and Source health already own that.

`requests` is self-measured by hub code and is always present: a stream that
forwarded model output counts even when it then died without a terminal, because
that call demonstrably reached the model. Token counts are vendor-reported and may
be absent for a served call — streaming chat completions report usage only when the
client asked for it — so `token_reports` counts the calls that carried a report, is
never greater than `requests`, and a missing report is never read as zero usage.
Every reported integer is bounded by a ceiling fixed in server code, never by a
total the response declares.

Input composition follows each protocol rather than one invented total.
`cached_input_tokens` is always the subset of `input_tokens` that was served from
cache: Anthropic's own `input_tokens` excludes both cache members, so the reported
input total is their sum; OpenAI's already includes cached input, so the cached
count is informational only. The subset holds even when an upstream reports a cached
count above the input it belongs to, or reports one without a readable input count:
the merged cached count is bounded by the merged input count. Both subset promises —
cached input inside input, token reports inside requests — are repaired on read, so a
corrupt or hand-edited ledger degrades into a smaller true statement instead of
publishing an impossible one.

`source_id` and `model_id` are reported in the canonical form configuration admitted,
so one model is one row rather than one row per spelling.

Days are local-calendar days on the Avibe host. `from_day` and `to_day` bound the
requested window even when no turn fell inside it; `days[]` contains only days that
carry a metered turn. `label` is joined from current Source config, so it is `null`
for a Source that has since been removed and follows a rename immediately.

## Runtime installation and host support

Every runtime response carries `host_platform`, and every status carries `error_key`.
The server detects the Avibe host; clients never substitute the browser platform.
`manifest.resolution` is the one host-admission projection: `resolved` means a trusted
inventory contains an exact `host_platform` asset; `unresolved` means no trusted local
inventory exists yet, so `version` and `source_sha` are absent and the network-backed
install admission remains reachable; `unsupported` means a trusted inventory exists
without an exact host asset. An unsupported host leaves `health: "not_installed"`;
the install route fails before creating an install claim or downloading with HTTP 422
and `error: "runtime_platform_unsupported"`; the next status read remains truthful
with `error_key: null` and no persisted install failure.

`POST /api/models/runtime/install` is idempotent. Only `not_installed` on a supported
host starts work: it clears the previous `error_key`, durably enters `installing` with
exactly `installed_version: null`, `verified: false`, and `listening: null`, starts the
owned install job, and returns that state. A reload and concurrent repeat while
`installing` read/return the same state instead of starting another job. Calls from
`not_started`, `ok`, `degraded`, or `down` are HTTP 200 no-ops that return the current
RuntimeDependency without mutating runtime state: they start no download, do not clear
or replace the verified binary, and neither start, stop, nor restart the process.
Installed-state handling precedes host-support refusal, so an existing verified
installation is never disrupted merely because the current manifest lacks that
platform. Successful
verification settles at `not_started` with `error_key: null`; it does not start the
runtime. Failure settles at `not_installed` with
`error_key: "settings.models.install.fail.detail"`. The key is the closed presentation
carrier for the persisted failure; raw downloader or verifier text remains only in
scrubbed logs. `/start` never performs installation.

On service bootstrap, persisted `installing` with no worker owned by the reconstructed
process is an orphaned install, never a live-job proof. Recovery atomically replaces the
claim generation before it re-resolves the pinned target through the shared installer
and verifies its exact identity before archive access. Every success, failure, and
abandon settlement is a compare-and-set against that generation; a stale owner cannot
clear or overwrite a newer owner's claim. A complete manifest-matching binary settles
directly at `not_started`; otherwise the owned recovery continues the pinned installation
while retaining `installing`. A transient collision with the shared install file lock is
retried with exponential backoff from 250 ms to 4 seconds for at most 30 seconds. Failure
to claim or schedule recovery, exhaustion of that bounded window, or another terminal
recovery failure settles the current generation at `not_installed` with the same closed
`error_key`. Thus a page reload observes a live owned job, while a service restart cannot
strand the state permanently.

## Error codes

Minimum v5 set:

`source_not_found`, `flow_not_found`, `flow_expired`, `discovery_failed`,
`invalid_source_order`, `source_create_in_progress`, `source_nonce_conflict`, `source_last_supplier`,
`source_in_route_chain`,
`source_model_in_route_chain`, `backend_model_in_route`,
`candidate_suppliers_changed`, `mode_switch_blocked`, `engine_down`,
`runtime_platform_unsupported`, `reauth_confirmation_required`,
`native_source_already_exists`, `native_login_in_progress`,
`migration_item_conflict`, `source_model_tiers_managed`, `turn_not_found`,
`provenance_unavailable`, `probe_no_candidate`, `direct_mode`.

`source_model_tiers_managed` is the HTTP 409 refusal for editing an `upstream` or
`catalog` tier declaration. Its structured sibling is
`{reasoning_efforts_source: "upstream" | "catalog"}` and its `detail` is the matching
`settings.models.sourceDetail.tiers.managed.<provenance>` i18n key.

`native_source_already_exists` is an API-boundary refusal for native OAuth start;
its structured sibling is `{existing_source_id}` and the adapter is not invoked.
`native_login_in_progress` is a distinct transient `409` from the shared native-login
owner. It means another login currently owns the same credential; retry remains on the
native channel and the response does not assert that a native Source already exists.

Boundary-only action-refusal values cover operations the UI already does not offer but
a script or regression can call directly. `reauth_confirmation_required` is API/test-
only truth: it has no product copy key or rendering slot.

The top-level cascade-guard vocabulary is `source_last_supplier |
source_in_route_chain | source_model_in_route_chain | backend_model_in_route`; a changed confirmation plan returns
the same family with newly recomputed arrays rather than a parallel discriminator.

Removed: `invalid_priority_order`.

## Mechanical guards

JSON Schema draft-07 cannot express cross-document or live-state relations. The
contract harness and API-boundary tests enforce:

<!-- authority-consumer: credential.refresh_once credential.refresh_failed credential.refresh_rejected credential.static_unauthorized credential.account_classified credential.request_nonfallback -->
<!-- authority-consumer: turn.served turn.exhausted turn.request_nonfallback turn.engine_down turn.streamed_fallback turn.no_candidate.unconfigured turn.no_candidate.blocked turn.canceled -->
<!-- authority-consumer: mutation.source_metadata mutation.credential_replace mutation.source_refresh mutation.model_create mutation.model_efforts mutation.model_delete mutation.source_delete mutation.route_replace mutation.route_restore mutation.default_sources -->
<!-- authority-consumer: import.keep_native import.copy_key import.reauth import.controlled -->
<!-- authority-consumer: protocol anthropic openai_responses openai_chat -->
<!-- authority-consumer: observation.outcome observed ambiguous unreachable authentication_failed adapter_error timeout -->
<!-- authority-consumer: observation.discovery succeeded failed not_attempted -->
<!-- authority-consumer: runtime.health ok degraded down not_installed installing not_started -->
<!-- authority-consumer: runtime.install_error runtime_platform_unsupported -->
<!-- authority-consumer: source.create_nonce nonce.in_flight nonce.released nonce.committed -->
<!-- authority-consumer: guard.error backend_model_in_route source_last_supplier source_in_route_chain source_model_in_route_chain -->
<!-- authority-consumer: candidate.error candidate_suppliers_changed -->
<!-- authority-consumer: guard.decision guard_decision.unforced_no_impact guard_decision.unforced_confirmation guard_decision.forced_no_impact guard_decision.forced_confirmed guard_decision.forced_unconfirmed -->
<!-- authority-consumer: oauth.start_nonce oauth_nonce.released oauth_nonce.in_flight oauth_nonce.committed -->
<!-- authority-consumer: oauth.terminal oauth_terminal.flow_only oauth_terminal.create_success oauth_terminal.reauth_success oauth_terminal.materialization_interrupted oauth_terminal.materialization_plain_error -->
<!-- authority-consumer: network.failure network_failure.shaped_before_first_byte network_failure.transport_before_first_byte network_failure.shaped_after_first_byte network_failure.transport_after_first_byte -->
<!-- authority-consumer: live_backoff.health backoff -->
<!-- authority-consumer: live_backoff.reason models.source.backoff.connection_failed -->

| Guard | Boundary |
| --- | --- |
| every example validates and JSON round-trips | contract harness |
| authority registry and mirror relations are generated from live files in the same test run; every registered closed branch has a consumer and every registered consumer resolves to one authority | `mirror-registry.json` harness |
| every non-null `then` constraint has matching `required` | contract harness |
| every `sources.order` id exists, is unique, and is eligible | config loader + source-order route |
| the per-model Route PUT accepts only explicit `hops` and its server path never reads or implicitly applies `sources.order`; the shared planner consumes defaults for inherited routes, while default writes preserve manual arrays | API route negative fixture |
| eligibility contains one row per source and every ordered source is eligible | AgentSupply assembler |
| every AgentSupply eligibility row carries `in_current_model_chain` and `process_availability_reason`; membership nullability follows `selected_model_id`, and only a native source may carry `native_cli_unavailable` | AgentSupply assembler |
| `AgentChain.chain` returns the shared effective hops in planner order, including missing, model-unsupported, and process-unavailable native CLI items; `AgentChain.current` is null or identifies one exact hop in that array | chain assembler |
| `model_supply` has one row per menu model with unique ids | AgentSupply assembler |
| probe `source_id` names an existing source | probe assembler |
| non-null event endpoints name existing sources at emission time | event emitter |
| `channel_switch.from_source == channel_switch.to_source` | event emitter |
| API AgentSupply includes `cli_present`, `selected_by_agent`, `selected_model_id`, `selected_model_explicit`, default `sources.order`, sparse `routes`, `supply_status`, `model_supply[].route_origin`, `model_supply[].has_runnable_hop`, and `named_agents[].route_reason`; every Source includes persisted `last_discovered_at`, optional persisted `client_nonce`, derived `adopted_by`, model retirement tombstones, and every model's `reasoning_efforts_source`; source creation returns `added_to` and top-level `adopted_by` equal to `source.adopted_by`; saved refresh and discovered-model retirement use the guarded Source envelope | API payload test |
| every OAuthFlow response includes `intent` | API payload test |
| Hub OAuth and native CLI `POST /sources/<id>/reauth` requests with missing or false `acknowledge_irreversible` return `reauth_confirmation_required` before the OAuth adapter is called; true acknowledgement is the only start path, while transactional API-key PUT is unaffected | API route negative/positive fixtures |
| OAuth start claims `(client_nonce, vendor, channel)` before provider work; a blocked first call plus concurrent same-tuple retry coalesces to one pending result and exactly one provider start; pending-start failure/task cancellation releases after cleanup; success atomically exposes one echoed-nonce flow; explicit cancellation retains that nonce-bearing flow as `cancelled` so a same-tuple retry starts no provider, while existing expiry releases it for exactly one fresh start; no-nonce cancellation forgets | OAuth registry totality, clocked expiry, API payload, and auth-setup closed-loop tests |
| every RuntimeDependency API payload includes server-derived `host_platform` and `status.error_key`; `installing` has exactly null `installed_version`, false `verified`, and null `listening`, with one positive and each-field contradiction fixtures; only supported `not_installed` starts a download, installed states are state-preserving no-ops, page reload preserves a live `installing` job, and service bootstrap reconciles an orphan before serving runtime endpoints | RuntimeDependency schema and API payload tests |
| Source create reserves `client_nonce` in process before work; the client reads Sources before any lost-response retry; fixtures cover `nonce.in_flight` (`source_create_in_progress`/no work), `nonce.released` (atomic reserve/exactly one fresh attempt), and `nonce.committed` (`source_nonce_conflict` followed by a list read that finds exactly one Source with that nonce), while AC-26 cleanup or process restart releases any live reservation and Source deletion releases its committed nonce; after restart or deletion and a list miss, same-nonce create is positively asserted as a fresh creation | config, API payload, cancellation/restart, Source-delete, and client retry tests |
| Source observation rejects unregistered request/status evidence fields; clients consume only the contracted outcome, reachability, authentication, protocol, discovery, models, and parallel `model_metadata` facts; metadata ids and order exactly match `models`, malformed `supported_parameters` degrades to null without dropping the id, and an empty array remains distinct | observation schema, runtime parser, and API payload tests |
| tier resolution chooses the first applicable `upstream > catalog > user > null` rung; upstream uses only valid OpenRouter-shape capability metadata and protocol defaults, catalog uses each exact row without filtering, OAuth/import reuse the same resolver, and older persisted rows infer `user` for non-empty tiers or null for empty tiers | catalog contract, ladder unit tests, config load fixture, and materialization tests |
| model tier PATCH and add-model upsert refuse managed `upstream`/`catalog` rows with `source_model_tiers_managed` and an exact provenance sibling, while `user`/null rows remain editable; refresh emits one redacted override event only after a committed replacement of a non-empty user declaration | API mutation, refresh, and event tests |
| a stripped effort and the exact hop declaration consulted appear as a pair only on the attempt where stripping occurred, in turn provenance and a redacted logger line | gateway/provenance unit and D10 e2e tests |
| network fixtures cover shaped/transport × `stream_started: false/true`, where the boundary is the first user-visible model-output byte: shaped results at either phase enter only their existing non-permanent Source classifier; unclassified pre-output connection failure creates bounded live backoff with no config write; only later output from that same Source clears its streak, while another Source's successful fallback does not; the API/read assembler emits a future deadline for a live overlay and normalizes an expired overlay before serialization; concurrent cooldown/needs-action/error/missing-Source/unsupported-model facts suppress the overlay and keep stronger projections, so waiting never hides a durable blocker; simultaneous native-process unavailability alone preserves backoff health/deadline while taking reason precedence and yielding interrupted; probe `connection_failed` requires the exact Hub/unreachable/null-latency shape | clocked table-driven resolver/API/event, AgentChain precedence, ProbeResult relation, and concurrent-transition tests |
| every `GuardRefusal` validates against `guard-refusal.schema.json` and has a nonempty current plan; fixtures cover every guard-decision row, including unforced refusal, exact echoed-plan confirmation, missing/different echo returning the new plan, and old-echo empty-plan ordinary success without a fabricated 409 | API payload and concurrent-mutation test |
| `PUT /sources/<id>/credential` success contains exactly the standard `{source, removed_hops, interrupted}` mutation tail; `{recovered, interrupted_pairs}` is absent there and remains owned only by terminal OAuth `intent: "reauth"` | contract negative fixture and API payload test |
| OAuth status/submit covers every `oauth_terminal.*` row; a native re-auth materialization error with a nonempty acquired-side-effect report emits that exact `interrupted_pairs`, while the same error with no report and every non-materialization error omit the field rather than sending `[]` | contract totality fixture plus positive/negative API and client payload tests |
| each `source_in_route_chain` or `source_model_in_route_chain` refusal has nonempty `would_remove_hops`; each `source_last_supplier` refusal has nonempty `would_interrupt`; mismatched code/empty-required-array combinations fail schema validation even when the other array is nonempty | guard schema relation fixture |
| both GuardRefusal plan arrays contain unique objects; duplicate hop and supply-gap entries fail schema validation; producers emit SupplyGap rows in ascending `(backend, model_id)` order and each `agents` array in stable-id lexicographic order, with permutation fixtures proving canonical output | guard schema uniqueness and producer-order fixtures |
| `model_supply[].chain_length: 0` implies `has_runnable_hop: false`; the opposite pair fails schema validation | AgentSupply schema correlation fixture |
| contract and in-repo adapter interface copies are byte-identical; the five retained-material enum members and ref-pairing predicates are mutation-tested | contract harness |

Serializer completeness follows the issue #939 pattern. Persisted fields must
round-trip through config serialization. Derived fields are exempt from persistence
but require an API-payload test.
