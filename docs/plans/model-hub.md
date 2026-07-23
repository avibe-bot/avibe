# Model Hub — Product Spec

Status: **signed** (2026-07-23 02:58) · amended v1.1 (2026-07-23 10:40) per spike
S2 findings — subscription supply channels & the consent-gated experimental flag
(owner decision 10:33: hybrid supply by default; hub-held Claude subscription
login offered ONLY as a consent-gated experimental feature)
Owner decisions incorporated through: 2026-07-23 10:33 (+08:00)
Design source: `../avibe-docs/design.pen`, frames `产品改造 V4 01r – V4 09` (Light)
Discussion record: Show Page of session `sesb7r2qwb4z4` (rounds 1–10, with superseded
explorations V2/V3 archived inline)

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

1. Connect a source once — every Agent can use it.
2. Subscriptions are consumed first (already paid); when quota runs out
   Avibe switches to the next source automatically and switches back on
   recovery. Work never stalls. (Mechanism, v1.1: per-turn channel dispatch —
   subscriptions burn via the CLI's sanctioned native channel; the hub
   arbitrates the api-key tier. See §4.1/§4.2.)
3. Priority order is user-owned: the list order **is** the spending order.
   Nothing is hard-coded; paid sources are always visibly marked.

Core persona: individual users who already pay for Claude Pro/Max or ChatGPT
Plus/Pro ("spend what I bought first"). Secondary: API-key-only users.
Explicit non-persona: relay-station operators ("站长") — Avibe ships no
operations console.

## 3. Vocabulary (locked; UI copy uses ONLY these nouns)

| Concept | zh | en | Notes |
| --- | --- | --- | --- |
| The settings surface | 模型 | Models | Single nav entry between 通讯平台 and 后端 |
| Where tokens come from | 来源 | Source | Two kinds only: 订阅账号 (subscription account, OAuth) and API Key (key + editable base URL) |
| Spend order | 优先级 | Priority | One global draggable list; order = spend order |
| Supply mode per backend | 中枢模式 / 直连模式 | Hub / Direct | Hub = default & recommended; Direct = legacy native-config mode, kept but not recommended |

Banned from UI copy: 网关/gateway, 路由/router, 逻辑模型, Provider(作为界面
名词), 账号池, 中转站(as a **category**; the word may appear only inside
helper copy as an example use-case for a custom base URL). "Relay station" is
NOT a source type — it is an API Key with a custom base URL (owner decision
07-23; avoids the unanswerable official/unofficial classification for
OpenAI/Anthropic-compatible vendor endpoints).

## 4. Architecture: upstream / hub / downstream

### 4.1 Upstream — Sources (global, will keep growing)

Each source carries: kind (subscription | api_key), credential, protocol
(anthropic | openai | openai_compatible …), an editable base URL (api_key
kind; prefilled for known vendors), a **model list** it can supply
(auto-discovered where possible, e.g. `/models`; manually extendable via
custom model entries), billing type (包月 | 按量 ¥), state
(active | standby | cooling-down with retry ETA), and usage (subscription
cycle % / monthly spend).

**Supply channel (amended v1.1, per S2 ToS review).** Each source has a
`supply_channel`:

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

The same model may be supplied by multiple sources; that is exactly what the
global priority list arbitrates.

### 4.2 Hub — request resolution (step 0 + three steps)

0. **Channel dispatch (amended v1.1)** — per turn, before launch: if the
   top-priority eligible source for the requesting agent is `native_cli`
   (e.g. a healthy Claude subscription for Claude Code), launch the CLI
   natively with zero injection — sanctioned form, hub untouched. If that
   source is quota-exhausted/cooling (inferred from prior native-turn errors
   plus recovery timers), launch with hub injection so steps 1–3 arbitrate
   the hub-channel tier. Recovery flips the next turn back. This is possible
   because Avibe launches backends per request; switching never happens
   mid-process. `native_cli` sources are eligible only for their sanctioned
   client (Claude sub → Claude Code); enforced in code via
   `allowed_origins`-style binding.
