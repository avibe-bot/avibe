# Model Hub — Product Spec

Status: **v3.0** (2026-08-09) · supersedes v2.0 (2026-07-29) outright
Owner decisions incorporated through: 2026-08-09 (+08:00)
Design source: `../avibe-docs/design.pen`. The V6 frames remain the visual baseline;
the v3 interaction draft for the two-module information architecture is owner-approved
as the implementation baseline (2026-08-07 afternoon). The design lane still owes
production-complete desktop/mobile states.
Contracts: `model-hub-contracts/` remain unchanged by this docs-only revision.
`model-hub-implementation.md` records the exhaustive final-shape handoff. Its mechanical
closure must coexist on one tested PR head; all remaining consumers and evidence must
land before release.

## 0. Revision note — why v3 replaces v2

Owner ruling (2026-08-07): **Model Hub is Avibe's default local model gateway.**
Its end-to-end product model is:

1. upstream sources — vendor subscriptions, API keys, and third-party relays represented
   as API keys with custom Base URLs;
2. the local Gateway — protocol adaptation, model pairing, ordered routing, failover,
   retry, and recovery;
3. downstream Agents — consumers of the Gateway, not owners of upstream credentials.

Every `(backend, menu model)` owns one persisted ordered route chain, and every hop
names the exact `(source_id, model_id)` to call. This formally supersedes v2 §9's “No
per-model ordering” non-goal on 2026-08-07.

**Configured-chain ruling (owner 2026-08-09, S-1).** Matching happens when a Source is
added and writes the resulting exact hops. From then on the Gateway screen is the
configuration and the configuration is what executes: users add, remove, reorder, or
edit a hop's explicit model mapping, while runtime only walks those hops and classifies
live availability and fallthrough. There is no `follow | custom` state, no runtime
Source/model matching, and no second projection from a backend order. §4.3 is the only
normative execution algorithm for this stored chain.

The final product has no separate fixed-menu `mappings` structure beside route chains.
A same-model choice is simply a hop whose `model_id` equals the menu model; a mapped
choice is a hop whose explicit `model_id` differs. Both are ordinary configured hops.
The final shape and its rationale are in §4.6 and are **owner-vetoable (2026-08-07,
amended by S-1 on 2026-08-09)**. Model Hub has not shipped, so no compatibility layer,
data conversion, policy transition, or second routing authority is part of the
implementation.

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
Gateway-held subscription may participate in a cross-vendor configured chain. The only
warning is one factual sentence shown when the user chooses the Claude-as-Gateway
path: Anthropic explicitly prohibits it, enforces server-side blocks, real account
bans have occurred, and the path may fail intermittently. Native Claude, ChatGPT in
either channel, and cross-vendor routing show no warning. No experimental flag or
per-source consent record exists in the final specification.

**Surface ruling (owner 2026-08-07).** The Models page has two product modules:
Sources and **Gateway**. Sources answer what upstream access the user owns. Gateway is
the main work surface for exact pairing, model mapping, and per-model chain order.
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
   native attempt ahead of a user-owned configured chain.
3. **Every route is visible configuration.** Each `(backend, menu model)` stores one
   exact ordered chain of `(source_id, model_id)` hops. Adding a Source proposes and
   persists its matches once; afterward the user edits that same chain directly.
4. **The user owns every configured order and mapping.** No health score, learned
   ranking, vendor label, or runtime inventory walk silently rewrites the chain. Avibe
   may skip an unrunnable hop for the current turn; it never mutates configured order
   or model ids as a side effect.
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
| Per-model route | 路由链 | Route chain | Ordered hops for one `(backend, menu model)`; every hop is an exact Source + upstream model pair |
| Explicit model mapping | 型号映射 | Model mapping | A configured hop whose upstream `model_id` differs from its menu model; the system never invents one at runtime |
| Per-backend path | 网关 / 直连 | Gateway / Direct | Wire values are `hub | direct`; Gateway is the default product path, Direct is the diagnostic/self-managed path |
| Gateway hop using the official CLI login | 原生 | Native | Required for a `native_cli` hop inside Gateway mode; it is not Direct mode |
| Per-backend health rollup | 供给状态 | Supply status | 正常 / 降级 / 暂时全部在冷却 / 无可用来源 (§4.5) |
| Recoverable fallback state | 接管 | Takeover | Derived by §4.3 when a later hop is current because the first hop is recoverably unavailable; never persisted |

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
| 优先级 / priority as a standalone global noun | **Banned**, except in the backend-scoped UI title “全局路由优先级” / “Global route priority” | The title names one backend's shared Source order; otherwise name the owning model Route chain and use ordinal copy such as “first upstream” |

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
(subscription cycle % / monthly spend). Existing `last_discovered_at` records the last
successful full inventory replacement; it is not a connectivity-check timestamp.
Every Source read also carries the server-derived
`adopted_by: [{backend, menu_model}]` projection of persisted Route references for
backends currently in Hub mode. That unique projection is sorted by backend then menu
model. It is reloadable Source-card data, not persisted Source state, and clients never
reconstruct it from live chains. Routes retained while a backend is in Direct mode are
not included because that backend bypasses the gateway.

The Source workflow is complete at both entry points:

- **Connectivity and protocol observation.** The normal Add Source form asks for an
  optional name, an interface selection (Auto detect by default), Base URL, and API key.
  A concrete interface restricts the preflight to one candidate; Auto detect tries the
  supported candidates in the adapter's authoritative order. The user-triggered Add
  action reuses one connectivity interaction to classify reachability/authentication
  and prove the protocol before Save can commit. Its schema-invalid probe settles before
  a relay selects or invokes a model, so observation never depends on a synthetic model
  being routable. This unsaved operation does not save a Source, consume routing order,
  or run an Agent turn. For an API key it may provision an engine credential only
  transiently:
  every success, failure, timeout, and cancellation path revokes that ref before the
  operation settles. A revoke failure enters the existing durable pending-revocation
  reconciliation rather than leaving an unreferenced credential untracked. No test
  response exposes the ref. A reachability or authentication failure is reported
  independently from protocol observation: Save may proceed only when that interaction
  still produced response evidence for the protocol, and it provisions the committed
  Source independently.
- **CPA ownership after Save.** CPA requires a concrete upstream provider configuration,
  so it cannot identify an unsaved Source without first being told the interface this
  preflight exists to prove. Once the Source is saved, Avibe maps its proven protocol to
  CPA's existing `claude-api-key`, `codex-api-key`, or `openai-compatibility` provider
  section and every Agent invocation runs through CPA. Avibe owns only the bounded
  pre-save evidence step; CPA owns compatible request translation and upstream dispatch.
- **Saved Source refresh and recovery.** Source details exposes the distinct
  `POST /api/models/sources/<id>/refresh` operation. It is intentionally mutating: it
  tests the stored protocol, rediscovers inventory, updates Source health, and clears a
  `needs_action` or `error` blocker only when current evidence proves recovery. Before
  committing a smaller inventory it runs §4.5's configured-hop and supply-gap guards.
  This is the **only saved-Source test/discovery mutation and the only corresponding
  Source-details button**, labelled “Refresh models” / 「重新拉取」. Its request,
  guarded refusal, and success are exactly the refresh row of §4.5's authoritative
  Source-mutation matrix. The UI displays the resulting inventory and state and never
  presents a second “Test connectivity” action.
- **Model discovery.** Third-party Anthropic-compatible and OpenAI-compatible Sources
  expose an explicit “Fetch models” action while they are still in Add Source. A saved
  Source gets the same discovery behavior only through the refresh operation above;
  there is no parallel saved discovery route. Discovery uses the observed protocol
  adapter, replaces only the discovered slice, preserves manual entries, and renders
  added, removed, unchanged, and failed results. Rediscovering an unchanged model id
  preserves its edited `reasoning_efforts`, `display_name`, `discovered_at`, and
  `retired` value. A non-retired discovered id absent from the new upstream list leaves
  the discovered slice; a retired row is the exception and remains a persistent
  tombstone whether the id is present or absent upstream. Unsaved Add Source discovery applies
  the same transient credential rule as connectivity observation: success, failure,
  timeout, and cancellation all revoke the provisioned ref, and a revoke failure enters
  durable pending-revocation reconciliation before the operation settles.
  The saved Source surface may render freshness only as “Model list updated at …” /
  「型号列表更新于…」 from `last_discovered_at`. It carries no latency or “last checked”
  field or copy.
- **Model inventory and manual entries.** Model `id` is unique within a Source. Every
  model-list item has
  `{id, origin: "discovered" | "manual", reasoning_efforts: string[], retired?:
  boolean}`; discovery
  creates `origin: "discovered"`, while a user-created entry uses `origin: "manual"`.
  Omitted `retired` means false, and only a discovered entry may carry true. A retired
  row remains readable but is excluded from add-time matching, model-capability
  eligibility, new Route validation, live runnability, and invocation. DELETE on a
  discovered entry stages `retired: true`; DELETE on a manual entry removes the row.
  Both use §4.5's exact-hop and protected-supply guards, and no refresh automatically
  clears retirement.
  `reasoning_efforts` is required and may be empty. It declares the exact reasoning
  effort values that the upstream model supports; it does not select one value. An
  empty list declares none, and no entry receives a default, selected value, or
  prefilled value. Existing optional `display_name` and `discovered_at` metadata remain
  part of the entry and survive refresh. This capability list belongs to
  Source model inventory, not the deferred Configure Agents module: it describes which
  invocation values that exact upstream model accepts, not which model or effort an
  Agent selects.
  Editing the list on either a discovered or manual entry uses the atomic
  `PATCH /api/models/sources/<source_id>/models/<model_id>` mutation with
  `{reasoning_efforts}`; the adapter validates the submitted list
  and re-echoes the canonical stored value as `{source: Source}`. Editing the list never
  changes `id`, `origin`, or a route chain. Model creation, retirement, and manual
  deletion use the same Source-model subresource. This all-inventory editing scope is an
  owner-vetoable orchestrator
  ruling dated 2026-08-08: discovery endpoints commonly return only ids, so a
  discovered-only immutable list would leave the capability permanently undeclarable.

