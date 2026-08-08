# Model Hub — Product Spec

Status: **v3.0** (2026-08-09) · supersedes v2.0 (2026-07-29) outright
Owner decisions incorporated through: 2026-08-09 (+08:00)
Design source: `../avibe-docs/design.pen`. The V6 frames remain the visual baseline;
the v3 interaction draft for the two-module information architecture is owner-approved
as the implementation baseline (2026-08-07 afternoon). The design lane still owes
production-complete desktop/mobile states.
Contracts: `model-hub-contracts/` remain unchanged by this docs-only revision.
`model-hub-implementation.md` records the exhaustive final-shape handoff that the first
implementation lane must land atomically with every contract consumer and test.

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
formally supersedes v2 §9's “No per-model ordering” non-goal on 2026-08-07. §4.3 is
the only normative resolution algorithm for both policies; no mapping pipeline or
second derivation survives elsewhere in this document.

The final product has no fixed-menu `mappings` structure beside route chains. A
single-target choice is simply the degenerate Custom-chain case in which the hops use
the same `model_id`; richer chains may choose a different model per Source. The final
shape and its rationale are in §4.6 and are **owner-vetoable (2026-08-07)**. Model Hub
has not shipped, so no compatibility layer, data conversion, or second routing
authority is part of the implementation.

**Subscription ruling (owner 2026-08-07, amended later the same day).** Recommended
custody is vendor-specific. Claude subscriptions stay in Claude Code's local login by
default for compliance; adding one as a Gateway upstream remains an explicit optional
path. ChatGPT subscriptions are recommended and defaulted to a Gateway-held Source;
native Codex login remains supported but is neither recommended nor the default
add-flow guidance. Each backend has at most one `native_cli` Source because its official
CLI exposes one current local login; additional accounts are added as Gateway-held
Sources. This singleton ruling is **owner-vetoable (2026-08-08)**. The channel handoff
remains a first-class product story, not an escape hatch or hidden pre-pass; §4.3 alone
defines its exact runtime behavior.

A user may explicitly add either subscription as a Gateway-held upstream, and a
Gateway-held subscription may participate in a cross-vendor custom chain. The only
warning is one factual sentence shown when the user chooses the Claude-as-Gateway
path: Anthropic explicitly prohibits it, enforces server-side blocks, real account
bans have occurred, and the path may fail intermittently. Native Claude, ChatGPT in
either channel, and cross-vendor routing show no warning. No experimental flag or
per-source consent record exists in the final specification.

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
2. **Subscription custody is vendor-specific, and native takeover stays seamless.**
   Claude recommends its compliant native login; ChatGPT recommends a Gateway-held
   Source. Each backend has at most one native Source. §4.3 defines the complete
   native-to-Gateway handoff, recovery, and no-replay behavior without placing a hidden
   native attempt ahead of a user-owned Custom chain.
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
| Route policy | 跟随来源顺序 / 自定义链 | Follow source order / Custom chain | §4.3 resolves both policies; Custom is user-owned and frozen |
| Per-backend path | 网关 / 直连 | Gateway / Direct | Wire values are `hub | direct`; Gateway is the default product path, Direct is the diagnostic/self-managed path |
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

**Copy-density rule (owner-approved interaction baseline, 2026-08-07 afternoon).**
Use the glossary nouns in controls and status. Put explanations behind a compact info
icon or contextual affordance; do not turn Sources or Gateway into permanent help-text
panels. This is the vocabulary recut applied to interaction copy, not a third module or
an excuse to hide action-required state.

## 4. Architecture: upstream → Gateway → Agents

The v3 split, stated once: **Sources represent upstream access; Gateway owns local
adaptation and routing; Agents consume the result.** Ordering is never a property of
a Source.

### 4.1 Supply — Sources (global assets, no ordering)

Each source carries: kind (subscription | api_key), vendor, credential
reference, protocol (`anthropic | openai_responses | openai_chat`), an editable base
URL (api_key kind; prefilled for known vendors), a **model list** it can supply
(auto-discovered where possible, e.g. `/models`; manually extendable via custom
model entries), billing type (包月 | 按量 ¥), state (§4.5), and usage
(subscription cycle % / monthly spend).

The Source workflow is complete at both entry points:

- **Connectivity and protocol observation.** The normal Add Source form asks only for
  optional name, Base URL, and API key. Its user-triggered Add action reuses one
  connectivity interaction to classify reachability/authentication and observe the
  protocol before Save can commit; it adds no separate step and presents no protocol
  selector. This unsaved operation does not save a Source, consume routing order, or
  run an Agent turn. For an API key it may provision an engine credential only
  transiently:
  every success, failure, timeout, and cancellation path revokes that ref before the
  operation settles. A revoke failure enters the existing durable pending-revocation
  reconciliation rather than leaving an unreferenced credential untracked. No test
  response exposes the ref. A reachability or authentication failure is reported
  independently from protocol observation: Save may proceed only when that interaction
  still produced response evidence for the protocol, and it provisions the committed
  Source independently.
