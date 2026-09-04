# Model Hub — Add-API-key: detect-then-confirm flow

Status: owner-approved 2026-09-03 (this conversation). This file is the design
authority for the change; `docs/plans/model-hub-ui-spec.md` §1.5 is the surface
spec and receives a dated amendment from this plan (see "Spec amendment").

## Background

The Add-API-key dialog currently shows 接口类型 (interface type) as the second
form field, above Base URL and API key — implying the user must know the
interface before connecting. The owner ruled:

1. Move the interface-type control **below the API key field**.
2. It is not an input the user owes the product; it is a **detection result the
   product owes the user**, confirmed before saving: fill Name / Base URL /
   API key → automatic detection → user confirms.
3. The fetched model list is **not displayed** in the dialog (count only,
   exactly as today). Full list maintenance stays on Source details.

## Approved interaction

### Field order

名称(可选) → Base URL → API Key → 接口类型 (result area, last).

### Interface-type area states

| State | Presentation |
| --- | --- |
| Idle (Base URL or key empty) | Dimmed row: value 自动探测, hint "填好 Base URL 和 API Key 后自动识别". Manual-override disclosure available. |
| Ready (inputs filled, no result yet) | Same row, undimmed. Manual-override disclosure available. |
| Detecting | Spinner row "识别接口中…" |
| Identified | Mint strip: `✓ <Protocol> · 拉到 N 个模型` (or `<Protocol> · 已经连接，但这个供应商没有可用模型` for zero). Manual-override disclosure below. |
| Undetermined (state ④) | Existing gold strip + the four-segment selector **expanded in place** (retry stays disabled while Auto remains selected — existing rule). |
| Inventory (state ⑤) | Existing behavior unchanged (advisory strip, 仍要添加 path). |

Manual override is a collapsed disclosure "手动指定接口类型" holding the existing
four-segment selector (Auto + Anthropic Messages / OpenAI Responses / OpenAI
Chat Completions). Selecting a concrete protocol is a **declaration**
(2026-09-04): persistence requires authentication on that path, not a
protocol-shaped response. Auto detect still requires matching response proof.
Changing the selection invalidates any existing result (existing `editProtocol`
behavior); no auto re-probe. See `docs/plans/model-hub-vendor-preset-protocol.md`.

### Two-step primary

