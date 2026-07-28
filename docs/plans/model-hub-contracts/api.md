# Model Hub — REST API contract

All endpoints live under `/api/models/`. Envelope: success `{ok: true, ...}`;
failure `{ok: false, error: <machine_code>, detail?: <i18n key or safe text>}`.
Every response includes `contract_version: 2`. Auth follows existing UI-server
session auth; localhost curl is rejected the same way as other `/api/*` routes.

> **v2 target, not current runtime.** Contracts land before implementation
> (the v1 precedent). The shipped-but-dormant v1 serializers still emit
> `contract_version: 1` and still expose `PUT /api/models/priority`; the
> serializer PR that lands per-agent ordering bumps the envelope and removes
> that route in one change. Read this file as the destination.

| Method & path | Req / Resp schema | Notes |
| --- | --- | --- |
| GET `/api/models/sources` | → `{sources: Source[]}` | **v2:** an unordered asset inventory. Sources carry no position, rank, or priority field; the array order is display convenience only (never a spend order). |
| POST `/api/models/sources` | `SourceCreate` (kind, vendor, base_url?, key? / oauth flow ref) → `{source: Source, adopted_by: AdoptedBy[]}` | api_key create validates + discovers models (test-and-add, frame V4 06r). The pasted key is TRANSIENT: L2 provisions it into the engine-owned store (`provision_credential`) and persists only the returned `credential_ref`; on persist failure it revokes. Secrets never enter config, logs, or any response. **v2 `adopted_by`** closes the "so what now?" loop in the same response — see below. |
| PATCH `/api/models/sources/<id>` | partial Source (display_name, base_url) → `Source` | never accepts credential material in plaintext beyond initial create |
| DELETE `/api/models/sources/<id>` | → `{ok}` | refuses while source is the only supplier of a checked/mapped model unless `force=true`. Also drops the id from every backend's `sources.order`. |
| POST `/api/models/sources/<id>/test` | → `{ok, discovered: n}` | re-discovery |
| GET `/api/models/agents` | → `{agents: AgentSupply[]}` | includes `current`, **v2** `sources` (policy + order + eligibility), `supply_status`, and `model_supply` per backend. Response agents[] carry server-populated read-only `builtin_models` / `standard_vendors` (integration 2026-07-24). |
| **PUT** `/api/models/agents/<backend>/sources` | `{policy, order}` → `AgentSupply` | **v2, replaces `PUT /api/models/priority`.** Authoritative: the server re-echoes the full canonical order. See semantics below. |
| PATCH `/api/models/agents/<backend>/mode` | `{mode}` → `AgentSupply` | hub⇄direct switch; never silent (plan §4) |
| PUT `/api/models/agents/<backend>/mappings` | `{mappings}` → `AgentSupply` | fixed-menu backends only |
| PUT `/api/models/agents/opencode/menu` | `{menu}` → `AgentSupply` | open menu config |
| **GET** `/api/models/agents/<backend>/chain?model=<id>` | → `AgentChain` | **v2.** The effective chain for that (agent, model). An empty `chain` is `ok: true` with `chain: []`, not an error. |
| **POST** `/api/models/agents/<backend>/probe` | `{model?}` → `ProbeResult` | **v2.** One minimal dry-run request through the current chain (「试跑一次」). `model` defaults to the backend's current model. |
| POST `/api/models/custom-models` | `{source_id, model_id, display_name?}` → `Source` | appends manual-provenance model entry (frame V4 08) |
| DELETE `/api/models/custom-models` | `{source_id, model_id}` → `Source` | |
| GET `/api/models/events?limit=n&before=<id>` | → `{events: ResolutionEvent[]}` | adapter-owned feed (最近切换). **v2:** each event carries `severity`; the IM push layer keys off `severity == "action_required"` and never re-derives urgency from `kind`. |
| POST `/api/models/oauth/start` | `{vendor, channel}` → `OAuthFlow` | runtime-declared presentation |
| GET `/api/models/oauth/status/<flow_id>` | → `OAuthFlow` | 2s polling, server holds flow |
| POST `/api/models/oauth/submit` | `{flow_id, value}` → `OAuthFlow` | value = pasted code or callback URL per `presentation.expects` |
| POST `/api/models/oauth/cancel` | `{flow_id}` → `{ok}` | |
| POST `/api/models/migration/scan` | → `MigrationScan` | read-only. Unaffected by the v2 ordering ruling — native-config import is an onboarding feature, not a priority mechanism. |
| POST `/api/models/migration/apply` | `{item_ids: []}` → `{applied: n, sources: Source[]}` | copy-only; originals untouched (tested) |
| GET `/api/models/runtime/status` | → `RuntimeDependency` | engine manifest + health |

