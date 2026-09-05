# OpenCode overlay contract (v4, 2026-09-04)

<!-- authority-consumer: protocol anthropic openai_responses openai_chat -->

How Avibe generates the OpenCode runtime config overlay in Gateway mode. Owner-locked
identifier rules (spec §4.8 v4) restated as testable requirements. Supersedes the 07-23
contract (one OpenAI-compatible provider, `vendor/model` ids); nothing of that shape survives.

## Delivery

- The overlay is injected at `opencode serve` launch as `OPENCODE_CONFIG` (the overlay file)
  and `OPENCODE_CONFIG_CONTENT` (OpenCode's runtime-override tier: the overlay composed with
  Avibe's managed runtime policy, today the native-Skill restrictions). The recorded content
  hash covers that composed inline content, so a change in either input restarts the server.
  OpenCode merges its own configuration layers underneath; Avibe never writes the user's
  `opencode.json`.
- `enabled_providers` lists exactly the provider ids the overlay generates, so providers from
  the user's own configuration or from environment keys are not loaded and their models never
  appear in `/config/providers`. In Direct mode no overlay exists.
- **Addressing.** In Gateway mode Avibe addresses OpenCode with `providerID` = the fixed
  provider id of the selected row's `native_protocol` from the table below
  (`openai_responses` → `avibe-openai`, `anthropic` → `avibe-anthropic`) and `modelID` = the
  menu id; the Direct-mode `agents.opencode.default_provider` setting is not consulted. A
  selector naming no menu row keeps the existing no-chain handling.
- The overlay content hash is recorded at launch; when the effective overlay changes (menu
  edit, `native_protocol` edit, gateway credential rotation) Avibe waits for active work to
  finish, then restarts the serve process. Restarts are config events: no `resolution-event`
  of kind `channel_switch`/`switch` is emitted.
- **Empty menu.** With no checked rows the overlay is the empty overlay —
  `enabled_providers` naming `avibe-openai` and that provider with `models: {}` — written
  through the same change-and-restart path as any other overlay; the serve process is never
  stopped or launched without an overlay in Gateway mode.

## Provider entries

- One provider entry per downstream protocol used by at least one checked menu row, so no
  generated provider is ever empty and `enabled_providers` equals the generated set. With an
  empty menu the empty overlay (Delivery) is written instead:

  | `native_protocol` | provider id | `name` | `npm` | downstream endpoint |
  | --- | --- | --- | --- | --- |
  | `openai_responses` | `avibe-openai` | `Avibe · OpenAI` | `@ai-sdk/openai` | Gateway `/v1/responses` |
  | `anthropic` | `avibe-anthropic` | `Avibe · Anthropic` | `@ai-sdk/anthropic` | Gateway `/v1/messages` |
  | `gemini` (reserved) | `avibe-gemini` | — | — | not generated |

  `options.apiKey` is the local gateway token (never an upstream credential);
  `options.baseURL` is the local Gateway base for that SDK (exact path per SDK recorded from
  the spike, S1/S2). No `@ai-sdk/openai-compatible` provider is generated: downstream Chat
  Completions is retired for OpenCode. Direct-mode user-defined providers are out of scope.
- `avibe-` is reserved for **creation**: Avibe's Direct-mode custom-provider creation refuses a
  new id with that prefix (existing reserved-id error). An existing entry that already carries
  such an id stays readable and editable exactly as today — the reservation never hides or
  rejects released configuration.
  Avibe does not detect a hand-authored collision: a user who declares an `avibe-*` provider
  in their own OpenCode configuration gets OpenCode's merge behaviour, which is unsupported.
- Provider ids are fixed strings — never derived from a token digest, a Source, or a hop.

## Menu projection

- `provider[<id>].models` is keyed by the bare menu id and enumerates exactly the checked menu
  rows whose `native_protocol` maps to that provider — an empty Route chain does not remove a
  row (C1 lets a user add a model no Source supplies; invoking it takes the existing no-chain
  handling) — with the row's display name, context/output limits, modalities, tool and
  reasoning support. `featured|full` is a UI view state; it cannot add a model to the overlay.
- Reasoning variants are emitted in the shape the provider's SDK consumes (S4):
  `avibe-openai` → `variants[effort] = { "reasoningEffort": effort }` (reaches the wire as
  `reasoning.effort`); `avibe-anthropic` → `variants[effort] = { "effort": effort }` (reaches the
  wire as `output_config.effort`, exact on a same-protocol hop). The legacy
  `thinking.budgetTokens` shape is not used: the engine folds it into a bucketed effort, and
  `thinking: {type, effort}` silently becomes a 1024-token budget. A row with
  `supports_reasoning: false` has no variants.
- The invocation selects the exact effective `(source_id, model_id)` hop. Invocation never
  normalizes a provider or chooses a different plan tier. The same-protocol
  guarantee is protocol-specific: **Anthropic** — when the hop's Source protocol is
  `anthropic`, the request body reaches the upstream unchanged apart from the `model` field,
  which the Gateway rewrites to the effective hop's model id (a user-configured substitution,
  `model-hub.md` §4.5, is invoked as written); **Responses** — when the hop's Source protocol
  is `openai_responses`, `input`, `reasoning`, and `prompt_cache_key` reach the upstream intact
  while the engine's Codex executor edits the envelope (below). The Anthropic guarantee is
  body-level (S3): with the engine flags
  `disable-claude-cloak-mode: true`, credential `cloak.mode: never`, and
  `rebuild-mid-system-message: false`, an Anthropic-frontend body reaches an `anthropic`
  upstream byte-identical; the engine's transport headers remain its own (a Claude-CLI
  fingerprint: `User-Agent`, `Anthropic-Beta`, `X-Stainless-*`, session id) and upstream auth
  is the engine's Bearer form. A Responses-frontend body reaching a `codex-api-key` upstream
  is rewritten by the engine's Codex executor regardless of flags (`max_output_tokens`
  dropped; `parallel_tool_calls: true`, `instructions: ""`, and an `image_generation` tool
  added) — a known engine limitation the implementation verifies against each real
  `openai_responses` Source before enabling the route, and tracks as an engine follow-up.
  When the hop's protocol differs, the engine's automatic translation applies; S4 records what
  survives: Responses-frontend effort reaches an `anthropic` upstream intact, an
  Anthropic-frontend `output_config.effort` is dropped on the way to an `openai_chat` upstream
  (a second reason models are grouped by native protocol, not by supplier).

