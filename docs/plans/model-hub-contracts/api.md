# Model Hub — REST API contract

All endpoints live under `/api/models/`. Envelope: success `{ok: true, ...}`;
failure `{ok: false, error: <machine_code>, detail?: <i18n key or safe text>}`.
Every response includes `contract_version: 2`. Auth follows existing UI-server
session auth; localhost curl is rejected the same way as other `/api/*` routes.

**`detail` is a string, and structured failure data goes in a named sibling
key** (normative, 07-29 review round 3). The shipped client types it as one —
`ApiErr.detail?: string` and `ApiCallError.detail?: string`
(`ui/src/components/settings/models/types.ts`, `modelsApi.ts`) — and every
existing producer fills it with an i18n key or a safe fragment. An error that
needs a payload therefore adds its own typed field next to `error`, exactly as
success responses nest `probe` / `agent` / `provenance`, instead of widening
`detail` to `string | object`. A union there would break both live consumers on
the first structured error and force every future reader to type-check a field
whose only job is to be rendered.

> **v2 target, not current runtime.** Contracts land before implementation
> (the v1 precedent). The shipped-but-dormant v1 serializers still emit
> `contract_version: 1` and still expose `PUT /api/models/priority`; the
> serializer PR that lands per-agent ordering bumps the envelope and removes
> that route in one change. Read this file as the destination.