Removed in v2: `PUT /api/models/priority` (the global spend order). Its schema
file survives only as a tombstone — see `priority.schema.json`.

## `PUT /api/models/agents/<backend>/sources` semantics

Request is the whole intended state, never a delta:

```json
{ "policy": "custom", "order": ["src_anthkey01", "src_claudepro1", "src_relay9c1x"] }
```

Validation (`invalid_source_order` on any failure, with `detail` naming the first
offending id):

1. every id exists and is not deleted;
2. every id is **eligible** for `<backend>` (§4.4 matrix — server-authoritative);
3. ids are unique;
4. it is a **subset**, not a permutation: omitting an eligible source is the
   normal way to say 未启用, and is not an error.

Policy handling:

- `{"policy": "follow"}` — the `order` field is **ignored**; the server recomputes
  from the recommendation rule (own native subscription → api_key by creation time
  ascending) and echoes the result. This is the wire form of 「恢复推荐顺序」.
- `{"policy": "custom", "order": [...]}` — stored verbatim and never reordered by
  the server afterwards (spec §2 promise 4).
- Sending `policy: "custom"` with an `order` equal to the current recommendation is
  still a fork to custom — the user asked to own it.

Response is the full `AgentSupply` for that backend, so one round trip refreshes
the drawer, the row's order chips, and `supply_status`.

```json
{
  "ok": true,
  "contract_version": 2,
  "agent": {
    "backend": "claude",
    "mode": "hub",
    "menu_kind": "fixed",
    "current": { "model_id": "claude-opus-4-6", "source_id": "src_anthkey01", "channel": "hub" },
    "sources": {
      "policy": "custom",
      "order": ["src_anthkey01", "src_claudepro1", "src_relay9c1x"],
      "eligibility": [
        { "source_id": "src_anthkey01", "eligible": true, "reason_key": null },
        { "source_id": "src_claudepro1", "eligible": true, "reason_key": null },
        { "source_id": "src_relay9c1x", "eligible": true, "reason_key": null },
        { "source_id": "src_chatgptplus", "eligible": false,
          "reason_key": "models.eligibility.subscription_wrong_client" }
      ]
    },
    "supply_status": "degraded",
    "model_supply": [
      { "model_id": "claude-opus-4-6", "chain_length": 2 },
      { "model_id": "claude-haiku-4-5", "chain_length": 0 }
    ]
  }
}
```

## `adopted_by` on source create

```json
{ "ok": true, "contract_version": 2,
  "source": { "id": "src_zhipukey01", "kind": "api_key", "...": "..." },
  "adopted_by": [ { "backend": "codex", "policy": "follow", "position": 2 } ] }
```

One entry per backend that picked the new source up **automatically** — i.e. those
on `policy: "follow"`, where `position` is its 1-based slot in the recomputed
order. Backends on `custom` are absent from the list, which is exactly the set the
success state offers one-tap enable for (「Claude Code 未启用此来源」). Eligible-for-nobody
sources yield `adopted_by: []`.

## Error codes (minimum set)

`source_not_found`, `flow_not_found`, `flow_expired`, `discovery_failed`,
`invalid_source_order` (see the four rules above), `mapping_target_unavailable`,
`mode_switch_blocked`, `engine_down`, `consent_required` (hub-held subscription
paths while the experimental flag is unset), `migration_item_conflict`,
`probe_no_candidate` (probe requested while this backend's effective chain is
empty — `supply_status: "interrupted"`).

Removed in v2: `invalid_priority_order`.

Serializer completeness: every field in these schemas must round-trip through
`config_to_payload` (or the runtime status assembler) and is covered by the CI
completeness guards (issue #939 pattern) in the same PR that introduces it.
Derived, never-persisted fields (`supply_status`, `model_supply`,
`sources.eligibility`, `AgentChain`, `ProbeResult`) are exempt from config
round-tripping but must still be covered by an API-payload test.
