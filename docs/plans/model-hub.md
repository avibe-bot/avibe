# Model Hub — Product Spec

Status: **v3.0** (2026-08-07) · supersedes v2.0 (2026-07-29) outright
Owner decisions incorporated through: 2026-08-07 (+08:00)
Design source: `../avibe-docs/design.pen`. The V6 frames remain the visual baseline;
v3's two-module information architecture supersedes their product grouping and needs
new approved frames before UI implementation.
Contracts: `model-hub-contracts/` at **FROZEN v4 (targeted)**. This docs-only revision
does not edit them; `model-hub-implementation.md` records the coordinated v5 revision
set for the first implementation lane.

## 0. Revision note — why v3 replaces v2

Owner ruling (2026-08-07): **Model Hub is Avibe's default local model gateway.**
Its end-to-end product model is:

1. upstream sources — vendor subscriptions, API keys, and third-party relays represented
   as API keys with custom Base URLs;
2. the local Gateway — protocol adaptation, model pairing, ordered routing, failover,
   retry, and recovery;
3. downstream Agents — consumers of the Gateway, not owners of upstream credentials.

v2's per-backend source order remains the default routing policy. v3 adds the missing
precision above it: every `(backend, menu model)` may instead own a **custom ordered
route chain**, and every hop names the exact `(source_id, model_id)` to call. This
formally supersedes v2 §9's “No per-model ordering” non-goal on 2026-08-07. The chain
projection in §4.6 is the single normative derivation for default and custom routes;
no mapping pipeline or second chain definition survives elsewhere in this document.

Existing fixed-menu `mappings` are absorbed rather than preserved beside the new
chain. A mapping's one target becomes the degenerate custom-chain case in which every
materialized hop carries that same `model_id`; richer chains may choose a different
model per source. The atomic evolution proposal and its rationale are in §4.6 and are
**owner-vetoable (2026-08-07)**. The feature remains pre-GA and default-off, so v3 does
not justify a permanent compatibility layer or two live routing authorities.

**Subscription ruling (owner 2026-08-07, amended later the same day).** Native use is
the recommended and default subscription path. A Claude subscription stays in Claude
Code's local login and a ChatGPT subscription stays in Codex's local login; each native
source naturally leads its own backend's order. When native quota is exhausted or
cooling, a failure observed before output starts falls through in that same turn to
the first runnable Gateway upstream for the backend, then automatically returns to
native after recovery. If native output has already started, the turn stops honestly
and the next turn uses the Gateway; Avibe never duplicates a partial response. This
channel handoff is a first-class product story, not an escape hatch or implementation
detail. Native-first is the recommended Follow default, not an invisible pre-pass: a
user who explicitly puts a Hub hop first in a Custom chain has intentionally overridden
it, and the stored Custom order remains authoritative.

A user may explicitly add either subscription as a Gateway-held upstream, and a
Gateway-held subscription may participate in a cross-vendor custom chain. The only
warning is one factual sentence shown when the user chooses the Claude-as-Gateway
path: Anthropic explicitly prohibits it, enforces server-side blocks, real account
bans have occurred, and the path may fail intermittently. Native Claude, ChatGPT in
either channel, and cross-vendor routing show no warning. The
`subscription_hub_experimental` flag and per-source consent record are retired from
the specification; implementation cleanup belongs to the v5 batch.

**Surface ruling (owner 2026-08-07).** The Models page has two product modules:
Sources and **Gateway**. Sources answer what upstream access the user owns. Gateway is
the main work surface for pairing, allocation, backend order, and per-model chains.
The line between them visualizes current state; it is not a configurable object. A
third “Configure Agents” module — adding models, reasoning effort, and related Agent
settings to Agent definitions — is explicitly deferred; v3 records the intent and
does not design it.

**GA direction (owner 2026-08-07).** Three directions are accepted, without expanding
the GA scope in this revision: conversion-fidelity evidence, a release gate covering
Avibe-owned engine asset mirroring plus the supported-platform matrix, and the
vocabulary recut in §3. §10 records only the remaining research questions and evidence
owed before those gates can be specified mechanically.

---

## 1. Background

Today each agent backend (Claude Code / Codex / OpenCode) carries its own
provider configuration, and Avibe edits the user's **native** CLI config files
(`settings.json`, `config.toml`, opencode config). Credentials, base URLs and
model choices are scattered per backend; users must understand each CLI's
config model; Avibe mutates user-owned files.

Meanwhile many users own **subscriptions** (Claude Pro/Max, ChatGPT Plus/Pro)
plus one or more **API keys** (official vendors or OpenAI/Anthropic-compatible
endpoints). They want subscription quota consumed first, automatic fallback
when it runs out, automatic switch-back on recovery — without understanding
base URLs, protocol conversion, account pools or routers.

## 2. Product promise (user-facing, locked 2026-08-07)

1. **Connect upstream access once.** A vendor subscription, API key, or API key
   with a custom Base URL becomes a Source that every eligible Gateway route can
   use.
2. **Native subscriptions lead by default, and the Gateway takes over without drama.**
   Follow routes for Claude Code and Codex use their own local subscription login
   first. If that quota fails before output starts, the same turn uses the first
   runnable Gateway upstream; after a partial streamed response, the next turn does.
   Recovery returns subsequent Follow turns to native automatically. An explicit
   Custom chain may put a Hub hop first, and Avibe honors that user-owned order.
3. **Routing has two explicit grains.** Each backend owns a global Source order.
   Each menu model follows the chain projected from that order by default, or the
   user gives that `(backend, menu model)` an exact custom ordered chain of
   `(source_id, model_id)` hops.
4. **The user owns every custom order.** No health score, learned ranking, or cost
   heuristic silently rewrites a backend order or custom model chain. Avibe may
   skip an unrunnable hop for the current turn; it never mutates the configured
   order as a side effect.
5. **Adaptation stays local and invisible.** The Gateway performs protocol
   conversion, retry, failover, and recovery. Agents consume a stable model menu;
   users never configure an engine plugin or account pool.

Core persona: individual users who already pay for Claude Pro/Max or ChatGPT
Plus/Pro ("spend what I bought first"). Secondary: API-key-only users.
Explicit non-persona: relay-station operators ("站长") — Avibe ships no
operations console.

## 3. Vocabulary (v3 recut; UI copy uses only these nouns)

| Concept | zh | en | Notes |
| --- | --- | --- | --- |
| The settings surface | 模型 | Models | Single nav entry between 通讯平台 and 后端 |
| Upstream module | 来源 | Sources | Inventory of access the user owns; never an ordering surface |
| Local adaptation and routing module | 网关 | Gateway | First-class product noun, owner-locked 2026-08-07; pairing, allocation, ordering, retry, failover, recovery |
| Where tokens come from | 来源 | Source | Two kinds only: 订阅账号 (OAuth) and API Key (key + editable Base URL); a relay is the latter, not a third kind |
| Backend-wide fallback order | 来源顺序 | Source order | Ordered subset of sources eligible for one backend; never product-global |
| Per-model route | 路由链 | Route chain | Ordered hops for one `(backend, menu model)`; every hop is an exact Source + upstream model pair |
| Route policy | 跟随来源顺序 / 自定义链 | Follow source order / Custom chain | Follow is the §4.6 projection; Custom is user-owned and frozen |
| Per-backend path | 网关 / 直连 | Gateway / Direct | Wire values remain `hub | direct` until the v5 contract revision; Gateway is the default product path, Direct is the diagnostic/self-managed path |
| Per-backend health rollup | 供给状态 | Supply status | 正常 / 降级 / 暂时全部在冷却 / 无可用来源 (§4.5) |

The remaining vocabulary rulings below are **owner-vetoable (2026-08-07)**; they
apply to UI nouns, not precise technical prose in this specification.

| Formerly banned term | v3 ruling | Reason |
| --- | --- | --- |
| 网关 / Gateway | **Required product noun** | The owner named the main module Gateway; banning it now hides the product model |
| 路由 / route | **Allowed only as 路由链 / route chain or as a verb**; standalone 路由器 / router remains banned | Gateway is the component; route chain is the user-owned configuration |
| 逻辑模型 / logical model | **Banned** | Menu model and upstream model id already name the two real identities |
| Provider (as a UI noun) | **Banned** | The UI manages concrete Sources; vendor may appear as metadata, and “upstream provider” remains valid architecture prose |
| 账号池 / account pool | **Banned** | It implies operator tooling and multi-tenant pooling that Avibe does not ship |
| 中转站 / relay station as a category | **Banned** | It is an API Key Source with a custom Base URL; helper copy may use it as an example |
| 优先级 / priority as a standalone global noun | **Banned** | Name the owning backend Source order or model Route chain; ordinal copy such as “first upstream” is allowed |

