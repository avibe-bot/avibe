# Model Hub — REST API contract

Status: **FINAL v5 (2026-08-11 additive closure)**. All endpoints live under `/api/models/`.

Success envelope: `{ok: true, contract_version: 5, ...}`.
Failure envelope:
`{ok: false, contract_version: 5, error: <machine_code>, detail?: <i18n_key>}`.
`detail` is always a string. Structured error data lives in a named sibling.
Authentication and CSRF rules are the existing UI-server rules.

The shared envelope and every versioned nested contract use terminal version 5. Model
Hub has not shipped, so there is no internal contract migration or compatibility path.

## Route table

| Method and path | Request → response | Normative notes |
| --- | --- | --- |
| GET `/api/models/sources` | → `{sources: Source[]}` | Unordered asset inventory. Every Source carries server-derived `adopted_by` and any persisted `client_nonce`; array order is never a spend order. |
| POST `/api/models/sources/observe` | `{vendor, base_url?, key, protocol_order?}` → `{observation: SourceObservation}` | Non-persisting connectivity/authentication/protocol/inventory observation. `protocol_order` only orders the three probes; it never supplies a conclusion. No credential reference is returned. |
| POST `/api/models/sources` | `source-create.schema.json` → `{source: Source, added_to: AddedTo[], adopted_by: AdoptedBy[]}` | The server assigns `id` and `created_at`; plaintext keys are transient. Add-time matching and placement are materialized before response. An optional `client_nonce` is persisted for lost-response reconciliation. |
| PATCH `/api/models/sources/<id>` | `{display_name?, base_url?, force?: boolean}` → guarded Source-mutation envelope | Metadata/Base-URL mutation from the authoritative matrix in `model-hub.md` §4.5. |
| PUT `/api/models/sources/<id>/credential` | `{key, force?: boolean}` → guarded Source-mutation envelope | API-key replacement. `force` is a JSON body field, never a query parameter. |
| POST `/api/models/sources/<id>/reauth` | `{acknowledge_irreversible?: true}` → `{flow: OAuthFlow}` | A `native_cli` source requires the acknowledgement before OAuth starts. See repair rules. |
| DELETE `/api/models/sources/<id>?force=<bool>` | → guarded `409` or `{removed_hops, interrupted}` | A confirmed delete removes the Source from every backend Source order and Route chain in one transaction. |
| POST `/api/models/sources/<id>/refresh` | `{force?: boolean}` → guarded Source-mutation envelope | The sole saved connectivity/discovery/recovery mutation. |
| POST `/api/models/sources/<source_id>/models` | `{model_id, display_name?, reasoning_efforts}` → `{source: Source}` | Creates one user-authored model entry. The Source identity comes only from the path. |
| PATCH `/api/models/sources/<source_id>/models/<model_id>` | `{reasoning_efforts}` → `{source: Source}` | Replaces the complete capability list on either model origin without changing identity, origin, or Routes. |
| DELETE `/api/models/sources/<source_id>/models/<model_id>` | `{force?: boolean}` → guarded `409` or Source-mutation success | Deletes a manual entry; for a discovered entry it persists `retired: true` without deleting the row. Both outcomes use the same exact-hop and supply guards. |
| GET `/api/models/agents` | → `{agents: AgentSupply[]}` | Backend records include server-authoritative `cli_present` and `named_agents`, the enabled named-Agent live projection. |
| GET `/api/models/agents/<backend>/sources` | → `{agent: AgentSupply}` | Returns the authoritative effective order and eligibility. |
| PUT `/api/models/agents/<backend>/sources` | `{order: string[]}` → `{agent: AgentSupply}` | Stores and re-echoes the complete canonical order; no policy state exists. This route never touches a Route chain and has no guarded `409` branch. |
| POST `/api/models/agents/<backend>/chains/reorder` | → `{agent: AgentSupply}` | Idempotently reorders every stored Route by the current Source order without adding, removing, remapping, matching, or guarding. The total order is defined below. |
| PATCH `/api/models/agents/<backend>/mode` | `{mode}` → `{agent: AgentSupply}` | Explicit `hub` / `direct` switch. A qualifying Direct → Gateway switch atomically adopts the recognized CLI login as the first native Source; other switches create nothing. |
| PUT `/api/models/agents/opencode/menu` | `{menu}` → `{agent: AgentSupply}` | Open-menu configuration. |
| GET `/api/models/agents/<backend>/chain?model=<id>` | → `{chain: AgentChain}` | Hub only. Direct returns the documented `direct_mode` error. |
| PUT `/api/models/agents/<backend>/chain?model=<id>` | `{hops: RouteHop[], force?: boolean}` → guarded `409` or `{chain, removed_hops, interrupted}` | Replaces the exact Source/model pairs in submitted order after validating new or changed pairs; it is the `mutation.route_replace` row of the authoritative mutation matrix. |
| POST `/api/models/agents/<backend>/probe` | `{model?}` → `{probe: ProbeResult}` | Hub only. Direct returns the same `direct_mode` error. |
| GET `/api/models/events?limit=<n>&before=<id>` | → `{events: ResolutionEvent[]}` | Bounded source-resolution feed. |
| POST `/api/models/oauth/start` | `{vendor, channel, client_nonce?}` → `{flow: OAuthFlow}` | Starts creation of a new subscription source. The optional nonce makes the exact `(client_nonce, vendor, channel)` start idempotent while its flow exists. |
| GET `/api/models/oauth/status/<flow_id>` | → OAuth result | Terminal create and reauth shapes are below. |
| POST `/api/models/oauth/submit` | `{flow_id, value}` → OAuth result | Same terminal shape as status. |
| POST `/api/models/oauth/cancel` | `{flow_id}` → `{ok}` | Cancels and forgets the flow. |
| POST `/api/models/migration/scan` | → `{scan: MigrationScan}` | Read-only. |
| POST `/api/models/migration/apply` | `{item_ids: string[]}` → `{applied, sources, added_to}` | Each accepted import runs the same one-time matching and placement as Add Source; original files remain byte-identical. |
| GET `/api/models/turns/<turn_id>/provenance` | → `{provenance: TurnProvenance}` or documented absence error | Debug read for exactly attributed Hub turns. |
| GET `/api/models/runtime/status` | → `{runtime: RuntimeDependency}` | Read-only managed engine status. The nested object is contract v5; `not_started` is installed lazy-start idleness, not an alarm. |
| POST `/api/models/runtime/install` | → `{runtime: RuntimeDependency}` | Idempotently starts server-owned installation. It returns and persists `installing`; reload reads the same state. Uses the existing mutation authentication and CSRF guards. |
| POST `/api/models/runtime/start` | → `{runtime: RuntimeDependency}` | Explicitly starts the managed engine. Uses the existing mutation authentication and CSRF guards; status reads never start it. |

