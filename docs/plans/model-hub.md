# Model Hub — Product Spec

Status: **v2.0** (2026-07-29) · supersedes v1.1 (2026-07-23) outright
Owner decisions incorporated through: 2026-07-29 (+08:00)
Design source: `../avibe-docs/design.pen`, frames `产品改造 V6 01 – V6 04` (desktop)
and `产品改造 V6 M01 – V6 M02` (mobile). The V4 frames document the shipped v1 and
are kept as history; V5A/V5B/V5C are rejected explorations.
Contracts: `model-hub-contracts/` at `contract_version: 2`.
Discussion record: Show Page of session `sesb7r2qwb4z4` (v1 rounds 1–10) plus the
V6 redesign round (2026-07-28/29).

## 0. Revision note — why v2 replaces v1

Owner ruling (2026-07-28/29): **a single global source priority list is a product
model error.** Ordering is a *consumption* property, not a *supply* property. A
source is an asset the user owns; how eagerly to spend it is a decision each
agent backend makes for itself. Claude Code and Codex legitimately want different
orders over the same set of sources, and one global list cannot express that
without lying to at least one of them.

v2 therefore moves ordering off the source and onto the per-backend supply
strategy: each agent backend owns **an ordered subset of the sources it is
eligible for**, plus a policy stating whether that order follows the product's
recommendation or is frozen by the user.

**No back-compat, no migration shims.** The feature has not GA'd:
`VIBE_MODEL_HUB_ENABLED` is dormant by default and every backend defaults to
`direct` (PR #1019). v2 replaces the v1 structures outright — the global
`priority_order` config key and `PUT /api/models/priority` are removed, and old
keys are dropped on load rather than translated. Nothing user-visible regresses,
because nothing user-visible shipped.

This is distinct from the product's **native-config import** feature (migration
scan/apply, §6): that is an onboarding capability for users arriving with existing
CLI configuration, and this ruling does not touch it.

Changed since v1.1: §2 promises 3–4, §3 vocabulary, §4 (restructured around the
supply/consumption split, plus server-authoritative eligibility, the state
taxonomy, and the per-(agent, model) chain), §5 frames, §9 non-goals, §10 open
items.

**Addendum — cross-vendor models are first-class (owner ruling 2026-07-29 02:22).**
GPT models must be usable in Claude Code, and Claude models in Codex, as a
**built-in** hub capability — never a user-visible "plugin" concept. Two
consequences, both folded into this revision:

- v2 now says plainly that this already works: an `api_key` source of the other
  vendor plus an explicit per-agent mapping (§4.3), riding eligibility rules that
  admit any vendor's key for any backend (§4.4) and engine-core protocol
  translation. §9's non-goal is sharpened accordingly — what is default-off is
  *automatic* substitution, not cross-vendor supply the user asked for.
- §10.4 carries the v2.1 candidate that makes it a first-class menu entry. That item
  **evolves the fixed-menu decision locked 2026-07-22** — built-in ids only — into
  built-in core plus user-added upstream models. Recorded here because a locked
  decision is being changed, on the ruling above, and gated on a fidelity spike
  rather than adopted outright.

Subscriptions are untouched by all of this: they stay bound to their own vendor's
backend in both channels (S2/ToS, §4.4).

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

## 2. Product promise (user-facing, locked)

1. Connect a source once — every Agent that can use it, can use it.
2. Subscriptions are consumed first (already paid); when quota runs out
   Avibe switches to the next source automatically and switches back on
   recovery. Work never stalls. (Mechanism: per-turn channel dispatch —
   subscriptions burn via the CLI's sanctioned native channel; the hub
   arbitrates the api-key tier. See §4.3.)
3. **Spending order is per-Agent and user-owned.** Each Agent has its own
   ordered list of sources; that list order **is** its spending order. By
   default the order follows the product's recommendation; the moment the user
   touches it, the order becomes theirs and Avibe stops changing it.
4. **The product never reorders behind the user's back.** Predictability
   outranks cleverness: no health scores, no learned ordering, no silent
   promotion of a "better" source. Every order change is either an explicit user
   edit or the documented recommendation rule admitting a newly added source at a
   stated position.

Core persona: individual users who already pay for Claude Pro/Max or ChatGPT
Plus/Pro ("spend what I bought first"). Secondary: API-key-only users.
Explicit non-persona: relay-station operators ("站长") — Avibe ships no
operations console.

## 3. Vocabulary (locked; UI copy uses ONLY these nouns)

| Concept | zh | en | Notes |
| --- | --- | --- | --- |
| The settings surface | 模型 | Models | Single nav entry between 通讯平台 and 后端 |
| Where tokens come from | 来源 | Source | Two kinds only: 订阅账号 (subscription account, OAuth) and API Key (key + editable base URL). A source carries **no order** |
| Per-Agent spend order | 来源顺序 | Source order | Per agent backend: an ordered subset of that backend's eligible sources. Replaces v1's global 优先级 |
| Order policy | 跟随推荐 / 自定义 | Follow / Custom | 跟随推荐 = server-computed, new sources auto-join; 自定义 = frozen by the user |
| Supply mode per backend | 中枢模式 / 直连模式 | Hub / Direct | Hub = default & recommended; Direct = legacy native-config mode, kept but not recommended |
| Per-Agent health rollup | 供给状态 | Supply status | 正常 / 降级 / 无可用来源 (§4.5) |

Banned from UI copy: 网关/gateway, 路由/router, 逻辑模型, Provider(作为界面
名词), 账号池, 中转站(as a **category**; the word may appear only inside
helper copy as an example use-case for a custom base URL), plus — new in v2 —
**优先级** as a standalone global noun. An order always belongs to somebody: name
the Agent. "Relay station" is NOT a source type — it is an API Key with a custom
base URL (owner decision 07-23; avoids the unanswerable official/unofficial
classification for OpenAI/Anthropic-compatible vendor endpoints).

## 4. Architecture: supply, consumption, resolution

The v2 split, stated once: **sources supply; agents consume; ordering lives on
the consumer.**

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