## Effective route planning

The shared resolver owns OpenCode matching over complete non-retired inventory.
A manual key returns exact saved hops. Otherwise literal canonical model equality
selects the matching tier in default order; if none matches, eligible non-retired
Hub API-key defaults receive the original id unchanged. Subscriptions retain existing
known-model admission and are never speculative unknown-model candidates. No vendor-prefix repair or fuzzy matching
exists. Live health and protocol transport inspection annotate that chosen plan without
creating another tier. The overlay never duplicates this planning algorithm.

## Stability invariant (test requirement, L7)

For a fixed set of checked rows with fixed `native_protocol` values, the generated overlay is
byte-identical across: Source-order edits, Source cooldown/failover, route-chain edits,
engine restarts, and gateway token rotation apart from
`options.apiKey`. Adding/removing a Source without changing the effective Route targets also
leaves it byte-identical. A scenario test asserts this by diffing generated overlays under
each perturbation.

## Persisted compatibility

Supported saved shapes use the existing safe loader. Preserve sparse manual intent,
including empty, stale and dormant OpenCode entries; do not infer old authorship or
automatic intent from array equality/default order. Legacy shapes outside supported
conversion use documented safe degradation, never a blanket pre-release waiver.

## Spike record (S1–S6; evidence captured 2026-09-04 on OpenCode 1.18.18 and CLIProxyAPI 7.2.105 `4a2eb54d`, the binary Avibe's manifest pins, with the repo mock upstream recording path/headers/body)

| # | Question | Evidence |
| --- | --- | --- |
| S1 | `@ai-sdk/openai` + custom `baseURL` calls `/v1/responses` | Yes: `options.baseURL = <gateway>/v1`; a prompt through `avibe-openai/gpt-5` hit the mock at `/v1/responses` with `model: gpt-5`. No option exists or is needed to force Responses; the custom-provider path calls the SDK's `languageModel()`, which is Responses. |
| S2 | `@ai-sdk/anthropic` + custom `baseURL` calls `/v1/messages`; client auth header | Yes: `/v1/messages`, sent with `X-Api-Key` and `Anthropic-Version: 2023-06-01`. The engine accepts its client key as `x-api-key` or `Authorization: Bearer` (200/200; wrong key 401). Toward a `claude-api-key` upstream it sends Bearer. |
| S3 | Same-protocol passthrough | Not by default: the engine added `metadata.user_id`, split `system` into billing/identity/Claude-Code-prompt segments and moved OpenCode's system text into a user `<system-reminder>`. With `disable-claude-cloak-mode: true` + credential `cloak.mode: never` + `rebuild-mid-system-message: false` the inbound and upstream bodies are JSON-identical. Headers stay fingerprinted (Claude-CLI `User-Agent`, `Anthropic-Beta`, `X-App`, `X-Claude-Code-Session-Id`, `X-Stainless-*`); `fingerprint-profile` does not exist in this version. Responses → `codex-api-key` is rewritten regardless of `codex.identity-confuse`/`optimize-multi-agent-v2` (see Menu projection). |
| S4 | Variant shapes and cross-protocol survival | OpenAI `{ "reasoningEffort": "high" }` → `reasoning: {effort: high, summary: auto}`, preserved to a Codex upstream. Anthropic top-level `{ "effort": "high" }` → `output_config: {effort: high}`, preserved on a same-protocol hop. Anthropic `{ thinking: {type: enabled, budgetTokens: 4096} }` → engine folds to `thinking: adaptive` + `output_config.effort: medium`; `thinking: {type: enabled, effort: high}` → a 1024-token budget read back as `effort: low` (wrong). Cross-protocol: Responses effort `high` → `anthropic` upstream gets adaptive thinking + `effort: high`; Anthropic budget 4096 → `openai_chat` upstream gets `reasoning_effort: medium`; Anthropic top-level effort → `openai_chat` upstream gets nothing. |
| S5 | `enabled_providers` hides user/env providers | Yes: with a scratch global config declaring `global-other` with an API key and the overlay's `enabled_providers: ["avibe-openai","avibe-anthropic"]`, `/config/providers` listed only the two Avibe providers. |
| S6 | Canonical reference and display | `/config/providers` shows each provider's overlay `name` and keys its models by the bare id; a prompt carries `providerID` and `modelID` separately, so the canonical reference string is `avibe-anthropic/claude-opus-5`. |