- **Saved Source refresh and recovery.** Source details exposes the distinct
  `POST /api/models/sources/<id>/refresh` operation. It is intentionally mutating: it
  tests the stored protocol, rediscovers inventory, updates Source health, and clears a
  `needs_action` or `error` blocker only when current evidence proves recovery. Before
  committing a smaller inventory it runs §4.5's Custom-hop and Follow-supply guards.
  The UI names this action as refresh/recovery and displays the resulting inventory and
  state; it never presents it as the non-persisting Add Source connectivity operation.
- **Model discovery.** Third-party Anthropic-compatible and OpenAI-compatible Sources
  expose an explicit “Fetch models” action in both Add Source and Source details.
  Discovery uses the observed-and-stored protocol adapter, replaces only the discovered
  slice, preserves manual entries, and renders added, removed, unchanged, and failed
  results. Rediscovering an unchanged model id preserves its edited
  `reasoning_efforts`, `display_name`, and `discovered_at`; only an id absent from the
  new upstream list leaves the discovered slice. Unsaved Add Source discovery applies
  the same transient credential rule as connectivity observation: success, failure,
  timeout, and cancellation all revoke the provisioned ref, and a revoke failure enters
  durable pending-revocation reconciliation before the operation settles.
- **Model inventory and manual entries.** A user may add and remove exact manual model
  ids. Model `id` is unique within a Source. Every model-list item has
  `{id, origin: "discovered" | "manual", reasoning_efforts: string[]}`; discovery
  creates `origin: "discovered"`, while a user-created entry uses `origin: "manual"`.
  `reasoning_efforts` is required and may be empty. It declares the exact reasoning
  effort values that the upstream model supports; it does not select one value. An
  empty list declares none, and no entry receives a default, selected value, or
  prefilled value. Existing optional `display_name` and `discovered_at` metadata remain
  part of the entry and survive refresh. This capability list belongs to
  Source model inventory, not the deferred Configure Agents module: it describes which
  invocation values that exact upstream model accepts, not which model or effort an
  Agent selects.
  Editing the list on either a discovered or manual entry uses the atomic
  `PATCH /api/models/custom-models` mutation with
  `{source_id, model_id, reasoning_efforts}`; the adapter validates the submitted list
  and re-echoes the canonical stored value as `{source: Source}`. Editing the list never
  changes `origin` or a route chain. Manual add/remove and the guarded-removal distinction
  remain unchanged. This all-inventory editing scope is an owner-vetoable orchestrator
  ruling dated 2026-08-08: discovery endpoints commonly return only ids, so a
  discovered-only immutable list would leave the capability permanently undeclarable.

**Protocol observation (owner ruling 2026-08-09, superseding AC-27's 2026-08-07
manual-choice ruling).** Every stored `protocol` is traceable to a real response from
that upstream before Save. Avibe never infers the value from vendor name or Base URL;
known-vendor metadata may order the three probes but cannot produce a conclusion or a
save-time default. When observation cannot distinguish a protocol, the failure state
honestly asks the user for a one-time manual hint among the same three values. The hint
changes probe order only: its selected adapter must still receive a successful upstream
response before Save. A failed observation therefore stores nothing rather than guessing.

Once saved, `protocol` is immutable for that Source. Connectivity retest, model
discovery, refresh, credential replacement, Base URL replacement, and restart all use
the stored adapter and never rewrite it. Changing protocol means creating a new Source,
so a later operation cannot silently reinterpret existing inventory or Custom hops.
The stored shape carries no manual/automatic provenance marker and no unverified
protocol state: manual and automatic probe ordering become indistinguishable after the
same response-backed conclusion. “Add anyway” is available only after protocol has been
proved and some other information, such as model inventory, remains unavailable; that
uncertainty belongs to Source health, not protocol identity. Every saved Source therefore
has a response-proven protocol, and any path without that proof produces no Source.

`openai_chat` is the one Chat Completions-compatible transport; there is no separate
`openai_compatible` value because both names drove the same engine section and endpoint.
Chat Completions remains supported: OpenAI has not retired the platform API, and many
third-party and open-source upstreams expose it as their only compatible surface.

A source carries **no position, rank, or priority field anywhere** — not in
config, not in the API, not in the UI. The 来源 list is an asset inventory sorted
for reading convenience, never a spend order.

**Supply channel.** Each source has a `supply_channel`:

- `native_cli` — the credential remains in the official CLI's local store. This is
  the recommended and default channel for Claude subscriptions (Claude → Claude
  Code). ChatGPT native login (ChatGPT → Codex) remains supported, but is a
  secondary, non-recommended path and is never the default add-flow guidance.
- `hub` — the managed engine holds the credential and re-originates requests. This
  is the default for API keys and the recommended add-flow path for ChatGPT
  subscriptions. It is an explicit opt-in for Claude subscriptions. A hub-held
  subscription is a normal Gateway upstream and may appear in a cross-vendor
  custom chain (§4.4).

