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
| GET `/api/models/sources` | → `{sources: Source[]}` | **v2:** an unordered asset inventory. Sources carry no position, rank, or priority field; the array order is display convenience only (never a spend order). Each source does carry immutable `created_at`, which is what 跟随推荐 sorts by — a stored fact about the source, not a position in this array. |
| POST `/api/models/sources` | `SourceCreate` (kind, vendor, base_url?, key? / oauth flow ref) → `{source: Source, adopted_by: AdoptedBy[]}` | api_key create validates + discovers models (test-and-add, frame V4 06r). The pasted key is TRANSIENT: L2 provisions it into the engine-owned store (`provision_credential`) and persists only the returned `credential_ref`; on persist failure it revokes. Secrets never enter config, logs, or any response. **v2:** the server stamps immutable `created_at` here — it is server-assigned and never accepted from the client, since a client-supplied value could reorder 跟随推荐. **v2 `adopted_by`** closes the "so what now?" loop in the same response — see below. |
| PATCH `/api/models/sources/<id>` | partial Source (display_name, base_url) → `Source` | never accepts credential material in plaintext beyond initial create |
| DELETE `/api/models/sources/<id>` | → `{ok}` | refuses while the source is the last supplier of a checked/mapped model unless `force=true`. **v2 re-scopes what "last" counts over:** per affected backend, over that backend's **enabled order** (`sources.order`) — never over eligible inventory. With per-agent ordered subsets a source can be eligible for a backend while absent from its order, so the v1 inventory scan would count an unreachable source as the replacement, allow the delete without `force`, and leave that agent `interrupted` on the next turn (`kind: supply_interrupted`, `reason: no_enabled_source`). Refuse if ANY affected backend would be left with zero enabled suppliers; the confirm copy names those backends. Also drops the id from every backend's `sources.order`. |
| POST `/api/models/sources/<id>/test` | → `{ok, discovered: n}` | re-discovery |
| GET `/api/models/agents` | → `{agents: AgentSupply[]}` | includes `current`, **v2** `sources` (policy + order + eligibility), `supply_status`, and `model_supply` per backend. Response agents[] carry server-populated read-only `builtin_models` / `standard_vendors` (integration 2026-07-24). |
| **PUT** `/api/models/agents/<backend>/sources` | `{policy, order}` → `AgentSupply` | **v2, replaces `PUT /api/models/priority`.** Authoritative: the server re-echoes the full canonical order. See semantics below. |
| PATCH `/api/models/agents/<backend>/mode` | `{mode}` → `AgentSupply` | hub⇄direct switch; never silent (plan §4) |
| PUT `/api/models/agents/<backend>/mappings` | `{mappings}` → `AgentSupply` | fixed-menu backends only |
| PUT `/api/models/agents/opencode/menu` | `{menu}` → `AgentSupply` | open menu config |
| **GET** `/api/models/agents/<backend>/chain?model=<id>` | → `AgentChain` | **v2.** The capability chain for that (agent, model) — cooling members included, flagged `runnable: false`. An empty `chain` is `ok: true` with `chain: []`, not an error. Carries `supply_state` (`ok`/`waiting`/`interrupted`) at the MODEL grain: the single answer every model-scoped consumer reads, including the probe's `detail` and `TurnProvenance.model_supply_state`, so none of them consults the backend rollup for a question it cannot answer. `model` is a **menu identifier** (see identifier rules below). |
| **POST** `/api/models/agents/<backend>/probe` | `{model?}` → `{probe: ProbeResult}` | **v2.** One minimal dry-run request through the current chain (「试跑一次」). `model` is a **menu identifier** and defaults to `current.menu_model_id`. The result is NESTED under `probe`, never spread into the envelope — see below. |
| POST `/api/models/custom-models` | `{source_id, model_id, display_name?}` → `Source` | appends manual-provenance model entry (frame V4 08) |
| DELETE `/api/models/custom-models` | `{source_id, model_id}` → `Source` | |
| GET `/api/models/events?limit=n&before=<id>` | → `{events: ResolutionEvent[]}` | adapter-owned feed (最近切换). **v2:** each event carries `severity`; the IM push layer keys off `severity == "action_required"` and never re-derives urgency from `kind`. Adds `kind: supply_interrupted`, the only agent-scoped kind — see the worked payload below. |
| POST `/api/models/oauth/start` | `{vendor, channel}` → `OAuthFlow` | runtime-declared presentation |
| GET `/api/models/oauth/status/<flow_id>` | → `OAuthFlow` | 2s polling, server holds flow |
| POST `/api/models/oauth/submit` | `{flow_id, value}` → `OAuthFlow` | value = pasted code or callback URL per `presentation.expects` |
| POST `/api/models/oauth/cancel` | `{flow_id}` → `{ok}` | |
| POST `/api/models/migration/scan` | → `MigrationScan` | read-only. Unaffected by the v2 ordering ruling — native-config import is an onboarding feature, not a priority mechanism. |
| POST `/api/models/migration/apply` | `{item_ids: []}` → `{applied: n, sources: Source[]}` | copy-only; originals untouched (tested) |
| **GET** `/api/models/turns/<turn_id>/provenance` | → `{provenance: TurnProvenance}` | **v2.** What served one turn, incl. every attempt in order (spec §4.5 turn provenance). Read-only. `turn_not_found` when the turn is unknown or predates the feature. |
| GET `/api/models/runtime/status` | → `RuntimeDependency` | engine manifest + health |