1. **Mapping** — requested model ID → actual model. Identity by default.
   Only fixed-menu agents (Claude Code / Codex) can override, per-agent
   (e.g. Claude Code's `claude-opus-4-6` → `glm-5.2`). Mapping is an explicit,
   deterministic user choice.
2. **Candidates** — all sources able to supply that model, ordered by the
   single global priority list (filtered per request; no per-model priority).
3. **Supply** — use candidate #1; on quota-exhausted/429, transient 5xx or
   network failure enter cooldown and take the next; switch back on recovery.
   Convert protocol when needed. Every switch is appended to the
   human-readable 最近切换 log.

Error taxonomy (no blind fallback): parameter/protocol/tool-compat errors
surface to the caller; 401 → refresh then retry; 429 / explicit quota
exhaustion / transient 5xx / network → cooldown + next candidate. Once
streaming has started, no transparent retry.

**Mapping ≠ automatic cross-vendor fallback.** The latter ("Claude quota
gone → serve GPT") stays an experimental, default-off advanced flag with
visible per-event marking, pending capability/ToS verification. Architecture
reserves `allowed_origins` to restrict which clients a subscription
credential may serve.

### 4.3 Downstream — Agents

| Agent | Menu | Notes |
| --- | --- | --- |
| Claude Code | fixed (built-in model IDs) | wants another vendor's model ⇒ per-agent mapping in its 模型菜单 |
| Codex | fixed | same |
| OpenCode + future in-house agents | open | follows upstream model lists; supports user-defined custom model entries |

### 4.4 OpenCode identifier scheme (locked 07-23)

OpenCode models are `provider/model-id`. Rules:

- The provider segment uses the **standard vendor id** (`anthropic/`,
  `openai/`, `zhipuai/`, …) — identical to native OpenCode usage. No
  `avibe-` namespace (owner: keep it simple). Unrecognizable vendors fall
  back to a single `custom/` provider.
- Hub mode merely redirects those providers' transport to the local hub in
  the generated runtime config overlay. Therefore **identifiers are stable
  across Hub/Direct switches and across source add/remove/failover** —
  never encode a concrete source into the provider segment.
- Users never hand-assemble the string. Menu checkboxes pick models; the
  custom-model form generates and previews the identifier (source + model ID
  in → `zhipuai/glm-5.2-air` out). A custom model entry is, in data terms, a
  supplement to that source's supply list.

## 5. Surfaces (design.pen V4 frames)

| # | Frame | Content contract |
| --- | --- | --- |
| 01r | 设置 · 模型 main page | One page, two bands. **来源**: global list; drag handle + priority number; per-row icon, name, mono sub (account/key id; cooldown ETA lives here), usage column (subscription progress bar / monthly ¥), fixed-width aligned chip columns 包月/按量¥ and 使用中/备用/暂不可用; supply list appears ONLY as hover tooltip on icon/title; header = one-line policy sentence + status pill; 添加来源 button in card header. **Agent**: one row per backend — name + 菜单固定/菜单开放 badge, current supply as a composite pill `model ｜ ● source` (UI, not copy; friendly names here), mode chip 中枢 Hub / 直连 Direct, action 模型菜单 / 接入中枢. Below: 最近切换 (3 rows, human phrasing, view-all) and a single 高级 row (跨厂商自动顶替 default-off · 请求日志 · 诊断). |
| 02 | 后端 · Claude Code | 供给方式 card: two radio options — 中枢模式 Hub (推荐·默认; supply managed on Models page; native config untouched; "打开模型页") and 直连模式 Direct (legacy behavior preserved verbatim; detect strip "检测到既有直连配置 → 导入中枢…"). CLI path + connectivity test cards unchanged from current page. |
| 03 | 迁移对话框 | Non-destructive: per-item checklist grouped by backend. API keys + base URLs → direct import; subscription OAuth → **keep_native** by default (v1.1: stays in the CLI's sanctioned store and becomes a native_cli source; hub-held import only via the consent-gated experimental flag); Codex `auth.json` → controlled import behind the same flag. Footer promise: originals never modified/deleted; Direct always available. Triggers: first open after upgrade, setup wizard, backend-page banner. |
| 06r/07 | 添加来源 | Dropdown menu on the button (no type-chooser dialog): 连接 Claude 订阅 / 连接 ChatGPT 订阅 / 添加 API Key…. The API Key form is the only form dialog: vendor select (official vendors prefill base URL; 自定义 for compatible endpoints), key, base URL, test-and-add with model-discovery feedback ("发现 23 个可用模型"). |
| 09 | 连接订阅 | Reuses the existing `BackendOAuthPanel` / `AgentAuthService` state machine (start → 2s-poll → awaiting → verifying → success/failed/cancelled; 15-min timeout; cancel). Three declared flow forms, one shell: **A** paste auth code (Claude), **B** device code, self-completing (ChatGPT/Copilot), **C** paste redirected callback URL for replay (loopback unreachable from remote browsers). URL and device code always carry copy buttons (remote/phone operation). New OAuth subscriptions = declare a form, inherit the shell. |
| 04 | 模型菜单 · Claude Code | Mapping table: left = built-in real model IDs (mono, not editable), right = supply (default 跟随原生 · 按优先级供给; override e.g. → `glm-5.2` with per-row reset and a persistent capability warning: thinking/cache/tool semantics not fully equivalent). Per-agent scope. 恢复全部默认 in footer. |
| 05r | 模型菜单 · OpenCode | Grouped BY provider prefix (`anthropic/` · `openai/` · `zhipuai/` + plain-language annotation) — one taxonomy, full identifier visible by construction (group prefix + mono row ID); friendly name is secondary text. 精选/全量 toggle (default featured); checkbox = appears in OpenCode's model picker; colored dots = supplying sources; custom rows carry 自定义 badge + edit. |
| 08 | 添加自定义模型 | Fields: source, model ID, optional display name → live identifier preview with the vendor-id rule above. |

Pending mocks (not blocking sign-off): first-run empty state, Dark variants,
mobile, plus a full copy pass under the rule **"if UI style can express it,
don't write copy"** (established 07-22 and applied to 01r).

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
- Migration is copy-only and reversible; see frame 03 contract above.

## 7. Security boundaries (amended v1.1)

- Three credential rings, never mixed: management key (Avibe→engine admin
  API), local gateway token (the only thing backends receive), upstream
  credentials (API keys and — only under the consent-gated experimental
  flag — subscription OAuth tokens; engine-held, local runtime dir with
  restricted permissions, not `~/.cli-proxy-api`). By default the engine
  never holds subscription OAuth tokens: `native_cli` subscriptions keep
  their credential in the CLI's own sanctioned store (spec §4.1).
- Credentials never enter Avibe Cloud, IM messages or logs. Static keys may
  integrate with Avibe Vault; no duplicate key entry across surfaces.
- Gateway failure is fail-closed; Direct mode is the explicit escape hatch.

## 8. Data plane

The hub's data plane is a **replaceable, Avibe-managed, versioned runtime
dependency** (current candidate: CLIProxyAPI ~14 MiB download / ~41 MiB
binary): pinned version + SHA256, 127.0.0.1-only listener, random management
key and gateway token, lifecycle owned by Avibe. Its YAML/auth files/manage
UI are **not** product surface. The capability matrix (supported OAuth
vendors, protocol conversions, model-fallback plugin maturity) was a
point-in-time survey and MUST be re-verified from source/docs at
implementation time; subscription reuse across clients/vendors additionally
requires billing/quota/ban-risk and ToS review before any default-on
behavior.

## 9. Explicit non-goals (v1)

- No per-model priority ordering (global list only, filtered per request).
- No automatic cross-vendor substitution by default (advanced flag,
  experimental, visible marking).
- No billing-grade accounting, multi-tenant pools, or operator consoles.
- No third source category ("relay" merged into API Key).

## 10. Open items

1. Remaining mocks: empty state / Dark / mobile / copy pass (§5 pending);
   plus V4-01r/06r/09 touch-ups for supply channels: source-row 原生供给
   note, experimental marking, and the consent dialog for
   `subscription_hub_experimental` (copy from S2 §9).
2. ~~ToS + billing verification~~ **Done (spike S2, 2026-07-23)** —
   `model-hub-tos-review.md`; verdicts folded into §4.1/§4.2/§7. Residual:
   keep the consent copy in sync with vendor terms at release time.
3. ~~Data-plane capability re-verification~~ **Done (spike S1, 2026-07-23)**
   — `model-hub-engine-survey.md`; engine pinned v7.2.95, OAuth is
   runtime-declared (contracts), adapter owns error classification & events.
4. Naming final check in EN locale: Hub / Direct wording in en.json
   (中文已定: 中枢/直连; alternates 托管/自管、统一/独立 were considered and
   dropped).
5. Implementation plan & lane split (separate doc once this spec is signed).

## 11. Owner acceptance checklist (~10 min)

- [ ] §2 promises and §3 vocabulary match intent; nothing else leaks into UI copy.
- [ ] §4.2 resolution rules: mapping/priority split, error taxonomy, no-retry-after-stream.
- [ ] §4.4 identifier rules (vendor ids, `custom/` fallback, stability guarantee).
- [ ] §5 frame contracts match the V4 mocks you reviewed (01r/02/03/04/05r/06r/07/08/09).
- [ ] §6 Direct mode preservation + migration triggers acceptable.
- [ ] §8 data-plane posture (managed dependency, re-verify caveat) acceptable.