| Method & path | Req / Resp schema | Notes |
| --- | --- | --- |
| GET `/api/models/sources` | → `{sources: Source[]}` | **v2:** an unordered asset inventory. Sources carry no position, rank, or priority field; the array order is display convenience only (never a spend order). Each source does carry immutable `created_at`, which is what 跟随推荐 sorts by — a stored fact about the source, not a position in this array. |
| POST `/api/models/sources` | `SourceCreate` (kind, vendor, base_url?, key? / oauth flow ref) → `{source: Source, adopted_by: AdoptedBy[]}` | api_key create validates + discovers models (test-and-add, frame V4 06r). The pasted key is TRANSIENT: L2 provisions it into the engine-owned store (`provision_credential`) and persists only the returned `credential_ref`; on persist failure it revokes. Secrets never enter config, logs, or any response. **v2:** the server stamps immutable `created_at` here — it is server-assigned and never accepted from the client, since a client-supplied value could reorder 跟随推荐. **v2 `adopted_by`** closes the "so what now?" loop in the same response — see below. |
| PATCH `/api/models/sources/<id>` | partial Source (display_name, base_url) → `Source` | metadata only — never credential material. Replacement has its own route below, because "rename it" and "hand me a new key" are different operations with different failure modes. |
| **PUT** `/api/models/sources/<id>/credential` | `{key}` → `{source: Source, recovered: boolean, interrupted_pairs: SupplyGap[]}` | **v2 (07-29, review round 3).** Replaces the credential of an existing `api_key` source in place. **The sequence, the response shape and the refusal case are defined once, under 「Credential replacement and re-auth」 below** — rewritten there 07-29, review round 6, because three rounds of corrections had left this cell carrying two incompatible orderings of the same steps, the leading one unimplementable against the frozen adapter. **This route exists because spec §4.5 promises the interrupted-source card a one-tap 「换一个 Key」 and there was nowhere to send that tap:** create-a-new-source is not the same operation — it strands the old row, drops the source's position in every backend's `sources.order`, and loses its `created_at`, so 跟随推荐 silently reshuffles as a side effect of fixing a key. Replacement keeps the identity and the order. Same transient-secret rule as create: the plaintext never enters config, logs, or any response. |
| **POST** `/api/models/sources/<id>/reauth` | → `{flow: OAuthFlow}` | **v2 (07-29, review round 3).** The subscription counterpart: re-runs OAuth **bound to an existing source** rather than creating one, for `credential_expired` / `credential_revoked`. Thin by design — it is `POST /api/models/oauth/start` with `source_id` instead of `{vendor, channel}`, and the adapter surface it needs already exists (`start_oauth(source_id)` with deterministic source binding, adapter-interface v1.1). The flow then polls `/api/models/oauth/status/<flow_id>` like any other; its completion response is defined under 「OAuth completion responses」 below and its repair tail under 「Credential replacement and re-auth」, which is where the channel-specific rule for where the refreshed credential lands also lives. |
| DELETE `/api/models/sources/<id>` | → `{ok}`, or `{ok: false, error: "source_last_supplier", would_interrupt: SupplyGap[]}` | refuses while the source is the last supplier of a SELECTED model unless `force=true` — "selected" is wider than 「已勾选/已映射」, see the round-5 note at the end of this cell. **v2 re-scopes what "last" counts over:** per affected backend, over that backend's **enabled order** (`sources.order`) — never over eligible inventory. With per-agent ordered subsets a source can be eligible for a backend while absent from its order, so the v1 inventory scan would count an unreachable source as the replacement, allow the delete without `force`, and leave that agent `interrupted` on the next turn (`kind: supply_interrupted`, `reason: no_enabled_source`). **The guard is per (backend, model), not per backend** (corrected 07-29, review round 3): refuse if the delete would leave ANY protected model of ANY affected backend with no runnable supplier, and the confirm copy names those (backend, model) pairs. **Which models are protected, what 「no runnable supplier」 means, and what the refusal carries are ONE definition, stated once under 「The supply guard」 below** (07-29, review round 7) — this cell and the credential routes reference it rather than restating it. Round 6 wrote the test here as `model_supply[].chain_length` becoming 0, which is structural emptiness and a strictly weaker question than the one the guard asks: a chain still holding one revoked key is not empty and cannot serve, so the delete passed unguarded and the agent went `interrupted` on its next turn — the exact surprise this guard exists to prevent, reintroduced by counting rows instead of asking whether any row could run. Round 2 wrote it as "zero enabled suppliers" for the backend, which is a strictly weaker test than the sentence it was meant to implement: a backend with four enabled sources loses nothing by that test, yet deleting the only one that supplies `claude-haiku-4-5` silently starves that model and the user finds out on the next turn. Backend-level emptiness is simply the special case where every checked model hits zero at once. **"checked/mapped" is the wrong protected set — it is every SELECTED model** (corrected 07-29, review round 5): that phrase came from the V4 fixed-menu drawer, where a model earns protection by being checked in the menu or by owning a mapping row. Both tests miss the model a backend is actually running. `agents.<backend>.default_model` is a selection in its own right; so is `agents.<name>.model` for each enabled Vibe Agent; and a fixed-menu model that resolves by IDENTITY (`selected_model_id == resolved_model_id`, no mapping row — `agent-chain.schema.json` → `via_mapping: false`) is mapped in effect while being invisible to any mapping scan. Composed, those gaps let a source be the only supplier of the model the user's `pm` Agent runs on, hold no mapping row, sit unchecked in a menu the user never opened, and be deleted without `force` — the interruption arriving on `pm`'s next turn as `kind: supply_interrupted`, `reason: no_enabled_source`, which is the class of surprise this guard exists to prevent. The protected set is therefore a UNION of four selection facts, enumerated with its identifier namespace under 「The supply guard」 below — where 「mapping targets」 is corrected to the mapping's MENU side (07-29, review round 7): a mapping row rewrites `claude-opus-4-6 → glm-5.2`, so protecting the *target* protects a resolved id the user never selected, loses the built-in model the selection actually names, and coalesces every menu model mapped onto one upstream id into a single gap the client cannot attribute. The fourth term is the frozen per-Agent grain doing its job (see `selected_by_agent` above): with per-Agent ordered subsets there is no single "the backend's model" to test, so the guard has to close over the Agents — and the confirm copy names the Agents, not only the pairs, because 「删除后 pm 将没有可用来源」 is a sentence the user can act on while 「claude / claude-opus-4 将没有可用来源」 is not, when four Agents share that backend. Disabled Agents are deliberately excluded — they cannot take a turn, so their model is not a live capability — but a disabled Agent's model that is ALSO the backend default stays protected through the default term, which is the overlap the union is built to absorb. `force=true` still overrides the whole set; widening the guard changes what the user is warned about, never what they are forbidden. **The refusal is machine-readable** (07-29, review round 6): round 5 promised confirm copy naming the pairs and the Agents but declared no shape for either, so the client had nothing to render it from and no code to switch on. The refusal is `ok: false`, `error: "source_last_supplier"`, with the affected pairs in `would_interrupt` — the `SupplyGap` shape defined under 「The supply guard」 below, whose `agents` member is what 「删除后 pm 将没有可用来源」 is composed from. Also drops the id from every backend's `sources.order`. |
| POST `/api/models/sources/<id>/test` | → `{ok, discovered: n}` | re-discovery |
| GET `/api/models/agents` | → `{agents: AgentSupply[]}` | includes `current`, **v2** `sources` (policy + order + eligibility), `supply_status`, and `model_supply` per backend. Response agents[] carry server-populated read-only `builtin_models` / `standard_vendors` (integration 2026-07-24). |
| **PUT** `/api/models/agents/<backend>/sources` | `{policy, order}` → `AgentSupply` | **v2, replaces `PUT /api/models/priority`.** Authoritative: the server re-echoes the full canonical order. See semantics below. |
| PATCH `/api/models/agents/<backend>/mode` | `{mode}` → `AgentSupply` | hub⇄direct switch; never silent (plan §4) |
| PUT `/api/models/agents/<backend>/mappings` | `{mappings}` → `AgentSupply` | fixed-menu backends only |
| PUT `/api/models/agents/opencode/menu` | `{menu}` → `AgentSupply` | open menu config |
| **GET** `/api/models/agents/<backend>/chain?model=<id>` | → `AgentChain` | **v2.** The capability chain for that (agent, model) — cooling members included, flagged `runnable: false`. An empty `chain` is `ok: true` with `chain: []`, not an error. Carries `supply_state` (`ok`/`waiting`/`interrupted`) at the MODEL grain: the single answer every model-scoped consumer reads, including the probe's `supply` sibling and `TurnProvenance.model_supply_state`, so none of them consults the backend rollup for a question it cannot answer. The `waiting`/`interrupted` split is spec §4.5's predicate, stated there once — `interrupted` when at least one blocker needs the user, `waiting` only when every blocker is a cooldown. `model` is a **menu identifier** (see identifier rules below). |
| **POST** `/api/models/agents/<backend>/probe` | `{model?}` → `{probe: ProbeResult}` | **v2.** One minimal dry-run request through the current chain (「试跑一次」). `model` is a **menu identifier** and defaults to `selected_model_id`. The result is NESTED under `probe`, never spread into the envelope — see below. |
| POST `/api/models/custom-models` | `{source_id, model_id, display_name?}` → `Source` | appends manual-provenance model entry (frame V4 08) |
| DELETE `/api/models/custom-models` | `{source_id, model_id}` → `Source` | |
| GET `/api/models/events?limit=n&before=<id>` | → `{events: ResolutionEvent[]}` | adapter-owned feed (最近切换). **v2:** each event carries `severity`; the IM push layer keys off `severity == "action_required"` and never re-derives urgency from `kind`. **That field decides WHETHER to interrupt, not WHOM** (07-29, review round 5): no event carries a recipient, channel, or platform, and recipients are resolved at push time from the event's `agent` against the live routing table — spec §4.5 → 「Who receives an action-required push」 is normative, and this feed stays a record of what happened to supply rather than an outbox. Adds `kind: supply_interrupted`, the only agent-scoped kind — see the worked payload below. |
| POST `/api/models/oauth/start` | `{vendor, channel}` → `{flow: OAuthFlow}` | runtime-declared presentation. Creates a NEW source on success; re-authenticating an existing one is `POST /api/models/sources/<id>/reauth`. **v2 stamps `flow.intent`** so the completion response shape is a total function of the flow — see 「OAuth completion responses」 below. |
| GET `/api/models/oauth/status/<flow_id>` | → `{flow: OAuthFlow}`, plus the completion fields on the terminal success poll | 2s polling, server holds flow. **The terminal poll carries the flow's OUTCOME, not only its state** (07-29, review round 6) — the created source and its `adopted_by` for a create flow, the repair result for a reauth flow. See below. |
| POST `/api/models/oauth/submit` | `{flow_id, value}` → same shape as `status` | value = pasted code or callback URL per `presentation.expects`. A submit that terminates the flow carries the same completion fields, so a form-A paste needs no extra poll to learn what it produced. |
| POST `/api/models/oauth/cancel` | `{flow_id}` → `{ok}` | |
| POST `/api/models/migration/scan` | → `MigrationScan` | read-only. Unaffected by the v2 ordering ruling — native-config import is an onboarding feature, not a priority mechanism. |
| POST `/api/models/migration/apply` | `{item_ids: []}` → `{applied: n, sources: Source[]}` | copy-only; originals untouched (tested) |
| **GET** `/api/models/turns/<turn_id>/provenance` | → `{provenance: TurnProvenance}` | **v2.** What served one turn, incl. every attempt in order (spec §4.5 turn provenance). `outcome` has FOUR values, not three (07-29 round 3): `served` / `exhausted` / `failed_terminal` / `no_candidate`, where `failed_terminal` covers the non-fallback errors of spec §4.3 — param, protocol, tool-compat, and anything after the first streamed token — recorded in a `terminal_error` slot beside `served`. Read-only. `turn_not_found` when the turn is unknown or predates the feature. |
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
field is a resolved id.** So `chain?model=`, `probe {model}`,
`AgentChain.model_id`, `AgentSupply.selected_model_id` and
`TurnProvenance.requested_model_id` take menu identifiers, while
`AgentChain.resolved_model_id`, `ProbeResult.model_id`,
`AgentSupply.current.model_id` and every `resolved_model_id` in
`TurnProvenance` — `failed_attempts[]`, `served`, `terminal_error` — report
resolved ids. (Round 2 replaced that record's single `attempts[]` array with
those three slots; the reference here was not updated with it.)