**Native Source singleton (orchestrator ruling, owner-vetoable 2026-08-08).** Each
backend may own at most one `native_cli` Source. The official CLI exposes one current
local login and no selectable credential slot, so a second row would not identify a
second runnable account. When that row exists, Add Source disables another “Use
native” choice for the backend and the API rejects duplicate creation with the
existing Source id. Additional accounts belong in Gateway custody as `hub` Sources.
§4.3 alone defines how the singleton participates in resolution.

The ChatGPT recommendation is an owner product ruling based on Codex OAuth supporting
login from third-party applications. It supersedes the earlier memo's experimental
default; it does not weaken the single Claude warning below.

There is no feature flag, consent stamp, experimental row state, or per-route warning
for hub-held subscriptions. The single exception is informational copy shown while
adding **Claude as a hub-held Source**:

> Anthropic explicitly prohibits routing Claude subscriptions through third-party
> gateways, enforces server-side blocks, and real account bans have occurred; this
> path may stop working intermittently.

That sentence does not appear for native Claude, ChatGPT in either channel, or when
the user later places an already-added Source in a cross-vendor chain.

The same model may be supplied by multiple Sources; §4.3 alone resolves the backend
Source order and per-model policy.

### 4.2 Gateway strategy — backend order plus per-model policy

One record per agent backend. It owns:

- `mode` — Gateway (`hub` on the wire) | Direct (`direct`).
- `menu_kind` plus the menu itself: fixed for Claude Code / Codex, open for
  OpenCode. Menu enrollment is distinct from Gateway routing.
- **the backend's Source order** — an ordered subset of the sources eligible for
  this backend (§4.4), plus an order-ownership policy. This order is the input to
  `follow` routes; it is not a second filter on a model's exact custom chain.
- **one route policy per menu model** — `follow` the backend order (default), or use
  an exact custom chain. §4.3 is the only normative resolver for either policy.

The backend Source order itself has this ownership policy:

| Policy | zh | Behavior |
| --- | --- | --- |
| `follow` (default) | 跟随推荐 | Order is server-computed by the recommendation rule below. A newly added eligible source **joins automatically** at its recommended position. |
| `custom` | 自定义 | A user-owned, frozen ordered subset. A newly added eligible source does **not** join; the UI hints 「有新来源未启用」 and offers one-tap enable. |

State machine: `follow` --any manual edit--> `custom` --「恢复推荐顺序」--> `follow`.
Forking to `custom` is implicit and immediate: reordering, enabling, or removing a
source while in `follow` freezes the current order as the user's own. Returning to
`follow` discards the frozen subset and recomputes.

Independently, each menu model's route policy is `follow` or `custom` as stored in
§4.6. Changing one model's route never changes the backend Source-order policy, and a
Source omitted from that order may still be named explicitly by an eligible custom
hop. Omission means “not used by Follow routes,” not “globally disabled.”

**Recommendation rule (deterministic; document verbatim, implement verbatim).**
For a given backend, the recommended order is:

1. all eligible **own-vendor subscriptions in the recommended form**, by
   `created_at` ascending — Claude Code has at most its singleton Claude `native_cli`;
   Codex uses ChatGPT `hub` Sources;
2. then own-vendor subscriptions in the supported but non-recommended form, by
   `created_at` ascending — Claude `hub`, or the singleton ChatGPT `native_cli` when
   present;
3. then other eligible hub-held subscription Sources, by `created_at` ascending;
4. then all eligible API-key Sources, by `created_at` ascending;
5. tie-break anywhere above by Source `id` ascending.

The rule is *exhaustive over eligible sources*: nothing eligible can fall outside
it, which is what makes 跟随推荐 safe to auto-join. Enrollment in this order does not
mean a Source is adopted by any effective route; §4.3 alone derives that result.

Nothing else participates: no health score, no latency, no cost heuristic, no
usage-based reordering. This rule is the *entire* content of 跟随推荐, and it is
stable — the same set of sources always yields the same order.

The rule preserves the own-vendor-first principle while making the vendor split
visible: a ChatGPT subscription added through the recommended Hub path leads Codex,
while a user who keeps a native Codex login still gets a deterministic supported
fallback position. Claude native remains the recommended lead for Claude Code; a
Claude Hub Source never outranks it merely because it was added later.

Two obligations follow, and both are contract, not implementation detail:

- **Creation order must be persisted**, as required immutable `created_at` on the source
  (`source.schema.json`). Insertion order in the config file is not a contract and
  the sources array is explicitly unordered (`api.md`), so without a stored stamp
  rules 1 through 4 are not reproducible.
- **Rule 5 is not decoration.** Sources created or imported in the same operation may
  share a timestamp; Source `id` orders those ties. Every Source is stamped before it
  becomes visible, so the two keys define a total order without relying on array order.

**Routing configuration is per backend and per model; health is source-global.** Quota
and reachability belong to the Source, not the Agent that touched it. §4.3 is the only
normative place that turns that shared state into candidate and execution behavior.

### 4.3 The only normative resolution algorithm

This section alone derives the capability chain, channel choice, execution order,
probe result, route-adoption projection, event correlation, provenance, menu counts,
and Gateway rendering for backend `B` and caller-facing menu model `M`. Every consumer
uses this result; no other section, schema, service, or UI may restate or independently
derive source/model pairing, order, or fallthrough.