Removed in v2: `PUT /api/models/priority` (the global spend order). Its schema
file survives only as a tombstone — see `priority.schema.json`.

## Identifier rules (v2)

Two namespaces exist and mixing them silently misses:

- **menu identifier** — what the user picked and what every menu renders: the
  built-in id for fixed-menu backends (`claude-opus-4-6`), the prefixed
  `vendor/model` form for open menus (`zhipuai/glm-5.2`, per
  `opencode-overlay.md`).
- **resolved model id** — what goes upstream after mapping, always bare
  (`glm-5.2`).

Rule: **every request parameter is a menu identifier; every "what actually ran"
field is a resolved id.** So `chain?model=`, `probe {model}` and
`AgentChain.model_id` take menu identifiers, while `AgentChain.resolved_model_id`,
`ProbeResult.model_id`, `AgentSupply.current.model_id` and
`TurnProvenance.attempts[].resolved_model_id` report resolved ids.

Consequence worth stating because it is the easy bug: `current.model_id` is a
resolved id, so it is **not** a valid default for the chain query or the probe. For
OpenCode it is bare (`glm-5.2`) while the chain expects `zhipuai/glm-5.2`, and the
lookup would fail or, worse, match a different vendor's identically-named model.
Defaults therefore read `current.menu_model_id`.

## `PUT /api/models/agents/<backend>/sources` semantics

**The request body is its own shape, not an `AgentSupply.sources` instance.** In
the response object both `policy` and `order` are required, because the server
always knows the canonical order; in the request `order` is required only under
`policy: "custom"`, because under `follow` there is nothing for the client to say.
Spelling that out here rather than reusing one schema for both directions is
deliberate — a client that had to send an ignored `order` to select 跟随推荐 would
be inventing data, and the next reader could not tell whether it mattered.

Request is the whole intended state, never a delta:

```json
{ "policy": "custom", "order": ["src_anthkey01", "src_claudepro1", "src_relay9c1x"] }
```

```json
{ "policy": "follow" }
```

Validation (`invalid_source_order` on any failure, with `detail` naming the first
offending id):

1. every id exists and is not deleted;
2. every id is **eligible** for `<backend>` (§4.4 matrix — server-authoritative);
3. ids are unique;
4. it is a **subset**, not a permutation: omitting an eligible source is the
   normal way to say 未启用, and is not an error.

Policy handling:

- `{"policy": "follow"}` — `order` MUST be omitted; the server recomputes from the
  recommendation rule (own-vendor subscription → api_key by `created_at` ascending
  → id tie-break) and echoes the result. This is the wire form of 「恢复推荐顺序」.
  An `order` sent alongside `follow` is rejected with `invalid_source_order` rather
  than silently dropped: accepting and ignoring it would let a client believe it
  had set an order it did not set.
- `{"policy": "custom", "order": [...]}` — `order` is required (an empty array is
  legal and means 全部未启用); stored verbatim and never reordered by the server
  afterwards (spec §2 promise 4).
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
    "current": {
      "model_id": "claude-opus-4-6",
      "menu_model_id": "claude-opus-4-6",
      "source_id": "src_anthkey01",
      "channel": "hub"
    },
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