**Source-create boundary.** `source-create.schema.json` is the complete API-key create
request: `vendor` and transient `key`, plus optional `display_name`, `base_url`, probe-
constraining `protocol`, client-generated `client_nonce`, and boolean
`accept_unavailable_inventory`; omission is `false`, and the boolean consents only when
the server's repeated response-backed observation proves a protocol but returns
`discovery: failed`. Omitted `protocol` auto-detects across the supported interfaces;
a supplied value probes exactly that interface but is not evidence and is persisted only
after a matching response. Source identity, protocol
evidence, discovered inventory, health, usage, custody metadata, and timestamps remain
server-owned. When supplied, `client_nonce` is unique among live Sources and live-process
create reservations. The server reserves it atomically in process before observation or
credential work and persists `Source.client_nonce` unchanged only on commit, so a client
that loses the response can identify the committed row on the next Source list read.
There is no durable pre-create claim record, and neither representation stores a request
digest, terminal envelope, or plaintext credential. Pre-commit failure or
cancellation releases it only after AC-26 retained-material settlement. On restart an
in-flight create and its process-local reservation no longer exist; pending revocation
is reconciled before a retry begins fresh. Source deletion releases the nonce. A lost-response client
must read Sources before retrying; after that read observes no live nonce owner, a
same-nonce request is a fresh create, including after deletion. It is not Source identity
or a routing input.

**Source-create nonce state machine (authoritative and exhaustive; simplified by owner
subtraction ruling 2026-08-11 20:35, superseding the 19:10/19:42 receipt design).** After
a lost response the client reads Sources before retrying the same nonce. An exact match
reconciles the Source; a list miss permits a fresh retry. An in-progress conflict means
wait and retry; a committed conflict means repeat the Source read and select the exact
`client_nonce` match. Malformed request fields retain shared request-validation behavior.

| Decision | Live condition | Retry relation | Server action and HTTP/API result | Upstream work | First consumer |
| --- | --- | --- | --- | --- | --- |
| `nonce.in_flight` | this process holds the nonce reservation for unfinished create work | same nonce with any otherwise-valid request | retain reservation; HTTP 409 `source_create_in_progress` | none | Add Source wait/retry loop and concurrent-create fixture |
| `nonce.released` | no live-process reservation and no live Source owns the nonce, including after restart | same nonce with any otherwise-valid request | atomically reserve the nonce in process and proceed as a fresh create | exactly one new attempt owned by the reservation | create retry loop, AC-26 cleanup/recovery, restart, and Source-delete tests |
| `nonce.committed` | one live Source carries the nonce | same nonce with any otherwise-valid request | HTTP 409 `source_nonce_conflict`; client list read finds exactly one Source with that nonce | none | D-36 list reconciliation and committed-conflict fixture |

There is no committed replay promise: ordinary Source reads, not a stored terminal
envelope, reconcile the rare lost-response path. `released` is absence, not a tombstone
row; Source deletion makes the nonce claimable again, and a same-nonce create after the
required list miss is definitionally new. A stale client that skips the D-36 read can
recreate a deleted Source and is outside the single supported UI client's threat model.
These rules add no endpoint, receipt, digest, or server-side response snapshot.

**OAuth-start nonce state machine (authoritative and exhaustive; owner ruling
2026-08-11 19:42).** The key is the exact `(client_nonce, vendor, channel)` tuple and is
claimed before provider work. A different tuple never resolves to this claim or flow.
Every flow created from a nonce-bearing request has a non-null date-time `expires_at`
from its first response through every terminal replay, which bounds both reconciliation
and retained cancellation. A flow without a nonce keeps the existing nullable expiry
branch for ordinary or presentation-only lifetime handling.

| Decision | Tuple condition | Server action and HTTP/API result | Provider starts | First consumer |
| --- | --- | --- | --- | --- |
| `oauth_nonce.released` | no claim or unexpired flow exists, including after pending-start failure/task-cancellation cleanup or after a retained canceled flow reaches its existing `expires_at` | atomically claim, start once, and make all coalesced callers await the same terminal result | exactly one under the new claim | I3 OAuth registry, cleanup-release fixture, and clocked expiry/restart fixture |
| `oauth_nonce.in_flight` | one provider start owns the claim but has not produced a flow | coalesce the exact-tuple retry with that pending start and return its same terminal result | none for the retry | AUTH-SETUP-210 blocked-first-call/concurrent-retry fixture |
| `oauth_nonce.committed` | provider success atomically converted the claim into one unexpired `OAuthFlow`, including one explicitly canceled afterward | return the same `flow_id`, current state, presentation, and echoed nonce; a canceled retry returns the retained `state: "cancelled"` flow | none | OAuth API idempotency and canceled-retry/provider-zero fixtures |

A shared provider-start failure or task cancellation before a flow exists settles
cleanup and releases the claim; success creates the flow atomically with no claim gap.
Explicitly canceling a nonce-bearing committed flow cancels provider work but retains
that same bounded terminal flow until its existing expiry, so a delayed exact-tuple
retry cannot reverse the user's cancellation. Expiry releases the tuple and makes a
later same-tuple request a fresh start. Canceling a flow without a nonce forgets it. A
new user action uses a new nonce. Source-create and OAuth-start correlation therefore
share D-36's rule: reconciliation is possible only when the client held the subject
correlation before sending.

**OAuth terminal response and materialization-error matrix (authoritative and exhaustive;
PM ruling 2026-08-12 01:28).** This matrix governs status/submit settlement.
`interrupted_pairs` uses the existing `SupplyGap` shape and reports a persisted effect
that already happened; it is never a refusal plan and never aliases `would_interrupt`.

| Decision | Flow/service condition | HTTP/API result | `interrupted_pairs` | First consumer |
| --- | --- | --- | --- | --- |
| `oauth_terminal.flow_only` | flow is non-terminal, or the adapter terminal is `failed` or `cancelled` without a local materialization error | successful `{flow}` | absent | OAuth poll/submit state machine |
| `oauth_terminal.create_success` | terminal create succeeds and its Source materializes | `{flow, source, added_to, adopted_by}` | absent | Add Subscription completion |
| `oauth_terminal.reauth_success` | terminal re-auth succeeds and its existing Source materializes | `{flow, source, recovered, interrupted_pairs}` | present as the complete report; may be empty | repair completion UI |
| `oauth_terminal.materialization_interrupted` | local terminal materialization fails after an acquisition-stage Source mutation has already left at least one sibling `(backend, model_id)` without supply | standard error envelope with the existing materialization error, no `flow`, and the exact nonempty report | present and nonempty | re-auth failure gap report and Source-list refetch |
| `oauth_terminal.materialization_plain_error` | local terminal materialization fails before any such interruption, or its exact report is empty | standard error envelope with the existing materialization error and no `flow` | absent, never an empty placeholder | ordinary OAuth failure rendering and Source-list refetch |

The same materialization error code can enter either error row; the deciding fact is the
persisted acquisition-stage route impact, not the error name. In particular, native
re-auth discovery failure after the Source has been cleared and marked unavailable
enters `oauth_terminal.materialization_interrupted` only when the computed report is
nonempty. Create materialization and re-auth failures with no stranded sibling enter the
plain-error row. No later Source read can reconstruct the historical report, so the
error envelope carries it exactly once.

**Protocol observation (owner ruling 2026-08-26, superseding the 2026-08-09
ambiguity-only selector ruling).** Every stored `protocol` is traceable to a real response
from that upstream before Save. Avibe never infers the value from vendor name or Base URL.
The form exposes Auto detect plus the three supported protocols before the first request.
Auto detect tries the authoritative sequence; a concrete selection restricts the attempt
to exactly that protocol. Neither is a save-time conclusion: the selected adapter must
still receive matching upstream response evidence before Save. A failed observation
therefore stores nothing rather than guessing.

Once saved, `protocol` is immutable for that Source. Connectivity retest, model
discovery, refresh, credential replacement, Base URL replacement, and restart all use
the stored adapter and never rewrite it. Changing protocol means creating a new Source,
so a later operation cannot silently reinterpret existing inventory or configured hops.
The stored shape carries no manual/automatic provenance marker and no unverified
protocol state: manual and automatic preflight become indistinguishable after the
same response-backed conclusion. “Add anyway” is available only after protocol has been
proved and some other information, such as model inventory, remains unavailable; that
uncertainty belongs to Source health, not protocol identity. Every saved Source therefore
has a response-proven protocol, and any path without that proof produces no Source.

`openai_chat` is the one Chat Completions-compatible transport; there is no separate
`openai_compatible` value because both names drove the same engine section and endpoint.
Chat Completions remains supported: OpenAI has not retired the platform API, and many
third-party and open-source upstreams expose it as their only compatible surface.

A source carries **no position, rank, or priority field of its own**. The Sources
module is an asset inventory sorted for reading convenience. Gateway configuration
owns one explicit Source order per backend and one exact Route chain per menu model;
neither order is stored on a Source.

**Supply channel.** Each source has a `supply_channel`:

- `native_cli` — the credential remains in the official CLI's local store. This is
  the recommended and default channel for Claude subscriptions (Claude → Claude
  Code). ChatGPT native login (ChatGPT → Codex) remains supported, but is a
  secondary, non-recommended path and is never the default add-flow guidance.
- `hub` — the managed engine holds the credential and re-originates requests. This
  is the default for API keys and the recommended add-flow path for ChatGPT
  subscriptions. It is an explicit opt-in for Claude subscriptions. A hub-held
  subscription is a normal Gateway upstream and may appear in a cross-vendor
  configured chain (§4.4).

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

The same model may be supplied by multiple Sources; §4.3 alone executes the exact
stored route for that model.

### 4.2 Gateway strategy — add-time defaults, global priority, then explicit configuration

One record per Agent backend owns `mode`, its menu, one stored Source order, and one
stored Route chain for each menu model. The Source order is the visible Gateway priority
and the Add-time placement input. Saving that priority atomically stores the complete
order and applies it to existing Routes without changing their hop membership or
mappings. There is no backend Source-order policy discriminator
and no per-model route policy. Runtime executes the exact hop order stored for each model.

Matching has exactly three one-time write points. Add Source matches the discovered
inventory observed by that transaction and appends accepted hops. A backend menu-model
add matches that one new row against the complete non-retired inventory (discovered and
manual) of configuration-eligible Sources in the backend's stored Source order, then
writes accepted hops in that order. A built-in reconcile add uses that same menu-model
match and seeds the built-in snapshot's label and reasoning efforts. A later refresh,
restart, health change, or turn never repeats any match for an existing row. Users maintain persisted chains directly after creation:
add or remove a hop, move it, or edit its explicit upstream `model_id` mapping.

Cancellation of Source creation has one commit boundary. Before the durable Source
commit, AC-26's transient-reference cleanup and pending-revocation accounting complete
before cancellation settles. After the commit, cancellation only ends the caller's
wait: Source creation and accepted placement finish atomically, remain readable after
reload, and have no server-side abort or rollback branch.