The removed global route `PUT /api/models/priority` has no replacement. Backend Source
order is explicit configuration through the sources route; exact per-model order is
explicit configuration through the chain route.

## Unsaved Source observation

`POST /api/models/sources/observe` accepts only the observation subset of a Source
create request:

```json
{
  "vendor": "custom",
  "base_url": "https://relay.example/v1",
  "key": "<transient plaintext key>",
  "protocol_order": ["openai_chat", "openai_responses", "anthropic"]
}
```

`base_url` may be null for an official vendor endpoint. `protocol_order`, when
present, contains each protocol exactly once and is only probe ordering; omitting it
uses the authoritative three-value order. The endpoint provisions an unbound engine
credential only for this operation, returns `observation-result.schema.json`, then
revokes the transient reference before settling. A revoke failure remains in the
existing pending-revocation journal. The response never contains that reference or
any persisted Source.

For an API-key `POST /api/models/sources`, the server performs the same
response-backed observation internally before its independent committed credential
provisioning. It accepts the create fields and optional `protocol_order`, but never
accepts a protocol conclusion from the caller. A null protocol produces no Source; a
proven protocol may be saved even when inventory discovery failed, with the resulting
Source health reflecting that uncertainty. Subscription OAuth creation follows its
vendor-specific observation flow before commit. Saved Sources use the stored
protocol for every later operation.

`source-create.schema.json` is the complete `SourceCreate` request. Its field table is
authoritative; no Source response field may be inferred backwards into the request:

| Field | Required | Producer → consumer | Rule |
| --- | --- | --- | --- |
| `vendor` | yes | Add Source client → observation adapter | Same normalized vendor id as the unsaved observation request. |
| `display_name` | no | Add Source client → Source metadata | Omission uses the server's canonical vendor label; it never carries credential material. |
| `base_url` | no | Add Source client → observation adapter | null or omission selects the official endpoint; a custom URL is validated before provisioning. |
| `key` | yes | Add Source client → transient and committed credential provisioning | Plaintext is write-only and never appears in a response, Source record, event, or log. |
| `protocol_order` | no | Add Source client → observation adapter | Contains all three protocols exactly once and only orders probes. |
| `client_nonce` | no | Add Source client → persisted Source read projection | Client-generated before send, unique among Sources when present, and echoed unchanged so the list read can reconcile a lost response. |

The request has no `id`, `created_at`, `state`, `usage`, `protocol`, discovered-model,
credential-ref, billing, or supply-channel field. The server assigns or observes all of
them. A successfully observed empty inventory is represented by the returned
`source.models: []`; the client never submits a discovered inventory as authority.

When `client_nonce` is present, a create whose nonce already belongs to a Source fails
with the existing `discovery_failed` validation error before observation or credential
provisioning. The client first re-reads `GET /api/models/sources` and identifies the
single Source by exact nonce, so a response-lost create is not submitted twice.

Cancellation has one commit boundary. Before the durable Source commit, AC-26 applies:
the transient or uncommitted ref is revoked, or is named by the durable pending-
revocation journal, before cancellation settles. After that commit, cancellation ends
only the caller's wait. The transaction completes normally, the Source and all accepted
placements remain committed, and the next Source/Agent read returns a coherent state
even if no client received the create response. There is no server-side abort path after
the commit point.

The observation result has six terminal outcomes: `observed`, `ambiguous`,
`unreachable`, `authentication_failed`, `adapter_error`, and `timeout`. Its
`protocol` is non-null only when a real upstream response shape and positive
authentication evidence prove the transport. Authentication is accepted only by a
shaped success or a shaped request-level error that occurs after authentication;
shaped authentication errors are rejected. Shaped server and rate-limit errors
prove reachability but not authentication, so they settle as `adapter_error` with
`reachable: true`, `authenticated: unknown`, and `protocol: null`. A local adapter
failure may use the same outcome with `reachable: null`. A bare HTTP status proves
reachability, but proves neither protocol nor authentication. Consequently,
`ambiguous` always has `reachable: true` and `protocol: null`.
`authentication_failed` also has `protocol: null`, because a rejected credential
does not establish a persistable protocol. `ambiguous` is the sole outcome that asks
for the one-time probe-order hint. Only `observed` attempts inventory discovery and
therefore reports `succeeded` or `failed`; every other terminal reports
`not_attempted` with an empty model list.

## Identifier rules

- `Source.id` and every non-null source reference match
  `^src_[a-z0-9]{8,}$`.
- The API boundary also verifies referential existence where a new reference is
  emitted or accepted. JSON Schema can validate format, not cross-document existence.
- Claude and Codex requests use fixed-menu built-in ids.
- OpenCode requests use a prefixed `vendor/model` menu id. The prefix chooses the
  provider; the resolver sends the bare model id upstream.
- Add-time matching may suggest a sanctioned alias or mapping, but it materializes the
  accepted result as an exact `{source_id, model_id}` hop before commit. Runtime neither
  resolves aliases nor substitutes a model.
- Source event references are checked when emitted. Retained feed entries remain
  valid after a later legal source deletion.

## K4 v5 field registry

These rows are authoritative for the new payload members only. They do not classify
every K4 route change as read-only or compatibility-neutral. G-3's discovered-model
DELETE behavior and G-14's mode-switch transaction are separately specified pre-release
route corrections; no existing field or enum meaning changes.