Consequence worth stating because it is the easy bug: `current.model_id` is a
resolved id, so it is **not** a valid default for the chain query or the probe. For
OpenCode it is bare (`glm-5.2`) while the chain expects `zhipuai/glm-5.2`, and the
lookup would fail or, worse, match a different vendor's identically-named model.
Defaults therefore read `AgentSupply.selected_model_id`, which is also the only
one of the two that still exists when nothing is runnable — `current` is null in
precisely the `waiting` / `interrupted` states where the drawer most needs to name
the selected model (round 3 hoisted it out of `current` for that reason).

### `selected_model_id` answers for ONE route, not for the backend

`AgentSupply` is keyed by backend, so `selected_model_id` reads as "the model this
backend will use". It is not (grain corrected 07-29, review round 4). A Vibe Agent
row in SQLite carries **both** `backend` and `model`, so `pm` and
`evm-contract-audit` can share the `claude` backend on different models, and
routing follows the selected Vibe Agent (`AGENTS.md` → Agent routing model). One
backend therefore has as many "selected models" as it has Agents.

What the shipped projection actually resolves is one specific route:
`core/controller.py::default_vibe_agent_model` takes the **global default** Vibe
Agent and returns its `model` only if that Agent's backend matches, else null —
whereupon `core/handlers/model_hub/service.py::requested_model` falls back to
`agents.<backend>.default_model`. So the field has always answered for a single
route while being named as if it answered for the backend. That is why v2 adds
`selected_by_agent`: the Agent whose `model` produced the value, or null when it
came from the backend default. The pair is honest about its own grain, and a UI
rendering 「当前模型」 can say whose.

Per-Agent questions have per-Agent routes already: `GET …/chain?model=<id>` and
`POST …/probe {model}` both take an explicit model, so anything that needs another
Agent's answer asks for it rather than reading this rollup.

**REQUIRED on every hub-mode response** (07-29, review round 5). A payload may not
publish `selected_model_id`, `current` or `supply_status` while leaving out the
route they describe: all three inherit this grain, so without the field a client
can render the global-default Agent's model as a backend-wide fact — the exact
ambiguity `selected_by_agent` was added to remove, reintroduced by omission. Both
worked payloads below now carry it. `null` is a legitimate value and means one
specific thing — the model came from `agents.<backend>.default_model` because the
resolved Agent pins none — so it is written explicitly rather than left absent; a
missing key would be indistinguishable from a serializer that forgot.

The obligation is stated here rather than as `required` in
`agent-supply.schema.json` for the standing reason (README → required-vs-optional):
that file's two frozen examples are `mode: hub` and predate every v2 field,
because they round-trip byte-faithfully through the SHIPPED serializer, so a
`required` under a hub branch would fail the contract gate instead of tightening
it. This is the same boundary that makes `sources`, `supply_status` and `severity`
optional in their schemas and mandatory at the API — the schema describes what
today's serializer can emit, this file describes what the v2 API owes its
callers. Enforcement therefore lands with the serializer PR, as a response-shape
assertion next to the ones listed under Serializer completeness.