**Add-time Source placement policy (sole authority; owner 2026-08-09 S-1).** The Add
Source service owns one write-time rule that chooses deterministic positions for the
new Source and every accepted exact match. The same transaction writes those positions,
returns each hop through `added_to.position`, and exposes the canonical backend Source
order; the Gateway renders the stored results and the user may adjust them immediately.
No adapter, UI consumer, refresh path, or runtime resolver may implement or rerun
placement. This named policy is one implementation rule, not a plugin point, registry,
user setting, or persisted policy discriminator.

**Current policy value (`placement-v1`).** Append a newly added Source to each
configuration-eligible backend Source order, and append every accepted exact match to
that menu model's current Route-chain tail. For a newly added menu model, write one
accepted hop per supplier in the already stored Source order. This is only the current policy value, not
an API, UI, or acceptance invariant. A later version may choose a better visible
position from model fit or a fixed Source-reliability priority, but it must still run
only at one of the three one-time write points, persist the chosen position, and leave runtime to execute that
configuration verbatim. There is no “new Source not enabled” state or prompt, and the
UI never uses position to distinguish new from old.

No health score, latency, cost, usage, vendor label, creation timestamp, or later
inventory result reorders existing configuration. `created_at` remains ordinary Source
metadata for audit/display; routing and placement never read or mutate it.

The explicit `POST /api/models/agents/<backend>/chains/reorder` operation is the sole
server-side post-creation operation that implicitly applies the stored Source-order
sequence to existing Routes. When the UI supplies an `order` body, persisting that
order and applying it to existing Routes are one mutation; an omitted body applies the
already stored order. It reorders existing hops only, preserves every exact
`(source_id, model_id)` member and mapping, and never reruns matching. Its complete stable
order is defined in §4.6. A user-authored per-model `hops` PUT carries only the submitted
explicit hop order, and its server handler never reads `sources.order`; an editor may use
its page-held Source-order projection to help sort a local draft, but that draft becomes
explicit `hops` with no Source-order semantics on the wire. Because the all-chain operation
cannot remove supply, it has no guarded `409`, `force`, or interruption branch even when
the first hop changes.

On `PATCH /api/models/agents/<backend>/mode`, a `direct` → `hub` transition also owns
one bounded adoption transaction. If the backend has a sanctioned, recognized CLI
login and no `native_cli` Source, that transaction creates the singleton native Source,
applies `placement-v1`, commits every accepted exact match, changes mode, and only then
assembles the returned AgentSupply. An existing native Source, an absent or
unrecognized login, or another mode transition creates nothing. Repetition cannot
create a second native Source.

**Current matching value (`matching-v1`).** Add Source uses the discovered inventory
observed by that transaction. A menu-model add uses the same matcher, including the
Claude alias rule, over each ordered eligible Source's complete non-retired inventory.
All three persist the concrete upstream id in the Route hop. Matching runs once for each add
and is never re-run by refresh, restart, health changes, or turn execution.

| Backend/source case | Candidate set | Accepted match and tie-break |
| --- | --- | --- |
| Claude fixed-menu id on a native `anthropic` Source | The Source's `discovered` models observed by this add transaction | A dated request is literal only. An undated version request matches the same family and exact version tuple; a bare `opus`, `sonnet`, or `haiku` alias matches that family at any version. Select `max(version_tuple, date_or_zero, model_id)`. `fable` has no bare alias. |
| OpenCode menu id | The Source's observed models projected through its normalized provider id | Exact checked id first; otherwise a bare model id matches only when exactly one checked identifier ends with `/<bare>`. Zero or multiple matches is rejected as ambiguous. |
| Codex fixed-menu id, or any non-native Source | The Source's observed models | Literal model-id equality only. Explicit user route edits may name an exact model, but runtime never infers or substitutes one. |

The resulting hop always contains the selected concrete upstream model id, never the menu
alias. A missing or ambiguous match leaves that menu model's Route empty; it does not
create a runtime matching branch.

The `matching-v1` decision is normative and exhaustive:

```text
for each configuration-eligible Source added in this transaction:
    candidates = [request.protocol] if selected else SOURCE_PROTOCOLS
    observed = response_backed_observation(Source, candidates)
    if observed.protocol is null:
        persist no Source and no Route hop
        return the classified observation failure

    for each backend menu model M:
        candidates = observed.discovered_models
        if backend == opencode:
            checked = M if M is checked else the unique checked identifier ending with "/" + M
            match = the unique candidate whose normalized provider/model identifier equals checked
        elif backend == claude and Source is native anthropic:
            match = native_claude_alias(M, candidates)
        else:
            match = literal_model_id(M, candidates)

        if match is not null:
            append {source_id: Source.id, model_id: match} to the stored Route tail

    persist the Source and all accepted concrete hops once
```

`native_claude_alias` is the literal former resolver rule: dated requests are literal;
undated version requests require an equal version tuple; bare aliases match a family;
the total order is `(version_tuple, date_or_zero, model_id)`. `exact_checked_identifier`
and the unique suffix rule are the OpenCode overlay rule. No other backend gets an
alias family, and explicit user-authored Route edits remain literal configuration.

**Routing configuration is per backend and per model; health is Source-global.** Quota
and reachability belong to the Source, not the Agent that touched it. §4.3 reads the
stored chain and annotates its live execution state; it does not construct another
chain.

### 4.3 The only normative configured-chain execution algorithm

For backend `B` and menu model `M`, let `C` be the exact persisted hop array at
`B.routes[M].hops`. Every hop is `{source_id, model_id}` and its `model_id` is the exact
upstream value to invoke. Configuration writes validate Source existence, channel
binding, and that the Source can call the submitted model; any alias resolution or
suggested mapping is completed before the hop is stored. Runtime never matches by
vendor or inventory, inserts a Source, substitutes a model, or reorders `C`.

The following pseudocode is normative and exhaustive:

```text
if B.mode == "direct":
    return DIRECT

attempted = false
for hop in C, in stored order:
    live = inspect_exact_hop(hop)
    annotate hop with live.runnable, live.reason, and live.retry_at
    if not live.runnable:
        continue

    attempted = true
    result = invoke_exact(hop.source_id, hop.model_id, exact_reasoning_effort(hop))
    if result == served: return SERVED(hop)
    if result == canceled: return CANCELED
    if result is terminal_request_error: return FAILED_TERMINAL(result)
    if result is engine_down_at_any_request_phase: return FAILED_TERMINAL(engine_down)

    apply_attributable_failure_decision(result)
    if result.output_started: return FAILED_TERMINAL(result)
    if result is fallback_class: continue
    return FAILED_TERMINAL(result)

return NO_CANDIDATE(classify_blockers(C)) if not attempted
else EXHAUSTED(classify_blockers(C))
```

`inspect_exact_hop` checks only whether that configured hop can run now. `healthy` and
an elapsed cooldown are runnable. An unelapsed cooldown, `needs_action`, `error`, a
missing/deleted Source, a configured model no longer callable by that Source, or an
unavailable native CLI process is not runnable; the hop stays at its configured
position. The canonical reasons are respectively the existing classified Source reason,
`source_missing`, `model_unsupported`, and `native_cli_unavailable`. A normal turn never
repairs or rewrites configuration.

`invoke_exact` preserves chain order. A `native_cli` hop uses the sanctioned backend's
singleton local login; a `hub` hop uses the local Gateway and may be cross-vendor. The
system never prepends native supply or chooses a model. If the requested reasoning
effort exactly appears in the configured hop model's `reasoning_efforts`, pass that one
value; otherwise omit the effort field, with no approximation or downgrade.

Parameter, protocol, and tool-compatibility failures are terminal without fallthrough.
A local Gateway start, listener, or process loss at **any** request phase is terminal
`engine_down`: it mutates no Source, does not replay output, and does not walk another
hop. Credential failures follow the authoritative matrix below. The network-failure
matrix owns every upstream transport branch. While `stream_started` is false, shaped
quota/429/authentication/5xx results retain their existing classifier while an
unclassified connection failure creates only live short backoff. The fact flips only at
the first user-visible model-output byte, never at HTTP status, headers, or another
response byte. After it flips, a stream interruption creates only its existing redacted
event and never mutates Source health or creates backoff. No post-output failure is
replayed.

The read projection is `C` with live annotations plus `current`, never a reconstructed
provider list. Takeover remains derived: the current hop is not `C[0]` and `C[0]` is
unavailable for a recoverable quota/cooldown or live connection-backoff reason. Recovery changes current execution
position on the next turn without changing `C`. Every switch is recorded for pull
surfaces; a successful handoff emits no conversation notice or setting.

**Wire observation contract (authoritative and exhaustive; owner ruling 2026-08-12).**
Observation is not validation. Model Hub observes upstream wire data only to settle
health, provenance, and fallback. It is a byte-transparent intermediary: bytes delivered
to the caller are identical to the bytes received from upstream, and it does not act as a
protocol conformance validator.

The complete observation surface is limited to these facts:

1. An official error envelope at a terminal position is `failed_terminal`; machine-code
   extraction reads only the protocol's declared C12 trust roots.
2. An official success terminal is `served` when its discriminator identity matches. For
   named SSE events this is the minimal `(event name, data.type)` match. This is
   discriminator-only observation, not validation of the remaining envelope members or
   event lifecycle.
3. EOF without a recognized terminal is the existing network family and is
   non-punitive to durable Source health.
4. `stream_started` flips only when content, refusal, tool-call, or image-generation
   model output crosses the caller boundary. Role metadata, transport frames, and error
   frames do not flip it.
5. The observer performs only the framing normalization required to see those facts:
   stripping an initial UTF-8 BOM and splitting SSE lines and events across CRLF, LF, or
   CR boundaries. Its private metadata copy has a fixed memory budget: large string
   values are elided, and an event whose remaining JSON structure still exceeds that
   budget becomes unobserved rather than invalid. Event names are retained only up to
   the supported discriminator budget. The original bytes continue downstream
   unchanged, and no model-output fact is asserted until the event's data discriminator
   has been observed.