- Primary reads **检测 / Detect** while there is no fresh result. Disabled
  unless Base URL + API key are non-empty and the name is valid (same validity
  as today's 添加).
- Detect runs the existing observation probe (`observeApiKeySource`). It is
  non-persisting; cancel while detecting aborts and returns to the form with
  values intact.
- On an identified result the primary becomes **确认添加 / Confirm & add**:
  persists directly with the proved protocol (`persist(seq,
  report.protocol ?? undefined)`). The server repeats its own observation
  inside the create attempt, so no client-side re-probe is needed.
- Editing Base URL, API key, or the protocol selection after a result
  invalidates it (existing edit handlers) and returns the primary to Detect.

### The origin axis is retired

The pull/add origin distinction existed to protect one promise: 拉取型号 was
可选, so "nothing you do here commits anything" had to hold for the whole
pull branch (spec §1.5 "Origin is an axis, not a state"). The optional test
button is removed by this change — Detect is a mandatory step of the single
add flow — so the axis has no remaining work:

- 拉取模型 / Fetch models button and its hint are deleted; Detect absorbs
  their role (and finally surfaces the detected protocol, which today's
  result strip omits).
- Every outcome state may offer its persisting exit under the existing
  protocol-proof equality: ③/④ still refuse to save; ⑤ still offers
  仍要添加 (its gate becomes "inventory outcome with a proved protocol", no
  longer origin-scoped).
- Cancel semantics: while detecting → abort to form; in an outcome state or
  during persist → existing behavior (dismiss; persist-stage cancel stays
  blocked).

The invariant is: **every persisting exit requires a protocol established on
the 2026-09-04 ladder** (catalog pin, user declaration, or matching response
proof; spec §1.5, E-3/E-5, `docs/plans/model-hub-vendor-preset-protocol.md`).

### Protocol glyphs (added by owner ruling, same conversation)

Every surface in this dialog that names a concrete interface type carries that
protocol family's brand glyph, at a glance (一眼识别):

- **Rule**: the glyph marks the **protocol family** — Anthropic Messages gets
  the Anthropic mark, OpenAI Responses and OpenAI Chat Completions get the
  OpenAI mark. It says "this endpoint speaks the Anthropic/OpenAI interface",
  not "this endpoint is operated by that company"; a relay speaking
  `openai_chat` shows the OpenAI glyph because that is the interface it was
  proved to speak. Auto detect (自动探测) carries no glyph.
- **Surfaces**: the manual-override segment options, the identified result
  strip (glyph before the protocol label), and ④'s expanded selector options.
- **Rendering**: inline-SVG React components in one new module
  (`protocolGlyph.tsx`), monochrome `currentColor`, ~14px, sized with the
  label's ink tokens so Light/Dark both work. No image assets, no new
  dependency; path data from the published simple-icons marks.
- This does not touch `vendorMeta.ts`'s stance that source-row identity is
  semantic, not vendor branding: the glyph belongs to the interface-type
  surface only.

### Unchanged

- Replace-key mode (whole lower branch of the dialog) — untouched.
- Backend contracts: `observe` / `create` routes, `SourceCreate` schema,
  `client_nonce`, `accept_unavailable_inventory` producer (⑤'s 仍要添加
  remains its only producer).
- Failure copy and strips for ③ auth/network/unclassified/engineDown,
  persist_failure, save_unconfirmed reconciliation.
- No model list rendering in the dialog — count only.
- Name-default fill behavior stays exactly as today (out of scope).

## Copy changes (en + zh, same change)

| Key | 中文 | English | Note |
| --- | --- | --- | --- |
| `addKey.subtitle` (new text) | 先检测连接与接口，确认后添加 | Detect the connection and interface first, then confirm to add | replaces old subtitle |
| `addKey.detect` (new) | 检测 | Detect | primary, step 1 |
| `addKey.confirm` (new) | 确认添加 | Confirm & add | primary, step 2 |
| `addKey.protocol.idleHint` (new) | 填好 Base URL 和 API Key 后自动识别 | Identified automatically once Base URL and API key are filled | idle/ready row hint |
| `addKey.protocol.detecting` (new) | 识别接口中… | Identifying the interface… | detecting row |
| `addKey.protocol.manual` (new) | 手动指定接口类型 | Manually specify interface type | disclosure label |
| `addKey.saving` (new) | 保存中… | Saving… | persist stage title |
| `addKey.saving.detail` (new) | 正在保存这个来源 | Saving this source | persist stage detail |
| `addKey.field.protocol.hint` | 不确定时用自动探测；已知类型时只验证所选接口 | unchanged | moves to the disclosure area |
| `addKey.pull.result*` / `pull.empty` | unchanged | unchanged | composed after the protocol label |
| removed | `addKey.test`, `addKey.test.hint`, `addKey.submit` | | delete from both locales |

Identified strip composes in JSX: `{protocolLabel} · {t(pull.result, {count})}`
(or `pull.empty`), so no new pluralization keys are needed.

## Spec amendment

`docs/plans/model-hub-ui-spec.md` §1.5 gets a dated owner-ruling block
(2026-09-03) recording: selector relocation below API key as a result area with
manual disclosure; two-step primary; removal of 拉取型号 and retirement of the
origin axis (with the rationale above); protocol-family glyphs on every
interface-type surface; copy-table rows updated accordingly.
Amend surgically (ruling block + copy table + the superseded element-inventory
rows); do not rewrite the section.

## Test expectations

- `AddApiKeyDialog.test.tsx`: update to the two-step flow; keep/extend contract
  coverage — protocol constraint forwarded to observe; persist payload shape
  unchanged (protocol from the proved report; `accept_unavailable_inventory`
  only from ⑤); result invalidation on every edit; ④ retry gated on concrete
  selection; ⑤ add-anyway reachable from a detect-origin inventory outcome.
- Origin-twin tests that exercised the removed optional button are deleted with
  their button (the axis they protected is gone).
- A small `protocolGlyph` test (render marks, assert currentColor sizing
  classes) if a sibling pattern exists; otherwise vitest render smoke is
  optional — do not invent a new test pattern.
- Focused run: `cd ui && npx vitest run src/components/settings/models/` for
  touched suites; `npm run build` before push; i18n keys present in both
  locales in the same change.

## Out of scope

Backend, `modelsApi.ts`, `types.ts` protocol enum, replace mode, frame-06+
surfaces, any design.pen edit.

## Ratified scope extensions (orchestrator, 2026-09-03)

The lane's scope-gap report surfaced that the change breaks callers outside
the original file list. Extensions granted in the same lane/PR:

- `ui/e2e/b-add-api-key.spec.ts`, `ui/e2e/g-guards-copy.spec.ts`,
  `ui/e2e/support/hub.ts` — adapt to the two-step flow (Detect is the
  non-persisting observe; the disclosure must be opened before a manual
  protocol segment is clickable). Preserve scenario intent, don't delete.
- `model-hub-ui-spec.md` amendment widened to include the §0.8 register rows
  this dialog owns: retire origin-twin / 拉取型号 rows, add idle / detecting /
  identified states, keep §0.10's completeness-gate mechanics intact.

Untracked-caller lesson for future specs: deleting a locale key (`addKey.test`,
`addKey.submit`) is an API change to every e2e helper and spec that clicks by
label — sweep `ui/e2e/` before the file list is frozen, not after the lane
reports the break.