If `B.mode == "direct"`, use Direct behavior and stop. Otherwise run these phases in
order on the Source catalog, `B`'s effective Source order `O`, and `M`'s stored route
policy:

1. **Construct one capability chain.**
   - For `custom`, read the stored exact `{source_id, model_id}` hops verbatim and in
     order. A write accepts only an existing Source eligible for `B` whose inventory
     advertises that model, or a fixed-menu native Source whose adapter proves a
     sanctioned compatible family/version alias. Exact pairs are unique; one Source
     may appear again with another model. A later inventory change that invalidates a
     stored pair retains it at the same position as `runnable: false`,
     `reason: "model_unsupported"`, and `retry_at: null` until explicit repair.
   - For `follow`, walk `O` once and emit at most one capable hop per Source. First
     apply §4.4 eligibility. For OpenCode, parse `M` as `provider/model`, normalize the
     Source vendor with the same provider-id function used by its runtime overlay, and
     compare normalized ids: a recognized vendor keeps its standard id and every
     unrecognized vendor maps to `custom`; the bare model is the upstream id. For a
     fixed-menu backend, admit only its native vendor. A fixed-menu native Source may
     retain a sanctioned compatible CLI alias even when that literal alias is absent
     from inventory. A fixed-menu Hub Source resolves a native-vendor built-in alias
     only against that Source's own inventory. Every other case requires advertised
     exact model identity. Follow never invents a foreign-vendor substitution.
   - Whenever a sanctioned native alias is admitted under either policy, retain the
     concrete inventory entry that proved the alias as that hop's capability evidence.
   - Annotate every emitted hop from the current Source inventory, source-global
     health, and current process availability. `healthy` and an elapsed cooldown are
     health-ready; `needs_action`, `error`, an unelapsed cooldown, unavailable native
     process, and `model_unsupported` are not. Blocked hops remain in place. The
     runnable view is only a filter over this chain, never a second chain.

2. **Dispatch the leading currently runnable hop.** Preserve chain order and select
   the first hop that is runnable now. A `native_cli` hop uses the sanctioned backend's
   singleton local login with no Gateway credential injection; a `hub` hop uses the
   local Gateway and may be cross-vendor. Never prepend a native attempt outside the
   chain: a Custom Hub-first chain stays Hub-first even while native is healthy.

3. **Execute hops with live revalidation.** Immediately before **every** attempted
   hop, re-read its Source inventory, source-global health, `retry_at`, and current
   process availability and re-evaluate the phase-1 capability and runnability
   predicates. Never trust the snapshot used to construct the chain. If an earlier
   hop placed a Source into cooldown, every later hop naming that same Source is
   skipped immediately unless it has become runnable again. For an attempted hop,
   resolve the hop to its phase-1 capability-evidence inventory entry: the literal
   entry for an exact model id, or the concrete entry retained for a sanctioned native
   alias. If the turn-requested reasoning effort is an exact member of that entry's
   `reasoning_efforts`, pass that one value to the adapter; otherwise pass `null` and
   let the upstream use its own default. The list never selects a value, and resolution
   performs no approximate mapping or downgrade fallback. Parameter, protocol, and
   tool-compatibility errors surface without fallback. A local Gateway start/listener/
   process failure is `engine_down`: it surfaces as a terminal local-runtime error,
   mutates no Source health, and does not walk another Hub hop because no upstream was
   attempted. A 401 refreshes once and retries that hop once.
   Before output starts, explicit quota exhaustion, 429, transient 5xx, or attributable
   upstream network failure sets the classified source-global cooldown and continues to
   the next hop. A second 401, or a classified 402/403 result of
   `credential_expired | credential_revoked | balance_exhausted | account_banned`,
   persists that Source as `needs_action` and also continues to the next hop before
   output; a non-self-healing account failure does not prevent another configured
   Source from serving the turn.
   After any output starts, never replay the request. Still classify and persist any
   attributable Source failure: self-healing failures set their cooldown and non-self-
   healing failures set `needs_action`, so the next turn does not immediately retry a
   hop known to be unavailable. Record the interrupted turn and let the next turn run
   this entire algorithm again. If no hop can complete, report the existing truthful
   terminal failure. Every switch is recorded for the pull surfaces; a successful
   handoff emits no conversation notice or setting.

Because health is source-global, a cooldown created through one backend affects every
route using that Source. Because every turn runs the algorithm again, an elapsed
cooldown naturally restores the configured leading Follow hop without mutating order.
The Model Gateway and Usage pages remain the pull surfaces for takeover state,
connector color, recent switches, and usage; provenance remains a debug affordance.