- `native_cli` — the credential lives in the CLI's own sanctioned store and
  quota is consumed by launching the CLI in its native form. Default for
  subscription sources (mandatory-default for Claude subscriptions:
  Anthropic prohibits and server-enforces credential use outside Claude
  Code; see `model-hub-tos-review.md`).
- `hub` — the engine holds the credential and re-originates requests.
  Default for api_key sources. For subscription sources this channel is
  available ONLY behind the consent-gated experimental flag
  (`subscription_hub_experimental`): explicit ban-risk consent copy (S2 §9),
  per-source opt-in, visible "experimental" marking in the source row.
  This applies to Claude and ChatGPT subscriptions alike; the flag ships,
  but nothing enables it silently.

The same model may be supplied by multiple sources; that is exactly what each
agent's source order arbitrates.

### 4.2 Consumption — per-Agent supply strategy (每 Agent 供给策略)

One record per agent backend. It owns:

- `mode` — 中枢 hub | 直连 direct.
- `menu_kind` plus the menu itself: `menu` (open-menu backends, i.e. OpenCode) or
  `mappings` (fixed-menu backends, i.e. Claude Code and Codex). Unchanged from
  v1: a mapping chooses a *model*, never a source.
- **the agent's source order** — an ordered subset of the sources eligible for
  this backend (§4.4), plus a policy:

| Policy | zh | Behavior |
| --- | --- | --- |
| `follow` (default) | 跟随推荐 | Order is server-computed by the recommendation rule below. A newly added eligible source **joins automatically** at its recommended position. |
| `custom` | 自定义 | A user-owned, frozen ordered subset. A newly added eligible source does **not** join; the UI hints 「有新来源未启用」 and offers one-tap enable. |

State machine: `follow` --any manual edit--> `custom` --「恢复推荐顺序」--> `follow`.
Forking to `custom` is implicit and immediate: reordering, enabling, or removing a
source while in `follow` freezes the current order as the user's own. Returning to
`follow` discards the frozen subset and recomputes.

**Recommendation rule (deterministic; document verbatim, implement verbatim).**
For a given backend, the recommended order is:

1. the backend's **own-vendor subscription**, if present and eligible — Anthropic
   subscription for Claude Code, OpenAI subscription for Codex — *regardless of
   supply channel*: a `native_cli` subscription and a consented hub-held one
   (`subscription_hub_experimental`) occupy the same first slot, because both are
   the same thing to the user, their own subscription, and the channel is a
   delivery detail. If both exist for one vendor, `native_cli` precedes the
   hub-held one — the sanctioned path is the safer default;
2. then all eligible `api_key` sources, **by `created_at` ascending**;
3. tie-break anywhere above by **source `id` ascending**.

The rule is *exhaustive over eligible sources*: nothing eligible can fall outside
it, which is what makes 跟随推荐 safe to auto-join. (A cross-vendor subscription
is never eligible for a foreign backend in the first place — §4.4 — so it is not
an omission here.)

Nothing else participates: no health score, no latency, no cost heuristic, no
usage-based reordering. This rule is the *entire* content of 跟随推荐, and it is
stable — the same set of sources always yields the same order.

Two obligations follow, and both are contract, not implementation detail:

- **Creation order must be persisted**, as immutable `created_at` on the source
  (`source.schema.json`). Insertion order in the config file is not a contract and
  the sources array is explicitly unordered (`api.md`), so without a stored stamp
  rule 2 is not reproducible.
- **Rule 3 is not decoration**, and it needs one companion rule to finish the job.
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

**Ordering is per-agent; health is global.** Cooldown and health state stay
**source-global**, shared across all agents. From first principles: quota
exhaustion and network reachability are properties of the *account*, not of the
agent that happened to touch it — if Claude Pro's cycle quota is gone, it is gone
for every consumer. The current implementation already works this way (the
cooldown pool keyed on the shared source row, `_cooldown` in
`core/handlers/model_hub/service.py`); v2 keeps it deliberately.

### 4.3 Resolution pipeline (step 0 + three steps)

0. **Channel dispatch** — per turn, before launch: if the first eligible,
   retry-ready source in *this agent's* order is `native_cli` (e.g. a healthy
   Claude subscription for Claude Code), launch the CLI natively with zero
   injection — sanctioned form, hub untouched. If that source is
   quota-exhausted/cooling (inferred from prior native-turn errors plus recovery
   timers), launch with hub injection so steps 1–3 arbitrate the hub-channel
   tier. Recovery flips the next turn back. This is possible because Avibe
   launches backends per request; switching never happens mid-process.
   `native_cli` sources are eligible only for their sanctioned client (Claude sub
   → Claude Code); enforced in code via `allowed_origins`-style binding.
