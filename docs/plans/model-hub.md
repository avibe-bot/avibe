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
- **Rule 3 is not decoration.** Two sources imported in one migration batch can
  legitimately share a timestamp, and records predating `created_at` have none at
  all; the id tie-break makes the order total in every case, so 跟随推荐 can never
  be ambiguous.

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
     model id; b. the source is eligible for this backend and channel (§4.4).
     What survives is the *capability chain* for this (agent, model) pair — what
     §4.6 defines and what the UI displays, cooling members included.
   - **runnability** (momentary, per turn): c. the source is retry-ready (not
     cooling, not `needs_action`). What survives is the *runnable candidate list*
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

### 4.5 State taxonomy — classified by "does it heal itself"

Three classes, because the action owed by the user differs in each.

**Source-level `state.status`:**

| Status | zh (UI) | Heals itself | Meaning |
| --- | --- | --- | --- |
| `active` | 使用中 | — | currently serving |
| `standby` | 备用 | — | healthy, not at the head of some order |
| `cooldown` | 暂不可用 (gold) | **yes** | quota/rate/network; `retry_at` known; recovers unattended |
| `needs_action` | 需处理 (rose) | **no** | OAuth expired, balance exhausted, key revoked/banned — dead until the user acts |
| `error` | 异常 | unknown | unclassified failure |

`needs_action` is new in v2 and carries a `detail_key` naming the cause, so the
row can offer **one tap to fix it** (re-auth, top up, replace key) instead of a
dead-end error string.

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

**Notification tiers** (the colleague test: interrupt only when action is owed):

| Class | Surface |
| --- | --- |
| self-healing (`cooldown`, `waiting`, recovery, in-turn switch) | 最近切换 feed only — never an IM push |
| `needs_action`, `interrupted` | proactive IM push, colleague voice, e.g. 「relay 余额不足，已切备用；点此处理」 |

Resolution events therefore carry `severity: info | action_required`, and the push
layer keys off that field rather than re-deriving urgency from `kind`. The two
tiers are cause-based, never count-based: "zero runnable candidates" is not by
itself a reason to interrupt anyone.

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
  「经映射」.
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
| Claude Code | fixed (built-in model IDs) | wants another vendor's model ⇒ per-agent mapping in its 模型菜单 |
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
- No automatic cross-vendor substitution by default (advanced flag,
  experimental, visible marking).
- No billing-grade accounting, multi-tenant pools, or operator consoles.
- No third source category ("relay" merged into API Key).

## 10. Open items

1. **Dry-run probe** (`POST /api/models/agents/<backend>/probe`): one minimal
   request through the agent's current chain → `{probe: {source_id, model_id,
   latency_ms, reachable, error, via_mapping}}`. The outcome field is `reachable`
   and the object is nested, so it never collides with the response envelope's
   `ok` — "the call worked" and "the upstream answered" are different questions.
   UI: 「试跑一次」 in the agent drawer footer. Contract frozen in v2; implementation
   lands with the L2 rebuild.
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
4. Remaining mocks (§5 pending): OpenCode drawer frame, empty state, Dark, copy
   pass; plus deleting the rejected V5A/V5B/V5C frames from design.pen once the
   owner confirms.
5. Implementation plan & lane split for v2 (separate doc; the v1
   `model-hub-implementation.md` describes the shipped v1 and is superseded for
   anything touching ordering).
6. Naming final check in the EN locale: Hub / Direct, and now Follow / Custom for
   跟随推荐/自定义, in `en.json`.
7. Deferred capability: engine-owned OAuth import (adapter rev) — prerequisite
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
- [ ] §4.5 turn provenance gets a real read contract, not just a promise.
- [ ] §4.6 chain per (agent, model) is the honesty fix you asked for, and showing
      cooling sources dimmed rather than hiding them is right.
- [ ] §5 frame contracts match the V6 mocks you reviewed (01–04, M01–M02).
- [ ] §9 non-goals: no health scoring, no session pinning, no global list.
- [ ] §10.3 deferring fallback spend attribution to v2.1 is acceptable.