## 4. Architecture: upstream → Gateway → Agents

The v3 split, stated once: **Sources represent upstream access; Gateway owns local
adaptation and routing; Agents consume the result.** Ordering is never a property of
a Source.

### 4.1 Supply — Sources (global assets, no ordering)

Each source carries: kind (subscription | api_key), vendor, credential
reference, protocol (anthropic | openai | openai_compatible …), an editable base
URL (api_key kind; prefilled for known vendors), a **model list** it can supply
(auto-discovered where possible, e.g. `/models`; manually extendable via custom
model entries), billing type (包月 | 按量 ¥), state (§4.5), and usage
(subscription cycle % / monthly spend).

A source carries **no position, rank, or priority field anywhere** — not in
config, not in the API, not in the UI. The 来源 list is an asset inventory sorted
for reading convenience, never a spend order.

**Supply channel.** Each source has a `supply_channel`:

- `native_cli` — the credential remains in the official CLI's local store. This is
  the recommended and default channel for every subscription: Claude → Claude Code,
  ChatGPT → Codex. It is the first hop for its own backend and participates in the
  native-to-Gateway handoff in §4.3 step 0.
- `hub` — the managed engine holds the credential and re-originates requests. This
  is the default for API keys and an explicit opt-in for subscriptions. A hub-held
  subscription is a normal Gateway upstream and may appear in a cross-vendor custom
  chain (§4.4, §4.6).

There is no feature flag, consent stamp, experimental row state, or per-route warning
for hub-held subscriptions. The single exception is informational copy shown while
adding **Claude as a hub-held Source**:

> Anthropic explicitly prohibits routing Claude subscriptions through third-party
> gateways, enforces server-side blocks, and real account bans have occurred; this
> path may stop working intermittently.

That sentence does not appear for native Claude, ChatGPT in either channel, or when
the user later places an already-added Source in a cross-vendor chain.

The same model may be supplied by multiple Sources; backend Source order and the
per-model policy in §4.6 arbitrate them.

### 4.2 Gateway strategy — backend order plus per-model policy

One record per agent backend. It owns:

- `mode` — Gateway (`hub` on the wire) | Direct (`direct`).
- `menu_kind` plus the menu itself: fixed for Claude Code / Codex, open for
  OpenCode. Menu enrollment is distinct from Gateway routing.
- **the backend's Source order** — an ordered subset of the sources eligible for
  this backend (§4.4), plus an order-ownership policy. This order is the input to
  `follow` routes; it is not a second filter on a model's exact custom chain.
- **one route policy per menu model** — follow the §4.6 projection from the backend
  order (default), or use an exact custom chain. This document does not define that
  chain anywhere except §4.6.

The backend Source order itself has this ownership policy:

| Policy | zh | Behavior |
| --- | --- | --- |
| `follow` (default) | 跟随推荐 | Order is server-computed by the recommendation rule below. A newly added eligible source **joins automatically** at its recommended position. |
| `custom` | 自定义 | A user-owned, frozen ordered subset. A newly added eligible source does **not** join; the UI hints 「有新来源未启用」 and offers one-tap enable. |

State machine: `follow` --any manual edit--> `custom` --「恢复推荐顺序」--> `follow`.
Forking to `custom` is implicit and immediate: reordering, enabling, or removing a
source while in `follow` freezes the current order as the user's own. Returning to
`follow` discards the frozen subset and recomputes.

Independently, each menu model's route policy is `follow` or `custom` as defined in
§4.6. Changing one model's route never changes the backend Source-order policy, and a
Source omitted from that order may still be named explicitly by an eligible custom
hop. Omission means “not used by Follow routes,” not “globally disabled.”

**Recommendation rule (deterministic; document verbatim, implement verbatim).**
For a given backend, the recommended order is:

1. the backend's **own-vendor `native_cli` subscription**, if present and eligible —
   Anthropic for Claude Code, OpenAI for Codex;
2. then all eligible hub-held subscription Sources, **by `created_at` ascending**;
3. then all eligible API-key Sources, **by `created_at` ascending**;
4. tie-break anywhere above by Source `id` ascending.

The rule is *exhaustive over eligible sources*: nothing eligible can fall outside
it, which is what makes 跟随推荐 safe to auto-join. Eligibility does not mean a Source
will appear in every model chain; §4.6 still requires model capability or an exact
custom hop.

Nothing else participates: no health score, no latency, no cost heuristic, no
usage-based reordering. This rule is the *entire* content of 跟随推荐, and it is
stable — the same set of sources always yields the same order.

Two obligations follow, and both are contract, not implementation detail:

- **Creation order must be persisted**, as immutable `created_at` on the source
  (`source.schema.json`). Insertion order in the config file is not a contract and
  the sources array is explicitly unordered (`api.md`), so without a stored stamp
  rules 2 and 3 are not reproducible.
- **Rule 4 is not decoration**, and it needs one companion rule to finish the job.
  Two sources imported in one migration batch can legitimately share a timestamp, and
  the id tie-break settles those. It does *not* settle how a record predating
  `created_at` compares to a stamped one — a tie-break orders equals, and null is not
  equal to a timestamp. The missing half is therefore stated normatively in
  `source.schema.json`: **an absent stamp sorts before every present one** (the record
  is older than the field itself), ties by id, and the serializer backfills such
  records with a constant epoch stamp rather than the upgrade time, so the backfill
  reproduces the order the user was already shown instead of reshuffling it. With both
  halves the sort is total over every mix of stamped, unstamped and same-stamped
  sources, so 跟随推荐 is neither ambiguous nor liable to drift on upgrade.

**Routing configuration is per backend and per model; health is global.** Cooldown
and health state stay **source-global**, shared across all consumers. From first principles: quota
exhaustion and network reachability are properties of the *account*, not of the
agent that happened to touch it — if Claude Pro's cycle quota is gone, it is gone
for every consumer. The current implementation already works this way (the
cooldown pool keyed on the shared source row, `_cooldown` in
`core/handlers/model_hub/service.py`); v3 keeps it deliberately.

### 4.3 Resolution pipeline (step 0 + three steps)

0. **Native-first channel dispatch — first-class product behavior.** On every turn,
   first inspect the route chain projected by §4.6. If its leading runnable hop is
   the backend's own `native_cli` subscription, launch the official CLI with its local
   login and zero Gateway credential injection. If that native source is exhausted,
   cooling, or unavailable in this process **before output starts**, continue within
   the same turn to the first runnable `hub` hop in that chain. If any output was
   already streamed, do not replay the request: end with §4.5's interrupted-turn copy,
   keep native cooling, and let the next turn select the Gateway hop. Once the native
   source is retry-ready again, the next Follow turn returns to it automatically. The
   UI and product copy must make this three-state story obvious: native now → Gateway
   takeover → native restored.

   This dispatch does not prepend native outside the chain. A Custom chain is an
   explicit override: when its first runnable hop is Hub, the turn uses Hub even while
   native is healthy. That is the §2 promise that the user owns every custom order,
   and avoids a hidden route before the chain the Gateway renders.

   A native subscription is bound to its sanctioned backend because only that CLI can
   consume the local login. A hub-held subscription is different: it is an ordinary
   Gateway upstream and may serve any backend through an explicit custom chain (§4.4).
1. **Capability chain** — obtain it from §4.6, and only from §4.6. This step adds no
   mapping, target rewrite, filter, or alternate ordering of its own.
2. **Runnable candidates** — retain the capability chain's order and select entries
   whose source health permits a retry **and** whose channel is available in this
   process. `healthy` and a cooldown past `retry_at` are health-ready; `needs_action`
   and `error` never are. Blocked entries remain visible and dimmed in the capability
   chain even though this turn skips them.
3. **Supply** — use candidate #1; on quota-exhausted/429, transient 5xx or
   network failure enter cooldown and take the next **within the same turn**;
   switch back on recovery. Convert protocol when needed. Every switch is
   appended to the human-readable 最近切换 log.

