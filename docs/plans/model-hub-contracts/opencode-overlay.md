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
- The overlay content hash is recorded at launch; when the effective overlay changes (menu
  edit, `native_protocol` edit, gateway credential rotation) Avibe waits for active work to
  finish, then restarts the serve process. Restarts are config events: no `resolution-event`
  of kind `channel_switch`/`switch` is emitted.

## Provider entries

- One provider entry per downstream protocol that at least one checked menu row uses:

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

## Migration (one-way, pre-GA, idempotent)

- **Discriminator.** The persisted Model Hub config carries `model_hub.contract_version`; a
  config without it is 7. The loader migrates exactly when the stored value is below 8 and
  writes 8 on the next save. Classification never inspects id shape — a v4 id may itself
  contain a slash (`moonshotai/kimi-k2`) — so a second load of a migrated config, or a config
  whose OpenCode menu holds only `removed_model_ids`, changes nothing.
- **Rows.** Every pre-v4 OpenCode menu id `vendor/model` becomes `model`. `native_protocol` is
  derived from the stripped `model` through the vendor map (`anthropic` family → `anthropic`;
  unknown or any other family → `openai_responses`), never from the old `vendor` segment,
  which named a supplier rather than the model.
- **Collisions.** When two or more pre-v4 rows strip to one id (`anthropic/foo`,
  `openrouter/foo`), the first in menu order survives with its metadata and position; the
  others' Route hops are appended to its chain in menu order, deduplicated by
  `(source_id, model_id)`; the others' rows are dropped. Removal markers strip the same way and
  deduplicate. The load never fails and never drops a hop; a fixture with such a config is a
  required test.
- **Selectors.** Every persisted selector that stores an OpenCode menu id is rewritten by the
  same mapping in the same migration: the `model` of every Vibe Agent definition whose backend
  is `opencode` (default and named agents alike) and any other persisted OpenCode selection. The
  Direct-mode `agents.opencode.default_provider` setting refers to OpenCode's own catalog and
  is untouched.
- No compatibility reader for the old form is kept; usage-ledger rows keyed by an old id are
  left as history.

## Spike record (S1–S6; filled from the spike lane's evidence before implementation starts)

| # | Question | Evidence |
| --- | --- | --- |
| S1 | `@ai-sdk/openai` + custom `baseURL` calls `/v1/responses` (exact base path) | pending |
| S2 | `@ai-sdk/anthropic` + custom `baseURL` calls `/v1/messages`; client auth header the Gateway accepts | pending |
| S3 | Same-protocol passthrough body diff, and the engine flags that make it empty for API-key upstreams | pending |
| S4 | Variant shapes on the wire per SDK, and what survives cross-protocol translation | pending |
| S5 | `enabled_providers` hides user/env providers in `/config/providers` | pending |
| S6 | Canonical reference as OpenCode reports it (`avibe-anthropic/<id>`) and provider `name` display | pending |
