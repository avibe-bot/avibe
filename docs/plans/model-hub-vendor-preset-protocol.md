# Model Hub — Add API key: vendor presets and declared protocol

Status: owner-directed 2026-09-04 (this conversation). This file is the
design authority for the change. `docs/plans/model-hub.md` (protocol
observation ruling), `docs/plans/model-hub-ui-spec.md` §1.5, and
`docs/plans/model-hub-contracts/` receive dated amendments from this plan.

Surface: V4 06r already drew the vendor dropdown; 模型网关 05 is the
current-implementation frame and now carries that field. Do not invent a
new dialog. Detect-then-confirm (#1831) stays. Type-scale fix from closed
#1842 (`f38ee133` on `fix/model-hub-idle-row-type-scale`) is folded in.

## Why this exists

DeepSeek (and Qwen, Kimi, and any gateway that answers `/v1/chat/completions`,
`/v1/responses`, and `/v1/messages` with the same body) cannot be added
today. Observation requires a **protocol-shaped** upstream response
(AC-27, 2026-08-26). Identical answers on all three paths never prove a
protocol. Manual selection is a probe constraint, never proof, so retry
is the same dead end. Live case: `https://api.deepseek.com` + a valid
key → ④ 「认不出它说哪种接口」.

#1731 closed a *shape-table* gap (`param: null`, 400 model-not-found).
It explicitly left vendor presets out of scope and kept AC-27. That
repair cannot save an endpoint whose three paths are indistinguishable.

V4 06r already specified the product intent: a **服务商** dropdown that
prefills the official URL. 05 never wired it; `AddApiKeyDialog` hard-codes
`vendor: 'custom'`.

## Protocol proof ladder (replaces the single AC-27 sentence)

Every stored `protocol` still has a named owner. Inference from a typed
URL string remains forbidden. The owner is one of:

| Rung | When | What observation must still prove | Where `protocol` comes from |
| --- | --- | --- | --- |
| 1. Catalog pin | User picked a first-wave vendor, not 自定义 | Reachable + authenticated | Catalog row's `protocol`. Shape proof is **not** required. |
| 2. Response proof | 自定义 + 自动探测 | Reachable + authenticated + protocol-shaped response (today's AC-27) | The observation evidence table |
| 3. User declaration | 自定义 + a concrete interface selected | Reachable + authenticated **on the constrained protocol's path** | The user-selected `protocol`. Shape proof is **not** required. A wrong declaration fails at runtime on a real call, same philosophy as reasoning-tier v2 evidence isolation. |

「仍要添加」 stays exactly where it is: protocol known, inventory missing.
③ (unreachable / rejected / timeout / adapter_error) and a still-ambiguous
auto-detect ④ still cannot save.

Once saved, `protocol` remains immutable on that Source. Changing it still
means a new Source. No `protocol_source` field in this change (avoids a
contract_version bump). Dialog badges are local form state. After save,
`Source.vendor` is the catalog id (`deepseek`, not `custom`), which is
enough for source-detail identity.

## First-wave catalog

Authoritative table, shipped as `vibe/data/api_key_vendors.json`. Vendor
ids reuse the `Source.vendor` pattern already named in
`source.schema.json` (`anthropic|openai|zhipuai|kimi|xai|…`). Model-id
prefix map in `vibe/data/model_vendors.json` is a different document
(family → vendor for catalog backfill) and is not this picker.

| id | Label | Official Base URL | Pinned protocol |
| --- | --- | --- | --- |
| `deepseek` | DeepSeek | `https://api.deepseek.com` | `openai_chat` |
| `qwen` | Qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `openai_chat` |
| `kimi` | Kimi | `https://api.moonshot.cn/v1` | `openai_chat` |
| `zhipuai` | 智谱 | `https://open.bigmodel.cn/api/paas/v4` | `openai_chat` |
| `openai` | OpenAI 官方 | `https://api.openai.com/v1` | `openai_responses` |
| `anthropic` | Anthropic 官方 | `https://api.anthropic.com` | `anthropic` |
| `openrouter` | OpenRouter | `https://openrouter.ai/api/v1` | `openai_chat` |
| `groq` | Groq | `https://api.groq.com/openai/v1` | `openai_chat` |
| `mistral` | Mistral | `https://api.mistral.ai/v1` | `openai_chat` |
| `xai` | xAI | `https://api.x.ai/v1` | `openai_chat` |
| `together` | Together | `https://api.together.xyz/v1` | `openai_chat` |
| `fireworks` | Fireworks | `https://api.fireworks.ai/inference/v1` | `openai_chat` |

`custom` is the dropdown default, not a catalog row. Gemini native is
deferred (not in the three-protocol vocabulary).

Engine `_OFFICIAL_BASE_URLS` today only lists anthropic/openai/codex.
This table is the replacement for api-key observation: look up by
`vendor`, then `base_url` or the official default.

## Dialog (05, aligned with V4 06r)

Field order: **服务商** → 名称(可选) → Base URL → API Key → 接口类型.

服务商 is a select, not a tile grid (06r already drew this; 05b was
deleted as a duplicate). Options: 自定义 · 兼容端点, then the first-wave
rows in the table order above.

### Preset selected (rung 1)

- `vendor` is the catalog id.
- Base URL prefills the official value, remains editable.
- Interface type is a **locked result row**: protocol-family glyph +
  catalog protocol label + badge 「内置目录」 / “Built-in catalog”. The
  manual disclosure is hidden. Hint: detection authenticates and fetches
  models; it does not have to prove the interface by shape.
- 检测 → observe with `vendor` + `protocol` = catalog pin. Success
  (authenticated) → ①″ mint strip with count + the same badge → 确认添加.
- Changing 服务商 resets URL, protocol lock, and any observation.
- Editing Base URL on a preset does **not** drop the pin (vendor stays
  `deepseek`). A relay that is not that vendor is 自定义.

### 自定义 (rung 2 / 3)

- `vendor` is `custom`. Base URL empty. Interface type stays the
  detect-then-confirm result area.
- Auto detect is rung 2 (unchanged evidence table).
- A concrete disclosure choice is now a **declaration** (rung 3), not a
  probe constraint. ④ copy must say so: 鉴权成功即可添加; a wrong
  declaration fails on a later real call. Retry with Auto still selected
  stays disabled.
- ①″ / ④ mint or gold strip: badge 「手动指定」 / “Manually specified”
  when a concrete protocol is selected; no badge on Auto.

### Unchanged

- Two-step primary (检测 → 确认添加).
- ⑤ 仍要添加.
- Replace-key mode.
- No model names in the dialog (count only).
- Glyphs stay on protocol family, in `protocolGlyph.tsx`.
- Type scale: every text node in this dialog declares its own size
  (pending cherry-pick of `f38ee133`).

## Contract amendments (shape unchanged, semantics change)

`contract_version` stays **7**. No new fields. Descriptions and
invariants change.

- `source-create.schema.json` `protocol`: a supplied value is persisted
  when observation is authenticated and either (a) `vendor` has a catalog
  pin for that protocol, or (b) the client declared that protocol on
  `custom`, or (c) a matching protocol-shaped response proves it.
  Omission still auto-detects and still requires shape proof.
- `POST /api/models/sources/observe` in `api.md`: same three-way rule.
  Catalog pin and declaration still require reachability and
  authentication; they never bypass ③.
- Contracts README invariant 2: replace “Vendor names, Base URLs, and
  manual hints may order probes but cannot create a saved protocol
  value” with the ladder above.
- `model-hub.md` protocol-observation ruling: dated 2026-09-04
  supersession of the 2026-08-26 “never infers from vendor name”
  sentence. Catalog pin is not inference from a typed URL; it is an
  explicit vendor choice against a shipped table.
- G-18 / G-27 rows: probe constraint language is now only the Auto
  branch on `custom`.

## Acceptance

- DeepSeek official URL + valid key, vendor `deepseek`, adds as
  `openai_chat` without a shaped proof. Same for a recorded DeepSeek
  `param: null` / identical-three-path fixture.
- 自定义 + declared `openai_chat` + authenticated DeepSeek-shaped
  responses adds. Auto detect on the same fixture still lands ④.
- Official Anthropic / OpenAI presets still add (rung 1 pin; existing
  shape tests stay green).
- ③ still refuses on 401 / unreachable.
- ⑤ still the only 仍要添加.
- Replace-key byte-equivalent.
- `check_model_hub_authorities.py` clean (version stays 7).

## Out of scope

- `protocol_source` on `Source` (follow-up if source detail needs a
  provenance badge).
- Gemini native / a fourth protocol value.
- Expanding #1731's evidence table further (rung 2 stays as shipped).
- OpenCode provider cards (different “服务商” surface).
- Agent “Add models” picker (#1839) — consumes Sources after they exist.

## Lane split

Contracts freeze here. Implementation is two PRs, backend first:

1. **Backend** — `api_key_vendors.json`, official URL lookup, observe and
   create proof ladder, DeepSeek-shaped fixtures, schema/api.md/README
   description edits, `model-hub.md` ruling. No `ui/**` except the
   contract-version literal mirrors if a bump becomes necessary (it
   should not).
2. **UI** — vendor select on `AddApiKeyDialog`, i18n, ④ copy, locked
   protocol row, badges, e2e B1–B4/B11, cherry-pick `f38ee133`, §1.5
   dated amendment. Rebase after backend merges.

Neither lane merges itself.