Response size is not a protocol-validity rule. Model Hub applies no local byte ceiling to
successful upstream response bodies, SSE lines or frames, pre-output replay, or model
inventory responses. Request admission limits remain separate request-side policy. Memory
thresholds may spill retained replay bytes to a temporary file, but must never truncate,
reject, reclassify, or trigger fallback for an otherwise valid response. Pre-output
replay has one absolute transport deadline, so keepalives cannot renew the wait forever;
buffered responses spill before replay. One taxonomy-backed selective JSON projector owns
their terminal error, machine-code, and usage facts for every body size; it lexes only
the finite protocol paths and skips unrelated subtrees while the exact body remains on
disk for replay. Model discovery uses the same selective projection for its inventory.
Optional response mutation, such as restoring an OpenCode tool alias, operates on the
same exact-byte spool for every body size, has a bounded working set, and fails open to
the original bytes when it cannot
finish within that set.

Everything else is forwarded and ignored for settlement. Model Hub does not judge
`sequence_number` presence or order; completeness of the event vocabulary; lifecycle
ordering such as `created` before `completed`; envelope members beyond the terminal
discriminator and declared error trust roots; malformed data payloads; duplicate JSON
keys; non-finite JSON values; or other JSON conformance details. Unknown events and
unparseable data remain transparent and cannot poison a later recognized terminal.
Once a terminal fact is observed, it is a fact barrier: later frames, including
keep-alives, cannot replace or invalidate it.

**Credential-failure decision matrix (authoritative and exhaustive; owner ruling
2026-08-09).** The refresh branch is selected by the credential's actual refresh
capability, not by vendor, Source kind label, or HTTP status alone. Surrounding prose
and every contract consumer may reference this matrix but cannot introduce another
credential-failure branch.

| Decision | Observed result | Credential capability | Retry / classification | Persisted state and remedy | Route effect |
| --- | --- | --- | --- | --- | --- |
| `credential.refresh_once` | first `401` | exposes a refresh operation | refresh once, then retry the same hop exactly once | none before the retry resolves | stay on this hop for the bounded retry |
| `credential.refresh_failed` | the refresh operation times out, is rejected, or returns an invalid response | exposes a refresh operation | no retry; use the existing `credential_expired` or `credential_revoked` classification the adapter can prove | the matching existing `needs_action` detail and refresh-capability-specific remedy | before output, continue to the next runnable hop |
| `credential.refresh_rejected` | retry after refresh is still `401` | exposes a refresh operation | no second refresh; `credential_expired` | `needs_action` + `models.source.needs_action.oauth_expired`; `POST /sources/<id>/reauth` | before output, continue to the next runnable hop |
| `credential.static_unauthorized` | first `401` | no refresh operation, including a static API key | no retry; `credential_revoked` | `needs_action` + `models.source.needs_action.credential_revoked`; `PUT /sources/<id>/credential` | before output, continue to the next runnable hop |
| `credential.account_classified` | classified `402/403` account result | any | no credential refresh; retain the adapter's existing source-global credential classification | `needs_action`; choose the remedy from both classification and credential capability: refresh-capable auth re-authorizes, a static key is replaced, balance is topped up, and a banned account goes to the vendor | before output, continue to the next runnable hop |
| `credential.request_nonfallback` | a non-fallback request-level failure | any | no credential refresh and no Source-global credential classification | no Source-health mutation; surface the request failure | terminal without fallback |

**Network-failure totality matrix (authoritative and exhaustive; owner ruling
2026-08-11 19:44–19:56).** “Shaped” means the adapter received an explicit, closed
machine classification such as quota exhaustion/429, an authentication-family result,
or 5xx. “Transport” means connection or stream failure without such a code. The phase is
the existing `stream_started` fact: false until the first user-visible model-output byte,
and true from that byte onward. HTTP status, headers, and other response bytes do not
start the model-output stream.

| Decision | Failure shape | Phase | Persisted Source judgment | Live backoff | Route/event effect |
| --- | --- | --- | --- | --- | --- |
| `network_failure.shaped_before_first_byte` | explicit closed code/classification | `stream_started: false`; before first user-visible model output | apply the existing non-permanent quota/rate/auth/server family and its unchanged recovery rule | none | before output, follow that family's existing retry/fallback rule and emit its existing redacted event |
| `network_failure.transport_before_first_byte` | no explicit code; connection failed | `stream_started: false`; before first user-visible model output | none; retain the prior Source state byte-for-byte | set Source-scoped in-memory connection backoff, then continue to the next runnable hop | emit redacted `network` event; no configuration mutation |
| `network_failure.shaped_after_first_byte` | explicit closed code/classification arrives only after model output began | `stream_started: true`; after first user-visible model output | apply that existing non-permanent family and its unchanged recovery rule | none | terminal, no replay; emit only the existing redacted event |
| `network_failure.transport_after_first_byte` | stream interrupted without explicit code | `stream_started: true`; after first user-visible model output | none; the successful connection/authentication/output evidence wins | none | terminal, no replay; emit only the existing redacted `network` event |

Connection backoff is live execution state, never Source/configuration state. For the
same Source, consecutive `transport_before_first_byte` decisions use delays
`1, 2, 4, 8, 16, 30, 30, ...` seconds. While the deadline is future, it overlays only
an otherwise `healthy` exact hop whose Source and configured model capability are still
present. That hop reads `health: backoff`, `runnable: false`, the deadline as `retry_at`,
and `reason: models.source.backoff.connection_failed`. Source cooldown,
`needs_action`, `error`, `source_missing`, or `model_unsupported` suppresses the live
overlay and keeps that durable/self-healing blocker's established health, reason, and
retry facts. The one process-layer exception is simultaneous `native_cli` unavailability:
its actionable `native_cli_unavailable` reason takes the single reason slot while the
backoff health and deadline remain visible and the chain is `interrupted`.
Deadline expiry makes the hop runnable again without a write. The first subsequent
user-visible model-output byte produced by that same affected Source clears both
deadline and streak automatically; successful fallback output from another Source does
not. Source endpoint/credential replacement and process reconstruction also clear them
because the state is in-memory and identity-specific. Before an API read is serialized,
the assembler captures one read time and normalizes an expired overlay to the Source's
underlying non-backoff health and runnability; a stale backoff deadline never crosses the
API boundary. The maximum is 30 seconds. This family never uses
`models.source.cooldown.*`, never writes `Source.state`, and never creates a permanent
health verdict.

**Live connection-backoff projection (authoritative and exhaustive; owner ruling
2026-08-11 19:44–19:56).** This table owns the one live health value that has no
persisted Source-state counterpart.

| Decision | Required live annotation | Persistence | First consumer |
| --- | --- | --- | --- |
| `backoff` | `runnable: false`, future `retry_at`, and `reason: models.source.backoff.connection_failed`; simultaneous native-process unavailability instead takes reason precedence as `native_cli_unavailable` without erasing health/deadline | in-memory only; never Source/configuration state | AgentChain and AgentSupply health reads |

**Live-backoff blocker-precedence totality (exhaustive; PM ruling
2026-08-11 23:49).** This is projection precedence, not a second health classifier.
An internal deadline may continue to age while a stronger fact suppresses its read
overlay.

| Underlying hop fact while the deadline is live | Backoff projection | Emitted hop facts | Fully blocked chain rollup |
| --- | --- | --- | --- |
| Source `healthy`, exact Source/model capability present, process available | apply | `backoff`, `connection_failed`, future deadline, not runnable | `waiting` when every hop is cooldown or this row |
| Source `cooldown` | suppress | existing cooldown health/reason/deadline, not runnable | `waiting` when every hop is cooldown or ordinary backoff |
| Source `needs_action` | suppress | existing `needs_action` health/reason and retry facts, not runnable | `interrupted` |
| Source `error` | suppress | existing `error` health/reason and retry facts, not runnable | `interrupted` |
| Source absent (`source_missing`) | suppress | existing missing-Source health, `source_missing`, and retry facts, not runnable | `interrupted` |
| Exact model capability absent (`model_unsupported`) | suppress | existing capability-missing health, `model_unsupported`, and retry facts, not runnable | `interrupted` |
| Source `healthy`, exact capability present, `native_cli` process unavailable | apply with the process-reason exception | `backoff`, `native_cli_unavailable`, the same future deadline, not runnable | `interrupted` |

The final mirror registry checks the closed (classification, credential capability) →
`detail_key` → remedy relation in both directions. The resolver suite executes every
row and fails any extra retry or unlisted remedy.

Because health is source-global, a cooldown created through one backend affects every
route using that Source. Because every turn runs the algorithm again, an elapsed
cooldown naturally restores the configured leading hop without mutating order.
The Model Gateway and Usage pages remain the pull surfaces for takeover state,
connector color, recent switches, and usage; provenance remains a debug affordance.

**Settlement freshness is owned by the attempt that started (round-7 audit, fixed
round-8).** Because health is source-global and every turn re-runs the algorithm, a
failure only describes the Source *as the failing attempt used it*. Each attempt
therefore takes a monotonic settlement generation **at attempt start and nowhere else**,
and a settlement whose generation is older than the newest attempt on that Source writes
no health. Two consequences are normative. A settlement that carries no generation cannot
prove it was not superseded, so it also writes no health — minting one at settlement time
would let any stale attempt certify itself as the newest, which is exactly how a restored
pre-restart failure cooled a standby Source. And because the generation ledger is
in-memory and restarts with the process, a persisted launch identity restores as
*pre-attempt*: older than every attempt this runtime can start, yet still able to settle a
Source this runtime has not attempted again. Refusing a health write is never punitive —
history, the terminal outcome, and the projection are unaffected.

**Takeover projection.** A configured route is in **takeover** exactly when its current
hop is not the first stored hop and that first hop is unavailable for a
self-healing quota/cooldown or live connection-backoff reason. This is computed from the resolved chain's current
hop and live runnability; it is never a stored boolean or a second routing field. A
chain with no runnable hop is not takeover: it reaches §4.5's truthful `exhausted`
terminal outcome and must not reuse takeover's visual semantics.

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

### 4.4 Configuration eligibility is server-authoritative (v3)

Which Sources may be written into a backend's configured chain follows the
channel-aware matrix:

| Source | claude | codex | opencode |
| --- | --- | --- | --- |
| `api_key` (any vendor) | ✅ | ✅ | ✅ |
| `subscription`, vendor `anthropic`, channel `native_cli` | ✅ | ✗ | ✗ |
| `subscription`, vendor `openai`, channel `native_cli` | ✗ | ✅ | ✗ |
| `subscription`, any vendor, channel `hub` | ✅ | ✅ | ✅ |

`allowed_origins` enforces **configuration-time channel semantics**, not a product-risk gate. For
`native_cli`, it contains only the sanctioned backend because the credential remains
inside that CLI. For `hub`, it may contain any supported backend selected by Gateway
configuration, including a cross-vendor consumer. No flag or consent record changes
either result. Runtime does not re-run this matrix to choose or reject a provider; a
validated stored hop is its authority.

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