**Open decision for the owner, deliberately not settled in this PR:** whether the
Models page should surface per-Agent model selection as a first-class control
(one row per Vibe Agent, not per backend). That is a product-surface question with
its own frames, and the frozen ordering ruling — order is a per-Agent subset, no
global list — is untouched either way; naming the grain here does not pre-empt it.

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
  recommendation rule **defined once in spec §4.2** and echoes the result. This is
  the wire form of 「恢复推荐顺序」. The rule is deliberately NOT restated here
  (corrected 07-29, review round 4): round 3 carried an abbreviated parenthetical
  — "own-vendor subscription → api_key by `created_at` ascending → id tie-break" —
  which silently dropped §4.2's channel clause, that a sanctioned `native_cli`
  subscription precedes a hub-held one for the same own vendor. With both
  present the abbreviation fell through to the id tie-break and could put the
  experimental hub credential first, i.e. the paraphrase did not merely omit the
  clause, it contradicted it. That is what a second home for a predicate costs,
  so the fix is one home rather than a better copy: §4.2 states the rule, and
  every other file cites it.
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
    "selected_by_agent": "pm",
    "selected_model_id": "claude-opus-4-6",
    "current": {
      "model_id": "claude-opus-4-6",
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

## Interrupted agent payload (`current: null`)

The case worth writing out, because it is the one that motivated hoisting the
selection out of `current` (round 3): the backend has nothing runnable, so
`current` is null, yet the drawer still has to name the model it is complaining
about, offer its chain drill-in, and default 「试跑一次」 to it.

```json
{
  "ok": true,
  "contract_version": 2,
  "agent": {
    "backend": "codex",
    "mode": "hub",
    "menu_kind": "fixed",
    "selected_by_agent": null,
    "selected_model_id": "gpt-5.3-codex",
    "current": null,
    "supply_status": "interrupted",
    "sources": {
      "policy": "follow",
      "order": ["src_chatgptplus"],
      "eligibility": [
        { "source_id": "src_chatgptplus", "eligible": true, "reason_key": null }
      ]
    },
    "model_supply": [ { "model_id": "gpt-5.3-codex", "chain_length": 1 } ]
  }
}
```

Read it together: one enabled source, which *can* supply the model
(`chain_length: 1`), but it is `needs_action`, so nothing is runnable and
`supply_status` is `interrupted` rather than `waiting`. `current` is null because
naming a source here would render 使用中 for one that will not be called;
`selected_model_id` survives because the user's choice did not change. The two
payloads show both values of `selected_by_agent`: `"pm"` above, where a named Vibe
Agent pins that model, and `null` here, where no Agent on this backend pins one
and the value came from `agents.<backend>.default_model`. Null is a fact about
provenance, not a missing field — the drawer says 「后端默认」 instead of naming an
Agent, and 「当前模型」 is never attributed to an Agent that did not choose it.

This payload lives here rather than in `agent-supply.schema.json`'s `examples`
for the standing reason (README → required-vs-optional): those examples
round-trip byte-faithfully through the shipped v1 serializer, so a v2 field in
them would assert that dormant v1 code already speaks v2.

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

## The supply guard: `SupplyGap` and `would_interrupt`

Two destructive operations can starve a model someone is running on: deleting a
source, and rotating a healthy source onto a credential that sees fewer models.
**They share one predicate, one shape and one refusal code, defined here once**
(07-29, review round 7). Round 6 gave the shape a home under credential replacement
and left the predicate paraphrased in the DELETE cell, where it drifted into a
structural test — and a guard two routes describe differently is not one guard
(README → 「A predicate has exactly one home」).

**The shape.** `SupplyGap` is `{backend, model_id, agents: string[]}`, all three
required, and it is what both keys carry: `would_interrupt` on a refusal (what WOULD
be starved), `interrupted_pairs` on a completed repair (what WAS). One shape, two
keys, because the two facts differ only in mood.

**Namespace: menu identifiers throughout** (see the identifier rules above).
`model_id` is a menu identifier — `claude-opus-4-6`, `zhipuai/glm-5.2` — never the
resolved id it maps to, so the protected set, the gap report and the confirm copy all
live in the namespace the user selected in and every menu renders. Mapping is applied
*inside* the supply computation and nowhere else: to decide whether `claude-opus-4-6`
is still supplied you resolve it through its mapping row and test the chain of
`glm-5.2`, then report the gap as `claude-opus-4-6`.

**What is protected: a backend's EFFECTIVE selections** — the union of four facts,
each named as a menu identifier:

1. every checked fixed-menu model;
2. every model that owns a mapping row, named by its MENU side (`builtin_id`), never
   by the target it rewrites to;
3. `agents.<backend>.default_model`;
4. for every ENABLED Vibe Agent on that backend, its **effective model**: its own
   `model` when it has one, otherwise `agents.<backend>.default_model` — inheritance
   is a selection, not the absence of one.

Disabled Agents are excluded: they cannot take a turn, so their model is not a live
capability. A disabled Agent's model that is *also* the backend default stays
protected through term 3, which is the overlap the union exists to absorb.

**The gap predicate: no runnable supplier, not an empty chain.** A protected pair is
a gap when, AFTER the change, its capability chain holds no `runnable: true`
member — `runnable` exactly as `agent-chain.schema.json` defines it (healthy, or
cooling with `retry_at` already past; never `needs_action`, never `error`).
Equivalently: the pair's `supply_state` would stop being `ok`. `chain_length > 0` is
NOT the test, and that was the round-6 defect: a chain of one revoked key is
non-empty and cannot serve, so the structural test passed the delete and the agent
went `interrupted` on its next turn.

**One boundary, stated so nobody re-derives it.** This predicate is deliberately not
the `interrupted` predicate. A pair whose remaining members are all merely cooling is
`waiting`, not `interrupted` (spec §4.5) — and it IS reported here, because the guard
asks 「删完之后现在还有能跑的来源吗」, which is false in both cases, while
`waiting`/`interrupted` answers the different question 「要不要你处理」. The guard
over-warns by exactly that one case, on purpose: it is a confirmation prompt,
`force=true` overrides it either way, and a client that wants to distinguish 暂时
from 需处理 reads that pair's `supply_state` from the chain query. Nothing here
forbids an operation; it decides what the user is told before they confirm.

**`agents` names the Agents that would actually break, INCLUDING the ones that
inherit the backend default** (07-29, review round 7). Membership is by effective
model, term 4 above: an enabled Vibe Agent belongs in `agents` when the starved pair
is the model it would actually run on, whether it names that model itself or inherits
it from `agents.<backend>.default_model`. **This reverses round 6's parenthetical**,
which emptied the list for a gap protected only through the backend default;
recorded as an orchestrator ruling, open to owner veto at final review. The reason
for the reversal: the field answers 「谁会坏」, and round 6's rule answered 「谁明确
写了这个模型」 — a different question whose answer is `[]` on a real interruption.
That is not a neutral omission but false reassurance, since the client then renders
「无 Agent 受影响」 for a refusal that exists *because* an Agent is about to stop
working, and 中断必须点名成因与受影响方 is the whole reason the member was added.
`agents` may still be legitimately empty — a checked-but-unassigned menu model, or a
mapping row no Agent runs on — and after this ruling that is the only way it empties.

## Credential replacement and re-auth

Two routes share one repair: `PUT …/sources/<id>/credential` (paste a new key) and
the completion of `POST …/sources/<id>/reauth` (re-run OAuth on an existing source).
**This section states the PROPERTIES that repair must satisfy, not the steps it
takes** (rewritten 07-29, review round 7). Rounds 3–6 each corrected the step list,
and each correction bred the next round's findings: an ordered list of five steps
admits an unbounded supply of 「what about between 3 and 4」 questions, and round 7
asked four of them at once — where the guard runs, when the engine learns about the
swap, whether the steps are executable on the default channel at all, and what
counts as recovered. The invariants below are finite and total; the sequence that
satisfies them belongs to the implementing lane, and one is sketched
non-normatively at the end.

**The commit point.** Every invariant is stated against one named instant. The
**commit point** is the single write that makes the replacement live: `credential_ref`
swapped, `masked_credential` refreshed, the discovered list merged into
`Source.models`, `state.retry_at` and `state.detail_key` cleared, affected chain state
recomputed, **and the engine's own source table synchronised to the new ref**
(`sync_sources`, the last step of the frozen adapter's canonical flow). Those happen
together or not at all. The engine sync is INSIDE the commit phase, not after it,
because a swap the engine has not seen is not a repair — round 7's finding was
precisely that: persist-then-revoke with the sync left implicit lets the engine keep
serving turns from a handle we just revoked, so a successful repair breaks the next
turn.

1. **Atomicity.** There is exactly one commit point. Before it the prior state is
   fully recoverable and still serving; after it the replacement is fully in effect.
   No intermediate state is observable to a turn — there is no instant at which the
   source's `credential_ref` and the engine's binding disagree, and no half-applied
   repair to reconcile later.
2. **No revoked-credential window.** The engine must never hold a reference to a
   revoked credential. The old ref is revoked only AFTER the commit point — that is,
   only once the engine's source table reflects the swap — never before it and never
   inside it. A revoke that then fails must not undo a completed repair: it leaves an
   orphaned handle in the engine store, which is a cleanup problem, not a supply
   problem.
3. **Failure preserves the prior state.** Any failure before the commit point leaves
   the original credential active and serving, and revokes whatever temporary
   replacement ref the attempt minted; the route answers `discovery_failed`.
   **Temporary refs never outlive the operation** — success revokes the old one,
   failure revokes the new one, and no path leaves two live handles for one source.
   「Prior state」 means the state this contract owns: the `Source` row and the
   engine-owned credential store (see invariant 5 for the channel where that
   boundary bites).
4. **Guard before commit.** `would_interrupt` is evaluated against the POST-swap
   world — the model set the new credential actually discovered — and evaluated
   BEFORE the commit point. A refusal therefore happens with the prior state intact,
   with nothing to roll back beyond invariant 3's temporary ref, and it is
   unrepresentable for this route to commit a swap and *then* find it was not allowed
   to: the guard is a precondition, not a postcondition. Which pairs count, and what
   counts as starved: 「The supply guard」 above, the same definition DELETE uses.

5. **Per-channel executability.** Every supply channel must have a path that
   satisfies the invariants above through the surfaces it actually has (round 4 made
   this channel-specific; round 7 asked whether the default channel could execute the
   steps at all). What a credential *is* differs by channel:
   - **`api_key` sources and consented hub-held subscriptions** hold an engine-owned
     handle. Repair mints a replacement ref, discovers through it, swaps at the
     commit point. This is the only channel where invariants 2 and 3 have refs to
     talk about.
   - **`native_cli` subscriptions hold no engine ref at all** — the Source contract
     pins `credential_ref: null`, because the credential lives in the CLI's own
     sanctioned store. **Replacement there IS re-auth of that store**, performed at
     the CLI's own surface; the engine-ref steps do not apply and must not be
     simulated, since there is nothing to provision, nothing to swap and nothing of
     ours to revoke. Writing the hub behaviour as universal would put a non-null ref
     on a source whose channel forbids it, failing validation on the path most users
     take. What the commit point writes there is the state half only — the discovered
     list, the cleared `state` fields, the recomputed chain, the engine sync — and
     invariants 1, 4 and 6 hold unchanged, evaluated on the same `Source` row.

   Two boundaries keep that honest rather than hand-waved. **Discovery reads through
   the CLI's store**, not through a ref we hold, so 「discover before committing」 is
   still satisfiable: the CLI is authoritative for the credential, we are
   authoritative for the row. And **invariant 3's rollback is bounded by what this
   contract owns**: a completed CLI re-auth has already replaced the user's login
   inside a store we do not manage, and no adapter surface can put the previous login
   back. So a post-re-auth discovery failure leaves the `Source` row untouched and
   answers `discovery_failed` — the row keeps its prior state, which is what invariant
   3 promises — while the CLI store keeps whatever the user just authorised. That
   asymmetry is declared, not accidental: pretending we could restore the old login
   would be inventing an adapter surface, and re-authenticating a CLI is something the
   user did at the vendor's own prompt, not a transaction we brokered.

   **Read that as a statement about the row, not about the user's supply** (07-29,
   review round 8). On this channel the preserved row is intact and no longer TRUE: its
   `models` and `state` describe the login that has just been replaced, so the next
   native turn runs on the new account against a row describing the old one, and a
   refusal that 「preserves the prior supply」 preserves a supply that no longer exists.
   Invariant 3 is therefore satisfiable here only in its weak sense — we corrupt
   nothing — and the strong sense it carries on the ref-holding channel, that a failed
   repair leaves the user no worse off, is not available once the login is gone. Two
   ways close it: require confirmation BEFORE the irreversible login, or commit the
   swap and report the resulting gaps instead of refusing. Both have a UI consequence,
   so the choice is the owner's, recorded as **AC-2** in `model-hub-implementation.md`
   and NOT decided here. What is decided: this channel must not present a
   post-re-auth refusal as though the prior supply were intact. **What
   「recovered」 observes there** is exactly what it observes everywhere — whether this
   operation cleared the source's blocker state (invariant 6). Nothing in that
   definition reads a credential handle, which is why it needs no channel-specific
   case.
6. **Recovery symmetry.** `recovered` is true when the operation cleared a BLOCKER
   state, and 「blocker」 is the same set the chain classifier uses: `state.status` of
   `needs_action` **or** `error` — the two values `agent-chain.schema.json` counts in
   the branch that makes a chain `interrupted`, and the two whose `retry_at` is null
   because nothing clears them unattended. One definition, referenced, never
   re-enumerated. Round 6 wrote 「cleared a `needs_action`」, a strict SUBSET of that
   set, so repairing a source sitting in `error` — a state round 6 itself promoted to
   a blocker in the same commit — would have reported `recovered: false`, filing a
   real repair as an elective rotation: it suppresses the `recover` event the user is
   owed and sends the operation down the stricter refusal branch below for a source
   that was already unusable. A cooldown is deliberately NOT a blocker here, because
   it heals itself: replacing a key while a source is merely cooling is an elective
   rotation.

**"Healthy" here is `active` or `standby`, recomputed — never a literal** (round 4).
Round 3 wrote `state.status: "healthy"`, which is not in the enum, so a literal
implementation would have made every SUCCESSFUL replacement fail Source validation.
Nor is the fix a third literal: `active` vs `standby` is a display rollup over "is
this source serving any agent right now", so the commit point clears the two detail
fields and lets the rollup resolve. A route that hard-coded either would be asserting
a fact about every backend's order from inside a single-source operation.

**`Source.models` merge rule:** entries with `provenance: discovered` are REPLACED by
the new discovery, entries with `provenance: manual` are PRESERVED. The model list of
an `api_key` source is a property of the CREDENTIAL, not of the base URL — a
replacement key on a different plan, project or tier can legitimately see fewer
models — while a hand-added id is the user's assertion, and swapping a key does not
withdraw it. A subscription can likewise come back on a different plan than it went
in on, so re-auth runs the same refresh.

**The response names what the repair cost**, which is what round 6 found missing: the
prose promised every newly starved (backend, model) pair while the declared shape was
only `{source, recovered}`. `recovered` answers invariant 6; `interrupted_pairs`
reports the protected pairs the repair left with no runnable supplier — the same
predicate and the same `SupplyGap` shape as 「The supply guard」 above — and is `[]`,
present and empty, when the repair cost nothing:

```json
{ "ok": true, "contract_version": 2,
  "source": { "id": "src_relay9c1x", "kind": "api_key", "...": "..." },
  "recovered": true,
  "interrupted_pairs": [
    { "backend": "claude", "model_id": "claude-haiku-4-5", "agents": ["pm"] }
  ] }
```

**Illustrative sequence for the ref-holding channel (NON-NORMATIVE).** Offered
because the frozen adapter surface forces most of it, and because rounds 3–5 argued
about an ordering that cannot be written: `discover_models` takes a `credential_ref`
and `provision_credential` is the only thing that mints one
(`adapter-interface.py`), so 「validate the key, *then* provision it」 has no handle to
validate with — provisioning IS how a key is validated, and the docstring's canonical
flow is already **provision → discover → persist → sync**, which shipped
`create_source` implements. Round 5 was right that discovery must precede the swap and
expressed it as discovery preceding the *provision*; both halves are satisfied by
provisioning a replacement ref that is not yet the source's. A conforming walk:
provision the pasted or newly authorised credential into the engine store, yielding a
replacement ref while the source still points at its old one and still serves from it
→ discover through the replacement ref → evaluate the guard against what was
discovered → commit → revoke the old ref. Any failure or refusal before the commit
revokes the replacement ref and answers with the prior state intact. **This is an
example, not the contract**: an implementation that satisfies the six invariants by
another route is conforming, and a step list that reads better while violating one is
not.

**A predicted gap gates by intent, not by severity.** Repairing a blocked source
(`recovered == true`, invariant 6) PROCEEDS and reports through `interrupted_pairs` —
refusing would trap the user in the exact state 「换一个 Key」 exists to escape, and a
narrower working key is strictly better than a revoked one. An elective rotation of an
already-serving source (`recovered == false`) is refused unless `force=true`, with the
same `source_last_supplier` + `would_interrupt` shape DELETE uses, because there the
user has a working supply and no reason to be surprised out of part of it. Both
branches read the same guard (invariant 4); only the response to it differs.

**`kind: recover` is CONDITIONAL on the prior state, not on success** (round 5). The
route already answers "did this clear a blocker" in `recovered`, and the same
success can happen to a perfectly healthy source — a scheduled key rotation. Emitting
`recover` unconditionally publishes a restoration for a source that was never
interrupted: the timeline gains a 「已恢复」 entry with nothing to recover from, and
severity promotion can push it to the user as news. So emit `kind: recover` **iff
`recovered == true`**, with `to_source: <this source>` — `recover` is the one kind whose
subject sits on the WINNING side. When `recovered == false` the routes emit **no
resolution event at all**, and that is deliberately NOT a 9th `kind`: the enum names
*resolutions of supply state*, and a rotation that starts healthy and ends healthy
resolves nothing, while a new value would force every consumer's switch to grow a
branch that renders nothing.

## OAuth completion responses

`POST /api/models/oauth/start` and `POST …/sources/<id>/reauth` both return a flow,
and the UI polls `GET /api/models/oauth/status/<flow_id>` (2s, 15-min timeout). **The
terminal `state: "success"` response carries the flow's OUTCOME alongside the flow**
(07-29, review round 6). Round 5 left that shape undefined, which made the two
creation paths inconsistent: `POST /sources` closes the 「so what now?」 loop with
`adopted_by`, while a subscription added through OAuth — the *default* way a
subscription is added — returned only a flow state. The closing loop is a v2 product
commitment for BOTH creation paths, so the shape is declared rather than left to the
implementing lane.

The response is a total function of `flow.intent`, which v2 adds to `OAuthFlow`:

```json
{ "ok": true, "contract_version": 2,
  "flow": { "flow_id": "oaf_claude01", "intent": "create", "state": "success",
            "source_id": "src_claudepro1", "...": "..." },
  "source": { "id": "src_claudepro1", "kind": "subscription", "...": "..." },
  "adopted_by": [ { "backend": "claude", "policy": "follow", "position": 1 } ] }
```

- `intent: "create"` → `{flow, source, adopted_by}`, with `source` and `adopted_by`
  carrying exactly the `POST /sources` semantics, because it is the same event: a new
  source exists and the backends on 跟随推荐 picked it up. Auto-creation on flow
  success is already the shipped behaviour; what round 6 adds is that the created
  source stops being discarded from the response.
- `intent: "reauth"` → `{flow, source, recovered, interrupted_pairs}` — the repair tail
  above, field for field, because it is the same repair reached through a different
  door. `adopted_by` is deliberately **absent** here rather than `[]`: the source
  already existed and no order changed, whereas `[]` on the create path asserts the
  real fact 「eligible for nobody」.

`intent` is what makes the shape derivable from the payload instead of from client
memory of which button was pressed. `source_id` cannot serve: hub-channel *create*
flows also set it, for the pending source they bind to, so it does not discriminate
creation from repair. A non-terminal poll, and a flow that ends `failed` or
`cancelled`, carry `flow` alone — there is no source and no repair to report. The
field is OPTIONAL in `oauth-flow.schema.json` for the standing reason (README →
required-vs-optional): the shipped flow payloads are validated against that file by
`tests/test_model_hub_api.py`, and they predate the field, so a `required` there
would fail the contract gate instead of tightening it. The server always emits it
from v2 on, which is a response-shape assertion in the serializer PR.

## Probe response nesting

```json
{ "ok": true, "contract_version": 2,
  "probe": { "reachable": false, "source_id": "src_relay9c1x", "model_id": "glm-5.2",
             "latency_ms": 287, "error": "models.source.needs_action.balance_exhausted",
             "via_mapping": true, "contract_version": 2, "backend": "claude" } }
```

The latency is **measured, not null** (corrected 07-29, review round 4). A
`balance_exhausted` is learned FROM a completed upstream response — the server
answered and told us the credit is gone — and `latency_ms: null` is reserved for
attempts that never completed at all, which the UI renders as 未完成. A frozen
example carrying that pair teaches every implementation and example-driven test
to show a completed billing answer as an unfinished request, so
`probe-result.schema.json` now makes the pair unrepresentable: any
`models.source.needs_action.*` error requires an integer latency. null belongs to
the transport family (`models.source.cooldown.network` / `.timeout`), which has
its own example on that schema.

`ProbeResult` is **nested**, and its outcome field is `reachable`, not `ok`. Both
halves of that matter. `reachable` means **the upstream completed the request
usably** — 能不能用, the question 「试跑一次」 asks — and NOT "the upstream answered"
(wording corrected 07-29, review round 6: that phrasing survived from round 4 and
contradicted both the corrected schema definition and the example directly above,
where a `balance_exhausted` is learned FROM a completed 402 and is `reachable:
false`). The schema makes the alignment mechanical: `reachable: true` requires a
null `error`, so `true` is exactly "a usable completion for this (source, model)".
Transport failures and completed-but-refused responses are both `false`, and
`reachable: false` beside a measured `latency_ms` is the normal shape of the second.
Whether the attempt completed at all is still answerable from the same object —
`latency_ms != null` — so the two questions stay separable without a second flag.
The envelope's `ok`, meanwhile, means the API call succeeded: a probe that cleanly
establishes the source is broken is a *successful* call reporting `reachable: false`
— spreading the object into the envelope would collide two different questions on
one key, and every client would eventually read the wrong one.

`ok: false` on this route is reserved for the call itself failing:
`probe_no_candidate`, `engine_down`, an unknown backend.

```json
{ "ok": false, "contract_version": 2, "error": "probe_no_candidate",
  "detail": "models.probe.no_candidate.waiting",
  "supply": { "supply_state": "waiting", "retry_at": "2026-07-29T09:15:00Z" } }
```

The structured half lives in its own `supply` key, **not in `detail`** (corrected
07-29, review round 3): round 2 put the object there, which contradicts the
envelope rule above and the two shipped client types that already declare
`detail?: string`. A client would have rendered `[object Object]` next to the one
error whose whole purpose is to explain itself. `detail` keeps doing its one job —
an i18n key, here varying with the state so the string alone is already showable.

`supply.supply_state` is the requested model's `AgentChain.supply_state`, so the copy
can be 「该模型的来源都在冷却,约 09:15 恢复」 for `waiting` and 「该模型暂无可用来源,需处理」
for `interrupted`. `supply.retry_at` is the earliest across that chain and is null for
`interrupted`, because nothing in it recovers on a timer. `supply` is present on
`probe_no_candidate` and on no other error code — a per-error typed sibling is
cheap precisely because it does not have to generalise.

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
time, so this is always ELIGIBLE for a proactive push and never feed-only. Emitted once
per transition, never per starved turn. Note what the payload does not carry: no
recipient, no channel, no platform. `agent: "codex"` is the addressing input — the push
layer resolves it to the scopes whose routing currently selects a Codex Vibe Agent (spec
§4.5 → 「Who receives an action-required push」). This kind is the clearest case for
resolving at delivery rather than at emit: it typically fires from a Web UI settings
mutation, which has no originating conversation to reply into.

It lives in `api.md` rather than as a `resolution-event.schema.json` example for the
same reason `severity` does — the round-trip test drives every example in that file
through the shipped v1 `ResolutionEvent`, which has no `severity` field yet (README →
required-vs-optional discipline).

## Error codes (minimum set)

`source_not_found`, `flow_not_found`, `flow_expired`, `discovery_failed`,
`invalid_source_order` (the four rules above, plus `order` sent with
`policy: "follow"`), `source_last_supplier` (**added 07-29, review round 6** — the
guard that refuses a destructive change while it is the last supplier of a selected
model had prose, a `force=true` override and no code, so a client could not tell it
apart from a generic failure. Carries `would_interrupt: SupplyGap[]`. Raised by
DELETE without `force`, and by an elective credential rotation that would starve a
pair), `mapping_target_unavailable`, `mode_switch_blocked`,
`engine_down`, `consent_required` (hub-held subscription paths while the
experimental flag is unset), `migration_item_conflict`, `turn_not_found`,
`probe_no_candidate` (probe requested while this backend has no **runnable**
candidate for the model. The typed `supply` sibling names which case, since 「稍等即可」
and 「需处理」 are different answers for the user — and it is read from the REQUESTED
model's own chain (`AgentChain.supply_state`), never from the backend's
`supply_status` rollup. The
rollup answers for the backend's *current* model, so probing a starved non-current
menu item while the selected model is healthy would report `ok` and leave `supply`
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

**Mechanical guards the schemas cannot carry** (07-29, review round 5). Round 5 pushed
every invariant it could into the schemas themselves — closed enums instead of prefix
patterns, biconditionals instead of one-way implications, `required` alongside every
non-null `then`. What is listed here is the residue: invariants JSON Schema draft-07
cannot state at all. Each one ships as a check in the contract test that already walks
these files, in the same PR that introduces the field it guards. They are recorded
here so a reader can tell "draft-07 cannot express this" from "nobody thought of it":

| Guard | Why not in the schema |
| --- | --- |
| `probe-result.error` enum ≡ ALL THREE of `source.state.detail_key`'s enforced status vocabularies — cooldown ∪ needs_action ∪ the single `error` key (widened 07-29, review round 7) | cross-file identity; a bare `Draft7Validator` resolves no `$ref` across files, and two hand-kept lists drift. Round 6 widened two files and left this one asserting an identity it no longer had, so a source could enter `error` while no probe had a legal value to report it. Round 7's correction is mechanical, not editorial: the comparison now runs across files for EVERY vocabulary any of these schemas claims to mirror, from the table in `README.md` → 「Vocabulary mirrors and their blast radius」 |
| `source.state.detail_key.examples` ≡ the union of the enforced per-status enums | `examples` is documentation, so the human-readable list can silently outlive the enforced one — this already happened once in round 4, and again in round 6. The round-6 recurrence needed a second fix in the CHECKER, not the schema: the `error` key is pinned as a `const`, and a checker that collects only `enum` lists cannot see a closed vocabulary of size one, so its own union came up one key short and the comparison passed vacuously. `const` now counts as an enum of cardinality one |
| every vocabulary shared across these files is a registered row with a mechanical rule — equality, a documented projection (`chain[].health`, `supply_state`, `model_supply_state`), or a total partition (`turn-provenance.failed_attempts[].reason` against `resolution-event.reason`) | all of them are cross-file, and the non-equality ones are worse than cross-file: they are relations between vocabularies at different grains, which no schema keyword expresses in either direction. The rules and their reasons live in `README.md` → 「Vocabulary mirrors and their blast radius」; the partition is what makes a new `reason` a decision rather than a default, since it fails until the new value is either mirrored or excluded on the record |
| the INVERSE: a field whose description names another contract file must appear in that table (07-29, review round 7) | this is the half that closes the class rather than the instance. Round 6's three drifted mirrors were each asserted in prose and compared by nothing; only the pair someone had thought to check was checked. Detection is by schema STEM, not by the `.schema.json` suffix and not by the word "mirror" — the claim round 7 missed (`turn-provenance` → `failed_attempts[].reason`, naming "the resolution-event `reason` vocabulary") used neither |
| every non-null `then` constraint is accompanied by `required` for the field it constrains | draft-07 `properties` validates only fields that are PRESENT, so a conditional over an optional field is toothless when it is absent. The checker carries exactly two declared exceptions (`agent-supply` → `supply_status`, `resolution-event` → `severity`), each one a fail-SAFE absence forced by a frozen example, and fails if a declared exception stops being needed |
| `AgentChain.chain` has unique `source_id` values | `uniqueItems` compares whole items, so the same source twice with different health flags passes — and inflates `chain_length` into counting one credential as two fallbacks |
| `AgentChain.chain` preserves the relative order of `sources.order` | ordering of a *different* document is not expressible |
| every id in `sources.order` appears in `sources.eligibility` with `eligible: true` | one field constraining another; draft-07 has no `$data` |
| `channel_switch` events have `from_source == to_source` | same reason — equality between two sibling values |
| `selected_by_agent` is present on every hub-mode `AgentSupply` response | a `required` here would fail the byte-faithful round-trip on that file's two frozen examples (see the response-shape assertion above) |
| `model_supply` holds exactly one row per menu model: `model_id` values are UNIQUE and the set COVERS that backend's whole menu (07-29, review round 6) | both halves are inexpressible. `uniqueItems` compares whole items, so two rows for one model differing only in `chain_length` validate — and then a `0` row sits beside a `2` row for the same id, with every consumer reading whichever it hits first, which is worse than a missing row: the 「无来源可供」 flag becomes a coin flip. Coverage is a relation to a different document (the backend's menu), which draft-07 cannot reach. Recorded as a server-validated invariant in spec §4.4 |
| the `reason`↔`detail_key` correspondence between `resolution-event` and `source.state` is a BIJECTION over the five non-self-healing causes (07-29, review round 6) | cross-file identity again, and here it is load-bearing rather than tidy: a source state with no event reason is a blocker that cannot be announced, and an event reason with no source state is a push whose 「去处理」 lands on a row that renders nothing. The five pairs are listed at `resolution-event.schema.json` → `reason` |
| `intent` is present on every v2 `OAuthFlow` response | same boundary as `selected_by_agent`: the shipped flow payloads are validated against that schema by `tests/test_model_hub_api.py` and predate the field, so `required` would fail the gate rather than tighten it |