**Cross-vendor and conversion fidelity ruling (owner 2026-08-08).** Cross-vendor or
converted supply is functionally usable while its reasoning chain may degrade; that is
expected behavior, not a defect. A relay may itself fall back across models or vendors,
some upstreams omit reasoning, and Claude verifies official reasoning signatures, so a
relay may discard non-official reasoning content. The M0 survey measurements and their
go/no-go rows remain unchanged as recorded evidence. An official-API attribution
re-test is **owner-waived**, not still unverified. Compatibility copy is compact,
info-level, and never a conspicuous per-hop warning. Tool calls, streaming, and system
semantics remain the functional floor; the observed Messages-direction system/tuple
distortion stays recorded as relay behavior, does not block the product direction, and
creates no new acceptance criterion.

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
The final vocabulary contains neither `consent_required` nor `opencode_api_key_only`;
Hub-held subscriptions are eligible for OpenCode, and risk copy is not eligibility. Native
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

**A normal turn never probes a blocked Source in hope that it recovered.** The explicit
Source-details recovery path is `POST /api/models/sources/<id>/refresh`, the same saved
mutation defined in §4.1. It may test `needs_action` or `error` after a
user acts; a successful current observation clears the blocker without recreating or
reordering the Source. AC-3 and AC-11 use this one route and no parallel `/test` recovery
route.

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
| self-healing (`cooldown`, `waiting`, recovery, in-turn switch) | 最近切换 feed, connector state, and the row's status pill on the Model Gateway and Usage pull surfaces. Every survived fallback is silent. Other in-turn copy appears only when the turn did **not** proceed transparently: the retry form when §4.3 forbids the transparent retry, or the `waiting` form when nothing is runnable but every blocker is timed |
| `needs_action`, `error`, `interrupted` | the in-turn copy of the turn that hit it — the **interrupted** form's cause breakdown plus a pointer to 「模型网关」 — and 需处理 state on the Model Gateway and Usage pages until cleared. A blocker left behind by a turn that **succeeded** is page-and-feed only, by the row above |

`error` is named in the second row explicitly (07-29, review round 6). It was
implicitly there all along — it is a blocker, and blockers are what the row is about —
but leaving it unnamed while the status table called it 「unknown」 is how a reviewer
ends up asking, correctly, which tier an unclassified failure belongs to.

**No proactive or successful-takeover delivery** (owner rulings 2026-07-29 10:54 and
2026-08-08; the latter supersedes the 2026-08-07 afternoon notice and setting while
retaining the earlier push cut). **No resolution event is pushed anywhere, and a
successful switch produces no turn copy.** Avibe does not open or annotate a
conversation merely to report supply state. An interruption is surfaced **in the turn
that hit it**, and otherwise waits on the Model Gateway and Usage pages for a user who
chooses to inspect it. The rationale is deliberate invisibility: tokens should feel
like tap water or air, not a mechanism that competes with the user's current work. This
also aligns with the 2026-07-29 ruling that provenance is a debug affordance rather than
a conversation feature. It dissolves
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

- **survived transparently → silent.** A fallback that carried the turn produces no
  Error, warning, informational copy, or configurable notification. The switch is
  recorded and surfaces where a record belongs: the 最近切换 feed and the row's
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

The same invariant applies to **every Source-inventory mutation**, not only Source
deletion. Reversible or transactional changes — API-key Base URL replacement, API-key
credential replacement, explicit refresh/recovery, and manual-model deletion — first
stage the resulting inventory and run **both** guards: compare it with every exact
Custom hop using the §4.3 phase-1 capability predicate, and recompute `would_interrupt`
for every protected Follow or Custom menu model. Either the literal model remains
advertised or a sanctioned native alias retains compatible family/version evidence. If
an exact hop would cease to satisfy that predicate, the non-forced mutation is refused
with `source_model_in_custom_chain` and ordered `would_remove_hops`; another Source
supplying the same menu model does not make that exact reference disposable. If no
exact hop is lost but a protected route loses its last supplier, it is refused with
`source_last_supplier`. When both apply, the exact-hop error leads and the response
still carries both complete arrays. A confirmed `force=true` applies the inventory
change and removes only those invalidated hops in one transaction, preserving the
identity and relative order of all survivors and keeping an empty route `custom`.
It also reports every resulting supply gap; force is confirmation, not a claim that
the mutation is interruption-free.

The force carrier and response are uniform and JSON-body based for inventory changes:
`PATCH /api/models/sources/<id>` carries
`{display_name?, base_url?, force?: boolean}` (`force` is meaningful only when
`base_url` changes);
`PUT /api/models/sources/<id>/credential` carries `{key, force?: boolean}`;
`POST /api/models/sources/<id>/refresh` carries `{force?: boolean}`; and
`DELETE /api/models/custom-models` carries
`{source_id, model_id, force?: boolean}`. Omitted `force` is false. A guarded `409`
returns `{error, would_remove_hops: RouteHopRef[], would_interrupt: SupplyGap[]}` with
both arrays present. Success returns
`{source: Source, removed_hops: RouteHopRef[], interrupted: SupplyGap[]}` with empty
arrays when nothing was cascaded or interrupted. No query-parameter variant exists for
these four mutations.
Automatic background discovery never performs this cascade: when neither literal
inventory nor sanctioned-alias evidence remains, it records the model as
`model_unsupported`, keeps the configured hop visible and non-runnable, and waits for
an explicit user refresh/edit to repair or confirm removal.

