# Model Hub — REST API contract

Status: **FROZEN v4 (targeted)**. All endpoints live under `/api/models/`.

Success envelope: `{ok: true, contract_version: 3, ...}`.
Failure envelope:
`{ok: false, contract_version: 3, error: <machine_code>, detail?: <i18n_key>}`.
`detail` is always a string. Structured error data lives in a named sibling.
Authentication and CSRF rules are the existing UI-server rules.

The shared envelope remains v3. Targeted v4 amendments version their changed nested
schema objects independently; the channel class pass also tightens the service-side
OAuth seam and adds derived fields to the existing AgentSupply/source-creation
responses without changing the envelope version.

## Route table

| Method and path | Request → response | Normative notes |
| --- | --- | --- |
| GET `/api/models/sources` | → `{sources: Source[]}` | Unordered asset inventory. Array order is never a spend order. |
| POST `/api/models/sources` | `SourceCreate` → `{source: Source, adopted_by: AdoptedBy[], skipped_by: SkippedBy[]}` | The server assigns `id` and immutable `created_at`; plaintext keys are transient. |
| PATCH `/api/models/sources/<id>` | `{display_name?, base_url?}` → `{source: Source}` | Metadata only. |
| PUT `/api/models/sources/<id>/credential` | `{key, force?: boolean}` → `{source, recovered, interrupted_pairs}` | API-key replacement. The optional `force` is a JSON body field, never a query parameter. |
| POST `/api/models/sources/<id>/reauth` | `{acknowledge_irreversible?: true}` → `{flow: OAuthFlow}` | A `native_cli` source requires the acknowledgement before OAuth starts. See repair rules. |
| DELETE `/api/models/sources/<id>?force=<bool>` | → `{ok}` or `source_last_supplier` + `would_interrupt` | Guard evaluates the post-delete enabled orders. |
| POST `/api/models/sources/<id>/refresh` | → `{source: Source, discovered: integer}` | Explicit source re-discovery, including blocked-source recovery. The returned source contains the replacement model list and successful `last_discovered_at`. |
| GET `/api/models/agents` | → `{agents: AgentSupply[]}` | Backend records include `named_agents`, the enabled named-Agent live projection. |
| GET `/api/models/agents/<backend>/sources` | → `{agent: AgentSupply}` | Returns the authoritative effective order and eligibility. |
| PUT `/api/models/agents/<backend>/sources` | source-order request → `{agent: AgentSupply}` | Full canonical order is re-echoed. |
| PATCH `/api/models/agents/<backend>/mode` | `{mode}` → `{agent: AgentSupply}` | Explicit `hub` / `direct` switch. |
| PUT `/api/models/agents/<backend>/mappings` | `{mappings}` → `{agent: AgentSupply}` | Fixed-menu backends only. |
| PUT `/api/models/agents/opencode/menu` | `{menu}` → `{agent: AgentSupply}` | Open-menu configuration. |
| GET `/api/models/agents/<backend>/chain?model=<id>` | → `{chain: AgentChain}` | Hub only. Direct returns the documented `direct_mode` error. |
| POST `/api/models/agents/<backend>/probe` | `{model?}` → `{probe: ProbeResult}` | Hub only. Direct returns the same `direct_mode` error. |
| POST `/api/models/custom-models` | `{source_id, model_id, display_name?}` → `{source: Source}` | Adds manual provenance. |
| DELETE `/api/models/custom-models` | `{source_id, model_id}` → `{source: Source}` | Removes only the named manual model. |
| GET `/api/models/events?limit=<n>&before=<id>` | → `{events: ResolutionEvent[]}` | Bounded source-resolution feed. |
| POST `/api/models/oauth/start` | `{vendor, channel}` → `{flow: OAuthFlow}` | Starts creation of a new subscription source. |
| GET `/api/models/oauth/status/<flow_id>` | → OAuth result | Terminal create and reauth shapes are below. |
| POST `/api/models/oauth/submit` | `{flow_id, value}` → OAuth result | Same terminal shape as status. |
| POST `/api/models/oauth/cancel` | `{flow_id}` → `{ok}` | Cancels and forgets the flow. |
| POST `/api/models/migration/scan` | → `{scan: MigrationScan}` | Read-only. |
| POST `/api/models/migration/apply` | `{item_ids: string[]}` → `{applied, sources}` | Imported sources auto-join eligible follow orders; custom orders stay frozen. |
| GET `/api/models/turns/<turn_id>/provenance` | → `{provenance: TurnProvenance}` or documented absence error | Debug read for exactly attributed Hub turns. |
| GET `/api/models/runtime` | → `RuntimeDependency` status | Managed engine status. |