Error taxonomy (no blind fallback): parameter/protocol/tool-compat errors
surface to the caller; 401 → refresh once, then retry; 429 / explicit quota
exhaustion / transient 5xx / network → cooldown + next candidate, with cooldown
duration classified per cause (network / rate-limit / quota). Once streaming has
started, no transparent retry — see §4.5 for the copy this obliges.

**Cross-vendor supply is a built-in v3 capability** (owner rulings 2026-07-29 and
2026-08-07). A custom chain can name an OpenAI model from Claude Code, an Anthropic
model from Codex, and a hub-held subscription from either vendor in either backend.
The user configures concrete Source + model hops; engine translation remains invisible,
with no plugin surface and no warning beyond the Claude hub-add sentence in §4.1.

Syntax conversion exists. Semantic fidelity across tool calls, streaming, system
prompts, thinking/reasoning, cache semantics, and service tiers remains a GA evidence
question, not a second routing model. The parallel fidelity lane and §10 record that
gate without blocking the v3 specification of explicit chains.

### 4.4 Eligibility is server-authoritative (v3)

Which sources a backend may consume at all — independent of order and model
capability — follows the channel-aware matrix:

| Source | claude | codex | opencode |
| --- | --- | --- | --- |
| `api_key` (any vendor) | ✅ | ✅ | ✅ |
| `subscription`, vendor `anthropic`, channel `native_cli` | ✅ | ✗ | ✗ |
| `subscription`, vendor `openai`, channel `native_cli` | ✗ | ✅ | ✗ |
| `subscription`, any vendor, channel `hub` | ✅ | ✅ | ✅ |

`allowed_origins` enforces **channel semantics**, not a product-risk gate. For
`native_cli`, it contains only the sanctioned backend because the credential remains
inside that CLI. For `hub`, it may contain any supported backend selected by Gateway
configuration, including a cross-vendor consumer. No flag or consent record changes
either result.

The agents payload
now carries a per-source eligibility signal (`eligible` + `reason_key`) computed
once on the server. The UI stops deciding: the chokepoint `isSourceEligible`
(`ui/src/components/settings/models/menus/identifiers.ts`), which self-documents
as ESCALATED precisely because it hand-mirrors backend logic, becomes a pure
projection of server truth. This pays down a debt the v1 lanes escalated and never
closed — two independent implementations of one rule, free to drift silently.

`reason_key` is an i18n key, so the drawer can explain structural ineligibility.
The v5 vocabulary removes `consent_required` and `opencode_api_key_only`; Hub-held
subscriptions are eligible for OpenCode, and risk copy is not eligibility. Native
wrong-client use retains `subscription_wrong_client`.

**Server-validated invariants** (07-29, review round 6). Eligibility is not the only
rule the server owns rather than the schema. These hold on every agents payload and
each is enforced by the route that writes it, because JSON Schema draft-07 cannot
state them at all — the full list with the reason per item is in `api.md` →
「Mechanical guards the schemas cannot carry」, and the boundary itself is
`model-hub-contracts/README.md` → required-vs-optional:

- **`sources.order`** — every id exists, is eligible for this backend, appears once,
  and the whole list is a subset of the eligible set (omitting one is how the user
  excludes it from Follow routes). Rejected as `invalid_source_order`, naming the
  first offending id.
- **`model_supply`** — exactly **one row per menu model**: `model_id` values are
  unique, and the set covers that backend's whole menu. Duplicates are the dangerous
  direction: two rows for one model let `chain_length: 0` sit beside `chain_length: 2`
  for the same id, and since consumers read the first match, the 「无来源可供」 flag
  becomes a coin flip rather than a fact. A missing row is milder but still leaves the
  drawer unable to say anything about a model the menu offers. Neither half is
  expressible — `uniqueItems` compares whole items, so rows differing only in
  `chain_length` pass, and coverage is a relation to a different document.
- **`AgentChain.chain`** — default-policy entries preserve the relative order of
  `sources.order`; custom-policy entries preserve the exact configured hop order.
  A default chain contains at most one hop per Source. In a custom chain each exact
  `(source_id, model_id)` pair appears at most once; one Source may intentionally
  appear with different models. Every custom hop's Source exists, is eligible, and
  supplies its exact `model_id` when the mutation commits. It need not appear in
  `sources.order`, because that order belongs to Follow routes.

### 4.5 State taxonomy — classified by "does it heal itself"

Three classes, because the action owed by the user differs in each.

**Source-level `state.status`:**

| Status | zh (UI) | Heals itself | Meaning |
| --- | --- | --- | --- |
| `active` | 使用中 | — | currently serving |
| `standby` | 备用 | — | healthy, not at the head of some order |
| `cooldown` | 暂不可用 (gold) | **yes** | quota/rate/network; `retry_at` known; recovers unattended |
| `needs_action` | 需处理 (rose) | **no** | OAuth expired, balance exhausted, key revoked/banned — dead until the user acts |
| `error` | 异常 | **no** | unclassified failure — no `retry_at`, so nothing clears it unattended |

`needs_action`, introduced in v2 and retained by v3, carries a `detail_key` naming the cause, so the
row can offer **one tap to fix it** (re-auth, top up, replace key) instead of a
dead-end error string.

**`error` is a blocker, not a third class** (07-29, review round 6). This table
first wrote its self-healing column as 「unknown」, and that word was the root of a
real gap: `error` carries no `retry_at`, so nothing will clear it unattended, and the
chain contract has always counted it WITH `needs_action` in the branch that makes a
chain `interrupted` (`agent-chain.schema.json`). What we could not classify is the
**cause**; that is never a promise about **recovery**. Read as 「unknown」 it left one
transition unrepresentable — the last runnable source of a chain landing in `error` —
which is an interruption the surfacing rule below owes the user an explanation for,
while the event vocabulary had no cause that could carry it. The emitter's only options were
to borrow a cause nobody had established or to stay silent about the state we
understand least. So the vocabulary gained a fifth non-self-healing cause rather than
the obligation being quietly dropped: that transition is announced as
`kind: needs_action` with `reason: unclassified_error`, which is the exact counterpart
of `state.detail_key: models.source.error.unclassified`. It is not a new event kind —
「a source went dead and stays dead until someone acts」 is what `needs_action` already
means, and *which* way it died is what `reason` is for — and not a widening of
`supply_interrupted`, which nulls both endpoints and would strip the feed row of the
one source it needs to open. The five non-self-healing source keys and the five
non-self-healing event causes are a bijection, checked mechanically rather than
promised (`api.md` → 「Mechanical guards the schemas cannot carry」).

Two of those three taps use routes frozen by the current contract:
`PUT /api/models/sources/<id>/credential` replaces an api_key in place and
`POST /api/models/sources/<id>/reauth` re-runs OAuth bound to the existing source
(`api.md`; the adapter already exposes `start_oauth(source_id)`). Both are
**replacement, not re-creation** — deliberately, because "add a new source and
delete the old one" is not the same operation from the user's side: it loses the
source's `created_at` and its slot in every backend's order, so 跟随推荐 quietly
reshuffles as a side effect of fixing a key, and any `custom` order that named the
old id silently shortens. Recovery must not be a reorder. (Top-up is the third tap
and needs no *replacement* route of ours — no credential of ours changes; it is a
link out to the vendor.)

**What re-checks a topped-up balance is NOT 「the next probe or turn」** (07-29, review
round 8; the earlier wording claimed it was). `balance_exhausted` is a `needs_action`
state, `needs_action` is never `runnable`, and the probe answers `probe_no_candidate`
when the chain has no runnable member — so for a source that is the only supplier of a
model, both paths exclude the very source whose recovery they would have to observe,
and the state can never clear on its own. The same trap is generic to `needs_action`:
a re-keyed source recovers because `PUT …/credential` re-discovers through the new
credential, but a source whose blocker was cleared *at the vendor* has no such route.
Defining the path that may test a blocked source after an explicit user action — its
surface, and what it is allowed to spend — is an implementation requirement, recorded
as **AC-3** in `model-hub-implementation.md`. This spec does not invent that route at
round 8: it is a new tap on a frozen contract, and freezing it as a side effect of a
review round is how the last two rounds generated findings.

**Agent-level derived `supply_status`** (computed, never stored):

Derived from the same question, so the agent level splits where the source level
splits — on whether the user owes an action:

| Value | zh (UI) | Heals itself | Meaning |
| --- | --- | --- | --- |
| `ok` | 正常 | — | serving from the intended head of the chain |
| `degraded` | 降级 | — | serving via a fallback, and/or some sources in the chain are down |
| `waiting` | 暂时全部在冷却 | **yes** | nothing runnable right now, but every blocker is a cooldown — recovers unattended at the earliest `retry_at` |
| `interrupted` | 无可用来源 | **no** | nothing runnable and at least one blocker needs the user, or the capability chain is structurally empty |

`interrupted` is the honest name for the state v1 could not express: the source
list looks populated, yet *this* agent has nothing left to call. The UI shows
「当前无可用来源」 with a cause breakdown and exactly two exits — fix the
`needs_action` items, or add a source.

`waiting` exists to keep the surfacing rule below consistent. An agent whose
sources are *all* mid-cooldown has nothing runnable, but nothing is owed either —
it heals itself in minutes. Collapsing that into `interrupted` would tell the user to
go fix a problem that resolves before they finish reading the sentence, which is
exactly what the self-healing tier is supposed to prevent. Its copy states the
recovery time, not a fault; `current` is null in both states, so neither ever renders
a stale 使用中.

**Two grains, one taxonomy.** `supply_status` above is the **agent** rollup, and it
answers for that backend's *currently selected* model. The same three classes are
also evaluated per **(agent, model)** pair, as `supply_state` on the chain
(`agent-chain.schema.json`) — because the user routinely asks about a model that is
not the selected one: inspecting a chain, probing a menu item, reading why a turn
gave up. The rollup cannot answer those. A backend whose selected model is healthy
reports `ok` while some *other* menu model has nothing runnable at all, so anything
model-scoped that consults the rollup reports the wrong thing with confidence. Every
model-scoped consumer therefore reads the model-scoped field — the chain drill-in,
the probe's typed `supply` sibling, the turn record's `model_supply_state` — and the
rollup stays what its name says. One taxonomy, two grains, and only one definition of
「稍等即可」 vs 「需处理」 at each.

The predicate itself is stated **once, here**, and every contract that carries either
grain points back at this table rather than restating it: `interrupted` when the chain
is empty **or at least one blocker needs the user**, `waiting` only when every blocker
is a cooldown. The asymmetry is deliberate and load-bearing — `interrupted` is the
OR-branch, `waiting` the AND-branch, so a chain holding one cooling source and one
revoked key is `interrupted`. Reading it as "every member needs the user" leaves that
mixed chain matching neither value and, worse, hides the action the user is owed for
the revoked key behind the fact that something else in the chain is merely cooling.

**Surfacing tiers** (the colleague test: ask for action only when action is owed):

| Class | Where the user meets it |
| --- | --- |
| self-healing (`cooldown`, `waiting`, recovery, in-turn switch) | 最近切换 feed and the row's status pill — **and nothing in the turn when the turn survived** (07-29, review round 4 ruling): a fallback that worked is not news, and announcing it inside a turn that succeeded is exactly the interruption the push cut exists to prevent. In-turn copy appears only when the turn did **not** proceed transparently: the retry form when §4.3 forbids the transparent retry, the `waiting` form when nothing is runnable but every blocker is timed |
| `needs_action`, `error`, `interrupted` | the in-turn copy of the turn that hit it — the **interrupted** form's cause breakdown plus a pointer to 「模型」 — and 需处理 state on the 「模型」 page until cleared. A blocker left behind by a turn that **succeeded** is page-and-feed only, by the row above |

`error` is named in the second row explicitly (07-29, review round 6). It was
implicitly there all along — it is a blocker, and blockers are what the row is about —
but leaving it unnamed while the status table called it 「unknown」 is how a reviewer
ends up asking, correctly, which tier an unclassified failure belongs to.

**No proactive delivery** (owner ruling 2026-07-29 10:54; supersedes the recipient
machinery this section carried through review rounds 5, 7, 8 and 9). **No resolution
event is pushed anywhere.** Avibe does not open a conversation to report supply state:
an interruption is surfaced **in the turn that hit it**, and otherwise waits on the
「模型」 page for the user to come looking. That is the colleague test read strictly — a
colleague who cannot do the work says so when you ask them to do it; they do not
message every channel they belong to the moment their key expires. It also dissolves
the recipient problem the earlier rounds kept narrowing without closing (which scopes,
which grain of 「Agent」, what a zero-scope result means): with nothing delivered, there
is nobody to address.

What survives the cut, so the removed text is not read back in:

- resolution events are still **recorded**, unchanged in kind, cause and shape. They
  feed **the 最近切换 feed**; **source and agent status read live state, not events**
  (07-29, review round 4 — the earlier wording said the status UI reads events too,
  which contradicts the derivation rule below and would leave a recovered source marked
  affected until its event aged out of the bounded feed). A record answers 「what
  happened」; a status answers 「what is true now」, and only one of those changes when a
  source recovers silently. Recording was never the delivery mechanism; it is the record.
- `severity: info | action_required` survives as **feed/UI metadata only**: it sorts
  and styles the feed and decides whether a row reads 需处理. Nothing keys off it to
  send anything.
- the two tiers stay **cause-based, never count-based**: "zero runnable candidates" is
  not by itself a reason to put an error in front of anyone.