Native CLI re-authentication and Hub OAuth re-authentication are the irreversible
exception. Each presents AC-2's server-enforced acknowledgement **before** login starts;
the user can abort there, but the product promises no rollback or post-login refusal
once the OAuth exchange has begun. After authentication, the engine commits the
resulting credential and any inventory it can establish. Exact hops whose source/model
pair is no longer present remain visible but non-runnable with exactly
`reason: "model_unsupported"` and `retry_at: null`, and the response reports the
resulting gaps and `needs_action` work for a later explicit edit or force cascade. It never silently
converts the route to Follow or claims that the old supply is intact. Thus no
credential lifecycle event or inventory drift silently calls a model the Source no
longer advertises or rewrites the user's chain.

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
inside Gateway mode and unsatisfiable outside it without fabricating a source. Existing
installations with no Model Hub state start in Direct until the user explicitly switches
that backend (§6), which makes this a first-class onboarding case rather than an edge
case left to the implementer to discover.
Whether a Direct turn gets a no-source provenance representation or the route answers
「此回合无网关记录」 is an implementation requirement recorded as **AC-1** — a question
about the record and the route only, since 14:03 left no affordance to render it.
**Neither branch licenses silence** (07-29, review round 8): the every-turn promise above
is scoped to Gateway-mode turns by the paragraph that opens this one, and a Direct turn must
still answer the contracted route with a documented payload or a documented error — what
it may not do is come back indistinguishable from a turn whose provenance was never
written. The current contract chooses and tests the representation; v3 does not reopen
it. Cancellation remains FSM truth rather than transport inference.