| Field | Producer | First consumer | Invariant |
| --- | --- | --- | --- |
| `Source.client_nonce` | Source create commit | lost-create reconciliation | Exact optional echo of `SourceCreate.client_nonce`; unique among Sources and never used for routing. |
| `Source.models[].retired` | discovered-model DELETE | Source detail inventory | Omission means false; only a discovered row may be true; true rows remain readable and never supply. |
| `Source.adopted_by` | Source read assembler | Source cards and Source detail status | Complete unique persisted-reference projection sorted by backend then menu model; clients do not derive it from `hops`. |
| `AgentSupply.cli_present` | backend CLI detector | zero-installed-backend state | Boolean installation fact only; it does not imply login or process readiness. |
| `AgentSupply.model_supply[].has_runnable_hop` | exact-chain live annotator | backend-group collapse predicate | Uses the AgentChain runnability axiom; it is independent of configured `chain_length`. |
| `RuntimeDependency.host_platform` | server host detector | unsupported-host runtime pill | Names the Avibe host, not the browser; exact membership in `manifest.assets[].platform` decides install support. |
| `RuntimeDependency.status.error_key` | runtime installer | install-failed runtime state | Closed persisted i18n key: `settings.models.install.fail.detail` after installation fails; null after a new attempt begins and in every non-failure state. |
| `RouteHopRef.position` | guarded mutation planner | guarded-change hop row | One-based position in the named Route before the attempted mutation. |
| `OAuthStart.client_nonce` | OAuth client before send | OAuth start idempotency | Optional client-generated correlation; the exact tuple with vendor and channel is the idempotency scope while the flow exists. |
| `OAuthFlow.client_nonce` | OAuth start echo | lost-start reconciliation | When the request supplied the nonce, every flow response echoes it unchanged. |

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
      "supply_status": "degraded"
    },
    {
      "name": "reviewer",
      "effective_model_id": "claude-haiku-4-5",
      "supply_status": "interrupted"
    }
  ]
}
```

`named_agents` lists every enabled named Vibe Agent whose backend matches this
record. `effective_model_id` is the Agent's explicit model. `supply_status` is
derived independently for that effective model. The
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
structurally empty. The boolean is computed from the complete exact chain with the same
source-health AND process-availability rule as `AgentChain`, so the page never issues a
chain read per row and never treats configured membership as live supply.

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

The request is total:

```json
{"order": ["src_anthkey01", "src_relay9c1x"]}
```

- `order` is the complete desired ordered subset. The empty subset is legal.
- Every id is unique, exists, and is eligible for the backend. Otherwise the route
  returns `invalid_source_order`.
- Add Source and native import invoke `placement-v1` once and persist its chosen
  position. Refresh, restart, health changes, and turns never recompute this order.

The whole-order PUT is intentionally outside the §4.5 Source-mutation envelope matrix.
It stores only `sources.order`, never reads or writes a Route chain, and therefore never
returns a guarded `409`. This is the G-9 tombstone: adding a guard branch would imply an
order save is allowed to mutate supply, which violates S-1.

`POST /api/models/agents/<backend>/chains/reorder` is the only operation that applies
the stored Source order to existing Routes. For each Route, give every hop its original
zero-based index `i` and sort by this total key:

```text
source_id in sources.order  -> (0, sources.order.index(source_id), i)
source_id not in the order  -> (1, i, i)
```

Thus ordered Sources come first in Source-order sequence; hops of the same Source keep
their prior order; every unlisted hop follows and all unlisted hops keep their relative
order. The operation changes only hop order. It preserves the multiset of exact
`(source_id, model_id)` pairs byte-for-byte, never runs matching, and never adds,
removes, or remaps a hop. It is idempotent and has no `force`, guarded refusal, removed-
hop, or interruption branch: changing a preferred first hop is an explicit non-
destructive configuration edit, not loss of supply. This route is the first consumer of
the Source-order sequence under the current `placement-v1` policy.

Eligibility is server-authoritative. An ineligible row carries exactly one closed
`reason_key` from `agent-supply.schema.json`; an eligible row carries null.

### Direct-to-Gateway native adoption

For `PATCH /api/models/agents/<backend>/mode`, a transition from `direct` to `hub`
reuses the same sanctioned native-login recognition and response-backed observation
boundary as native import. If that backend has a recognized CLI login and has no
`native_cli` Source, the mode change, creation of the backend's singleton native Source,
`placement-v1` Source-order insertion, and every accepted add-time exact match commit in
one transaction. The returned `AgentSupply` is assembled after that commit and exposes
the resulting order, eligibility, Routes, and supply summaries.

An existing native Source, an unrecognized or absent CLI login, or any transition other
than `direct` → `hub` creates nothing. The mode switch itself remains legal in those
cases. Repeating the request never creates a second native Source. `cli_present` alone
does not satisfy the recognition predicate.

### Exact Route-chain configuration

Every menu model stores one `hops` array. Each hop is an exact
`{source_id, model_id}` pair; a differing upstream id is the mapping itself, not a
separate mapping object. A write validates only newly introduced or changed pairs.
The whole-array PUT uses `force` in the JSON body and the same guarded refusal/reporting
family as Source mutations. A successful write returns `{chain, removed_hops,
interrupted}`; a non-forced write that would empty protected supply returns the shared
`409 {error, would_remove_hops, would_interrupt}` envelope.
An unchanged stale pair may be retained or reordered so a user can add a working
fallback without discarding a temporarily unavailable configured hop. Runtime walks
the stored array verbatim and annotates only live runnability.

## Source creation outcome

`AddedTo` is:

```json
{"backend": "claude", "menu_model": "claude-opus-4-6", "source_id": "src_anthkey01", "model_id": "claude-opus-4-6", "position": 2}
```

`position` is one-based in the persisted Route chain after commit. `AdoptedBy` is the
stable Source-card projection of persisted references:

```json
{"backend": "claude", "menu_model": "claude-opus-4-6"}
```

Neither array carries a policy field. A Source with no accepted automatic match returns
empty arrays and remains available for explicit Route editing; there is no skipped-order
reason or “not enabled” state.

The terminal result of both ordinary API-key creation and OAuth creation is:

```json
{
  "ok": true,
  "contract_version": 5,
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
menu-side `model_id`. It is present and may be empty.

The protected model set for a backend is the union of:

1. explicit models of enabled named Vibe Agents;
2. checked open-menu models;
3. every menu model with a persisted Route row, including an empty `hops` array.

The guard evaluates each protected `(backend, model)` against the post-mutation
state. It counts only runnable exact hops in that model's stored Route chain, never
eligible inventory or the backend Source order by itself. A pair with no runnable hop
appears once in `would_interrupt` or `interrupted`.

Every guarded Source/inventory mutation uses the §4.5 envelope matrix. A refusal is:

```json
{
  "ok": false,
  "contract_version": 5,
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

DELETE uses the query `force=true`; the other guarded mutations use the JSON body
`force: true`. Success returns the exact envelope selected by the authoritative matrix.

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
5. **Server-enforced native acknowledgement.** For a `native_cli` source,
   `POST …/reauth` requires `{"acknowledge_irreversible": true}`. Missing or false
   returns `reauth_confirmation_required` before any OAuth adapter call. This is
   unconditional and does not claim that a pre-login supply prediction exists.
   Hub-channel repair does not require this acknowledgement.
6. **Recovery symmetry.** Both routes return the same repair tail:
   `{source, recovered, interrupted_pairs}`. `recovered` is true exactly when the
   prior source state was `needs_action` or `error`.
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
  "contract_version": 5,
  "source": {"id": "src_relay9c1x", "kind": "api_key"},
  "recovered": true,
  "interrupted_pairs": []
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
matching `^ofn_[a-z0-9]{16,64}$`. The server echoes it on `OAuthFlow` and makes the
exact `(client_nonce, vendor, channel)` tuple idempotent while that flow exists. A retry
after a lost start response returns the same `flow_id`, current state, and presentation;
it does not invoke the provider or open a second flow. A new user action generates a
new nonce. Reusing the nonce with a different vendor or channel is a different tuple and
does not let one flow masquerade as another.

`OAuthFlow.intent` makes the terminal shape a function of the flow:

- non-terminal, failed, or canceled → `{flow}`;
- terminal `intent: "create"` → `{flow, source, added_to, adopted_by}`;
- terminal `intent: "reauth"` → `{flow, source, recovered, interrupted_pairs}`.

Status and submit return the same terminal shape:

```json
{
  "ok": true,
  "contract_version": 5,
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

In Hub mode, `AgentChain.chain` is the exact stored per-model Route chain in the same
order. Runtime does not filter or rebuild it: cooling, missing, model-unsupported,
source-blocked, and process-unavailable native CLI hops stay at their configured
positions with live annotations.
`AgentChain.current` is either null or the exact `{source_id, model_id}` identity of
the hop that is current for the next execution. Recovery changes `current` on the next
turn without changing the stored `chain` array.
Each item carries `channel`, source-global `health`, process-aware `runnable`, and
nullable `reason`. The complete axiom is:

`runnable = health-permits AND process-available`.

Process availability is definitionally true for `channel: "hub"` in v2; there is no
configuration knob for it. For `native_cli`, `reason: "native_cli_unavailable"` is an
orthogonal process fact legal at every health and always forces `runnable: false`. The
item stays visible and dimmed, and makes a fully blocked chain `interrupted`, even when
its health is `cooldown`; `reason: null` means process-available. An empty Hub chain
remains a valid `interrupted` chain.

```json
{
  "ok": true,
  "contract_version": 5,
  "chain": {
    "contract_version": 5,
    "backend": "codex",
    "model_id": "gpt-5.6",
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
  "contract_version": 5,
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
  "contract_version": 5,
  "probe": {
    "contract_version": 5,
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
  "contract_version": 5,
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
  "contract_version": 5,
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
  "contract_version": 5,
  "error": "probe_no_candidate",
  "detail": "models.probe.no_candidate.waiting",
  "supply": {
    "supply_state": "waiting",
    "retry_at": "2026-07-29T09:15:00Z"
  }
}
```

## Turn provenance absence

`TurnProvenance` is returned only for an exactly attributed Hub turn. Direct and
ambiguous absence are explicit and distinguishable from an unknown turn:

```json
{
  "ok": false,
  "contract_version": 5,
  "error": "provenance_unavailable",
  "detail": "models.provenance.direct_mode"
}
```

```json
{
  "ok": false,
  "contract_version": 5,
  "error": "provenance_unavailable",
  "detail": "models.provenance.attribution_ambiguous"
}
```

An unknown `turn_id` returns `turn_not_found`. The server derives ambiguous absence
live from “known turn, no exact record”; it does not persist a placeholder and never
guesses which attempt belonged to the turn.

## Resolution events

Events are state/feed records, not recorded fan-out. `severity` is presentation
metadata (`info` or `action_required`). Consumers derive current impact from live
source orders and named-Agent effective models.

`model_id: null` means a source-scoped system event is about the source as a whole.
Model-scoped kinds require a string. `agent: system` is invalid on backend-scoped
`supply_interrupted`. Source endpoints use canonical ids and are checked for existence
when emitted.

## Runtime installation and host support

Every runtime response carries `host_platform`, and every status carries `error_key`.
The server detects the Avibe host; clients never substitute the browser platform.
Installation is supported exactly when one `manifest.assets[].platform` equals
`host_platform`. An unsupported host leaves `health: "not_installed"`; the install
route fails before download with HTTP 422 and
`error: "runtime_platform_unsupported"`; the next status read remains truthful with
`error_key: null`.

`POST /api/models/runtime/install` is idempotent. From `not_installed` on a supported
host it clears the previous `error_key`, durably enters `installing`, starts the owned
install job, and returns that state. A reload and concurrent repeat read/return the same
`installing` state instead of starting another job. Successful verification settles at
`not_started` with `error_key: null`; it does not start the runtime. Failure settles at
`not_installed` with `error_key: "settings.models.install.fail.detail"`. The key is the
closed presentation carrier for the persisted failure; raw downloader or verifier text
remains only in scrubbed logs. `/start` never performs installation.

On service bootstrap, persisted `installing` with no worker owned by the reconstructed
process is an orphaned install, never a live-job proof. Before runtime endpoints become
ready, the server first verifies the pinned target: a complete manifest-matching binary
settles directly at `not_started`; otherwise it discards only uncommitted staging,
atomically claims the singleton install lease, and restarts the pinned installation
from scratch while retaining `installing`. Failure to claim or schedule that recovery
settles at `not_installed` with the same closed `error_key`. Thus a page reload observes
a live owned job, while a service restart cannot strand the state permanently.

## Error codes

Minimum v5 set:

`source_not_found`, `flow_not_found`, `flow_expired`, `discovery_failed`,
`invalid_source_order`, `source_last_supplier`, `source_in_route_chain`,
`source_model_in_route_chain`, `mode_switch_blocked`, `engine_down`,
`runtime_platform_unsupported`, `reauth_confirmation_required`,
`native_source_already_exists`,
`migration_item_conflict`, `turn_not_found`,
`provenance_unavailable`, `probe_no_candidate`, `direct_mode`.

`native_source_already_exists` is an API-boundary refusal for native OAuth start;
its structured sibling is `{existing_source_id}` and the adapter is not invoked.

Boundary-only action-refusal values cover operations the UI already does not offer but
a script or regression can call directly. `reauth_confirmation_required` is API/test-
only truth: it has no product copy key or rendering slot.

Removed: `invalid_priority_order`.

## Mechanical guards

JSON Schema draft-07 cannot express cross-document or live-state relations. The
contract harness and API-boundary tests enforce:

<!-- authority-consumer: credential.refresh_once credential.refresh_failed credential.refresh_rejected credential.static_unauthorized credential.account_classified credential.request_nonfallback -->
<!-- authority-consumer: turn.served turn.exhausted turn.request_nonfallback turn.engine_down turn.streamed_fallback turn.no_candidate.unconfigured turn.no_candidate.blocked turn.canceled -->
<!-- authority-consumer: mutation.source_metadata mutation.credential_replace mutation.source_refresh mutation.model_create mutation.model_efforts mutation.model_delete mutation.source_delete mutation.route_replace -->
<!-- authority-consumer: import.keep_native import.copy_key import.reauth import.controlled -->
<!-- authority-consumer: protocol anthropic openai_responses openai_chat -->
<!-- authority-consumer: observation.outcome observed ambiguous unreachable authentication_failed adapter_error timeout -->
<!-- authority-consumer: observation.discovery succeeded failed not_attempted -->
<!-- authority-consumer: runtime.health ok degraded down not_installed installing not_started -->
<!-- authority-consumer: runtime.install_error runtime_platform_unsupported -->

| Guard | Boundary |
| --- | --- |
| every example validates and JSON round-trips | contract harness |
| authority registry and mirror relations are generated from live files in the same test run; every registered closed branch has a consumer and every registered consumer resolves to one authority | `mirror-registry.json` harness |
| every non-null `then` constraint has matching `required` | contract harness |
| every `sources.order` id exists, is unique, and is eligible | config loader + source-order route |
| eligibility contains one row per source and every ordered source is eligible | AgentSupply assembler |
| every AgentSupply eligibility row carries `in_current_model_chain` and `process_availability_reason`; membership nullability follows `selected_model_id`, and only a native source may carry `native_cli_unavailable` | AgentSupply assembler |
| `AgentChain.chain` re-echoes the stored exact hops in the same order, including missing, model-unsupported, and process-unavailable native CLI items; `AgentChain.current` is null or identifies one exact hop in that array | chain assembler |
| `model_supply` has one row per menu model with unique ids | AgentSupply assembler |
| probe `source_id` names an existing source | probe assembler |
| non-null event endpoints name existing sources at emission time | event emitter |
| `channel_switch.from_source == channel_switch.to_source` | event emitter |
| API AgentSupply includes `cli_present`, `selected_by_agent`, `selected_model_id`, `selected_model_explicit`, policy-free `sources.order`, `supply_status`, `model_supply[].has_runnable_hop`, and `named_agents`; every Source includes persisted `last_discovered_at`, optional persisted `client_nonce`, derived `adopted_by`, and model retirement tombstones; source creation returns `added_to` and top-level `adopted_by` equal to `source.adopted_by`; saved refresh and discovered-model retirement use the guarded Source envelope | API payload test |
| every OAuthFlow response includes `intent` | API payload test |
| every OAuthFlow started with `client_nonce` echoes it, and repeated start of the same nonce/vendor/channel tuple returns one `flow_id` | API payload test |
| every RuntimeDependency API payload includes server-derived `host_platform` and `status.error_key`; install transitions follow the registered health matrix, page reload preserves a live `installing` job, and service bootstrap reconciles an orphan before serving runtime endpoints | API payload test |
| every `RouteHopRef` carries the one-based pre-mutation `position`; refusal and forced success report byte-identical references | API payload test |
| contract and in-repo adapter interface copies are byte-identical; the five retained-material enum members and ref-pairing predicates are mutation-tested | contract harness |

Serializer completeness follows the issue #939 pattern. Persisted fields must
round-trip through config serialization. Derived fields are exempt from persistence
but require an API-payload test.