`position` is computed with the full recommendation rule, tie-break included, so
the server must have written the source's immutable `created_at` before answering —
`adopted_by` is the first consumer of that field.

## Probe response nesting

```json
{ "ok": true, "contract_version": 2,
  "probe": { "reachable": false, "source_id": "src_relay9c1x", "model_id": "glm-5.2",
             "latency_ms": null, "error": "models.source.needs_action.balance_exhausted",
             "via_mapping": true, "contract_version": 2, "backend": "claude" } }
```

`ProbeResult` is **nested**, and its outcome field is `reachable`, not `ok`. Both
halves of that matter: the envelope's `ok` means "the API call succeeded", while
`reachable` means "the upstream answered". A probe that cleanly establishes the
source is broken is a *successful* call reporting `reachable: false` — spreading
the object into the envelope would collide two different questions on one key, and
every client would eventually read the wrong one.

`ok: false` on this route is reserved for the call itself failing:
`probe_no_candidate`, `engine_down`, an unknown backend.

```json
{ "ok": false, "contract_version": 2, "error": "probe_no_candidate",
  "detail": { "supply_state": "waiting", "retry_at": "2026-07-29T09:15:00Z" } }
```

`detail.supply_state` is the requested model's `AgentChain.supply_state`, so the copy
can be 「该模型的来源都在冷却,约 09:15 恢复」 for `waiting` and 「该模型暂无可用来源,需处理」
for `interrupted`. `retry_at` is the earliest across that chain and is null for
`interrupted`, because nothing in it recovers on a timer.

## Agent-interrupted event payload

```json
{ "id": "evt_20260729c", "ts": "2026-07-29T06:55:10Z", "agent": "codex",
  "kind": "supply_interrupted", "model_id": "gpt-5.3-codex",
  "from_source": null, "to_source": null, "reason": "no_enabled_source",
  "severity": "action_required",
  "human_zh": "Codex:启用的来源已全部移除,现在无法运行 —— 去「模型」页加一个来源",
  "human_en": "Codex: every enabled source was removed, so it cannot run — add one on the Models page" }
```

The one event kind scoped to an **agent** rather than a source (spec §4.5). It fires
on the transition into `supply_status: interrupted` when no source changed state —
typically the last member of that backend's order being deleted or unchecked — which
is the case a source-keyed feed structurally cannot express. `from_source` and
`to_source` are null: nothing switched, the chain emptied. `severity` is
`action_required` by contract (schema conditional), never a judgement call at emit
time, so this is always a proactive push and never feed-only. Emitted once per
transition, never per starved turn.

It lives in `api.md` rather than as a `resolution-event.schema.json` example for the
same reason `severity` does — the round-trip test drives every example in that file
through the shipped v1 `ResolutionEvent`, which has no `severity` field yet (README →
required-vs-optional discipline).

## Error codes (minimum set)

`source_not_found`, `flow_not_found`, `flow_expired`, `discovery_failed`,
`invalid_source_order` (the four rules above, plus `order` sent with
`policy: "follow"`), `mapping_target_unavailable`, `mode_switch_blocked`,
`engine_down`, `consent_required` (hub-held subscription paths while the
experimental flag is unset), `migration_item_conflict`, `turn_not_found`,
`probe_no_candidate` (probe requested while this backend has no **runnable**
candidate for the model. `detail` names which case, since 「稍等即可」 and 「需处理」 are
different answers for the user — and it is read from the REQUESTED model's own chain
(`AgentChain.supply_state`), never from the backend's `supply_status` rollup. The
rollup answers for the backend's *current* model, so probing a starved non-current
menu item while the selected model is healthy would report `ok` and leave `detail`
unable to say anything true. Probing a cooling source is not offered: it would spend
a request against an account we already know is exhausted).

Removed in v2: `invalid_priority_order`.

Serializer completeness: every field in these schemas must round-trip through
`config_to_payload` (or the runtime status assembler) and is covered by the CI
completeness guards (issue #939 pattern) in the same PR that introduces it.
Derived, never-persisted fields (`supply_status`, `model_supply`,
`sources.eligibility`, `AgentChain`, `ProbeResult`, `TurnProvenance`) are exempt
from config round-tripping but must still be covered by an API-payload test.
`created_at` is the opposite case — persisted, immutable, and therefore inside the
completeness guard the moment the serializer emits it.