Five outcomes are recorded, not one: `served`; `exhausted` (fallback walked to the
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
`canceled` is the fifth outcome: the turn FSM, never a transport guess, says Stop/cancel
settled the turn. An in-flight attempt may be retained as interrupted-at-cancel but
receives no fabricated Source failure reason; attribution-ambiguous attempts remain
absent under AC-4's control fixture.
The terminating attempt is recorded in exactly one place — failed
attempts in an ordered list, the served attempt in its own field, the terminal error
in a third, at most one of the latter two ever populated, the full sequence
reconstructible by appending. That is a shape decision rather than a validation one:
it makes 「两个成功者」, 「成功者不在最后」 and 「摘要指向列表里没有的来源」 impossible to
write down, instead of invariants prose asks every implementer to respect.

### 4.6 Route-policy storage and mutation

§4.3 is the sole normative derivation. This section defines only its persisted input
and mutation boundary. Route-policy storage is sparse: for each `(backend, menu model)`,
an absent row means `{policy: "follow"}`. A stored row is either `{policy: "follow"}`
or `{policy: "custom", hops: [{source_id, model_id}, ...]}`. A newly introduced bundled
menu model therefore follows the backend Source order automatically, while an existing
Custom row remains untouched. Custom hop order and exact pair identity are user-owned.
Writes use the capability predicate defined in §4.3 phase 1; reads return the §4.3
result and never project a second chain here.

Source deletion has one defined Custom state. A non-forced delete refuses while any
Custom hop names that Source. A confirmed forced delete removes every exact hop naming
it atomically, preserves all survivor order, and leaves the route `custom` even when no
hop survives. Inventory loss without Source deletion instead retains the hop in place as
§4.3's visible `model_unsupported` entry. A deleted Source is never represented by a
different stale-hop reason or silently converted to Follow.

There is no `mappings` field, mapping mutation, mapping resolver, or mapping diagnostic
in the final product. A single-target override is a Custom chain whose hops use that
target `model_id`; it is not a parallel data structure. This is smaller and more honest
than coexistence: one route owner answers both “which Source?” and “which upstream
model?”, one §4.3 result powers runtime and UI, and every validation applies to one
shape. Keeping mappings would preserve an implicit Source choice beside an explicit
Source choice and make precedence depend on which consumer happened to read first.
This final-shape decision is **owner-vetoable (2026-08-07)**.

The chain resource is
`GET /api/models/agents/<backend>/chain?model=<id>`, with a matching `PUT` carrying
`{policy: "follow"}` or
`{policy: "custom", hops: [{source_id, model_id}, ...]}`. The read projection carries
the policy plus the §4.3 result.

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
  back to a single `custom/` provider. §4.3 phase 1 uses this same normalized id
  when matching a Source to an OpenCode menu model.
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
  Opening it renders the exact §4.3 result; blocked hops remain in place and dim.
- Adding Claude selects `native_cli` by default and presents Gateway custody as the
  optional path. Adding ChatGPT recommends and selects `hub` by default; native Codex
  login remains an available secondary choice without default guidance. Only the
  Claude + Gateway branch shows §4.1's one-sentence warning; it is informational, not
  a consent flow. When a backend already has its singleton native Source, its native
  choice is disabled rather than creating an alias for the same CLI login.
- Add Source exposes §4.1's combined connectivity/protocol observation without a normal
  protocol control; Source details runs the separately named mutating refresh/recovery
  against the stored protocol. Both surfaces expose
  compatible model discovery. Results stay in the current flow and use compact status plus an
  info affordance for explanation; the page does not grow permanent instructional
  paragraphs. Every inventory model exposes an editable per-model `reasoning_efforts`
  list beside the exact id. The list has no default item or selected state; the control
  form follows the owner-approved `design.pen` baseline and is not prescribed here. A
  protocol selector appears only inside an observation-failure state and its hint still
  requires a successful response before Save.
- Compatibility detail for converted or cross-vendor supply stays behind a compact
  info affordance: functionality is supported while reasoning content may degrade.
  There is no per-hop warning or alert treatment.
- Recently switched, connector state, source/route status, and usage remain pull
  surfaces. A successful fallback adds no turn copy. If every source is unavailable,
  the existing failure path still reports the error honestly.

The existing V6 frames remain a visual baseline for row density, health states, and
mobile treatment, but their Agent-card grouping and mapping drawer are not v3 product
authority. The owner-approved v3 interaction draft is the implementation baseline for
the two modules, native → Gateway takeover → native recovery, default versus custom
model chains, and the Claude hub-add warning. The design lane adds production-complete
desktop/mobile states without reopening the approved information
architecture.

**Deferred third module: Configure Agents.** The intent is to let users add models,
reasoning effort, and related model preferences to Agent definitions from this product
area. v3 does not define its information architecture, data contract, controls, or
delivery lane. It must not appear as a placeholder third module in the v3 UI.

## 6. Modes & onboarding

- **Gateway (wire value `hub`, default)**: every backend on a fresh installation starts
  in Gateway mode. An existing installation with no Model Hub state starts in Direct;
  each backend moves to Gateway only after the user explicitly switches it in the
  Models page. This onboarding rule prevents a silent routing change for existing
  users without introducing an internal contract-conversion path. Avibe injects
  runtime-only configuration into processes
  it launches (env vars for Claude Code; `-c` overrides for Codex app-server;
  `OPENCODE_CONFIG` overlay for OpenCode, gateway-config hash tracked for
  long-lived `opencode serve`). Native user configs are never written.
- **Availability is default-on.** Absence of `VIBE_MODEL_HUB_ENABLED` cannot disable
  the controller, `/api/models/` routes, or Models UI. K3 deletes the old default-off
  gate; an explicitly configured development/emergency override may disable the surface,
  but no fresh user depends on an environment variable to receive the product default.
- **Direct (supported diagnostic/self-managed path)**: current behavior —
  per-backend native config editing (auth tabs, API key + base URL, writes to
  `settings.json` etc.), useful for diagnostics and self-managed setups.
- Backends can differ in mode; the Gateway module surfaces the mode per backend.
  A `native_cli` hop inside Gateway mode is not Direct mode: Avibe still owns the
  pre-stream same-turn fallback and recovery policy.
- **Native-config import** remains copy-only
  and reversible, a per-item checklist grouped by backend. API keys + base URLs →
  direct import; subscription OAuth → `keep_native` by default (stays in the CLI's
  sanctioned store and becomes a `native_cli` source). This import-custody default
  prevents silent credential movement; it does not replace §4.1's ChatGPT add-flow
  recommendation. A hub-held
  subscription is established only through the explicit OAuth add flow, not by
  importing a native credential file; Codex `auth.json` → `keep_native`. Footer
  promise: originals never modified or deleted; Direct always available. Triggers:
  first open after upgrade, setup wizard, backend-page banner.
- **Add-source closing loop (v3).** Creating a Source reports two different facts.
  `order_enrolled_by: [{backend, order_policy}]` means the Source entered a backend's
  Follow Source order. `adopted_by: [{backend, menu_model, route_policy}]` means at
  least one effective route produced by §4.3 phase 1 contains a capable hop using that
  Source. Transient health and process availability do not change adoption. Enrollment
  alone never yields “adopted” success copy; Custom backends may still offer one-tap
  enable without claiming a route already uses the Source.

## 7. Security boundaries

- Three credential rings, never mixed: management key (Avibe→engine admin
  API), local gateway token (the only thing backends receive), upstream
  credentials (API keys and explicitly hub-held subscription OAuth tokens;
  engine-held in a restricted local runtime directory, not `~/.cli-proxy-api`).
  Claude defaults to the official CLI store through `native_cli`; ChatGPT defaults
  to the engine-held Gateway path. A native credential is never copied into engine
  custody implicitly: even for ChatGPT, moving from an existing native login to the
  recommended Hub path requires an explicit OAuth add flow.
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
  supersedes v2's “No per-model ordering” non-goal. The scope is exactly §4.3 and
  §4.6's stored route-policy input;
  there is no session-level or request-level editor.
- **No native CLI account selector or multiple native slots.** A backend has one
  `native_cli` Source because its official CLI has one current local login. Additional
  accounts are Gateway-held Sources. Selectable native profiles are deferred until an
  official CLI exposes a stable account-selection contract.
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
- **No protocol guessing or post-save backfill.** A stored protocol comes from a real
  pre-save upstream response, never a vendor/Base-URL string heuristic. If observation
  fails, the product may ask once for a manual probe-order hint, but the hinted adapter
  must still return a successful response before anything is saved. No later operation
  changes the stored value.
- No billing-grade accounting, multi-tenant pools, or operator consoles.
- No third source category ("relay" merged into API Key).
- No v3 Configure Agents module (§5), runtime plugin UI, or GA scope beyond the
  three directions recorded in §10. Source-inventory `reasoning_efforts` capability
  lists are in scope and do not create Agent-definition configuration.

## 10. Open items and GA research directions

These items do not enlarge the owner-approved GA scope. They turn the three accepted
directions into questions that later lanes must answer before writing mechanical gates.

1. **Conversion fidelity (parallel K2 lane).** Keep the M0 measurements and go/no-go
   rows unchanged. The owner accepts relay-attributed reasoning loss and waives an
   official-API attribution re-test; this item is evidence, not an unresolved product
   blocker. Continue to record tool calls, streaming, system prompts, reasoning, cache,
   and terminal semantics without exposing plugins or per-hop warnings. Tool,
   streaming, and system behavior remain the functional floor; the recorded
   Messages-direction system/tuple distortion is non-blocking relay behavior.
2. **Release gate: engine asset mirror.** Research the exact Avibe-owned mirror,
   provenance, manifest publication order, availability monitor, restore behavior,
   and immutable-SHA evidence required before the pinned engine is a GA dependency.
   Do not change the pin or claim the gate complete in this specification.
3. **Release gate: platform matrix.** Re-verify install, startup, upgrade, rollback,
   and smoke evidence for every platform the current runtime contract lists. Decide
   the minimum repeatable evidence and unsupported-host behavior. Do not add platforms
   or platform-specific product promises here.
4. **Configure Agents — deferred.** First-class user-added menu models, reasoning
   effort, and Agent-definition configuration belong to the deferred third module.
   Its architecture and contract are intentionally absent from v3.
5. **Later diagnostics and accounting.** Request-log UI, fallback spend attribution,
   and quota projection remain post-v3 candidates. Each needs evidence from existing
   provenance/usage data before it becomes a product promise.
6. **Remaining UI evidence.** The approved v3 interaction draft is the implementation
   baseline. The design lane still owes complete desktop/mobile frames, empty and
   failure states, Dark variants, and English copy;
   a product re-review is required only if those artifacts change the approved
   information architecture. Rejected V5 explorations remain history until separately
   deleted.
7. **Engine-owned OAuth file import.** Keep `controlled_import` deferred until a
   concrete adapter capability can preserve refresh semantics; explicit OAuth add is
   the only hub-held subscription path in v3.

## 11. Owner acceptance checklist (~10 min)

- [ ] §0 and §2 say “default local model Gateway,” recommend Claude native and
      ChatGPT hub-held custody, and point all channel behavior to §4.3.
- [ ] §3 makes Gateway a first-class noun and the owner-vetoable banned-term table
      matches the intended UI language.
- [ ] §4.1 defaults Claude to `native_cli` and ChatGPT to `hub`; explicit hub-held
      Claude is the only branch with a warning, no flag or consent remains, and each
      backend has at most one native Source.
- [ ] §4.1 defines manual connectivity testing, model discovery, manual model
      add/remove, and editable `reasoning_efforts` lists for every inventory entry;
      every saved protocol is response-proven before Save and immutable afterward,
      with no persistent provenance marker or protocol-level unverified value.
- [ ] §4.1 exposes exactly `anthropic | openai_responses | openai_chat`, retains Chat
      Completions, and shows protocol choices only after observation cannot decide.
- [ ] §4.2 keeps own-vendor subscription supply first in the vendor-recommended form
      and never reorders a custom backend order or custom model chain.
- [ ] §4.4 allows every hub-held subscription to serve every backend while retaining
      native CLI's sanctioned-backend binding.
- [ ] §4.3 is the document's only resolution derivation, including normalized
      OpenCode matching and per-hop live runnability checks; §4.6 only stores and
      mutates exact `(source_id, model_id)` pairs.
- [ ] The owner-vetoable final route shape is acceptable: sparse route rows default to
      Follow, Custom owns exact pairs, and no mapping or dual routing authority exists.
- [ ] §4.5 keeps state source-global, status live-derived, and every successful
      takeover silent; terminal in-turn errors plus Model Gateway/Usage pull state
      remain available.
- [ ] §5 has exactly Sources + Gateway modules; the connector is state-only and
      Configure Agents is deferred without a placeholder design.
- [ ] §6 distinguishes Source-order enrollment from actual route adoption and never
      calls enrollment alone “adopted.”
- [ ] §6 clearly distinguishes a native hop inside Gateway mode from Direct mode.
- [ ] §9 explicitly supersedes the old no-per-model-ordering non-goal and keeps
      automatic model invention out of Follow policy.
- [ ] §10 records the owner-waived official-API fidelity re-test, preserves M0 evidence,
      and does not expand GA scope.
- [ ] The implementation plan appends AC-22 onward and gives the first implementation
      lane an exhaustive final-shape contract/consumer/test handoff for one atomic commit.