**Server-validated invariants** (07-29, review round 6; replaced for configured chains
by S-1 on 2026-08-09). These hold on every agents payload and each route that writes
configuration:

- **`routes`** — exactly one row per menu model; every row owns one ordered `hops`
  array. Exact `(source_id, model_id)` pairs are unique within a route, while one Source
  may intentionally appear again with another model. Every new/edited hop names an
  existing Source allowed by the matrix above and an exact callable model validated at
  write time. Runtime may annotate a later-invalid hop but cannot remove or replace it.
- **Source deletion** — the transaction removes every hop naming the deleted Source
  from every backend route, preserves survivor order, reports the resulting gaps, and
  leaves a configuration that passes the same canonical validator used on reload.
- **`model_supply`** — exactly **one row per menu model**: `model_id` values are
  unique, and the set covers that backend's whole menu. Duplicates are the dangerous
  direction: two rows for one model let `chain_length: 0` sit beside `chain_length: 2`
  for the same id, and since consumers read the first match, the 「无来源可供」 flag
  becomes a coin flip rather than a fact. A missing row is milder but still leaves the
  drawer unable to say anything about a model the menu offers. Neither half is
  expressible — `uniqueItems` compares whole items, so rows differing only in
  `chain_length` pass, and coverage is a relation to a different document.
- **`AgentChain.chain`** — preserves the exact stored hop order and model ids. Its only
  additional fields are live annotations and current execution position; no consumer
  performs another Source/model walk.

### 4.5 State taxonomy — classified by "does it heal itself"

Three classes, because the action owed by the user differs in each.

**Source-level `state.status`:**

| Status | zh (UI) | Heals itself | Meaning |
| --- | --- | --- | --- |
| `active` | 正常 | — | healthy source; route use is shown by the persisted reference projection |
| `standby` | 正常 | — | healthy source; when `adopted_by` is non-empty it is shown as supplying the configured route |
| `cooldown` | 暂不可用 (gold) | **yes** | shaped quota/rate/server result; persisted `retry_at` known; recovers unattended |
| `needs_action` | 需处理 (rose) | **no** | OAuth expired, balance exhausted, key revoked/banned — dead until the user acts |
| `error` | 异常 | **no** | unclassified failure — no `retry_at`, so nothing clears it unattended |

`needs_action`, introduced in v2 and retained by v3, carries a `detail_key` naming the cause, so the
row can offer **one tap to fix it** (re-auth, top up, replace key) instead of a
dead-end error string.

**Runtime dependency state matrix (authoritative and exhaustive; owner ruling
2026-08-11).** `host_platform` is detected on the Avibe server, never from the browser,
and installation is supported exactly when it equals one
`manifest.assets[].platform`. The install route and status reads mirror this closed
state set; prose cannot add another runtime health value. `RuntimeDependency.enabled`
is orthogonal persisted user intent: it defaults to false when absent, explicit Start
sets it true, explicit Stop sets it false, and service startup starts the runtime only
when it is true. A transient process loss changes health, never this switch state.

| Decision | Meaning | Entry and exit rule | `status.error_key` |
| --- | --- | --- | --- |
| `ok` | verified runtime is listening and healthy | successful start or health recovery; a demanded loss exits to `down` | null |
| `degraded` | runtime is listening but its health check proves impaired service | current health evidence only; recovery exits to `ok`, loss exits to `down` | null |
| `down` | an installed runtime was demanded and failed or stopped | failed start or demanded process loss; a successful later start exits to `ok` | null |
| `not_installed` | no verified managed binary is installed | initial/unsupported state, or failed install; supported install enters `installing` | null initially/unsupported; `settings.models.install.fail.detail` after install failure |
| `installing` | one server-owned installation job is in progress | persisted before work begins with `installed_version: null`, `verified: false`, and `listening: null`; reload/repeat stays here; verified success exits to `not_started`, failure to `not_installed` | null |
| `not_started` | binary is installed and verified but intentionally idle | successful installation or pre-demand restart; explicit/runtime demand exits to `ok` or `down` | null |

`POST /api/models/runtime/install` is idempotent and owns the
`not_installed → installing → not_started | not_installed` transition. It fails before
download on an unsupported `host_platform`; a reload never translates `installing`
back to `not_installed`, and `/start` never performs installation. Calls from
`not_started`, `ok`, `degraded`, or `down` return the current RuntimeDependency as an
HTTP 200 no-op. They start no download, do not replace the verified binary, and never
start, stop, or restart the process; this installed-state no-op is evaluated before the
unsupported-host branch.

A service restart distinguishes persisted state from a live worker. Before runtime
endpoints become ready, an `installing` row with no worker in the reconstructed process
is reconciled atomically: a complete binary that verifies against the pinned manifest
settles at `not_started`; otherwise uncommitted staging is discarded and exactly one
fresh install job is claimed while health remains `installing`. If that recovery cannot
be claimed or scheduled, it settles at `not_installed` with
`settings.models.install.fail.detail`. A page reload therefore retains the live job,
while a process restart cannot preserve an ownerless transition forever.

**Runtime install refusal matrix (authoritative and exhaustive; owner ruling
2026-08-11).** Authentication and CSRF failures retain their shared HTTP contract; this
table owns the runtime-specific synchronous refusal branch.