- **the record stays single-grained; per-backend impact is derived, not recorded**
  (orchestrator ruling, 07-29 — **supersedes** review round 8's 「expand every affected
  backend」 sentence, which existed to address a notification that no longer gets sent).
  A source-**STATE** kind — the source-health family, `cooldown` / `needs_action` /
  `skip`, and the availability form of `recover` — is recorded **once**, unattributed
  to backends: `agent` keeps its existing semantics — the discovering context, or
  `system` when nothing discovered it — and nothing in the record claims a set of
  affected backends. Source health is a property of the source, so the fan-out was
  never information the record held; it is a **live derivation** the consumers already
  have to do anyway, because backend orders and per-model chains change after the event is written and a
  frozen set would go stale the moment one does. The feed renders those state lines
  unattributed (「relay.example 连续超时 → 暂停使用 1 小时」, as the V4/V6 frames already
  show them). **The TRAFFIC kinds are outside this rule and must not be swept into it**
  (corrected 07-29, review round 10): `switch`, `channel_switch` and the fallback-return
  form of `recover` are one backend's traffic moving, and the frozen schema states that
  two-family split itself (`resolution-event.schema.json` → `from_source`: 「TRAFFIC
  events say where the traffic went … STATE events name the one source they are
  about」). Naming the backend there is the event's own subject, not an impact claim
  about a source — which is why AC-18's frozen example legitimately opens 「Claude
  Code:」 while a shared source's failure line does not. The round-9 wording 「the feed
  renders source events as unattributed lines」 was too wide: read over the traffic
  family it contradicted a frozen example this PR does not touch.
  The status surfaces answer 「what is affected」 by asking the current question
  against current orders — **at two grains, which must not be collapsed** (corrected
  07-29, review round 9). The SOURCE grain: **a backend has affected supply when a
  source that is blocking *now* appears in the capability chain of at least one of its
  protected models** — the (backend, model) test `api.md`'s supply guard already
  computes from its four-fact union. The AGENT grain is narrower, and is what an agent
  status pill reads: **the chain it evaluates is the chain of the model that Agent
  effectively runs** — its explicit `agents.<name>.model`, or the
  `agents.<backend>.default_model` it inherits (ruling #4's effective-model rule) —
  never the protected union. **A grain says which chain to read; it never says which
  class comes out** (corrected 07-29, review round 10). The class is the taxonomy's,
  and this paragraph now says so **without restating it** (corrected again 07-29, review
  round 12 — the round-10 wording claimed not to restate the predicate and then restated
  it, as 「nothing runnable and at least one blocker needs the user」, which silently
  dropped the OR-branch's other half: a **structurally empty** chain has no blocker at
  all, so a forced deletion or an emptied order would have fallen out of `interrupted`
  and lost the 需处理 state and the in-turn explanation the user is owed. The table and
  `agent-chain.schema.json` both carry the empty-chain case; a restatement that has to be
  kept in sync with two of them is the defect, not the wording). **Read the class off the
  table above** — that is the same 「point back rather than restate」 rule every contract
  carrying either grain already follows. Round 9 wrote blocking-source *membership* as the test,
  which reported `interrupted` for the ordinary fallback — source A revoked, source B
  still serving — where the table says `degraded` and the turn goes straight through.
  Membership is not exhaustion, and the one place the predicate lives is that table.
  The union is deliberately wider than the live selections, so evaluating it at the
  Agent grain would render an Agent affected over a ticked-but-unassigned menu model,
  which is exactly the case AC-9's Case A forbids (`model-hub-implementation.md`, AC-9). Both halves are
  current-state reads, and both are load-bearing
  (07-29, review round 3): chain membership alone folds a *historical* event against a
  *current* chain, and since a recovered source normally stays in the same orders and
  chains, and the failure event stays in the bounded feed, that predicate would pin the
  pill to 「affected」 for as long as the event is retained — a recovery could never
  clear it. The event is what the **feed** renders; it is not what the **pill** reads.
  The pill reads the source's live blocking state — the same **contracted** facts the
  resolver consults when it picks a candidate: `state.status` and, for a cooling-down
  source, `state.retry_at` (`source.schema.json`), surfaced per chain entry as
  `runnable` (`agent-chain.schema.json`). **No `blocked_until` field exists anywhere in
  the contracts** (corrected 07-29, review round 9: the earlier
  `blocked_until` / disabled / credential-invalid wording named a field the frozen
  schemas never defined, and a source-level `disabled` the frozen source contract does
  not express at all — `state.status` admits only `active | standby | cooldown |
  needs_action | error`, of which `cooldown`, `needs_action` and `error` are the blocking
  ones, and `source.schema.json` carries no enablement field (corrected again 07-29,
  review round 15: the round-9 text named `disabled` as a `state.status` value, inviting a
  `status == "disabled"` predicate that never matches)) — so a source that has recovered
  stops contributing to any pill on the next render, with no event written to say so and
  none needed. That test is the
  consumer's, evaluated at render time; it is not a field.
  Note that the chain grain is what makes it right: a `follow` order holds every
  eligible source — an API-key source is eligible for every backend — so a GLM-only key
  sits in Codex's order while appearing in no chain Codex can run, and treating order
  membership as impact would mark a backend degraded over supply it could never have
  used. A **backend-scoped** kind (`supply_interrupted`, whose cause is that backend's
  own order or selection) still names exactly the one backend it is about, because
  there the backend *is* the subject of the event rather than a consequence of it.
  If some later consumer genuinely needs a recorded affected-set, it gets a field then,
  with the evidence that derivation was insufficient — not speculatively now.
- **which named Agents an interruption is about** is narrower still, and it is now a
  question the delete guard and the confirm dialogs ask rather than a delivery one:
  `SupplyGap.agents` resolves the Agents whose **effective** model is the one that
  loses supply — including the Agents that inherit the backend default, since those
  Agents do use the model — and it is **allowed to be empty**, because the protected
  set is deliberately wider than the live selections: it protects a model the user
  ticked and assigned to nobody, which is right for refusing a delete. An empty list is
  a correct answer, not a missing one.

The event carries no recipient, channel, platform, or audience field — it never did,
and after this ruling nothing would want one. The feed is a record of what happened to
supply; it is not an outbox.

**In-turn error copy is the normative surfacing mechanism, and silence is its first
case** (07-29, orchestrator ruling on review round 4 — this **supersedes** round 2's
action tail and the 「已自动换线」 in-turn line). A turn supply affected either says
nothing at all, or says exactly one of three **classes** — never a fourth story, and never
a tail appended to one of them. The `interrupted` class carries **two copy variants**
(below), so three classes are four message forms; what is forbidden is a fourth *story*,
not a fourth string (clarified 07-29, review round 15 — the count read 「three things」
after the 16:35 split created the second variant, and read literally it would have forced
one required form to be merged away):

- **survived transparently → silent.** A fallback that carried the turn produces **no
  in-turn copy whatsoever**. The turn worked; the user asked for work and got it, and
  interrupting a successful answer to narrate the plumbing is the same restraint failure
  the push cut removed one layer up — it is a push, merely delivered in-band. The switch
  is recorded and surfaces where a record belongs: the 最近切换 feed and the row's
  已切换 state. **This includes the case where the switch left a real problem behind** —
  a revoked key a second source covered is filed as its usual two records
  (「one `switch`, info + one `needs_action`, action_required」), and the `needs_action`
  half surfaces as 需处理 on the 「模型」 page, not as a line on a turn that succeeded.
  Round 2 reached the opposite conclusion by asking 「where else would the user hear
  about it」 and answering 「nowhere」; the page is that somewhere, and it is the surface
  the cut deliberately kept for exactly this class. Note the implementation consequence,
  which is why the ruling went this way: a successful turn returns through the normal
  result path and never reaches the failure emitter, so **every form below is on the
  failure path** and the copy has one home rather than two.
- **self-healing** — supply moved and the turn could **not** proceed transparently:
  「下一回合已自动换线，直接重试即可」 when §4.3 forbids the transparent retry. It names
  no fault and asks for nothing, because the user's next action is one retry. This is
  the whole of the form now: the surviving-turn line above moved to silence.
  **Confirmed by the orchestrator, 07-29**: the tail is dropped from *this* case too, not
  just from the silent one — the retry moment's action is the retry itself, and
  residual-blocker guidance lives in the interrupted copy and the 「模型」 page. Keeping a
  tail here would leave one exception whose only argument — 「the user is already being
  interrupted, so one more line is free」 — is the argument the whole section rejects.
- **waiting** — nothing runnable *right now*, but every blocker clears on a timer with
  no user action: the copy states **what it is waiting on and when it recovers**
  (「全部来源冷却中，约 12 分钟后恢复」), and asks for nothing. It is a distinct form
  rather than a variant of the other two, because the self-healing copy would be a lie
  (the turn did not survive) and the interrupted copy would be worse than one (it would
  send the user to 「模型」 to fix something that fixes itself). A turn whose blockers
  are mixed — some timed, some needing action — is `interrupted`, not `waiting`: the
  AND/OR taxonomy above decides that, and the presence of one user-actionable blocker
  is what makes the third form the wrong one.
- **interrupted** — this class has **two entries, and they need different copy**, because
  the taxonomy's OR-branch admits a case with nothing to break down.
  - *blocked* — nothing runnable and at least one blocker needs the user: a **cause
    breakdown** at the grain that actually failed (which model, which sources, and which
    blocker each of them is in), then a pointer to 「模型」, where the one-tap fixes live.
    This is the only place the user is told to act, so it carries the whole story rather
    than a truncated headline that forces a second question.
  - *structurally empty* — the chain has no candidate at all, so there is no source and no
    blocker to name. **The copy is owned by the lane that emits it** (L3, §3), and the
    exact strings land in that lane's design pass, which carries **its own owner approval
    step** — they are not written here (07-29 16:35 ruling). What this spec fixes is its
    **semantics**, and all three are already contracted: it **names the model** the turn
    asked for, it states the cause using the **event layer's own reason**
    (`no_enabled_source | no_eligible_source | model_unsupported`, below) **rendered
    through `vibe/i18n/` like every other backend-emitted string**, and it **points at
    「模型」** like the blocked form. A generic error, or silence, satisfies
    none of the three.

One asymmetry has to be named, because it is easy to implement wrong: an agent can
enter `interrupted` with **no source changing state at all** — its last enabled
source is dropped from its order, or its selected model stops being supplied by
anything left in that order. Every other entry in the feed is keyed on a source, so
that transition gets its own agent-scoped event kind (`supply_interrupted`, with
`reason: no_enabled_source | no_eligible_source | model_unsupported` naming which
one-tap fix applies) instead of borrowing a credential or quota reason that would
misstate the cause. It fires once, on the transition — never once per starved turn.
Its counterpart guard is on delete: refusing to remove a source that is the last
enabled supplier of some **selected model** for some backend (`api.md`) is
what keeps this event rare rather than routine. Note the grain — per (backend,
model), not per backend. A backend with four enabled sources is not safe by
inspection: if only one of them supplies `claude-haiku-4-5`, deleting it starves
that model while the backend still looks well supplied, and the user learns about it
from a failed turn. Backend-level emptiness is just the case where every selected
model hits zero at once. **"Selected" is deliberately wider than 「已勾选」**: it is
the union of an open menu's checked entries, every menu model that owns a custom route
chain, `agents.<backend>.default_model`, and each enabled Vibe Agent's own `model`.
The protected identifier is always the **menu model**, never a hop's upstream
`model_id`, because the menu model is what an Agent can run and what the chain query
addresses. A custom chain that no Agent currently selects still represents deliberate
configuration and remains protected from a silent Source deletion; its
`SupplyGap.agents` list may correctly be empty. `api.md` → DELETE carries the full set
and the confirm copy names affected Agents when any exist.

Exact-hop referential integrity is a separate guard from the supply-gap calculation.
A non-forced Source DELETE refuses whenever any Custom chain names that Source, even
when a later hop still supplies the menu model, and returns `source_in_custom_chain`
plus ordered `would_remove_hops` entries naming each `(backend, menu_model,
source_id, model_id)` reference. `force=true` is an explicit cascade confirmation:
the same transaction deletes the Source and every exact hop that names it, while the
identity and relative order of all surviving hops remain unchanged. The route stays
`custom` even if no hop survives, so deletion never silently changes it to Follow.
Any resulting protected-model gap is reported through the existing
`would_interrupt` projection alongside `would_remove_hops`. This explicit cascade is
not the silent side effect prohibited by §2; without the confirmation, neither the
Source nor any chain changes.

**Turn provenance.** Each turn whose attribution is **exact** records the model@source
that served it — the write rule below is what 「exact」 means, and it is the promise's
scope, not a caveat on it — and that record is readable through a contracted route.
**It has no chat surface** (owner
ruling 2026-07-29 14:03, superseding the earlier 「per-turn detail in the conversation
surface」 phrasing): users should be unaware of supply machinery, so provenance
inspection is a **debug affordance, not a user feature**, and it appears neither in
the Web transcript nor on any IM platform. If it is ever surfaced, the place is the
请求日志 / 诊断 entry in the 「模型」 page's 高级 area — a post-v3 candidate, not v3. Mid-stream failure, where no transparent
retry is permitted (§4.3), must say exactly 「下一回合已自动换线，直接重试即可」 and
nothing further: the user's next action is one retry, so the copy states that instead
of describing the fault. A source the switch left needing repair surfaces as 需处理 on
the 「模型」 page, per §4.5 — not as a second line here.

This promise needs an interface, not just a paragraph, so the frozen contract carries one:
`turn-provenance.schema.json` + `GET /api/models/turns/<turn_id>/provenance`.
It defines *what* is recorded and *how it is read*; where it is stored is the
implementing lane's call, with one constraint — provenance is written when the turn
resolves and stays readable after the process exits, because "which source paid for
this turn" is a billing question the user asks days later, not just live. A turn
that switched sources mid-flight lists every attempt in order, so the record
explains the switch rather than merely naming the winner.

**The write rule is: record only when the attribution is exact** (07-29 15:07 ruling,
guarded 16:35). The gateway credential is minted per **process scope**, and the turn is
resolved from that scope through the turn FSM. When the scope holds exactly one active
turn **and no untracked caller is sharing it**, the attribution is a determination and the
record is written; concurrent turns — or any use of that scope the FSM cannot see, which a
shared transport makes possible — mean **no provenance record is written at all**. One
tracked turn is not the same as one user, and a rule that conflated them would license
exactly the misattribution this design exists to remove. Absence is honest, and it is all
that remains — a healthy attempt emits no transition event either. `turn_id` therefore
stays required, and nothing in this interface marks a degraded grain, because no record
has one. **The correlation mechanism itself — scope keys, token registry, the FSM
lookup — is designed and owned by L3** (07-29 16:20 ruling): see L3's design note,
bounded by the invariants in `model-hub-implementation.md` §3. This spec fixes what must
be true of the record, not how the attempt is tied to the turn.

**The same rule bounds which turns v3 records at all: no FSM truth → no record**
(07-29 15:42 ruling). Exactness is resolved through the turn FSM, so a turn the FSM
does not track cannot be recorded exactly — and v3 does not record it approximately.
**IM and CLI turns write no provenance in v3**, and this spec states that limitation
rather than implying a coverage it does not have. The loss is debug-marginal because
the *other* half of the trace is channel-independent: the source-grained
resolution-event feed covers every channel, so an IM turn's failures and switches still
appear in the feed even though its per-turn attempt list does not exist. **Post-v3
candidate**: extend FSM registration to the IM and CLI dispatch paths — provenance then
follows for free, because the write rule above is path-agnostic and needs no change when
coverage widens.

**That interface covers Gateway-mode turns** (07-29, review round 8): a `served` record
requires a `source_id` matching `^src_`, and a Direct-mode turn runs from native
configuration with no `Source` row to name — so 「每个回合都有记录」 is satisfiable
inside Gateway mode and unsatisfiable outside it without fabricating a source. Existing users
stay in Direct until they migrate (§6), which makes this the common case rather than
an edge one, so it is named here instead of left to the implementer to discover.
Whether a Direct turn gets a no-source provenance representation or the route answers
「此回合无网关记录」 is an implementation requirement recorded as **AC-1** — a question
about the record and the route only, since 14:03 left no affordance to render it.
**Neither branch licenses silence** (07-29, review round 8): the every-turn promise above
is scoped to Gateway-mode turns by the paragraph that opens this one, and a Direct turn must
still answer the contracted route with a documented payload or a documented error — what
it may not do is come back indistinguishable from a turn whose provenance was never
written. The current contract chooses and tests the representation; v3 does not reopen
it. Cancellation remains FSM truth rather than transport inference.

Four outcomes are recorded, not one: `served`; `exhausted` (fallback walked to the
end, every attempt failed for a fallback cause); `failed_terminal` — an attempt hit
one of §4.3's **non-fallback** errors and the turn stopped there, param/protocol/
tool-compat, or anything after the first streamed token, where no transparent retry
is permitted; and `no_candidate` — the turn that never touched a source, because this
(agent, model) chain had nothing runnable. `failed_terminal` is the fourth because
§4.3's error taxonomy deliberately routes those errors to the caller instead of the
next candidate, and a record with only three outcomes had no honest slot for the
result: not served, not exhausted (the chain was never exhausted), not
`no_candidate` (something was tried). Filing it as `exhausted` would additionally
force a fallback reason like `server_error` onto it and blame the user's account for
a malformed request. `no_candidate` is precisely the turn a user needs explained, so
the record has to hold an **empty** attempt list rather than force the emitter to
fabricate a phantom attempt or write nothing at all; it carries the model-scoped
`waiting`/`interrupted` state instead, which is the thing that actually explains it.
The terminating attempt is recorded in exactly one place — failed
attempts in an ordered list, the served attempt in its own field, the terminal error
in a third, at most one of the latter two ever populated, the full sequence
reconstructible by appending. That is a shape decision rather than a validation one:
it makes 「两个成功者」, 「成功者不在最后」 and 「摘要指向列表里没有的来源」 impossible to
write down, instead of invariants prose asks every implementer to respect.

### 4.6 The only chain projection — per (backend, menu model)

This section is the **single normative derivation** of the chain that the resolver,
chain API, probe, event correlation, provenance, menu counts, and Gateway UI consume.
Those callers may filter its `runnable` members or render its annotations; none may
re-derive source/model pairing or order.

For backend `B`, caller-facing menu model `M`, and `B`'s effective Source order `O`:

1. Read `M`'s route policy.
2. If the policy is **custom**, read the stored hop list verbatim. Every hop is
   exactly `{source_id, model_id}`. Mutation-time validation requires each Source to
   exist, be eligible for `B`, and advertise that exact upstream model. Each exact
   pair appears at most once; a Source may appear again with another model. The
   capability chain is those hops in that order; vendor and model may differ from hop
   to hop. `O` does not filter a custom chain.
3. If the policy is **follow** (the default), walk `O` once and project at most one
   hop per Source:
   - reject the Source when it is not eligible for `B` and its channel (§4.4);
   - for OpenCode, split `M` into `vendor/model`, require the Source vendor to match
     the provider segment, and use the bare model id;
   - for a fixed-menu backend, require its native vendor — Anthropic for Claude Code,
     OpenAI for Codex — before any alias or exact-identity rule. A foreign-vendor
     Source enters that backend only through an explicit Custom hop;
   - for a fixed-menu `native_cli` Source, preserve an exact CLI alias such as `opus`
     or `sonnet[1m]`; the installed official CLI owns that alias;
   - for a fixed-menu hub Source on the backend's native vendor, resolve built-in
     aliases against **that Source's discovered inventory only**: a version alias
     chooses the latest dated id for that exact version; `opus`, `sonnet`, `haiku`,
     `opus[1m]`, and `sonnet[1m]` choose the latest version/date in their family; a
     dated request stays exact;
   - otherwise use exact identity only when the Source advertises `M`. Manual
     inventory, foreign-vendor Sources, and undiscovered ids never enter the
     automatic alias branch. The follow projection never invents a cross-vendor
     substitution; that requires a custom hop.
   - include the hop only when the Source advertises the resulting upstream model.
4. Annotate every capability hop with its channel, source-global health, current
   process availability, `runnable`, reason, and `retry_at`. Do not remove or reorder
   blocked hops. The runnable candidate list is the resulting chain filtered to
   `runnable: true`; it is not a second chain.

Therefore a healthy native subscription at position 0 leads its own backend's Follow
routes; when it is cooling, the first runnable hub hop later in the same projection
takes over; when it recovers, the unchanged projection naturally selects it again. A
Custom chain can instead name `src_chatgptplus + gpt-5.6` ahead of
`src_anthkey01 + claude-sonnet-4-6` for Claude Code, making both the Source/model
choice and the deliberate native-first override explicit.

**Mapping evolution proposal — owner-vetoable (2026-08-07).** Contract v5 removes
`mappings` and `PUT /api/models/agents/<backend>/mappings`. Before changing any
eligibility rule, `allowed_origins` interpretation, or backend Source order, the
upgrade snapshots each backend's v4 resolver-effective Source order, eligibility
decisions, and advertised models. Legacy rows are then grouped by `builtin_id`. The
v4 resolver uses the first enabled row in stored order, so that resolver-effective row
alone defines the group's target; later enabled duplicates are ignored as shadowed and
do not overwrite it. A group with no enabled row becomes `follow`. This normalization
preserves current behavior even for duplicate rows that the old write path accepted.

Each resolver-effective mapping is then materialized into one Custom chain by walking
that **v4 snapshot** and emitting `(source_id, target_model_id)` for every Source the
v4 resolver would have considered for that target. All hops therefore share one target
model while retaining exactly the old fallback supplier set: a Hub-held subscription
made newly eligible by v5 does not enter a migrated Custom chain until the user edits
that route. The legacy behavior is a single-target, potentially multi-hop chain, and it
is one-hop only when exactly one Source can supply the target. If an effective mapping
cannot produce any valid hop, the upgrade fails closed and asks for configuration
review; it never falls through to a shadowed duplicate or keeps a live mapping beside
an empty chain.

The upgrade also converts the two persisted diagnostic stores before publishing v5.
Every attempt slot in `model_hub_turn_provenance.json` replaces `via_mapping` with the
non-lossy pair `route_policy: "legacy"` plus `requested_model_changed` equal to the old
boolean. A historical record cannot be relabeled Follow or Custom from today's config,
because the route may have changed since that turn; `legacy` states that evidence gap
instead of inventing attribution. Migrated provenance records and converted resolution
events carry `migrated_from_contract_version: 4`; v5 schemas permit `legacy` only with
that discriminator and forbid the discriminator on newly written rows. New v5
provenance records permit only `follow` or `custom`. Every
`mapping_applied` / `mapping` row in `model_hub_resolution_events.json` becomes the v5
`route_model_rewritten` / `configured_route` equivalent, preserving its id, timestamp,
backend, model, billing/severity fields, and recorded human strings. K3 must stage and
validate the converted config and both bounded stores, then publish them through one
recoverable upgrade transaction. A crash at any commit point resumes to all-v5 or
restores all-v4; readers never observe a mixed version, and historical feed and billing
attribution are never deleted. Only after this transaction commits are legacy mapping
rows removed. These retained diagnostics are historical evidence, not a second routing
authority.

This replacement is smaller and more honest than coexistence: one route owner answers
both “which Source?” and “which upstream model?”, one projection powers runtime and UI,
and every future validation applies to one shape. Keeping mappings would preserve an
implicit Source choice beside an explicit Source choice, making precedence and display
truth depend on which consumer happened to read first.

The chain query remains
`GET /api/models/agents/<backend>/chain?model=<id>`. Contract v5 adds the matching
mutation on the same resource: `PUT` with `{policy: "follow"}` or
`{policy: "custom", hops: [{source_id, model_id}, ...]}`. The read projection carries
the policy plus ordered hop annotations. A menu model whose capability chain is empty
is flagged 「无来源可供」; a non-empty all-cooling chain is `waiting`, not empty.

### 4.7 Downstream — Agents

| Agent | Menu | Notes |
| --- | --- | --- |
| Claude Code | fixed (built-in model IDs) | each built-in menu model follows the backend Source order or owns an exact custom route chain; adding new menu entries belongs to the deferred Configure Agents module |
| Codex | fixed | same |
| OpenCode + future in-house agents | open | follows upstream model lists; supports user-defined custom model entries |

### 4.8 OpenCode identifier scheme (locked 07-23, retained in v3)

OpenCode models are `provider/model-id`. Rules:

- The provider segment uses the **standard vendor id** (`anthropic/`,
  `openai/`, `zhipuai/`, …) — identical to native OpenCode usage. No
  `avibe-` namespace (owner: keep it simple). Unrecognizable vendors fall
  back to a single `custom/` provider.
- Gateway mode merely redirects those providers' transport to the local Gateway in
  the generated runtime config overlay. Therefore **identifiers are stable
  across Gateway/Direct switches, across source add/remove/failover, and — new in
  v3 — across any backend-order or per-model-chain edit**; never encode a concrete source into
  the provider segment.
- Users never hand-assemble the string. Menu checkboxes pick models; the
  custom-model form generates and previews the identifier (source + model ID
  in → `zhipuai/glm-5.2-air` out). A custom model entry is, in data terms, a
  supplement to that source's supply list.

## 5. Surfaces — two modules, one understandable handoff

Concrete first-run example: the user adds Claude Pro with the recommended “Use Claude
Code login” choice, then adds an Anthropic API key. The Gateway module shows one
continuous route for Claude Code: `Claude Pro (native) → Anthropic API Key (Gateway)`.
If Claude Pro cools down, the first hop dims, the second becomes current, and the
status says it will return to native automatically. The user does not have to infer
that “native” means Direct mode: this route is still **Gateway mode**, because Avibe
owns the handoff. Direct mode bypasses that route entirely.

The Models page has exactly two top-level product modules:

| Module | Owns | Does not own |
| --- | --- | --- |
| **Sources** | Add/edit subscription and API-key Sources; credential location; discovered/manual model inventory; usage and source-global health | Source order, model pairing, fallback policy |
| **Gateway** | Backend mode; backend Source order; per-menu-model follow/custom route chains; exact Source + model pairing; runnability, current hop, retry/failover/recovery state; probe and diagnostics entry | Credential entry, Agent-definition settings |

The visual connection between a Source and Gateway answers only current facts: enrolled,
used by N routes, serving now, cooling, or needs action. It has no id, CRUD route,
drag handle, or persisted policy. Configuration lives at one of the two real owners:
the Source or the Gateway chain.

Required interaction rules:

- Sources remains an unordered asset inventory; there is no reorder affordance in that
  module.
- Gateway is the primary editing surface. Backend Source order and per-model chains
  live together so the user never edits a fallback list in one place and its model
  pairing in another.
- A model row shows whether it follows backend Source order or owns a Custom chain.
  Opening it renders the exact §4.6 projection; blocked hops remain in place and dim.
- Adding a subscription selects `native_cli` by default. Choosing “Use as Gateway
  upstream” is explicit. Only the Claude + Gateway branch shows §4.1's one-sentence
  warning; it is informational, not a consent flow.
- Recently switched and source/route status remain pull surfaces. A successful
  fallback adds no copy to the turn (§4.5).

The existing V6 frames remain a visual baseline for row density, health states, and
mobile treatment, but their Agent-card grouping and mapping drawer are not v3 product
authority. The future UI lane must author and obtain approval for new desktop/mobile
frames covering both modules, native → Gateway takeover → native recovery, default
versus custom model chains, and the Claude hub-add warning before implementation.

**Deferred third module: Configure Agents.** The intent is to let users add models,
reasoning effort, and related model preferences to Agent definitions from this product
area. v3 does not define its information architecture, data contract, controls, or
delivery lane. It must not appear as a placeholder third module in the v3 UI.

## 6. Modes & migration

- **Gateway (wire value `hub`, default)**: Avibe injects runtime-only configuration into processes
  it launches (env vars for Claude Code; `-c` overrides for Codex app-server;
  `OPENCODE_CONFIG` overlay for OpenCode, gateway-config hash tracked for
  long-lived `opencode serve`). Native user configs are never written.
- **Direct (legacy, kept, not recommended)**: current behavior preserved —
  per-backend native config editing (auth tabs, API key + base URL, writes to
  `settings.json` etc.), useful for diagnostics and self-managed setups.
- Backends can differ in mode; the Gateway module surfaces the mode per backend.
  A `native_cli` hop inside Gateway mode is not Direct mode: Avibe still owns the
  pre-stream same-turn fallback and recovery policy.
- **Native-config import** remains copy-only
  and reversible, a per-item checklist grouped by backend. API keys + base URLs →
  direct import; subscription OAuth → `keep_native` by default (stays in the CLI's
  sanctioned store and becomes a `native_cli` source by default). A hub-held
  subscription is established only through the explicit OAuth add flow, not by
  importing a native credential file; Codex `auth.json` → `keep_native`. Footer
  promise: originals never modified or deleted; Direct always available. Triggers:
  first open after upgrade, setup wizard, backend-page banner.
- **Add-source closing loop (v3).** Creating a source answers "so what now?" in
  the same response: `adopted_by: [{backend, policy}]` tells the UI which agents
  picked it up automatically (those on 跟随推荐), so the success state can say so
  and offer one-tap enable for the agents on 自定义 that did not.

## 7. Security boundaries

- Three credential rings, never mixed: management key (Avibe→engine admin
  API), local gateway token (the only thing backends receive), upstream
  credentials (API keys and explicitly hub-held subscription OAuth tokens;
  engine-held in a restricted local runtime directory, not `~/.cli-proxy-api`).
  By default subscription credentials remain in the official CLI store through
  `native_cli`; the engine holds one only after the user selects the Gateway path.
- Credentials never enter Avibe Cloud, IM messages or logs. Static keys may
  integrate with Avibe Vault; no duplicate key entry across surfaces.
- Gateway failure is fail-closed; Direct mode is the explicit escape hatch.
- The contracted dry-run probe inherits the redaction invariant of resolution
  events: it reports classified outcomes, never raw upstream error bodies.

## 8. Data plane

The Gateway data plane is a **replaceable, Avibe-managed, versioned runtime
dependency** (current candidate: CLIProxyAPI ~14 MiB download / ~41 MiB
binary): pinned version + SHA256, 127.0.0.1-only listener, random management
key and gateway token, lifecycle owned by Avibe. Its YAML/auth files/manage
UI are **not** product surface.

**v3 routing requires no new engine policy.** Failover is ours, not the engine's: the engine
runs as a single global instance with its own cooling and request-retry disabled
(`vibe/model_hub_runtime/config.py`), model prefixes pin the source, and Python
owns candidate walking and error classification. That boundary was chosen because
the engine's blind switching is broader than our signed error taxonomy
(`model-hub-engine-survey.md`, P0). Per-model custom chains sit above that line:
Python projects and walks exact `(source_id, model_id)` hops; the engine executes the
one pinned hop it receives.

## 9. Explicit non-goals (v3)

- **No product-global priority list.** Ordering exists only as one backend's
  Source order or one `(backend, menu model)` Route chain.
- **Per-model ordering is explicitly in scope.** Owner ruling 2026-08-07
  supersedes v2's “No per-model ordering” non-goal. The scope is exactly §4.6;
  there is no session-level or request-level editor.
- **No health-scoring or smart auto-reordering.** No latency ranking, no learned
  preference, no cost optimizer. This is the §2.4 predictability promise — a
  product decision, not a missing feature.
- **No session-level source pinning.** "Run just this turn on that source" is a
  diagnostic need, served by Direct mode plus the dry-run probe — not by a
  per-session override that would make spending unpredictable.
- **No automatic model substitution.** Follow policy never invents a different
  upstream model for a foreign vendor. Cross-vendor supply is explicit through a
  custom Source + model hop; that hop may use an API key or a hub-held subscription
  and requires no additional warning.
- No billing-grade accounting, multi-tenant pools, or operator consoles.
- No third source category ("relay" merged into API Key).
- No v3 Configure Agents module (§5), runtime plugin UI, or GA scope beyond the
  three directions recorded in §10.

## 10. Open items and GA research directions

These items do not enlarge the owner-approved GA scope. They turn the three accepted
directions into questions that later lanes must answer before writing mechanical gates.

1. **Conversion fidelity (parallel K2 lane).** Extend
   `model-hub-engine-survey.md` with an agentic capability matrix and go/no-go per
   Anthropic ↔ OpenAI conversion pair: tool calls, streaming, system prompts,
   thinking/reasoning, prompt cache, and terminal error semantics. Record which pairs
   are engine-core and whether any require an engine build change. Do not expose
   plugins as user configuration.
2. **Release gate: engine asset mirror.** Research the exact Avibe-owned mirror,
   provenance, manifest publication order, availability monitor, restore behavior,
   and immutable-SHA evidence required before the pinned engine is a GA dependency.
   Do not change the pin or claim the gate complete in this specification.
3. **Release gate: platform matrix.** Re-verify install, startup, upgrade, rollback,
   and smoke evidence for every platform the current runtime contract lists. Decide
   the minimum repeatable evidence and unsupported-host behavior. Do not add platforms
   or platform-specific product promises here.
4. **Coordinated contract v5 and implementation batch.** The frozen v4 files remain
   untouched by v3. The implementation plan's v5 revision-set handoff is exhaustive
   for chain, subscription-channel, eligibility, event/provenance, and retired-consent
   effects; its first implementation lane freezes them once before downstream work.
5. **Configure Agents — deferred.** First-class user-added menu models, reasoning
   effort, and Agent-definition configuration belong to the deferred third module.
   Its architecture and contract are intentionally absent from v3.
6. **Later diagnostics and accounting.** Request-log UI, fallback spend attribution,
   and quota projection remain post-v3 candidates. Each needs evidence from existing
   provenance/usage data before it becomes a product promise.
7. **Remaining UI evidence.** New desktop/mobile frames, empty and failure states,
   Dark variants, and English copy need owner approval under §5's two-module model.
   Rejected V5 explorations remain history until separately deleted.
8. **Engine-owned OAuth file import.** Keep `controlled_import` deferred until a
   concrete adapter capability can preserve refresh semantics; explicit OAuth add is
   the only hub-held subscription path in v3.

## 11. Owner acceptance checklist (~10 min)

- [ ] §0 and §2 say “default local model Gateway” and preserve native subscription
      first → pre-stream same-turn Gateway takeover → automatic native recovery as
      one story, while Custom order remains authoritative.
- [ ] §3 makes Gateway a first-class noun and the owner-vetoable banned-term table
      matches the intended UI language.
- [ ] §4.1 defaults every subscription to `native_cli`; explicit hub-held Claude is
      the only branch with a warning, and no flag or consent mechanism remains.
- [ ] §4.2's recommendation puts the own-backend native subscription first and never
      reorders a custom backend order or custom model chain.
- [ ] §4.4 allows every hub-held subscription to serve every backend while retaining
      native CLI's sanctioned-backend binding.
- [ ] §4.6 is the document's only chain derivation, and its custom hops are exact
      `(source_id, model_id)` pairs.
- [ ] The owner-vetoable mapping evolution is acceptable: materialization from the v4
      resolver snapshot preserves every existing supplier and duplicate-row semantics;
      config plus both diagnostic stores upgrade recoverably; no dual routing authority.
- [ ] §4.5 keeps state source-global, status live-derived, successful fallback silent,
      and proactive delivery cut.
- [ ] §5 has exactly Sources + Gateway modules; the connector is state-only and
      Configure Agents is deferred without a placeholder design.
- [ ] §6 clearly distinguishes a native hop inside Gateway mode from Direct mode.
- [ ] §9 explicitly supersedes the old no-per-model-ordering non-goal and keeps
      automatic model invention out of Follow policy.
- [ ] §10 records only fidelity, asset-mirror, platform-matrix, vocabulary, and
      deferred research directions; it does not expand GA scope.
- [ ] The implementation plan appends AC-22 through AC-24 and assigns the complete
      frozen-contract impact to one coordinated v5 lane.