1. **Mapping** — requested model ID → actual model. Identity by default.
   Only fixed-menu agents (Claude Code / Codex) can override, per-agent
   (e.g. Claude Code's `claude-opus-4-6` → `glm-5.2`). Mapping is an explicit,
   deterministic user choice.
2. **Candidates (v2)** — start from **this agent's ordered subset** (§4.2), in
   its order, then filter in two stages:
   - **capability** (structural, stable): a. the source supplies the (mapped)
     model id; b. the source is eligible for this backend and channel (§4.4);
     c. for open menus, the source's vendor matches the **provider segment** of the
     requested identifier. Predicate c does not fold into a: sources advertise bare
     model ids, so `zhipuai/glm-5.2` and `custom/glm-5.2` present the *same* bare id,
     and without the vendor predicate the agent's source order alone would decide
     which upstream answers — quietly serving a zhipuai request from a relay that
     happens to sit higher. The shipped resolver already enforces it
     (`opencode_provider_id(source.vendor) == provider`,
     `core/handlers/model_hub/resolver.py`); v2 keeps it, and keeps it in the
     *capability* stage, because a vendor is structural and not momentary.
     What survives is the *capability chain* for this (agent, model) pair — what
     §4.6 defines and what the UI displays, cooling members included.
   - **runnability** (momentary, per turn): d. the source is retry-ready —
     `healthy`, or `cooldown` whose `retry_at` has already passed, since the
     resolver retries a recovered source rather than waiting for a state flip.
     Never `needs_action` (cannot recover unattended) and never `error` (already
     known broken); today's resolver skips both, and admitting either would spend
     the turn on a known failure. What survives is the *runnable candidate list*
     this turn walks.

   The two are one definition with one extra filter, deliberately: a cooling
   source must stay **visible and dimmed** in the chain (frame V6 04) while being
   skipped by the turn. Dropping it from the displayed chain would tell the user
   they own less than they do; keeping it in the runnable list would burn a turn
   on a known-exhausted account. There is no global list at any point, and no
   per-model ordering: a model never carries an order, it only filters the
   agent's one order.
3. **Supply** — use candidate #1; on quota-exhausted/429, transient 5xx or
   network failure enter cooldown and take the next **within the same turn**;
   switch back on recovery. Convert protocol when needed. Every switch is
   appended to the human-readable 最近切换 log.

Error taxonomy (no blind fallback): parameter/protocol/tool-compat errors
surface to the caller; 401 → refresh once, then retry; 429 / explicit quota
exhaustion / transient 5xx / network → cooldown + next candidate, with cooldown
duration classified per cause (network / rate-limit / quota). Once streaming has
started, no transparent retry — see §4.5 for the copy this obliges.

**Mapping ≠ automatic cross-vendor fallback.** The latter ("Claude quota
gone → serve GPT") stays an experimental, default-off advanced flag with
visible per-event marking, pending capability/ToS verification. Architecture
reserves `allowed_origins` to restrict which clients a subscription
credential may serve.

**Cross-vendor supply IS a supported v2 capability** (owner ruling 2026-07-29
02:22). Running a GPT model inside Claude Code, or a Claude model inside Codex, is
something v2 supports today: add an `api_key` source of the other vendor, then map
that backend's built-in model id to the model you want. This is a designed
capability, not an accident of the plumbing, and it is **built in** — there is no
user-visible "plugin" concept anywhere in it. The user configures 来源 + 模型; the
hub owns everything under that.

Two existing mechanisms carry it, which is why it needs no new machinery:

- **Eligibility already admits it.** §4.4 row 1: `api_key` sources of *any* vendor
  are eligible for *every* backend. The gate is kind + vendor, never protocol — an
  OpenAI key is a legitimate source for Claude Code by construction, not by
  exception.
- **Protocol translation is engine-core.** The source declares its upstream wire
  protocol (`anthropic | openai_responses | openai_chat | openai_compatible`,
  `model-hub-contracts/adapter-interface.py`); the calling backend fixes the
  client-side protocol; the engine's built-in translator registry connects the
  pair, in both directions, streaming and non-streaming (S1 survey §3 conversion
  matrix). No plugin participates in the anthropic↔openai pairs.

§4.6's 「经映射」 marking is the v2 UX for it: the user sees which link in the chain
is reached through a mapping, so cross-vendor supply is *visible* rather than a
silent substitution — which is exactly what separates it from the default-off
automatic case above.

**What v2 does not yet promise is fidelity.** S1 §3 settled that *syntax*
conversion is implemented and heavily tested, and equally that thinking,
prompt-cache, tool, image/audio and service-tier semantics are **not**
capability-equivalence guarantees. So the visible mapping warning stays, and "how
well does a GPT model actually behave as Claude Code's model" is a measurement
question, not a design one — §10.4 makes it a spike with a go/no-go per conversion
pair.

### 4.4 Eligibility is server-authoritative (v2)

Which sources an agent backend may consume at all — independent of the user's
order — follows the compatibility matrix (unchanged from v1, keyed on
kind + vendor, because the engine performs protocol translation):

| Source | claude | codex | opencode |
| --- | --- | --- | --- |
| `api_key` (any vendor) | ✅ | ✅ | ✅ |
| `subscription`, vendor `anthropic`, channel `native_cli` | ✅ | ✗ | ✗ |
| `subscription`, vendor `openai`, channel `native_cli` | ✗ | ✅ | ✗ |
| `subscription`, vendor `anthropic`, channel `hub` | requires `subscription_hub_experimental` | ✗ | ✗ |
| `subscription`, vendor `openai`, channel `hub` | ✗ | requires `subscription_hub_experimental` | ✗ |
| `subscription`, any other vendor | ✗ | ✗ | ✗ |

**The vendor→client binding is absolute; the flag only unlocks the channel.**
Read the last four rows together: a hub-held subscription is keyed on vendor
exactly like a native one, so `subscription_hub_experimental` can never make an
Anthropic subscription eligible for Codex. Getting this wrong would breach the
frozen security invariant (`model-hub-contracts/README.md` #3, from spike S2):
subscription credentials are never offered to agents outside their sanctioned
client. Subscriptions are never eligible for OpenCode in any channel — it has no
sanctioned subscription relationship with either vendor.

**What changes in v2:** the rules stay, the *authority* moves. The agents payload
now carries a per-source eligibility signal (`eligible` + `reason_key`) computed
once on the server. The UI stops deciding: the chokepoint `isSourceEligible`
(`ui/src/components/settings/models/menus/identifiers.ts`), which self-documents
as ESCALATED precisely because it hand-mirrors backend logic, becomes a pure
projection of server truth. This pays down a debt the v1 lanes escalated and never
closed — two independent implementations of one rule, free to drift silently.

`reason_key` is an i18n key, so the drawer can say *why* a source is greyed out
(「ChatGPT 订阅不适用于 Claude Code」) instead of merely hiding it.

**Server-validated invariants** (07-29, review round 6). Eligibility is not the only
rule the server owns rather than the schema. These hold on every agents payload and
each is enforced by the route that writes it, because JSON Schema draft-07 cannot
state them at all — the full list with the reason per item is in `api.md` →
「Mechanical guards the schemas cannot carry」, and the boundary itself is
`model-hub-contracts/README.md` → required-vs-optional:

- **`sources.order`** — every id exists, is eligible for this backend, appears once,
  and the whole list is a subset of the eligible set (omitting one is how the user
  says 未启用). Rejected as `invalid_source_order`, naming the first offending id.
- **`model_supply`** — exactly **one row per menu model**: `model_id` values are
  unique, and the set covers that backend's whole menu. Duplicates are the dangerous
  direction: two rows for one model let `chain_length: 0` sit beside `chain_length: 2`
  for the same id, and since consumers read the first match, the 「无来源可供」 flag
  becomes a coin flip rather than a fact. A missing row is milder but still leaves the
  drawer unable to say anything about a model the menu offers. Neither half is
  expressible — `uniqueItems` compares whole items, so rows differing only in
  `chain_length` pass, and coverage is a relation to a different document.
- **`AgentChain.chain`** — `source_id` values are unique (a duplicate inflates
  `chain_length` into counting one credential as two fallbacks) and appear in the
  relative order of `sources.order`.

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

`needs_action` is new in v2 and carries a `detail_key` naming the cause, so the
row can offer **one tap to fix it** (re-auth, top up, replace key) instead of a
dead-end error string.

**`error` is a blocker, not a third class** (07-29, review round 6). This table
first wrote its self-healing column as 「unknown」, and that word was the root of a
real gap: `error` carries no `retry_at`, so nothing will clear it unattended, and the
chain contract has always counted it WITH `needs_action` in the branch that makes a
chain `interrupted` (`agent-chain.schema.json`). What we could not classify is the
**cause**; that is never a promise about **recovery**. Read as 「unknown」 it left one
transition unrepresentable — the last runnable source of a chain landing in `error` —
which is an interruption the notification rule below owes the user a push for, while
the event vocabulary had no cause that could carry it. The emitter's only options were
to borrow a cause nobody had established or to stay silent about the state we
understand least. So the vocabulary gained a fifth non-self-healing cause rather than
the obligation being quietly dropped: that transition is announced as
`kind: needs_action` with `reason: unclassified_error`, which is the exact counterpart
of `state.detail_key: models.source.error.unclassified`. It is not a new event kind —
「a source went dead and stays dead until someone acts」 is what `needs_action` already
means, and *which* way it died is what `reason` is for — and not a widening of
`supply_interrupted`, which nulls both endpoints and would strip the push of the one
source it needs to open. The five non-self-healing source keys and the five
non-self-healing event causes are a bijection, checked mechanically rather than
promised (`api.md` → 「Mechanical guards the schemas cannot carry」).

Two of those three taps need a route that v1 never had, so v2 freezes them:
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

`waiting` exists to keep the notification rule below consistent. An agent whose
sources are *all* mid-cooldown has nothing runnable, but nothing is owed either —
it heals itself in minutes. Collapsing that into `interrupted` would push the user
an alert about a problem that resolves before they can read it, which is exactly
what the self-healing tier is supposed to prevent. Its copy states the recovery
time, not a fault; `current` is null in both states, so neither ever renders a
stale 使用中.

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
mixed chain matching neither value and, worse, drops the alert the user is owed for the
revoked key on the grounds that something else in the chain is merely cooling.

**Notification tiers** (the colleague test: interrupt only when action is owed):

| Class | Surface |
| --- | --- |
| self-healing (`cooldown`, `waiting`, recovery, in-turn switch) | 最近切换 feed only — never an IM push |
| `needs_action`, `error`, `interrupted` | proactive IM push, colleague voice, e.g. 「relay 余额不足，已切备用；点此处理」 |

`error` is named in the second row explicitly (07-29, review round 6). It was
implicitly there all along — it is a blocker, and blockers are what the row is about —
but leaving it unnamed while the status table called it 「unknown」 is how a reviewer
ends up asking, correctly, which tier an unclassified failure belongs to.

Resolution events therefore carry `severity: info | action_required`, and the push
layer keys off that field rather than re-deriving urgency from `kind`. The two
tiers are cause-based, never count-based: "zero runnable candidates" is not by
itself a reason to interrupt anyone.

**Who receives an action-required push** (07-29, review round 5). The tier says an
interruption is owed. It does not say to whom — and with several IM platforms,
per-channel Agent overrides, and state changes made from the Web UI, that is not
self-evident. Two shapes were available: carry recipients on the event, or resolve
them at delivery. **Recipients are resolved at delivery, and the event carries no
recipient, channel, platform, or audience field:**

- the event names an **Agent** (a source-scoped kind resolves to the Agents on the
  backends the failed source actually supplies — the fan-out bullet below states that
  test once and normatively), and Avibe already routes each scope to a
  selected Vibe Agent — so the recipient set is *every scope whose routing currently
  selects an affected Agent*, computed at push time against the live routing table
- **the expansion is two hops, because 「Agent」 on the event and 「Agent」 in routing
  are different grains** (07-29, review round 7): `resolution-event.agent` is a
  BACKEND identifier — its enum is `claude` / `codex` / `opencode` (plus `system`) —
  while routing selects a NAMED Vibe Agent such as `pm`. Normatively, then:
  **recipients resolve by expanding each affected backend into the Vibe Agents currently
  enabled on that backend, then into the scopes whose routing selects any of those
  Agents.** Both hops read live state at push time. The gap hid because the default
  Agent of each backend happens to be named after it, so a one-hop reading works until
  a user renames an Agent or enables a second one on the same backend — at which point
  it silently addresses nobody. This sentence fixes only the expansion; **whether a
  zero-scope result falls back to a 「home」 scope, and whether long-idle scopes are
  filtered out, remain the standing open owner decision below** and are deliberately
  not answered here
- **「Each affected backend」 is a SET, and it is not always the one the event names**
  (07-29, review round 8): a **source-scoped** kind affects every backend the failed
  source actually supplies, because source health is a property of the source, not of
  the backend that happened to discover it. One hub key shared by Claude and Codex
  fails once and starves both; reading `agent` as the whole affected set would push to
  the discovering backend's scopes and silently drop the other's from a notification
  this section already promised them. **「Actually supplies」 is the chain grain, not
  order membership** (07-29, review round 9): round 8 wrote the test as 「every backend
  whose `sources.order` contains `from_source`」, and a `follow` order holds every
  eligible source — an API-key source is eligible for every backend — so a GLM-only key
  sits in Codex's order while appearing in no chain Codex can run. Failing during a
  Claude turn, it would have interrupted every Codex-routed scope over a source that
  could never have served or disrupted them, and an alert that arrives for supply you
  do not use teaches the user to dismiss the ones that matter. The test is the one
  `api.md`'s supply guard already computes at the (backend, model) grain: **a backend is
  affected when the failed source appears in the capability chain of at least one of its
  protected models** (that document's four-fact union). Order membership is necessary
  and not sufficient — a source absent from the order is in no chain either — so this
  narrows over-notification without dropping anyone the round-8 rule protected. It
  refines which backends are affected and nothing else: the two hops still run from
  every backend in the resulting set, and `SupplyGap.agents` still includes the Agents
  that inherit the backend default. A **backend-scoped** kind (`supply_interrupted`,
  whose cause is that backend's own order or selection) affects exactly the backend it
  names. The two hops above then run from every backend in that set, deduped per scope
  as below
- delivery is **once per scope**, deduped: one revoked key that starves three Agents
  sharing a channel is one message naming the source, not three
- an event caused by a settings mutation has no originating conversation, and that is
  not a special case here, because origin was never the addressing key. A Web UI
  mutation and a mid-turn failure resolve their recipients identically
- resolving to **zero scopes is a correct outcome, not a dropped alert**: nothing is
  routed to that Agent right now, so nothing is interrupted right now. The state
  still shows on the 「模型」 page and still lands in the feed

Carrying recipients on the event would have frozen a routing snapshot into an
append-only feed: a scope re-pointed at a different Agent afterwards would still be
addressed by the old event, and a scope added later would never be. The feed is a
record of what happened to supply; it is not an outbox.

**Open decision for the owner, deliberately not settled here** (delivery-layer
policy — neither answer changes the event contract or the feed): whether an
action-required event resolving to zero scopes should fall back to a designated
「home」 scope, so a key revoked while nothing is routed there is still announced
rather than waiting to be discovered; and whether *every* scope routed to an affected
Agent is a recipient, or only recently-active ones — a channel configured months ago
is technically routed, and pushing a credential problem into it may read as noise
rather than as a colleague speaking up.

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
model hits zero at once. **"Selected" is deliberately wider than 「已勾选/已映射」**
(07-29, review round 5): it is the union of an open menu's checked entries
(`menu.checked` — 07-29, review round 9: round 5 wrote 「checked fixed-menu models」,
which names state a fixed menu does not persist; `api.md` carries the scoping), **the
menu-side `builtin_id` of every mapping row**, `agents.<backend>.default_model`, and
each enabled Vibe Agent's own `model`. Menu-side, not the mapping's target (07-29,
review round 8): for `claude-opus-4-6 → glm-5.2` the protected identifier is
`claude-opus-4-6`, because that is what an Agent can be running and what `api.md`'s
single definition of the guard tests — one namespace, the menu one. Testing `glm-5.2`
would compare a resolved id against menu identifiers, match nothing, and let the
delete proceed without `force` while the selected built-in loses its last supplier.
The earlier phrasing tested the menu instead of the runtime, so the model an Agent is
actually running could go unprotected — unchecked in a drawer the user never opened,
and resolving by identity with no mapping row to find. `api.md` → DELETE carries the
full set and the confirm copy names the affected **Agents**, since 「删除后 pm 将没有
可用来源」 is actionable where a bare (backend, model) pair is not.

**Turn provenance.** Each turn records the model@source that served it, and that
record is inspectable from the conversation surface as per-turn detail — available
on demand, never noise in the transcript. Mid-stream failure, where no transparent
retry is permitted (§4.3), must say exactly 「下一回合已自动换线，直接重试即可」:
the user's next action is one retry, so the copy states that instead of describing
the fault.

This promise needs an interface, not just a paragraph, so v2 freezes one:
`turn-provenance.schema.json` + `GET /api/models/turns/<turn_id>/provenance`.
It defines *what* is recorded and *how it is read*; where it is stored is the
implementing lane's call, with one constraint — provenance is written when the turn
resolves and stays readable after the process exits, because "which source paid for
this turn" is a billing question the user asks days later, not just live. A turn
that switched sources mid-flight lists every attempt in order, so the record
explains the switch rather than merely naming the winner.

**That interface covers Hub-mode turns** (07-29, review round 8): a `served` record
requires a `source_id` matching `^src_`, and a Direct-mode turn runs from native
configuration with no `Source` row to name — so 「每个回合都有记录」 is satisfiable
inside Hub and unsatisfiable outside it without fabricating a source. Existing users
stay in Direct until they migrate (§6), which makes this the common case rather than
an edge one, so it is named here instead of left to the implementer to discover.
Whether a Direct turn gets a no-source provenance representation or the route answers
「此回合无中枢记录」, and what the per-turn affordance shows then, is an implementation
requirement recorded as **AC-1**. Two further terminal states the four outcomes below
cannot express — a user cancel, and an attempt interrupted by one — are **AC-4**.

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

### 4.6 The chain per (agent, model) — capability vs runnable

The chain that actually executes is **per (agent, model)**: the agent's order,
filtered by "can this source supply this model" (§4.3 step 2, capability stage).
This closes v1's honesty gap — v1 displayed one order per agent while N different
chains ran underneath it, one per model.

What the UI shows is the **capability** chain: every source that *could* serve this
model, in the agent's order, with cooling and `needs_action` members present but
dimmed and labelled. Runnability is a per-item flag (`runnable`, plus `retry_at`),
not a filter — so one payload answers both "what do I own for this model" and "what
would run right now", and the two can never disagree on screen.

Surfaced as:

- Tapping the model box on an agent row reveals that model's chain, reusing the
  order-chip visual from the agent row. Supply reached through a mapping is marked
  「经映射」 — the v2 surface for cross-vendor supply (§4.3), so a GPT model serving
  Claude Code is legible in the chain instead of hiding behind a built-in id.
- Each item in the 模型菜单 drawer can reveal its own chain the same way.
- A menu model whose **capability** chain is empty is flagged 「无来源可供」 in the
  drawer — a checkbox that would silently fail is a bug, not a choice. Note the
  distinction this rests on: 「无来源可供」 is structural and stable, so it must not
  appear merely because every source is mid-cooldown. That case is
  `supply_status: waiting`, and the row stays checkable.

Contract: a chain query (`GET /api/models/agents/<backend>/chain?model=<id>` →
ordered `[{source_id, via_mapping, resolved_model_id, health, runnable,
retry_at}]`), plus cheap per-menu-item capability-chain counts on the agents
payload so the drawer can flag empties without N round-trips. `health` carries the
source-global health only; per-agent role is positional (the first `runnable` item
serves the next turn), never a stored per-agent 使用中/备用 flag — a source can lead
one agent's order and trail another's.

### 4.7 Downstream — Agents

| Agent | Menu | Notes |
| --- | --- | --- |
| Claude Code | fixed (built-in model IDs) | wants another vendor's model ⇒ per-agent mapping in its 模型菜单 — supported, §4.3; first-class user-added entries are the §10.4 v2.1 candidate |
| Codex | fixed | same |
| OpenCode + future in-house agents | open | follows upstream model lists; supports user-defined custom model entries |

### 4.8 OpenCode identifier scheme (locked 07-23, unchanged in v2)

OpenCode models are `provider/model-id`. Rules:

- The provider segment uses the **standard vendor id** (`anthropic/`,
  `openai/`, `zhipuai/`, …) — identical to native OpenCode usage. No
  `avibe-` namespace (owner: keep it simple). Unrecognizable vendors fall
  back to a single `custom/` provider.
- Hub mode merely redirects those providers' transport to the local hub in
  the generated runtime config overlay. Therefore **identifiers are stable
  across Hub/Direct switches, across source add/remove/failover, and — new in
  v2 — across any per-agent reordering**; never encode a concrete source into
  the provider segment.
- Users never hand-assemble the string. Menu checkboxes pick models; the
  custom-model form generates and previews the identifier (source + model ID
  in → `zhipuai/glm-5.2-air` out). A custom model entry is, in data terms, a
  supplement to that source's supply list.

## 5. Surfaces (design.pen V6 frames)

The V6 frames are the v2 UI source of truth. Structure: **L1 overview stays
minimal, the L2 drawer holds the editing surface.** The V5A/V5B/V5C explorations
all failed the same way — laying N backends' orders on one page at once renders
every source N times and starves each column.

| Frame | Content contract |
| --- | --- |
| **V6 01** 总览 | 来源 card is a pure asset inventory: **no drag handle, no position number, no 使用中 column** — icon, name, mono sub-line (account label / masked key; cooldown ETA), usage column (subscription progress bar / monthly ¥), billing chip 包月/按量¥, health chip. **Agent** card, one row per backend: a name row (+ 菜单固定/菜单开放 badge, mode chip 中枢/直连) and a supply row = `[模型盒 mono]` + the order chain as chips `1→2→3` + a policy/status badge. The current source chip is mint-highlighted; a cooling source's chip carries a gold dot. Row action 「来源顺序」 opens the drawer. Below: 最近切换 (3 rows, human phrasing, view-all) and a single 高级 row (跨厂商自动顶替 default-off · 请求日志 · 诊断). |
| **V6 02** Agent 抽屉 · 自定义态 | Three sections: **启用** (drag handle + position number + 当前/暂不可用 pill + 移出 ×), **未启用** (+ 启用 button; annotated where a mapping makes the source usable, e.g. 智谱「经模型菜单改写后可供 Claude Code」), **不适用** (greyed, with the `reason_key` cause, e.g. a ChatGPT subscription under Claude Code). Header right: 「恢复推荐顺序」. Footer left: 「模型菜单与映射」 entry. |
| **V6 03** Agent 抽屉 · 跟随推荐态 | Section-header badge 「跟随推荐中 · 新来源自动加入」; no 恢复推荐顺序 link, since it is already following. Demonstrates one source set ordered differently per agent (relay at Claude #3, Codex #2) — the whole point of v2. |
| **V6 04** 故障实况总览 | Gold status capsule 「Claude Pro 额度用完 · 已自动切换，恢复后切回」; the source row shows a 100% gold bar + 暂不可用; the agent's chain shows chip 1 dimmed with a gold dot and chip 2 mint = current. |
| **V6 M01** 移动总览 (390) | Agent row stacks: L1 (tile + name + mode chip) / `[模型盒 + 策略徽标]` / chain row (10px chips) / full-width 「来源顺序」 button. OpenCode shown as 直连 + note + 接入中枢. |
| **V6 M02** 移动来源顺序 (two states) | Bottom sheet, height fits content. Mobile moves 「恢复推荐顺序」 to the footer-left button, replacing the desktop header link; the follow state drops that button and shows the 跟随推荐中 section badge instead. |

Carried forward unchanged from v1, still described by the V4 frames: 后端 ·
供给方式 card (V4 02), 迁移对话框 (V4 03), 添加来源 menu + API Key form (V4 06r/07),
连接订阅 OAuth shell with flow forms A/B/C (V4 09), 模型菜单 · Claude Code mapping
table (V4 04), 模型菜单 · OpenCode grouped menu (V4 05r), 添加自定义模型 (V4 08).

**Obsolete under v2:** the old mobile M02 row-action panel with 上移/下移, and any
sort-mode control on the 来源 list. The source library has no order, so it gets no
reorder affordance; ordering affordances exist only inside an agent's drawer.

Pending mocks (not blocking): the OpenCode drawer frame (same pattern as V6 02),
first-run empty state, Dark variants, plus a copy pass under the rule **"if UI
style can express it, don't write copy"**.

## 6. Modes & migration

- **Hub (default)**: Avibe injects runtime-only configuration into processes
  it launches (env vars for Claude Code; `-c` overrides for Codex app-server;
  `OPENCODE_CONFIG` overlay for OpenCode, gateway-config hash tracked for
  long-lived `opencode serve`). Native user configs are never written.
- **Direct (legacy, kept, not recommended)**: current behavior preserved —
  per-backend native config editing (auth tabs, API key + base URL, writes to
  `settings.json` etc.), useful for diagnostics and self-managed setups.
- Backends can differ in mode; the Models page Agent rows surface per-backend
  mode with one-click 接入中枢.
- **Native-config import** (frame V4 03) is unchanged by the v2 ruling: copy-only
  and reversible, a per-item checklist grouped by backend. API keys + base URLs →
  direct import; subscription OAuth → `keep_native` by default (stays in the CLI's
  sanctioned store and becomes a `native_cli` source; hub-held import only via the
  consent-gated experimental flag); Codex `auth.json` → `keep_native`. Footer
  promise: originals never modified or deleted; Direct always available. Triggers:
  first open after upgrade, setup wizard, backend-page banner.
- **Add-source closing loop (v2).** Creating a source answers "so what now?" in
  the same response: `adopted_by: [{backend, policy}]` tells the UI which agents
  picked it up automatically (those on 跟随推荐), so the success state can say so
  and offer one-tap enable for the agents on 自定义 that did not.

## 7. Security boundaries

- Three credential rings, never mixed: management key (Avibe→engine admin
  API), local gateway token (the only thing backends receive), upstream
  credentials (API keys and — only under the consent-gated experimental
  flag — subscription OAuth tokens; engine-held, local runtime dir with
  restricted permissions, not `~/.cli-proxy-api`). By default the engine
  never holds subscription OAuth tokens: `native_cli` subscriptions keep
  their credential in the CLI's own sanctioned store (§4.1).
- Credentials never enter Avibe Cloud, IM messages or logs. Static keys may
  integrate with Avibe Vault; no duplicate key entry across surfaces.
- Gateway failure is fail-closed; Direct mode is the explicit escape hatch.
- The dry-run probe (§10.1) inherits the redaction invariant of resolution
  events: it reports classified outcomes, never raw upstream error bodies.

## 8. Data plane

The hub's data plane is a **replaceable, Avibe-managed, versioned runtime
dependency** (current candidate: CLIProxyAPI ~14 MiB download / ~41 MiB
binary): pinned version + SHA256, 127.0.0.1-only listener, random management
key and gateway token, lifecycle owned by Avibe. Its YAML/auth files/manage
UI are **not** product surface.

**v2 requires no engine change.** Failover is ours, not the engine's: the engine
runs as a single global instance with its own cooling and request-retry disabled
(`vibe/model_hub_runtime/config.py`), model prefixes pin the source, and Python
owns candidate walking and error classification. That boundary was chosen because
the engine's blind switching is broader than our signed error taxonomy
(`model-hub-engine-survey.md`, P0). Moving ordering from global to per-agent sits
entirely above that line: it changes which candidate list Python walks, and
nothing about how the engine is driven.

## 9. Explicit non-goals (v2)

- **No global priority list.** Ordering only ever exists per agent backend.
- **No per-model ordering.** A model filters an agent's single order (§4.3
  step 2); it never carries an order of its own.
- **No health-scoring or smart auto-reordering.** No latency ranking, no learned
  preference, no cost optimizer. This is the §2.4 predictability promise — a
  product decision, not a missing feature.
- **No session-level source pinning.** "Run just this turn on that source" is a
  diagnostic need, served by Direct mode plus the dry-run probe — not by a
  per-session override that would make spending unpredictable.
- **No *automatic* cross-vendor substitution by default.** Sharpened 2026-07-29,
  because the old one-liner was read as banning cross-vendor supply altogether.
  What is off by default is the product choosing another vendor *for* the user when
  their own runs dry ("Claude quota gone → silently serve GPT") — that remains an
  experimental, default-off advanced flag with visible per-event marking. Explicit
  cross-vendor supply is a **sanctioned path**: per-agent mapping over an `api_key`
  source (v2, §4.3), and user-added cross-vendor menu entries (v2.1 candidate,
  §10.4). The line is drawn at *who chose*, not at *which vendor* — and it never
  moves for subscriptions, which stay bound to their own vendor's backend (§4.4).
- No billing-grade accounting, multi-tenant pools, or operator consoles.
- No third source category ("relay" merged into API Key).

## 10. Open items

1. **Dry-run probe** (`POST /api/models/agents/<backend>/probe`): one minimal
   request through the agent's current chain → `{probe: {source_id, model_id,
   latency_ms, reachable, error, via_mapping}}`. The outcome field is `reachable`
   and the object is nested, so it never collides with the response envelope's
   `ok` — **whether the call worked and whether the upstream completed the request
   usably are different questions** (corrected 07-29, review round 9: this line still
   read 「the upstream answered」, the definition `api.md` and the frozen
   `probe-result.schema.json` reject — a completed 402 or 429 *answered*, and must
   report `reachable: false` with the error key that says why, so the old wording
   would have produced `reachable: true` alongside an error and failed the schema).
   UI: 「试跑一次」 in the agent drawer footer, **offered for Hub-mode backends only**
   (07-29, review round 9): a Direct backend has no source order to run the probe
   through and no source id to name in the result, and what it should answer instead is
   AC-7. Contract frozen in v2; implementation lands with the L2 rebuild.
2. **Quota projection**: nullable `projected_exhaust_at` on subscription usage
   (linear projection over recent usage), driving a sub-line 「按近 7 天用量，预计
   周三用完」. Phased deliberately — the contract field is frozen in v2, the
   projection itself may land later, and the UI must render the null case as
   simply absent.
3. **Fallback spend attribution** 「本月替补消费 $X」 — **v2.1 candidate, not in
   v2.** It needs per-source metered spend attributable to fallback turns
   specifically; whether the engine's usage data can support that (with its usage
   feed disabled for key-leak reasons, S1 gap ②) is unverified. Revisit once the
   L2 rebuild shows what turn-level accounting we actually hold.
4. **First-class cross-vendor menu entries for Claude Code / Codex — v2.1
   candidate, spike-gated** (owner ruling 2026-07-29 02:22). v2 already *supports*
   cross-vendor supply (§4.3); what it lacks is a natural way to **add** a model.
   Mapping makes the user spend a built-in slot: to run GPT-5 in Claude Code they
   overwrite `claude-opus-4-6` with it — expressive, but it reads as a disguise, and
   it costs them a slot they may still want. The v2.1 shape:

   - **Evolve the fixed-menu rule** (locked 2026-07-22, §4.7) into **built-in core
     + explicitly user-added upstream models**. The user picks 来源 + 模型 directly
     and the entry stands on its own — no GPT model wearing a built-in Claude id.
     This reuses the OpenCode custom-model pattern rather than inventing one
     (§4.8: the form takes source + model id and previews the identifier; the entry
     is a supplement to that source's supply list) — the same interaction, extended
     to the two fixed-menu backends. UI nouns stay 来源 / 模型: **Provider stays
     banned** as a UI noun (§3), and so does any user-facing notion of a plugin.
   - **Engine translation stays invisible.** If specific conversion pairs turn out
     to need CPA plugins, we bake them into the engine build/config we ship — never
     surfaced as user configuration. The survey's standing ruling holds unless the
     spike overturns it: dynamic-library plugins are globally disabled by default
     and must not become a runtime dependency (S1 §7). If the outcome does change
     the engine build or pin, that is a `runtime-dependency.schema.json` revision —
     new pin + SHA256, mirrored assets published before the manifest moves — not a
     config tweak.
   - **The spike is the gate.** Validate CPA v7.2.95 anthropic↔openai
     (Messages ↔ Responses / Chat Completions) fidelity under real **agentic**
     workloads: tool calls, streaming, system prompts, thinking/reasoning
     parameters. Do **not** re-litigate what S1 §3 already settled — syntax
     conversion exists, is registry-driven, and covers both directions; the open
     question is precisely the one S1 flagged as unguaranteed, semantic
     equivalence. Deliverable: a capability matrix plus **go/no-go per conversion
     pair**, stating which pairs are engine-core and which are plugin-dependent.
     Extend the findings in `model-hub-engine-survey.md`; do not start a new
     document.
   - **Scope guard — `api_key` sources only.** Cross-vendor supply is for API-key /
     provider sources. **Subscriptions stay bound to their own vendor's backend** in
     both channels; the S2/ToS ruling is unchanged (`model-hub-tos-review.md`, §4.4,
     contracts README security invariant 3). A ChatGPT subscription never becomes a
     source for Claude Code, before or after this item ships.
   - Contract impact, recorded so nobody assumes v2 covers it: this needs an
     `agent-supply` revision at `contract_version: 3` (a fixed-menu backend gains
     user-added entries alongside `mappings`). v2 deliberately carries **no**
     speculative fields for it.
5. Remaining mocks (§5 pending): OpenCode drawer frame, empty state, Dark, copy
   pass; plus deleting the rejected V5A/V5B/V5C frames from design.pen once the
   owner confirms. The §10.4 item, if it clears the spike, also needs a 模型菜单
   frame showing built-in core + user-added entries for a fixed-menu backend.
6. Implementation plan & lane split for v2 (separate doc; the v1
   `model-hub-implementation.md` describes the shipped v1 and is superseded for
   anything touching ordering).
7. Naming final check in the EN locale: Hub / Direct, and now Follow / Custom for
   跟随推荐/自定义, in `en.json`.
8. Deferred capability: engine-owned OAuth import (adapter rev) — prerequisite
   for any future auth-file controlled_import; revisit only with a concrete need.

## 11. Owner acceptance checklist (~10 min)

- [ ] §0 revision note states the ruling and its no-back-compat consequence correctly.
- [ ] §2 promises 3 and 4 (per-agent order; never reorder behind the user) match intent.
- [ ] §3 vocabulary: 来源顺序 / 跟随推荐 / 自定义 / 供给状态; 优先级 banned as a global noun.
- [ ] §4.2 recommendation rule is exactly what you want implemented verbatim.
- [ ] §4.2 cooldown staying source-global rather than per-agent is right.
- [ ] §4.4 eligibility moving to the server closes the `isSourceEligible` debt.
- [ ] §4.5 three-class state taxonomy plus the two-tier notification rule,
      including `waiting` — an all-cooling agent stays in the feed and is never
      pushed, because it fixes itself.
- [ ] §4.5 turn provenance gets a real read contract, not just a promise — including
      the turn that gave up before trying anything, which the record must be able to
      hold rather than skip.
- [ ] §4.5 the two grains: agent rollup `supply_status` for the selected model,
      chain `supply_state` for any (agent, model) the user asks about.
- [ ] §4.3 candidate filtering keeps both predicates today's resolver has — the
      OpenCode provider/vendor match (so `zhipuai/x` is never served by `custom/x`)
      and skipping `error` sources, not just cooling ones.
- [ ] §4.6 chain per (agent, model) is the honesty fix you asked for, and showing
      cooling sources dimmed rather than hiding them is right.
- [ ] §5 frame contracts match the V6 mocks you reviewed (01–04, M01–M02).
- [ ] §9 non-goals: no health scoring, no session pinning, no global list — and the
      sharpened cross-vendor line draws the boundary at *who chose*, not at vendor.
- [ ] §4.3 states your 07-29 ruling correctly: cross-vendor supply (GPT in Claude
      Code, Claude in Codex) is a supported, built-in v2 capability — no plugin
      concept ever reaches the user.
- [ ] §10.4 is the right shape for making it first-class in v2.1: built-in core +
      user-added 来源/模型 entries, engine translation invisible, gated on the
      agentic-fidelity spike, and API-key sources only — subscriptions stay bound
      to their own vendor.
- [ ] §10.3 deferring fallback spend attribution to v2.1 is acceptable.