| Decision | Entry condition | HTTP/API result | First consumer |
| --- | --- | --- | --- |
| `runtime_platform_unsupported` | exact `host_platform` has no equal `manifest.assets[].platform` | HTTP 422 normal failure envelope; no download; runtime remains `not_installed` with null `error_key` | install API client and boundary test |

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
**replacement, not re-creation** — deliberately, because the existing Source identity is
referenced by configured hops and must remain stable during credential repair. `created_at` is
ordinary audit/display metadata and is not a routing guard. Recovery must preserve the Source
and every stored route position. (Top-up is the third tap
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
| `waiting` | 暂时全部在冷却 | **yes** | nothing runnable right now, but every blocker is a persisted cooldown or live connection backoff — recovers unattended at the earliest `retry_at` |
| `interrupted` | 无可用来源 | **no** | nothing runnable and the stored chain is empty or at least one hop has a non-self-healing blocker: `needs_action`, `error`, `source_missing`, `model_unsupported`, or `native_cli_unavailable` |

These four values are the **only backend-level supply-health wording**. The Gateway
backend-group subtitle and the Usage page render this projection directly; neither
surface invents a parallel prose status. `takeover` is a separate display term for
§4.3's derived recoverable-fallback projection, not a fifth `supply_status` value or a
persisted field. A mechanical mirror/locale guard keeps the four status labels and the
takeover label synchronized across both locale sets.

AgentSupply also carries two orthogonal read facts. `cli_present` is the server-
authoritative per-backend executable-presence boolean; false on every backend is the
complete zero-installed-backend state, and the field proves neither login nor process
readiness. Every `model_supply` row carries `has_runnable_hop`, computed with §4.3's
exact-chain live predicate. Thus `chain_length > 0 && !has_runnable_hop` is an all-stale
Route, while `chain_length == 0 && !has_runnable_hop` is structurally empty.

`interrupted` is the honest name for the state v1 could not express: the source
list or configured chain can look populated, yet *this* Agent has nothing left to call. The UI shows
「当前无可用来源」 with a cause breakdown and exactly two exits — fix the
classified items, or edit the route. `native_cli_unavailable` uses its own mirrored
detail/remedy copy for restoring the sanctioned local CLI; it is never presented as an
upstream Source cooldown.

`waiting` exists to keep the surfacing rule below consistent. An agent whose
sources are *all* in persisted cooldown or live connection backoff has nothing runnable,
but nothing is owed either —
it heals itself in minutes. Collapsing that into `interrupted` would tell the user to
go fix a problem that resolves before they finish reading the sentence, which is
exactly what the self-healing tier is supposed to prevent. The Turn-outcome copy matrix
renders its recovery time rather than a fault; `current` is null in both states, so
neither ever renders a stale 使用中.

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
is a persisted cooldown or live connection backoff. The asymmetry is deliberate and
load-bearing — `interrupted` is the
OR-branch, `waiting` the AND-branch, so a chain holding one cooling source and one
revoked key is `interrupted`. Reading it as "every member needs the user" leaves that
mixed chain matching neither value and, worse, hides the action the user is owed for
the revoked key behind the fact that something else in the chain is merely cooling.

**Surfacing tiers** (the colleague test: ask for action only when action is owed):

| Class | Where the user meets it |
| --- | --- |
| self-healing (`cooldown`, `waiting`, recovery, in-turn switch) | 最近切换 feed, connector state, and the row's status pill on the Model Gateway and Usage pull surfaces. In-turn rendering is exclusively the Turn-outcome copy matrix below; this row adds no message branch |
| `needs_action`, `error`, `interrupted` | in-turn rendering comes exclusively from the Turn-outcome copy matrix; 需处理 remains on the Model Gateway and Usage pages until cleared. A blocker left behind by a turn that **succeeded** is page-and-feed only, by the row above |

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
  have to do anyway, because configured per-model chains change after the event is written and a
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
  against current configured chains — **at two grains, which must not be collapsed** (corrected
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
  all, so a forced deletion or an emptied chain would have fallen out of `interrupted`
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
  *current* chain, and since a recovered source normally stays in the same configured
  chains while the failure event stays in the bounded feed, that predicate would pin the
  pill to 「affected」 for as long as the event is retained — a recovery could never
  clear it. The event is what the **feed** renders; it is not what the **pill** reads.
  The pill reads the source's live blocking state — the same **contracted** facts the
  configured-chain executor checks before invoking each stored hop: `state.status` and,
  for a cooling-down source, `state.retry_at` (`source.schema.json`), surfaced per chain entry as
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
  Note that the configured-chain grain is what makes it right: Source inventory alone
  does not mean a backend uses that Source. Only an exact stored hop creates impact, so
  an unrelated Source failure cannot mark a backend degraded. A **backend-scoped** kind
  (`supply_interrupted`, whose cause is that backend's configured route) still names
  exactly the one backend it is about, because
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

**Turn-outcome copy matrix (authoritative and exhaustive; owner ruling 2026-08-09).**
In-turn copy is a
projection of the recorded terminal outcome plus only the discriminator named in this
table. Prose, emitters, and UI code may reference the selected row but cannot assert a
switch, remedy, or message branch absent from it.

| Decision | Terminal outcome | Exhaustive discriminator | Route/source fact | In-turn rendering |
| --- | --- | --- | --- | --- |
| `turn.served` | `served` | any, including a transparent fallback | the turn completed; any switch is only a pull-surface record | silent: no Error, warning, info notice, or appended action tail |
| `turn.exhausted` | `exhausted` | final model `supply_state` from the §4.5 taxonomy | fallback walked to the end; no attempt completed | `waiting` renders `models.launch.waiting`; `interrupted` renders `models.launch.interrupted` with the classified blockers |
| `turn.request_nonfallback` | `failed_terminal` | any non-fallback request-level failure | the attempted Source remains runnable and no switch occurred | `models.launch.request_incompatible`: this request is incompatible; switching Sources will not help |
| `turn.engine_down` | `failed_terminal` | local `engine_down` at any request phase, including after an upstream attempt or streamed output | no Source is blamed or mutated; no replay or next-hop walk occurs | `models.errors.engine_down`; after output began it also states that this turn's output may be incomplete |
| `turn.streamed_fallback` | `failed_terminal` | streamed fallback-class Source failure, plus `source_transition_persisted: boolean` recording whether its cooldown or `needs_action` transition was committed | replay is forbidden. When `source_transition_persisted=true`, render `models.launch.retry` only when live inspection of the same stored chain makes a different hop current for the next turn; otherwise no switch exists. When `false`, attempt history remains committed, no switch/current change is claimed, and the existing config-recovery warning owns remediation | persisted with a different current hop: “The next turn has switched Sources; retry.” Persisted with no runnable hop: use the same `waiting`/`interrupted` rendering selected for `exhausted`. Not persisted: `modelHub.errors.stream_interrupted`, with no route-change or new-remedy claim |
| `turn.no_candidate.unconfigured` | `no_candidate` | configured chain is empty | no Source was attempted because no hop is configured | `models.launch.route_unconfigured`, naming the requested model and pointing to Models |
| `turn.no_candidate.blocked` | `no_candidate` | configured chain is nonempty and model `supply_state` is `waiting` or `interrupted` | no Source was attempted because every exact hop is currently blocked | derive copy from the exact blocker set and reuse `_launch_failure` remedies: reauthorize, replace the key, or top up; `waiting` uses `models.launch.waiting`, while `interrupted` uses `models.launch.interrupted` |
| `turn.canceled` | `canceled` | the turn FSM, never a transport inference, settled Stop/cancel | no Source failure or route switch is fabricated | no Model Hub supply copy; the existing turn-canceled surface owns the message |

The matrix supersedes the earlier unconditional 「下一回合已自动换线」 sentence. Its
outcome/discriminator → copy-key relation is closed in the final mirror registry and
checked mechanically against both `vibe/i18n` locale files. A new outcome, discriminator,
or supply message ships as one new matrix row plus its enum/key/mirror fixtures; none may
land as standalone prose.

`source_transition_persisted` is an optional backend projection fact whose presence is
required only for `turn.streamed_fallback`. UI consumers deliberately do not consume it:
the selected copy key already carries the complete user-visible truth, following the
same optional-consumer discipline as `AgentSupply.routes`. It does not change
`contract_version`, which remains `5`.

One asymmetry has to be named, because it is easy to implement wrong: an Agent can enter
`interrupted` with **no Source changing state at all** — its route is unconfigured, a
configured Source was deleted, its exact model stopped being callable, or its native
CLI process is unavailable. Every other entry in the feed is keyed on a Source, so that
transition gets its own Agent-scoped event kind (`supply_interrupted`) with the exact
`route_unconfigured | source_missing | model_unsupported | native_cli_unavailable`
reason instead of borrowing a credential or quota reason. It fires once on the
transition, never once per starved turn. Its counterpart guard is on every explicit
route mutation: the response reports every selected model whose configured chain would
be emptied. Note the grain — per `(backend, model)`, not per backend. **"Selected" is
deliberately wider than 「已勾选」**: it is the union of an open menu's checked entries,
every menu model with a configured route, `agents.<backend>.default_model`, and each
enabled Vibe Agent's own `model`.
The protected identifier is always the **menu model**, never a hop's upstream
`model_id`, because the menu model is what an Agent can run and what the chain query
addresses. A configured chain that no Agent currently selects still represents deliberate
configuration and remains protected from a silent Source deletion; its
`SupplyGap.agents` list may correctly be empty. `api.md` → DELETE carries the full set
and the confirm copy names affected Agents when any exist.

Exact-hop referential integrity is a separate guard from the supply-gap calculation.
A non-forced Source DELETE refuses whenever any configured chain names that Source, even
when a later hop still supplies the menu model, and returns `source_in_route_chain`
plus ordered `would_remove_hops` entries naming each `(backend, menu_model,
source_id, model_id)` reference. `force=true` with that refusal's exact
`would_remove_hops` and `would_interrupt` arrays is an explicit cascade confirmation:
the same transaction deletes the Source and every exact hop that names it across every
backend route, while the identity and relative order of all surviving hops remain
unchanged. An emptied route remains an explicit empty configuration.
Any resulting protected-model gap is reported through the existing
`would_interrupt` projection alongside `would_remove_hops`. Each `RouteHopRef` also
carries one-based `position` in that named Route before the attempted mutation;
reporting sorts by backend, menu model, then that position, and a forced success repeats
the same references and positions as its refusal. This explicit cascade is
not the silent side effect prohibited by §2; without the confirmation, neither the
Source nor any chain changes.

The same invariant applies to **every Source-inventory mutation**, not only Source
deletion. Reversible or transactional changes — API-key Base URL replacement, API-key
credential replacement, explicit refresh/recovery, discovered-model retirement, and
manual-model deletion — first
stage the resulting inventory and run **both** guards: compare it with every exact
configured hop and recompute `would_interrupt` for every protected menu model. If an
exact configured model would cease to be callable, the non-forced mutation is refused
with `source_model_in_route_chain` and ordered `would_remove_hops`; another Source
supplying the same menu model does not make that exact reference disposable. If no
exact hop is lost but a protected route loses its last supplier, it is refused with
`source_last_supplier`. When both apply, the exact-hop error leads and the response
still carries both complete arrays. A confirmed `force=true` with an exact echo of both arrays applies the inventory
change and removes only those invalidated hops in one transaction, preserving the
identity and relative order of all survivors and keeping an empty route configured.
It also reports every resulting supply gap; force is confirmation, not a claim that
the mutation is interruption-free.

**Guard confirmation totality matrix (authoritative and exhaustive; owner subtraction
ruling 2026-08-11 20:35, with direct-Route scope corrected at 21:14).** The shared layer
recomputes under the atomic commit boundary. For Source and inventory mutations, a
guarded-impact plan is nonempty when the staged mutation has at least one
`would_remove_hops` or `would_interrupt` item. For `mutation.route_replace`, only a
nonempty `would_interrupt` activates the plan: the refusal also reports its submitted
removals, but a visible noninterrupting removal is ordinary success and reports those
items only as `removed_hops`. Confirmation is only the client's unchanged echo of the
two refusal arrays; no token, digest, version receipt, or server-side confirmation state
exists.

| Decision | `force` | Recomputed plan | Echoed refusal plan | HTTP/API result |
| --- | --- | --- | --- | --- |
| `guard_decision.unforced_no_impact` | false | empty, including visible noninterrupting `route_replace` removals | absent or supplied; echo is inert | ordinary mutation success |
| `guard_decision.unforced_confirmation` | false | nonempty | absent or supplied; echo is inert | HTTP 409 `GuardRefusal` with the current plan |
| `guard_decision.forced_no_impact` | true | empty | absent, exact, or stale | ordinary mutation success; `force` and any echo are inert because no guarded impact remains |
| `guard_decision.forced_confirmed` | true | nonempty | both arrays exactly equal the recomputed plan | commit once and return the matrix row's success envelope |
| `guard_decision.forced_unconfirmed` | true | nonempty | either array absent or either array differs | HTTP 409 `GuardRefusal` with the newly recomputed plan; remove nothing |

Every destructive guarded impact that commits therefore exactly matches the echoed plan. Every
409 carries a nonempty current plan and mutates no Route. A previously refused request
that now recomputes to an empty plan, including one carrying its old plan echo, follows the
ordinary success path without a fabricated guard error or request-validation variant.

**Guard error-plan relation (authoritative and exhaustive; owner ruling 2026-08-11
21:58).** The lead error always names a nonempty array that proves the refused impact;
the other array remains a complete projection and may independently be empty or nonempty.

| Decision | `error` | Required nonempty plan array | Other array |
| --- | --- | --- | --- |
| `guard_error.source_in_route_chain` | `source_in_route_chain` | `would_remove_hops` | `would_interrupt` remains complete |
| `guard_error.source_model_in_route_chain` | `source_model_in_route_chain` | `would_remove_hops` | `would_interrupt` remains complete |
| `guard_error.backend_model_in_route` | `backend_model_in_route` | `would_remove_hops` | `would_interrupt` remains complete |
| `guard_error.source_last_supplier` | `source_last_supplier` | `would_interrupt` | `would_remove_hops` remains complete |

**Source-mutation envelope matrix (authoritative and exhaustive; owner rulings
2026-08-09, confirmation binding simplified 2026-08-11 20:35).** These are all Source/inventory mutations, including writes that cannot
remove supply. Prose may describe their guard rationale but cannot define a request or
success envelope outside this table. Omitted `force` is false; every reported array is
present even when empty.

| Decision | Mutation | Request | Guarded `409` | Success |
| --- | --- | --- | --- | --- |
| `mutation.source_metadata` | change Source metadata/Base URL | `PATCH /api/models/sources/<id>` with `{display_name?, base_url?, force?: boolean, would_remove_hops?: RouteHopRef[], would_interrupt?: SupplyGap[]}` | `{error, would_remove_hops: RouteHopRef[], would_interrupt: SupplyGap[]}` | `{source: Source, removed_hops: RouteHopRef[], interrupted: SupplyGap[]}` |
| `mutation.credential_replace` | replace API key | `PUT /api/models/sources/<id>/credential` with `{key, force?: boolean, would_remove_hops?: RouteHopRef[], would_interrupt?: SupplyGap[]}` | same guarded `409` | same Source success envelope |
| `mutation.source_refresh` | refresh/recover saved Source | `POST /api/models/sources/<id>/refresh` with `{force?: boolean, would_remove_hops?: RouteHopRef[], would_interrupt?: SupplyGap[]}` | same guarded `409` | same Source success envelope |
| `mutation.model_create` | create a user-authored model entry | `POST /api/models/sources/<source_id>/models` with `{model_id, display_name?, reasoning_efforts}` | not guarded: it creates one new exact `id` with `origin: "manual"` and changes no existing `id`, `origin`, or Route | `{source: Source}` |
| `mutation.model_efforts` | replace one model entry's capability list | `PATCH /api/models/sources/<source_id>/models/<model_id>` with `{reasoning_efforts}` | not guarded: it changes no `id`, `origin`, or Route | `{source: Source}` |
| `mutation.model_delete` | retire a discovered model or delete a manual model | `DELETE /api/models/sources/<source_id>/models/<model_id>` with `{force?: boolean, would_remove_hops?: RouteHopRef[], would_interrupt?: SupplyGap[]}` | same guarded `409`; discovered retirement is staged before evaluating exact-hop and protected-supply loss | same Source success envelope; discovered success preserves the row with `retired: true`, manual success removes it |
| `mutation.source_delete` | delete Source | `DELETE /api/models/sources/<id>?force=<bool>` with body `{would_remove_hops?: RouteHopRef[], would_interrupt?: SupplyGap[]}` | same guarded `409`; a nonempty recomputed plan commits only when both arrays exactly match | `{removed_hops: RouteHopRef[], interrupted: SupplyGap[]}` after atomically pruning the Source from every backend Source order and every Route chain while preserving each survivor order; the deleted Source is not returned and legacy `{ok}` is invalid |
| `mutation.route_replace` | replace one model's complete Route chain | `PUT /api/models/agents/<backend>/chain?model=<id>` with `{hops: RouteHop[], force?: boolean, would_remove_hops?: RouteHopRef[], would_interrupt?: SupplyGap[]}` | only when `would_interrupt` is nonempty: `source_last_supplier` in the same guarded `409`, including all submitted removals; noninterrupting removal is ordinary success because it is the user's visible direct edit | `{chain: AgentChain, removed_hops: RouteHopRef[], interrupted: SupplyGap[]}`; noninterrupting removal needs no wire confirmation, and survivor order is the submitted order |

The request carrier shown in each row is the only one. The final `api.md`, server/client
envelopes, confirmation UI, and route tests mirror this matrix row-for-row.
`PUT /api/models/agents/<backend>/sources` is intentionally outside the matrix: it
changes only the persisted Source-order sequence, never a Route chain or its members, and
must not gain a guarded `409` branch. The UI uses the explicit
`POST /api/models/agents/<backend>/chains/reorder` operation with the complete order;
that operation stores the order and applies it to existing Routes in one mutation while
preserving the PUT's storage-only contract.
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
replaces the configured model or claims that the old supply is intact. Thus no
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
请求日志 / 诊断 entry in the 「模型」 page's 高级 area — a post-v3 candidate, not v3.
Mid-stream failure rendering comes only from §4.5's Turn-outcome copy matrix; this
provenance section does not infer or announce a switch. A Source left needing repair
still surfaces as 需处理 on the Models page, not as an appended turn message.

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

The five recorded outcomes and their exhaustive discriminators are defined only by
§4.5's Turn-outcome copy matrix. Provenance mirrors that closed outcome set rather than
reclassifying it. `no_candidate` carries an empty attempt list; `canceled` may retain an
unambiguously attributed interrupted-at-cancel attempt but never fabricates a Source
failure. Attribution-ambiguous attempts remain absent under AC-4's control fixture.
The terminating attempt is recorded in exactly one place — failed
attempts in an ordered list, the served attempt in its own field, the terminal error
in a third, at most one of the latter two ever populated, the full sequence
reconstructible by appending. That is a shape decision rather than a validation one:
it makes 「两个成功者」, 「成功者不在最后」 and 「摘要指向列表里没有的来源」 impossible to
write down, instead of invariants prose asks every implementer to respect.

### 4.6 Configured-chain storage and mutation

This section defines §4.3's persisted input and mutation boundary. Every known
`(backend, menu model)` has one row:

```json
{"hops": [{"source_id": "src_...", "model_id": "upstream-model-id"}]}
```

Each backend also persists one `sources.order: string[]`. It is a visible Gateway
configuration and Add-time placement input, contains only existing configuration-
eligible Source ids, and has no `follow | custom` or other policy discriminator. A
Source deletion removes its id from every backend order in the same transaction and
preserves the relative order of survivors. Serialization/reload rejects a dangling id.

`POST /api/models/agents/<backend>/chains/reorder` applies that sequence to every
existing Route without changing Route membership or mappings. When its optional `order`
body is present, it stores that sequence in the same mutation. For an original hop at
index `i`, use stable key `(0, source_order_index, i)` when its Source appears in the
current order and `(1, i, i)` otherwise, then sort lexicographically. Thus all listed
Sources come first in configured order, hops sharing a listed Source retain their
relative order, and all unlisted-Source hops follow while retaining their mutual
relative order. The operation is idempotent, never runs matching, and never adds,
removes, remaps, guards, or reports interruption. It is the only server-side existing-
chain operation that implicitly reads and applies `sources.order`; storing a new Source
order alone leaves every Route byte-identical. A per-model Route PUT is instead an explicit
user-authored `hops` replacement: its handler does not read `sources.order`, even when the
client used its already-held Source-order projection as a local draft-sorting aid.

`hops` may be empty and is always present. A newly introduced menu model runs
`matching-v1` once at that write and starts empty only when no ordered Source supplies it.
Refresh, restart, health changes, and turns do not retroactively match it. Only either
add-time match, an explicit user edit, or a confirmed guarded cascade changes the array.
Hop order, Source identity, and model mapping are user-visible configuration.

The full-list model PUT keeps its three-way merge. Caller additions are the ids in the
desired list but not its baseline; only one still absent from the latest stored list is
seeded, so a concurrently added row and its Route survive untouched. For caller additions,
the optional `expected_suppliers` map echoes the picker's ordered `(source_id, model_id)`
projection. The server recomputes every listed projection before staging the mutation. Any
difference refuses the whole write with `candidate_suppliers_changed` and a `changed` map
containing the current projections for the differing ids.

Backend catalogs also persist `removed_model_ids: string[]`. Removing any row adds its id
to the set; explicitly adding that id removes it. A catalog written without this field
loads with an empty set and reconciles normally. The controller re-reads the bundled
catalog, remote cache file, and installed CLI cache before every Model Hub agent read or
mutation and at startup. CLI cache participation follows the controller's executable
presence probe independently of backend enablement. It records the content-hash generation
it last reconciled, so a changed generation is applied within that request without any
notification from the UI process. A partial snapshot only exposes fewer built-ins at that
moment; it never infers a removal or tombstone. Each snapshot change adds, but never
removes, missing built-ins not in the set, in snapshot order among built-ins already present
(or at the menu tail when none remain), seeds snapshot label and reasoning efforts, and
runs the same one-time menu-add match. Remote-catalog payload, validators, success, failure,
and backoff state are keyed by the configured catalog source. Claude's locked `default` row
is excluded.

A write validates every newly introduced or changed exact pair before commit. An exact
pair already present in the persisted array may be retained or reordered even when a
later inventory or process change currently annotates it non-runnable; retaining or
moving that unchanged pair does not reclassify it as a new mapping. Its live reason
remains visible until the Source recovers, the user removes or changes the pair, or a
guarded cascade removes it. Source deletion follows §4.5's transaction: a non-forced
delete refuses while any route names the Source; a confirmed delete removes all such
hops across all backends and preserves survivor order.

There is no separate `mappings` field, policy discriminator, matching resolver, or
mapping diagnostic. `model_id == menu_model` is an identity mapping;
another explicit `model_id` is a user-configured substitution and must be invoked as
written. **The system never invents a substitution; user-configured mappings are legal
and authoritative.** This final-shape decision is **owner-vetoable (2026-08-07,
amended by S-1 on 2026-08-09)**.

The chain resource is `GET /api/models/agents/<backend>/chain?model=<id>`, with a
matching `PUT` carrying `{hops: [{source_id, model_id}, ...]}`. The read projection
returns the same stored array, in the same order, with only §4.3 live annotations and
current execution position added.

### 4.7 Downstream — Agents

| Agent | Menu | Notes |
| --- | --- | --- |
| Claude Code | fixed (built-in model IDs) | each built-in menu model owns one exact configured route chain; adding new menu entries belongs to the deferred Configure Agents module |
| Codex | fixed | same |
| OpenCode + future in-house agents | open | uses exact configured route chains; supports user-defined model entries |

### 4.8 OpenCode identifier scheme (locked 07-23, retained in v3)

OpenCode models are `provider/model-id`. Rules:

- The provider segment uses the **standard vendor id** (`anthropic/`,
  `openai/`, `zhipuai/`, …) — identical to native OpenCode usage. No
  `avibe-` namespace (owner: keep it simple). Unrecognizable vendors fall
  back to a single `custom/` provider. Add Source may use this normalized id when it
  proposes a one-time match; runtime reads only the stored exact hop.
- Gateway mode merely redirects those providers' transport to the local Gateway in
  the generated runtime config overlay. Therefore **identifiers are stable
  across Gateway/Direct switches, across source add/remove/failover, and — new in
  v3 — across any configured-chain edit**; never encode a concrete Source into
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
owns the handoff. Direct is the separate backend-wide mode and never labels one hop.

The Models page has exactly two top-level product modules:

| Module | Owns | Does not own |
| --- | --- | --- |
| **Sources** | Add/edit subscription and API-key Sources; credential location; discovered/manual model inventory; usage and source-global health | Route-chain order or model mapping |
| **Gateway** | Backend mode; exact per-menu-model configured chains; Source + model pairing; runnability, current hop, retry/failover/recovery state; probe and diagnostics entry | Credential entry, Agent-definition settings |

The visual connection between a Source and Gateway answers only current facts: configured
in N routes, serving now, cooling, or needs action. It has no id, CRUD route,
drag handle, or persisted policy. Configuration lives at one of the two real owners:
the Source or the Gateway chain.

A Source card's “Supplying …” line consumes the existing
`adopted_by: [{backend, menu_model}]` projection for Hub-mode backends: group its
configured rows by backend, de-duplicate backend names, and combine them with the current §4.3
runnability projection. No parallel “supplying backends” field is stored. If this
projection proves insufficient in implementation, the lane reports the exact missing
fact for a targeted expansion of `adopted_by`; it does not add a sibling field.

Required interaction rules:

- Sources remains an unordered asset inventory; there is no reorder affordance in that
  module.
- Gateway is the primary editing surface. Each model row shows the exact stored order
  and mapping that runtime will execute; blocked hops remain in place and dim.
- Add Source writes each one-time match at the deterministic position chosen by §4.2's
  placement policy. That position is visible immediately and remains user-editable.
  The UI never uses position to mean “new”: it has no bottom-only new section or other
  ordering-dependent newness. If temporary differentiation is needed, it uses a
  dismissible/transient marker derived from the add result, not a route field.
- Each Gateway backend group renders §4.5's exact `supply_status` value in its subtitle.
  This is the sole backend-health line; explanatory prose cannot compete with it.
- Adding Claude selects `native_cli` by default and presents Gateway custody as the
  optional path. Adding ChatGPT recommends and selects `hub` by default; native Codex
  login remains an available secondary choice without default guidance. Only the
  Claude + Gateway branch shows §4.1's one-sentence warning; it is informational, not
  a consent flow. When a backend already has its singleton native Source, its native
  choice is disabled rather than creating an alias for the same CLI login.
- Add Source exposes §4.1's combined connectivity/protocol observation and compatible
  model discovery with Auto detect plus a manual three-protocol selector. Source details exposes only the
  separately named mutating “Refresh models” / 「重新拉取」 action against the stored
  protocol; it has no “Test connectivity” button or second discovery mutation. Results
  stay in the current flow and use compact status plus an
  info affordance for explanation; the page does not grow permanent instructional
  paragraphs. Every inventory model exposes an editable per-model `reasoning_efforts`
  list beside the exact id. The list has no default item or selected state; the control
  form follows the owner-approved `design.pen` baseline and is not prescribed here. A
  selection constrains the first observation and every retry but still requires a
  successful response before Save. Source freshness may say only “Model list
  updated at …” / 「型号列表更新于…」 from `last_discovered_at`; latency and “last
  checked” copy are absent.
- Compatibility detail for converted or cross-vendor supply stays behind a compact
  info affordance: functionality is supported while reasoning content may degrade.
  There is no per-hop warning or alert treatment.
- Recently switched, connector state, source/route status, and usage remain pull
  surfaces. A successful fallback adds no turn copy. If every source is unavailable,
  the existing failure path still reports the error honestly.

The existing V6 frames remain a visual baseline for row density, health states, and
mobile treatment, but their Agent-card grouping and mapping drawer are not v3 product
authority. The owner-approved v3 interaction draft is the implementation baseline for
the two modules, native → Gateway takeover → native recovery, configured model chains,
and the Claude hub-add warning. The design lane adds production-complete
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
  the controller, `/api/models/` routes, or Models UI. I1 deletes the old default-off
  gate; an explicitly configured development/emergency override may disable the surface,
  but no fresh user depends on an environment variable to receive the product default.
- **Direct (supported diagnostic/self-managed path)**: current behavior —
  per-backend native config editing (auth tabs, API key + base URL, writes to
  `settings.json` etc.), useful for diagnostics and self-managed setups. On an existing
  installation's first Models-page visit, each Direct backend remains visible as a
  backend group labelled **Direct**, shows its current self-managed configuration
  summary, renders no Gateway chain, and offers one explicit **Switch to Gateway**
  action in that group. Gateway mode offers the inverse **Switch to Direct** action.
  The switch is per backend and reversible: it changes mode without deleting saved
  Sources or route configuration, and never rewrites the user's native config.
- Backends can differ in mode; the Gateway module surfaces the mode per backend.
  A `native_cli` hop inside Gateway mode is labelled **Native**, not Direct: Avibe still
  owns the pre-stream same-turn fallback and recovery policy. Product copy reserves
  “Direct” for `mode: direct` and does not explain either path as “not through Gateway.”
- **Native-config import** remains copy-only and reversible, a per-item checklist
  grouped by backend. Its action comes only from the authoritative matrix below; prose,
  scan code, and contract examples cannot add another value or infer a different default.

**Native-config import action matrix (authoritative and exhaustive; owner ruling
2026-08-09).** Originals are never modified or deleted, and Direct remains available.

| Decision | Action | Eligible detected item | Default / apply behavior |
| --- | --- | --- | --- |
| `import.keep_native` | `keep_native` | Claude or Codex subscription OAuth held by the sanctioned local CLI | selected by default; retain the credential in the CLI store, create the backend's singleton `native_cli` Source, and run the same one-time route match plus §4.2 placement as Add Source; reject a duplicate native Source before OAuth or partial commit |
| `import.copy_key` | `import` | API key plus optional Base URL, including an OpenCode provider key | selected by default; copy into a validated Hub Source, run the same one-time route match plus §4.2 placement as Add Source, and leave the original file byte-identical |
| `import.reauth` | `reauth` | detected material that cannot be safely copied or retained as a usable native login | not auto-applied as import; direct the user into the explicit authentication flow |
| `import.controlled` | `controlled_import` | future engine-owned OAuth-import capability that can preserve refresh semantics | reserved and not selectable/applicable in v3; explicit OAuth add is the only hub-held subscription path |

The final mirror registry compares this exact action enum with
`models.migration.action.<value>` in both UI locale files, following AC-19's closed-enum
guard; deferred still has explanatory copy even though it is not selectable.

The `keep_native` default prevents silent credential movement and does not replace
§4.1's ChatGPT add-flow recommendation. A hub-held subscription is established only
through explicit OAuth add, not native-file import. The import entry points are first
open after upgrade, the setup wizard, and the backend-page banner.
- **Add-source closing loop (v3).** Creating a Source returns
  `added_to: [{backend, menu_model, source_id, model_id, position}]` for every exact hop
written by the one-time match. `adopted_by: [{backend, menu_model}]` is the stable
Source-card projection of those persisted Hub-mode references; transient health and process
  availability do not change it. A Source with no automatic match reports an empty
  `added_to` and remains available for an explicit route edit, without a separate “not
  enabled” state.

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

An explicit start has one bounded 30-second readiness budget; it succeeds only
after both the gateway model endpoint and management config endpoint respond.
The operational budget is based on local live evidence: the first process was
still alive when the former 10-second window ended, while the next start reached
both health surfaces in 7.4 seconds. It adds bounded cold-start margin without
claiming an unobserved internal engine phase or introducing a retry loop. If
readiness is not established, Avibe terminates the child and preserves the
existing runtime failure contract.
CLIProxyAPI stdout and stderr are directed to the operating system's null sink
when the process is created. Child bytes therefore never enter Avibe's Python
memory or service logs and cannot create pipe backpressure. Startup diagnostics
contain only bounded supervisor-owned structured fields: outcome, managed
version, exit code when applicable, elapsed time, readiness budget, and the
existing error contract. Avibe does not retain, inspect, redact, or publish child
output.

**v3 routing requires no new engine policy.** Failover is ours, not the engine's: the engine
runs as a single global instance with its own cooling and request-retry disabled
(`vibe/model_hub_runtime/config.py`), model prefixes pin the source, and Python
owns configured-hop walking and error classification. That boundary was chosen because
the engine's blind switching is broader than our signed error taxonomy
(`model-hub-engine-survey.md`, P0). Per-model configured chains sit above that line:
Python walks exact `(source_id, model_id)` hops; the engine executes the
one pinned hop it receives.

## 9. Explicit non-goals (v3)

- **No product-global priority list.** The backend-scoped Source order is explicitly in
  scope, and execution ordering remains configurable inside each `(backend, menu model)`
  Route chain.
- **Per-model ordering is explicitly in scope.** Owner ruling 2026-08-07
  supersedes v2's “No per-model ordering” non-goal. The scope is exactly §4.3 and
  §4.6's stored configured-chain input;
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
- **No automatic model substitution.** The system never changes a configured hop's
  `model_id`. A user-configured mapping to another model or vendor is legal and is
  executed exactly; that hop may use an API key or a hub-held subscription and requires
  no additional warning.
- **No protocol guessing or post-save backfill.** A stored protocol comes from a real
  pre-save upstream response, never a vendor/Base-URL string heuristic. Auto detect and
  manual selection only choose which adapters to verify; the selected adapter must still
  return matching response evidence before anything is saved. No later operation changes
  the stored value.
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
7. **Engine-owned OAuth file import.** The §6 Native-config import action matrix is the
   sole authority for `controlled_import`; this research item adds no action branch.

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
      Completions, and shows Auto detect plus those three protocol choices before
      observation; an ambiguous Auto result requires one concrete choice to retry.
- [ ] §4.2 alone owns one-time placement: Add Source, menu-model add, and built-in
      reconcile persist every accepted match at one deterministic policy-chosen position;
      no runtime path reruns placement, and no UI test infers newness from position.
- [ ] §4.4 allows every hub-held subscription to serve every backend while retaining
      native CLI's sanctioned-backend binding.
- [ ] §4.3 is the document's only configured-chain execution algorithm: it reads stored
      hops verbatim, checks only live runnability and error fallthrough, and derives the
      non-persisted takeover projection; §4.6 stores the same exact pairs the UI shows.
- [ ] The owner-vetoable final route shape is acceptable: every backend has one explicit
      Source order, every menu model has one explicit `hops` array, and no
      `follow | custom`, separate mapping, or runtime matching authority exists.
- [ ] §4.5 keeps state source-global, status live-derived, and every successful
      takeover silent; `supply_status` is the sole backend-health line, and a no-runnable-
      hop exhaustion never borrows takeover semantics; terminal in-turn errors plus
      Model Gateway/Usage pull state remain available.
- [ ] §5 has exactly Sources + Gateway modules; the connector is state-only and
      Configure Agents is deferred without a placeholder design. Source-card supply
      attribution reuses `adopted_by`, and saved Source details has only guarded
      「重新拉取」 with no latency or “last checked” copy.
- [ ] §6 reports the exact hops Add Source wrote through `added_to`, and Source-card
      `adopted_by` reflects only persisted Hub-mode route references.
- [ ] §6 reserves Direct for `mode: direct`, labels a `native_cli` Gateway hop Native,
      and defines a visible, reversible Direct ↔ Gateway action for every backend.
- [ ] §9 explicitly supersedes the old no-per-model-ordering non-goal and states that
      only users configure model substitution.
- [ ] §10 records the owner-waived official-API fidelity re-test, preserves M0 evidence,
      and does not expand GA scope.
- [ ] The implementation plan appends AC-22 onward and gives I1 an exhaustive final
      contract handoff with a 13-file same-tested-head closure; every remaining consumer
      and test lands before release under the I1–I5 file split.