The removed global route `PUT /api/models/priority` has no v3 replacement.
Ordering is backend-owned through the sources routes.

## Identifier rules

- `Source.id` and every non-null source reference match
  `^src_[a-z0-9]{8,}$`.
- The API boundary also verifies referential existence where a new reference is
  emitted or accepted. JSON Schema can validate format, not cross-document existence.
- Claude and Codex requests use fixed-menu built-in ids.
- OpenCode requests use a prefixed `vendor/model` menu id. The prefix chooses the
  provider; the resolver sends the bare model id upstream.
- A mapping rewrites a menu id to an upstream model id. A mapping never chooses a
  source.
- Without an explicit mapping, a vendor-native fixed menu may resolve a stable menu
  alias per source. Claude version aliases select the latest dated id for that exact
  version; `opus`, `sonnet`, `haiku`, `opus[1m]`, and `sonnet[1m]` select the latest
  discovered id in their family. A dated request remains exact. Alias candidates come
  only from that source's discovered inventory; manual models, foreign-vendor sources,
  and undiscovered passthrough ids do not qualify. A native CLI source preserves an
  exact CLI alias instead of replacing it with a bundled-catalog model. Explicit
  mappings take precedence.
- Source event references are checked when emitted. Retained feed entries remain
  valid after a later legal source deletion.

## AgentSupply read projection

Each backend entry on `GET /api/models/agents` carries the v3 API-boundary keys:

```json
{
  "backend": "claude",
  "mode": "hub",
  "selected_by_agent": "pm",
  "selected_model_id": "claude-opus-4-6",
  "selected_model_explicit": true,
  "sources": {
    "policy": "follow",
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
    {"model_id": "claude-opus-4-6", "chain_length": 2}
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
`model_supply` carries only `chain_length`.

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

Request shapes are total:

```json
{"policy": "follow"}
```

```json
{"policy": "custom", "order": ["src_anthkey01", "src_relay9c1x"]}
```

- `follow` accepts no `order`; the server recomputes the live recommended order.
- `custom` requires the complete desired ordered subset. The empty subset is legal.
- Any manual order mutation changes policy to `custom`.
- Restoring `follow` is the explicit restore-to-recommendation operation.
- Every id is unique, exists, and is eligible for the backend. Otherwise the route
  returns `invalid_source_order`.
- New eligible sources auto-join `follow` at their recommended position.
- A `custom` order never changes because a source was created or imported.

Eligibility is server-authoritative. An ineligible row carries exactly one closed
`reason_key` from `agent-supply.schema.json`; an eligible row carries null.

### Mapping and menu enrollment

A mapping or open-menu target is accepted only when its selected source is enrolled
in the backend's post-mutation effective order. Under `follow`, the effective order
is `ModelHubConfig.effective_source_order`'s recommendation and is exhaustive over
eligible sources, so an eligible-but-non-enrolled target is representable only under
`custom`. Acceptance auto-appends that source to the custom order, and the confirm
step must surface the append. Acceptance never converts policy: a selection that
does not change the order is not an order edit. An ineligible source is never
auto-enrolled and the request fails with `mapping_target_unavailable`. Evidence at
master `05f72ae5`:
`service.py:1955-1985` validates targets against eligible inventory but does not
yet require or edit effective-order enrollment; the follow-up consumer lane owns
that acceptance change.

## Source creation outcome

`AdoptedBy` is:

```json
{"backend": "claude", "policy": "follow", "position": 2}
```

`position` is one-based in the effective order after commit. The array contains only
eligible `follow` backends that actually adopted the source and is frozen with the
source response. `custom` backends are absent and retain their prior order.

`SkippedBy` is:

```json
{"backend": "codex", "reason": "custom_order"}
```

Its closed backend vocabulary is `claude | codex | opencode`; its reason vocabulary
has one v2 member, `custom_order`. The array contains every eligible custom backend
that did not adopt the new source. Ineligible backends appear in neither array, so a
consumer can distinguish “eligible but deliberately skipped” from “cannot use this
source” without reimplementing the compatibility matrix. Evidence at master
`05f72ae5`: `service.py:767-783` emits only follow-policy adoption, collapsing
eligible custom skips into the same absence as ineligibility.

The terminal result of both ordinary API-key creation and OAuth creation is:

```json
{
  "ok": true,
  "contract_version": 3,
  "source": {"id": "src_anthkey01", "kind": "api_key"},
  "adopted_by": [
    {"backend": "claude", "policy": "follow", "position": 2}
  ],
  "skipped_by": [
    {"backend": "codex", "reason": "custom_order"}
  ]
}
```

The public HTTP wrapper, RPC layer, and client pass this shape through directly; no
extra `source` or `flow` nesting is added.

## Supply guard

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
3. the menu-side `builtin_id` of mappings whose `enabled` is true.

The guard evaluates each protected `(backend, model)` against the post-mutation
state. It counts only runnable suppliers in that backend's enabled effective order,
never eligible inventory outside the order. A pair with no runnable supplier appears
once in `would_interrupt` or `interrupted_pairs`.

DELETE and elective replacement of a healthy API key refuse with:

```json
{
  "ok": false,
  "contract_version": 3,
  "error": "source_last_supplier",
  "would_interrupt": [
    {
      "backend": "claude",
      "model_id": "claude-opus-4-6",
      "agents": ["pm"]
    }
  ]
}
```

DELETE uses the query `force=true`. API-key replacement uses the JSON body
`force: true`. A blocked-source repair proceeds and reports the remaining gaps in
`interrupted_pairs`; refusing it would prevent recovery from the state the route
exists to repair.

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
   the prior source strictly. `flow_source_ref` is the irreversible v4b case:
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
  "contract_version": 3,
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
sets the source to `standby`. The response returns that complete updated source and
the discovered count; clients do not reconstruct the model list from the count.
`last_discovered_at` advances only on this successful replacement. A classified
failure updates the source-global state, preserves the last successful model list and
timestamp, and returns the normal safe error. This route is the only refresh/recovery
operation; there is no parallel “test” or “recover” endpoint.

## OAuth completion

`OAuthFlow.intent` makes the terminal shape a function of the flow:

- non-terminal, failed, or canceled → `{flow}`;
- terminal `intent: "create"` → `{flow, source, adopted_by, skipped_by}`;
- terminal `intent: "reauth"` → `{flow, source, recovered, interrupted_pairs}`.

Status and submit return the same terminal shape:

```json
{
  "ok": true,
  "contract_version": 3,
  "flow": {
    "flow_id": "oaf_claude01",
    "intent": "create",
    "state": "success",
    "source_id": "src_claudepro1"
  },
  "source": {"id": "src_claudepro1", "kind": "subscription"},
  "adopted_by": [
    {"backend": "claude", "policy": "follow", "position": 1}
  ],
  "skipped_by": []
}
```

`adopted_by` is absent for reauth because the existing source order did not change.

## Chain and probe

In Hub mode, `AgentChain.chain` is the effective source order filtered by backend
eligibility and the server's per-source effective-model derivation. That derivation
applies an explicit mapping first, otherwise applies the vendor-native alias rule above,
then verifies the resulting id against source inventory. Cooling, source-blocked and
process-unavailable native CLI members stay in the chain at their original positions.
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
  "contract_version": 3,
  "chain": {
    "contract_version": 4,
    "backend": "codex",
    "model_id": "gpt-5.6",
    "chain": [
      {
        "source_id": "src_chatgptplus",
        "channel": "native_cli",
        "via_mapping": false,
        "resolved_model_id": null,
        "health": "healthy",
        "runnable": false,
        "reason": "native_cli_unavailable",
        "retry_at": null
      }
    ],
    "supply_state": "interrupted"
  }
}
```

In Direct mode both chain and probe refuse with:

```json
{
  "ok": false,
  "contract_version": 3,
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
  "contract_version": 3,
  "probe": {
    "contract_version": 4,
    "backend": "claude",
    "channel": "hub",
    "reachable": false,
    "source_id": "src_relay9c1x",
    "model_id": "glm-5.2",
    "latency_ms": 287,
    "via_mapping": true,
    "error": "models.source.needs_action.balance_exhausted"
  }
}
```

The probe walks the same §4.3 chain order and selects the first runnable item;
items already marked unavailable are never probed. For `channel: "hub"`, that
candidate keeps v3's total request-result truth table verbatim. For
`channel: "native_cli"`, the probe re-verifies process readiness after selection;
the fact may have gone stale, so an available candidate can honestly return
not-ready. `reachable` is READINESS, not completion evidence: no upstream call is
timed, so `latency_ms` is null in both directions. Ready carries `error: null`;
not-ready carries the closed i18n key `models.probe.native_cli_unavailable`.

```json
{
  "contract_version": 4,
  "backend": "codex",
  "channel": "native_cli",
  "reachable": true,
  "source_id": "src_chatgptplus",
  "model_id": "gpt-5.6",
  "latency_ms": null,
  "via_mapping": false,
  "error": null
}
```

```json
{
  "contract_version": 4,
  "backend": "codex",
  "channel": "native_cli",
  "reachable": false,
  "source_id": "src_chatgptplus",
  "model_id": "gpt-5.6",
  "latency_ms": null,
  "via_mapping": false,
  "error": "models.probe.native_cli_unavailable"
}
```

No candidate is an API error with a typed model-scoped state:

```json
{
  "ok": false,
  "contract_version": 3,
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
  "contract_version": 3,
  "error": "provenance_unavailable",
  "detail": "models.provenance.direct_mode"
}
```

```json
{
  "ok": false,
  "contract_version": 3,
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

## Error codes

Minimum v3 set:

`source_not_found`, `flow_not_found`, `flow_expired`, `discovery_failed`,
`invalid_source_order`, `source_last_supplier`, `mapping_target_unavailable`,
`mode_switch_blocked`, `engine_down`, `consent_required`,
`reauth_confirmation_required`, `migration_item_conflict`, `turn_not_found`,
`provenance_unavailable`, `probe_no_candidate`, `direct_mode`.

Removed: `invalid_priority_order`.

## Mechanical guards

JSON Schema draft-07 cannot express cross-document or live-state relations. The
contract harness and API-boundary tests enforce:

| Guard | Boundary |
| --- | --- |
| every example validates and JSON round-trips | contract harness |
| mirror registry equality/projection/partition/bijection/mapping, including mutation probes | `mirror-registry.json` harness |
| every non-null `then` constraint has matching `required`, except a declared fail-safe legacy-example exception | contract harness |
| every `sources.order` id exists, is unique, and is eligible | config loader + source-order route |
| eligibility contains one row per source and every ordered source is eligible | AgentSupply assembler |
| every AgentSupply eligibility row carries `in_current_model_chain` and `process_availability_reason`; membership nullability follows `selected_model_id`, and only a native source may carry `native_cli_unavailable` | AgentSupply assembler |
| `AgentChain.chain` source ids are unique and preserve effective order, including process-unavailable native CLI items | chain assembler |
| `model_supply` has one row per menu model with unique ids | AgentSupply assembler |
| probe `source_id` names an existing source | probe assembler |
| non-null event endpoints name existing sources at emission time | event emitter |
| `channel_switch.from_source == channel_switch.to_source` | event emitter |
| API AgentSupply includes `selected_by_agent`, `selected_model_id`, `selected_model_explicit`, `sources`, `supply_status`, `model_supply`, and `named_agents`; every Source includes persisted `last_discovered_at`; source creation returns both `adopted_by` and `skipped_by`; source refresh returns the updated Source plus count | API payload test |
| every OAuthFlow response includes `intent` | API payload test |
| contract and in-repo adapter interface copies are byte-identical; the five retained-material enum members and ref-pairing predicates are mutation-tested | contract harness |

Serializer completeness follows the issue #939 pattern. Persisted fields must
round-trip through config serialization. Derived fields are exempt from persistence
but require an API-payload test.
