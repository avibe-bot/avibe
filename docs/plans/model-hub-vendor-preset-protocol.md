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

**The ladder is the only owner (amended 2026-09-08).** A rung's verdict is
reached once, when the Source is saved, and nothing re-derives it afterwards.
The engine projection used to re-check a stored Source's `protocol` against its
vendor's current catalog pin, which contradicted the ladder twice: rung 2 can
legitimately prove a protocol the pin does not name, and a pin is mutable
product data, so repinning a vendor between releases retroactively invalidated
the Sources that vendor's own earlier pin had admitted. `_validate_source_target`
now asks only what the renderer downstream of it asks — can this Source's
upstream be resolved — so a pin is read where a protocol is *decided* and never
where one is replayed.

## First-wave catalog

Mirror of `vibe/data/api_key_vendors.json`, which is the shipped artifact
and the authority: on any drift — a cell, a missing row, or the row order —
the JSON wins and this table is what gets corrected (last synced 2026-09-08,
#1938). A data change belongs in the same PR as its row here. Vendor ids
reuse the `Source.vendor` pattern already named in `source.schema.json`
(`anthropic|openai|zhipuai|kimi|xai|…`). Model-id prefix map in
`vibe/data/model_vendors.json` is a different document (family → vendor for
catalog backfill) and is not this picker.

| id | Label | Official Base URL | Pinned protocol |
| --- | --- | --- | --- |
| `openai` | OpenAI | `https://api.openai.com/v1` | `openai_responses` |
| `anthropic` | Anthropic | `https://api.anthropic.com` | `anthropic` |
| `xai` | xAI | `https://api.x.ai/v1` | `openai_responses` |
| `gemini` | Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | `openai_chat` |
| `deepseek` | DeepSeek | `https://api.deepseek.com` | `openai_chat` |
| `qwen` | Qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `openai_chat` |
| `kimi` | Kimi | `https://api.moonshot.cn/v1` | `openai_chat` |
| `openrouter` | OpenRouter | `https://openrouter.ai/api/v1` | `openai_chat` |
| `zhipuai` | Zhipu AI | `https://open.bigmodel.cn/api/paas/v4` | `openai_chat` |
| `mistral` | Mistral | `https://api.mistral.ai/v1` | `openai_chat` |
| `groq` | Groq | `https://api.groq.com/openai/v1` | `openai_chat` |
| `together` | Together | `https://api.together.xyz/v1` | `openai_chat` |
| `fireworks` | Fireworks | `https://api.fireworks.ai/inference/v1` | `openai_chat` |

`custom` is the dropdown default, not a catalog row. The `gemini` row is
Gemini's OpenAI-compatible surface, reached on that vendor's own
`/v1beta/openai` base URL; Gemini **native** stays deferred (not in the
three-protocol vocabulary, and still Out of scope below). The row and the
deferral are about different wire formats, not a contradiction.

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
  Omission on `custom` still auto-detects and still requires shape proof;
  shipped catalog vendor omission selects the pin.
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

## Model-independent observation (2026-09-07)

Protocol observation omits `model` for every interface. A fabricated model can
enter a relay's scheduler and return capacity or upstream errors before request
validation, making valid credentials impossible to add. A model-free request
keeps observation independent of model availability and generation. The shared
request taxonomy applies to API-key and OAuth observation alike.

The owner's 2026-09-07 ruling separates configuration from verification. The
protocol owners remain catalog pins, custom declarations and response evidence;
explicit `save_unverified: true` may save either of the first two without an
upstream request as its gate; the bounded inventory discovery it then attempts
fills the Source and answers no credential question. Custom Auto cannot invent
an owner. Canonical input validation,
credential custody, nonce reconciliation and rollback remain unchanged.

Schema errors never authenticate: they can precede key lookup. Synthetic controls
were removed because unknown token grammar/checksums and collisions with another
valid key make their results inconclusive. A public model list cannot repair
that proof, and the inventory waiver does not waive authentication. Bare 401/403
responses remain unknown for every owner; only shaped authentication evidence
rejects a candidate. An unknown Auto sibling is not eliminated by another
candidate's rejection. API-key observation remains model-free, non-redirecting
and bounded by its deadline.

Every newly stored Hub credential carries an opaque `verification_pending` marker
independently of routing health, including observed creates and native-config imports.
Inventory refresh never clears it. Any actual successful invocation whose captured
marker and credential still match the fresh Source clears it inside the shared config
transaction. Later same-credential attempts do not negate proof. Credential/endpoint
replacement generates a new marker, including same-handle OAuth reauthentication.
The Source remains configurable and invokable, but list/detail surfaces do not
claim healthy/in-use status while verification is pending. The normal verified
save path still requires response-backed authentication and protocol ownership.

Completed Hub OAuth consent may retain its engine-bound credential under the fixed
vendor protocol with verification pending. Explicit upstream authentication
rejection keeps the existing needs-action state. The existing allowlisted
auth-index transport substitutes the engine-held token; no private-token reader
or fabricated OAuth control is needed. Native CLI OAuth is unchanged.

`AUTH-SETUP-112` covers every catalog pin and concrete custom protocol against
isolated HTTP middleware with both authentication/schema orders, public/protected
inventory and alphanumeric/punctuation-only credentials. Unknown validation cannot
authenticate, while explicit saving makes no request that could admit a Source.
`AUTH-SETUP-113` covers completed Hub OAuth admission, fixed protocol ownership,
pending verification and idempotent polling across credential shapes and upstream
outcomes. `AUTH-SETUP-114` covers Auto policy responses that must remain unknown
and cannot permit unverified saving without a concrete protocol declaration.

## Authentication witness on an owned interface (2026-09-07, amends the section above)

The model-independent ruling above left catalog pins and concrete `custom`
declarations with no way to verify anything at add time. Its probes deliberately
carry no `model`, so a strict interface answers them with a request-level schema
error, which that ruling classifies as authentication-unknown — correctly, since a
schema error can precede key lookup. Because the probe can therefore never succeed,
every API-key add funnelled into the explicit unverified save, valid credential and
healthy relay included.

The owner's ruling closes that gap without restoring status-based acceptance: where
the interface already has an owner, authentication is read from that owner's model
listing.

- The model-less probe still runs first and still owns reachability, protocol shape
  and immediate rejection. Its evidence table is unchanged.
- Only a probe that left authentication unknown asks further, and only for rung 1
  and rung 3 with an API-key credential. Custom Auto never asks: a listing names no
  protocol, so it cannot supply the owner Auto is missing.
- A listing accepts the credential only once the identical request carrying no
  credential is refused. An absent credential is not an altered one — no grammar to
  guess wrong, no other valid key to collide with — so the synthetic-control
  objection does not reach it, and what it answers is unambiguous: a listing that
  serves an uncredentialed request belongs to anyone who asks and attests to no
  credential. A public model list still repairs no proof.
- What that establishes is bounded, and no further evidence lifts the bound: the
  interface admits this credential and refuses admission without one. It is not
  proof that the interface read the value. Observation may make two requests —
  one carrying the credential and one carrying none — and a gate on the value
  and a gate on presence alone answer that pair identically, the second
  admitting a key it never validated because its probe answers out of a schema
  check that precedes the lookup it never performs. Separating them requires a
  third request carrying a different value, and an altered credential attests to
  nothing in either direction, so this ladder declines it. A credential an
  interface never validated therefore adds as verified and is caught by the
  first real call through the existing needs-action path, where a credential
  revoked after its add is already caught. Withholding verification for that
  case withholds it for every case, which is the state this ruling ends.
- `401`/`403` on that listing rejects the candidate. The shaped-evidence requirement
  exists so that a status cannot establish a *protocol*; this rung holds its protocol
  from its owner already, so the listing's refusal speaks about the credential alone.
- Every other answer — no listing, a non-JSON body, a timeout, a listing open to
  anyone — leaves the observation exactly where the model-independent ruling put it,
  explicit unverified-save exit included. The witness can only add verified adds; it
  can never remove one.
- An accepting listing is also the inventory this rung would fetch next, so the one
  request both verifies the Source and fills it.

`verification_pending` marks a Source that nothing upstream has accepted yet. An
add-time observation that authenticated the credential is that acceptance, so an
API-key create through the observed path stores no marker; for that path this
supersedes the "including observed creates" rule above. Explicit unverified saves,
Hub OAuth admission, native-config imports and every credential or endpoint
replacement still mark, and the first-successful-call clearing, its identity matching
and its shared transaction are unchanged. Inventory still never clears it, and
discovery on a replacement path is still not a witness: it asks no uncredentialed
control and answers no ownership question.

`AUTH-SETUP-112` covers both branches for every shipped catalog vendor and every
concrete custom protocol: a credential-gated listing verifies, populates and leaves
no marker, while a public listing keeps the credential unproven and leaves the
explicit unverified save as the only exit.

## Acceptance

- A relay that answers the model-less probe with a canonical `400` schema error and
  gates `GET /v1/models` on the key adds as verified — the owner's reported case.
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
