# OpenCode overlay contract (v4, 2026-09-04)

<!-- authority-consumer: protocol anthropic openai_responses -->

How Avibe generates the OpenCode runtime config overlay in Gateway mode. Owner-locked
identifier rules (spec §4.8 v4) restated as testable requirements. Supersedes the 07-23
contract (one OpenAI-compatible provider, `vendor/model` ids); nothing of that shape survives.

## Delivery

- The overlay is injected at `opencode serve` launch as `OPENCODE_CONFIG` (file) and
  `OPENCODE_CONFIG_CONTENT` (same bytes, OpenCode's runtime-override tier). OpenCode merges its
  own configuration layers underneath; Avibe never writes the user's `opencode.json`.
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
- **Nothing projectable.** When the effective overlay would contain no projectable row
  (the last routeful checked row was removed), Avibe writes no overlay and drains and stops
  the serve process; it relaunches with a fresh overlay when a projectable row exists again.
  In Gateway mode a serve process never runs without an overlay, so the user's providers are
  never exposed and a stale provider set never lingers in `/config/providers`.

## Provider entries

- One provider entry per downstream protocol used by at least one projected row — a checked
  menu row with a stored nonempty Route chain — so no generated provider is ever empty and
  `enabled_providers` equals the generated set. When no row is projectable the existing
  `mapping_target_unavailable` refusal applies, no overlay is written, and the serve process
  is stopped (Delivery):

  | `native_protocol` | provider id | `name` | `npm` | downstream endpoint |
  | --- | --- | --- | --- | --- |
  | `openai_responses` | `avibe-openai` | `Avibe · OpenAI` | `@ai-sdk/openai` | Gateway `/v1/responses` |
  | `anthropic` | `avibe-anthropic` | `Avibe · Anthropic` | `@ai-sdk/anthropic` | Gateway `/v1/messages` |
  | `gemini` (reserved) | `avibe-gemini` | — | — | not generated |

  `options.apiKey` is the local gateway token (never an upstream credential);
  `options.baseURL` is the local Gateway base for that SDK (exact path per SDK recorded from
  the spike, S1/S2). No `@ai-sdk/openai-compatible` provider is generated: downstream Chat
  Completions is retired for OpenCode. Direct-mode user-defined providers are out of scope.
- `avibe-` is a reserved provider-id prefix: Avibe's own Direct-mode custom-provider creation
  refuses it with the existing reserved-id error.
- Provider ids are fixed strings — never derived from a token digest, a Source, or a hop.

## Menu projection

- `provider[<id>].models` is keyed by the bare menu id and enumerates exactly the checked menu
  rows whose `native_protocol` maps to that provider and which have a stored nonempty Route
  chain, with the row's display name, context/output limits, modalities, tool and reasoning
  support. `featured|full` is a UI view state; it cannot add a model to the overlay.
- Reasoning variants are emitted in the shape the provider's SDK consumes:
  `avibe-openai` → `variants[effort] = { "reasoningEffort": effort }`; `avibe-anthropic` →
  the shape recorded from the spike (S4). A row with `supports_reasoning: false` has no variants.
- The invocation selects the exact stored `(source_id, model_id)` hop. Runtime never
  normalizes a provider, matches inventory, or substitutes a model. When the hop's Source
  protocol equals the row's `native_protocol` the Gateway passes the request through
  unchanged apart from credentials, host, and the `model` field, which it rewrites to the
  stored hop's model id — a user-configured substitution (`model-hub.md` §4.5) is invoked as
  written (same-protocol passthrough, S3: the engine's
  Claude/Codex cloaking and fingerprint rewrites are disabled for API-key upstreams); when it
  differs, the engine's automatic translation applies (S4 records what reasoning fields survive).

## Add-time matching (`matching-v1`)

OpenCode matching occurs once while adding a Source or a menu model, using the inventory
owned by that write. An exact model id wins; there is no prefix to strip or repair. The stored
Route carries the concrete upstream model id and this normalization is never repeated by
runtime or refresh.

## Stability invariant (test requirement, L7)

For a fixed set of checked rows with fixed `native_protocol` values, the generated overlay is
byte-identical across: Source-order edits, Source cooldown/failover, route-chain edits that
keep the chain nonempty, engine restarts, and gateway token rotation apart from
`options.apiKey`. Adding/removing a Source without changing the stored Route chains also
leaves it byte-identical. A scenario test asserts this by diffing generated overlays under
each perturbation.

## Upgrade (pre-GA: reset, not migrate)

Model Hub is behind the `VIBE_MODEL_HUB_ENABLED` release gate, so no released installation
holds OpenCode menu state; only development environments do. v4 therefore does not migrate
OpenCode menu state — it discards it:

- **Discriminator.** The persisted Model Hub config carries `model_hub.contract_version`; a
  config without it is 7. Exactly when the stored value is below 8, the loader empties the
  OpenCode agent's `models`, `routes`, `removed_model_ids`, and `menu` and writes 8 on the
  next save. Nothing else is read or rewritten: Sources, Source order, the OpenCode mode, the
  other backends, Vibe Agent definitions, and sessions are untouched.
- **Selectors.** A Vibe Agent definition or a persisted session override that still names a
  retired `vendor/model` id resolves through the existing no-menu-row handling until the user
  selects again; in Direct mode such a selector is OpenCode's own `providerID/modelID` and
  keeps working. No cross-store write exists, so the upgrade is crash-safe and idempotent by
  construction: a v8 config has no pre-v8 OpenCode state to touch.
- **Fixture.** A v7 config holding OpenCode rows, a standalone Route, removed markers, and a
  `menu` object loads to an empty OpenCode menu with Sources and mode intact, and loads
  identically a second time.

## Spike record (S1–S6; filled from the spike lane's evidence before implementation starts)

| # | Question | Evidence |
| --- | --- | --- |
| S1 | `@ai-sdk/openai` + custom `baseURL` calls `/v1/responses` (exact base path) | pending |
| S2 | `@ai-sdk/anthropic` + custom `baseURL` calls `/v1/messages`; client auth header the Gateway accepts | pending |
| S3 | Same-protocol passthrough body diff, and the engine flags that make it empty for API-key upstreams | pending |
| S4 | Variant shapes on the wire per SDK, and what survives cross-protocol translation | pending |
| S5 | `enabled_providers` hides user/env providers in `/config/providers` | pending |
| S6 | Canonical reference as OpenCode reports it (`avibe-anthropic/<id>`) and provider `name` display | pending |
