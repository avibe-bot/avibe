# Model catalog update — November 10, 2026

## Change contract

Add the verified general-purpose Agent models `claude-fable-5-2` and
`gpt-6.1-terra`, plus Claude Code's `fable` alias. Keep existing defaults,
recommendations, user selections, source ordering and routes unchanged.

- Native Claude/Codex catalogs and offline fallback agree on the new IDs.
- Fable offers low/medium/high/xhigh/max. Terra offers
  none/low/medium/high/xhigh/max; no undocumented `ultra` wire tier.
- Native Claude's `fable` alias selects the latest *observed* family member.
  A pinned 5.2 request never falls back to 5.1. Other backends remain literal.
- Metadata and price valuation use the existing first-party models.dev path
  (or explicit operator override), never a guessed sibling price.
- Thinking remains model/source-identity bound. Adding a generation does not
  authorize replay of its signatures into another model or account.

## Official release evidence

Verified from full official documentation, not inferred from relay inventories.

| Model | Release | Public API ID / snapshot | Context / output | Standard USD per million tokens |
| --- | --- | --- | --- | --- |
| Claude Fable 5.2 | 2026-11-09 | `claude-fable-5-2`; the dateless ID is pinned, no invented dated suffix | 1,000,000 / 128,000 | input 8; output 40; cache read 0.20; write 10 (5m), 16 (1h) |
| GPT-6.1 Terra | 2026-10-26 | `gpt-6.1-terra`; no separately documented dated snapshot | 1,050,000 / 128,000; max input 922,000 | input 1; output 5; cache read 0.05; write 1.25 (30m), 2.50 (12h) |

Fable's 1M context is native, not a separate API variant. Thinking is always-on
adaptive; an omitted thinking parameter is valid. Its API default effort is
high. Manual budgets, disabled thinking, and forced `tool_choice:any/tool` are
not supported. Claude Code requires 2.1.321 or later; `fable` is a native alias
whose exact target can vary by gateway, so Avibe does not globally rewrite it
to a fixed API ID.

Terra's default effort is medium. Tool calling requires Responses, not Chat
Completions. Codex support is documented from 0.159.0. Above 272K input tokens,
Terra charges 2x input/cache and 1.5x output for the full request.

Sources:

- https://platform.claude.com/docs/en/models/fable-5-2/overview
- https://platform.claude.com/docs/en/models/fable-5-2/whats-new-fable-5-2
- https://platform.claude.com/docs/en/models/overview
- https://code.claude.com/docs/en/model-config
- https://developers.openai.com/api/docs/models/gpt-6.1-terra
- https://developers.openai.com/api/docs/changelog
- https://developers.openai.com/api/docs/models/all
- https://developers.openai.com/api/docs/pricing
- https://learn.chatgpt.com/docs/models
- https://opencode.ai/docs/models/

## Metadata and pricing

A fresh, isolated models.dev fetch on November 10 contained both exact
first-party rows. Avibe's real search/parser returned Fable's 1M/128K limits,
five efforts and Anthropic protocol, and Terra's 1.05M/128K limits, six efforts
and Responses protocol. `PriceTable` returned the exact rates above, including
Fable's 16/MTok one-hour write and Terra's 2.50/MTok 12-hour write.

A still-valid 24-hour local cache fetched before the upstream Fable row was
published can temporarily show no match. This is cache freshness, not a model
ID/filter defect. No duplicate static metadata registry, forced-refresh UX or
sibling-price alias is introduced here. Until refreshed, unknown pricing stays
unknown; the existing editor permits exact-ID metadata entry. The `[1M]` menu
label alone does not configure an Agent's context budget.

## Validation and live smoke

Normal pace, one short read-only tool task per row; no synthetic load. Every
row completed its streaming path, executed one file-read tool (a read-only
shell command for Codex), and returned the exact expected marker.

| Model | Path | Result / identity evidence |
| --- | --- | --- |
| Fable 5.2 | Claude Code 2.1.321 | PASS; 25 stream events, one Read; result reports exact `claude-fable-5-2` and 1M/128K limits |
| Fable 5.2 | Vibey → Model Hub → configured Anthropic API-key source | PASS; two exact-model Anthropic requests, one successful read, completed response |
| Terra | Codex 0.167.0 | PASS; exact `-m gpt-6.1-terra`, JSON event stream, one successful read-only command; native JSON does not echo a server model ID |
| Terra | OpenCode 1.19.5 | PASS; provider inventory offers `openai/gpt-6.1-terra`, exact selection, JSON event stream, one successful read |
| Terra | Vibey → Model Hub → configured Responses source | PASS; two exact-model Responses requests, one successful read, completed response |

The Fable Hub smoke used a temporary menu alias with verified metadata and an
explicit exact-model route through the existing source. Its menu entry/route
were removed afterward and the original menu metadata was read back unchanged.
No credentials, source inventory, defaults or service lifecycle were changed.

Focused automated coverage includes catalog order/fallback, native alias and
legacy migration resolution, exact API identity per backend, declared effort
projection, all five Fable effort payloads and omitted effort, adaptive
refused-signature tool continuation, cross-generation signature isolation, and
exact pricing versus stale metadata. Existing default/recommendation tests
remain unchanged.

## Known by design / follow-up

- Fable 5.2 binds thinking to the preceding conversation prefix for every
  account. The normal live tool loop did not reproduce a prefix-binding 400.
  Preserve exact-origin/source replay and the existing pause-prefix guard; do
  not add a speculative generic-400 demotion or compatibility exception.
  This smoke is not an arbitrary-history-editing or long-session certification.
- Native catalogs advertise choices, not account entitlement. OpenCode and
  Vibey inventories remain source/user owned; adding a built-in row does not
  silently enroll a user in a model or change a route.
- `choose-model` already uses version-free family guidance and live model
  lists. Recommendations stay unchanged.
- Image-only `gpt-image-2.5-{sunburst,flare}` and
  `gpt-image-2.6-{sunburst,flare}` are a separate image-tool update, not Agent
  menu entries. Specialized `gpt-6-cyber` / `gpt-6-rosalind` require approved
  access; pricing alone does not establish a public `gpt-6-astra-law` backend
  contract. Voice/transcription models use different endpoints. Do not invent
  unverified 6.1 Astra/Luna, minis or Codex-suffixed IDs.
- Public documentation goes into the stable docs batch, not this repository's
  PR: aligned English/Chinese notes in `agents/claude-code.mdx`,
  `agents/codex.mdx`, and `concepts/model-hub.mdx` for availability, native
  minimum versions, source-owned menus, and Fable's always-on thinking. Leave
  historical benchmark pages/charts unchanged.
