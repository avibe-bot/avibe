# Model Hub capability model

Status: **design**, 2026-10-13; evidence made passive-only on 2026-10-15 with hosted tools design v2 (no probes).
Consumers: [`vibey-hosted-tools.md`](vibey-hosted-tools.md) and the effort menu (avibe-bot/avibe#2539). It extends [`model-hub-reasoning-intent.md`](model-hub-reasoning-intent.md) and
[`model-hub-reasoning-tiers.md`](model-hub-reasoning-tiers.md); it does not replace their forwarding rule.

## 1. Problem

The question is "what can this model do on this Source". Two failures from 2026-10-13 show the gaps:

- **Effort `ultra` (#2539).**
  - `vibe/data/backend_models.json` lists `ultra` for `gpt-6-astra`. That list is Codex's model menu, and `ultra`
    is a Codex client mode: the Codex client maps it to a lower wire tier before sending
    ([`model-hub-capability-authority.md`](model-hub-capability-authority.md)).
  - `resolve_reasoning_tiers` (`core/handlers/model_hub/reasoning_tiers.py`) copies that list onto every Source
    model, so Vibey offers `ultra` and sends it literally.
  - An OpenAI API-key relay answers `400 invalid_value … Supported values are: 'none', 'minimal', 'low', 'medium',
    'high', 'xhigh', and 'max'`. models.dev already lists `low … max` for the model.
- **Hosted tool `x_search`.** The same relay answers `400 Unsupported tool type: x_search`. Model Hub classifies
  `invalid_request_error` as terminal, so a route that falls over to it fails the turn.

Both failures are a claim about a model stood in for a fact about a Source. The model below keeps three kinds of
fact apart and gives each one owner.

## 2. Three layers

| Layer | Scope | Owner and location | Holds | Never holds |
| --- | --- | --- | --- | --- |
| **Base** | (provider, model) | models.dev, cached by `vibe/models_dev_catalog.py` | context and output limits, modalities, `tool_call`, `reasoning`, `reasoning_options` (wire effort values), token prices including context tiers (§6) | anything about a specific Source or relay |
| **Overlay** | provider model family, per protocol | Avibe. `vibe/data/backend_models.json` becomes the one overlay: its backend rows plus a new provider-neutral `providers` section (§4) | corrections and additions to the base: wire effort tiers where models.dev is wrong or late; **client presets** that never go on the wire (Codex `ultra`); **hosted tools** the provider offers for the model on that protocol; hosted-tool prices (§6) | per-Source truth; an allowlist |
| **Evidence** | (Source, model, feature) | Model Hub state, next to Source health | what real requests showed: negative evidence from a real upstream error, and positive evidence from a real response that carried the feature (display only), with time, wire stamp and scope | priors; anything learned without a real request the user's turn made |

Rules:

1. **Base and overlay are priors.** They decide what Avibe offers and shows before anything is known about a Source.
   They never block a request (reasoning-intent decision of 2026-09-08: capability declarations are not an
   allowlist).
2. **Negative evidence decides routing.** It is the only layer that may take a feature away from a hop. Positive
   evidence never changes routing; it only tells the user what ran (`vibey-hosted-tools.md` §8).
3. **The #2505 rule applies to every fact.** Unknown is not negative. Only explicit negative evidence excludes.
   Since hosted tools design v2, a hosted tool the priors offer is assumed on a hop until a real request refuses it:
   nothing is probed, and a refusal before output costs one re-rendered request (`vibey-hosted-tools.md` §3.5).
4. **One overlay.**
   - `backend_models.json` already overlays efforts for native backends.
   - Its backend rows gain `client_presets`, effort names a client maps before sending (Codex `ultra`). These are
     never wire tiers.
   - A new provider-neutral `providers` section holds what the backend rows cannot: per-provider wire effort
     corrections, hosted tools and prices (§4). Today the loader admits only `claude` and `codex` rows and no
     provider, so the section needs its own loader and schema.
   - No second file is introduced for Vibey.
   - Native backends keep reading their menu from their rows. Vibey and the gateway read wire values and the
     `providers` section.
5. **One owner per derived fact.** Model Hub resolves the layers once, for both the effort menu and hosted-tool
   modes. Vibey only reads `HopResolution`. Only the gateway's classifier writes evidence, and only Model Hub's
   pricing module turns usage into money. No other component re-derives any of these.

## 3. Evidence

### 3.1 Record

At most one record per (`source_id`, `model_id`, `feature`, `status`): a `verified` (display) and an `unsupported`
(routing) record for the same feature coexist. Absence of a record is `unknown`. The persisted shape is
below, and [`model-hub-contracts/capability-evidence.schema.json`](model-hub-contracts/capability-evidence.schema.json)
is its JSON Schema. The store is `core/handlers/model_hub/capability_evidence.py`.

| Field | Rule |
| --- | --- |
| `version` | `1` |
| `source_id`, `model_id` | non-empty |
| `feature` | `{kind: "effort", name}` or `{kind: "hosted_tool", name: "web_search" \| "x_search" \| "image_generate", aspect: "call" \| "replay"}`; `image_generate` has `call` only |
| `status` | `verified` (a real response carried the feature; display only) or `unsupported` (a narrow rule fired) |
| `basis` | `observed`: learned from a real request. There is no other basis (v1's `probe` is gone). |
| `observed_at` | UTC timestamp |
| `mode` | `verified` records only: how the call ran, `native`, `executor` or `direct` (`vibey-hosted-tools.md` §3.2) |
| `wire` | the wire stamp the record was observed under: the engine release, the Avibe release, and the model's rendered fields the Source identity does not cover (§3.3) |
| `detail` | required when `status` is `unsupported`: the HTTP status and upstream machine code, e.g. `400 invalid_value`, at most 64 characters, never upstream text |
| `source_identity` | digest of `retry.source_identity` when the record was written |

Records an rc build wrote with `basis: probe`, `shape` or `image_generation` do not validate, so they are pruned at
the next write (pre-release state, no migration).

Records live in the Model Hub state directory (`model_hub_capability_evidence.json`). They follow Source identity:
`retry.source_identity` covers kind, vendor, protocol, channel, base URL, credential and credential generation.
Changing any of those drops the Source's evidence: a record whose stored digest no longer matches reads as unknown
and is pruned at the next write. The credential generation is the verification marker the latest credential or
endpoint change minted, kept after verification clears the marker. So verifying a credential changes no identity,
and a later change never returns the Source to an earlier identity, even when OAuth reauthentication keeps the
credential's handle. Released Sources keep their digests: a verified one has no generation, where the identity held
no marker, and a pending one's marker becomes its generation.

### 3.2 Learning from real errors (negative evidence)

- **Narrow rules, one per feature, in one place:** `core/handlers/model_hub/evidence_rules.py`, run by the
  gateway's resolution loop on every upstream outcome. Nothing else writes evidence from real traffic. The engine
  client (L1) projects a 400 before output into an `UpstreamRejection`: the machine code and type from the one
  error-field extractor, the envelope's `param`, and the single token each recognized message names. The prose
  never leaves L1, and the failover decision never reads the rejection. A rule maps it to exactly one (Source,
  model, feature) only when it names that feature:
  - `Invalid value: '<v>'` where `<v>` is the effort the request sends on the wire, or `param` naming the effort
    field on an untranslated path → effort `<v>`;
  - `Unsupported tool type: <type>` for a type the request declared, matched to a catalog tool through the
    overlay's declaration for that Source and model → hosted tool `call`;
  - `Invalid encrypted_content in search_result block` while the request replays a web-search result → hosted
    tool `web_search` `replay`;
  - xAI's plain-text `Could not decrypt the provided encrypted_content` (no code) while the request replays an xAI
    payload: its status waits for the Vibey lane's xAI A/B (stale ciphertext versus unsupported). Until then it
    writes no evidence and only turns replay off for that Source for the rest of the turn.
- **Positive, display only.** A served response that carries the provider's own item for a hosted tool the request
  declared natively writes `verified` for the serving (Source, model, tool, `call`); a proxy executor call and a
  successful image call do the same for the Source that ran them. It is written only when the state changes or the
  existing record is over an hour old, so the evidence file is not rewritten per response
  (`vibey-hosted-tools.md` §3.2).
- **Generic errors never create evidence.** That covers 5xx, overload, rate limit, auth, and any 400 that does not
  name the feature.
- **What happens to the request.**
  - An effort rule match fails the request as it does today. The next request skips that hop (§3.4).
  - A hosted-content rule match on a pinned request becomes `pin_unavailable` with a machine-readable `reason`. Vibey
    re-resolves the hop, whose fresh facts now name that tool as unsupported, and resends with the same-named function
    (`vibey-hosted-tools.md` §3.5).
- **How long negative evidence lasts** (owner decision, 2026-10-15): one rule for efforts and hosted tools. There is
  no time limit. A record stays until one of these clears it:
  - the user clears it: a hosted-tool refusal in Models → Tools (`vibey-hosted-tools.md` §3.3, §8.1), an effort
    refusal on its Source chip (§5);
  - its wire stamp no longer matches (§3.3), which happens on its own when the Source's config, the engine release
    or the Avibe release changes.

### 3.3 No probes; the wire stamp

- **Nothing probes.** Since hosted tools design v2 (2026-10-15) no component calls a provider to learn a capability:
  not on a user action, not in the background. Efforts were never probed; hosted tools no longer are. Every record
  comes from a real request in a turn the user started (§3.2).
- **Negative evidence never outlives the wire it was learned on** (owner rule: a Source config change resets it).
  A record carries `source_identity` and `wire`, and reads as unknown once either differs from the current one.
  Together they cover every input of the engine renderer (`vibe/model_hub_runtime/config.py`, `_append_source`)
  and of the request:

  | Input the renderer or request reads | Covered by |
  | --- | --- |
  | vendor, protocol, base URL (or the vendor's official default) | `source_identity` |
  | credential (the API key value, or the OAuth auth file) and its auth scheme, which is fixed per credential ref | `source_identity` (`credential_ref`, `credential_generation`) |
  | supply channel and Source kind | `source_identity` |
  | the model's registered reasoning tiers (`thinking.levels`), which decide whether the engine forwards or strips an effort | `wire` (the model's rendered fields) |
  | Avibe's own rendering and request code: entry shapes, fixed per-entry flags (`cloak`, `rebuild-mid-system-message`), vendor-wide flags such as xAI's `is-compat` (#2620), the bundled overlay's declarations and `include` values, the adapters' request shape | `wire` (the Avibe release) |
  | the engine binary | `wire` (the engine release) |

  Not inputs that can reverse a refusal, so not covered: which models are registered (membership does not change
  how a registered model is sent), the routing prefix and reload marker (engine-internal), and the text-only marking
  (from the models.dev copy, not Source config; it only replaces images in tool results). The Source config has no
  custom headers. **Rule for future fields:** a Source field the renderer starts to read joins `source_identity`,
  or, if it is per model, the model's rendered fields (`capability_evidence.render_fields`).
  So a refusal learned under one engine, release or Source config cannot survive a change that could reverse it.
  The stamp is computed from the config in memory, never on a request path from the engine state. A change on the
  provider's side, which no stamp can see, waits for the user's clear.
- **What goes.** v1's `shape` field, probe-shape staleness, age-based re-verification, and the `hosted_wire`
  manifest field (never introduced; `engine_stamp()` loses its `hosted_wire` branch). Positive evidence needed them
  because it vouched for a wire; it no longer vouches for anything.

### 3.4 How layers resolve

| Consumer | Rule |
| --- | --- |
| Effort menu for a model on an Agent | each hop's declared tiers, then the union across hops. A hop's declared tiers follow `model-hub-reasoning-tiers.md`'s ladder: an upstream reasoning signal (the protocol's ladder, bounded by the model's catalog row when it has one), else the bundled backend row's efforts minus that row's `client_presets` (provenance `catalog`; it outranks a user list, with a visible override event), else the user's list, else models.dev `reasoning_options` when the catalog row is built. A persisted `catalog` list is also read without `client_presets`, since released builds wrote them, and Vibey's own model rows always drop them at config load (3.2.0 saved rows with Codex's `ultra`; Vibey is never the client a preset belongs to, and no provider documents `ultra` as a wire effort). A user-authored Source list is never filtered. The overlay's `wire_efforts` slots in before the backend row once a vendor needs it; no shipped entry sets it yet. A tier is omitted only if **every** hop that can serve the model has negative evidence for it: every hop the route would try or wait for, exactly the hops whose refusal ends a request in `effort_unsupported`. A tier with negative evidence on some hops is shown with "not on: <Source>". Evidence is keyed by the token a client puts on the wire, which can differ from the menu value: Vibey sends menu `minimal` to Anthropic as `low`. So Model Hub decides both in menu values, where routing decides, and the picker's own read (`GET /api/models/agents/<backend>/models`, an Editor surface) carries the result per model as the response-only `effort_menu`. Vibey's adapters map each menu value through the one function their request builders use (`registry.sent_effort`); a native CLI sends the value as the menu names it. The chain API keeps each hop's read-only `refused_efforts` (`agent-chain.schema.json`) for the evidence detail. |
| Effort at request time | Forwarded unchanged (reasoning-intent). A hop with current **negative** evidence for that effort is skipped for this request, as if it were not a candidate. If no candidate remains, the request fails before invocation with `effort_unsupported` naming the effort and the Sources, instead of a provider 400. Never clamped silently. |
| Hosted tool on a hop | `native: assumed` when the overlay offers the tool for the hop's (Source, model) on an untranslated path with an eligible credential (`runs_on`) and no current negative `call` evidence exists; otherwise `unsupported`, and Vibey declares the same-named function (`vibey-hosted-tools.md` §3.4). A refusal costs one re-rendered request; a silent drop is undetectable and accepted (`vibey-hosted-tools.md` §15). |
| Native payload replay | `replay: assumed` under the same conditions and no current negative `replay` evidence, only to the Source and identity that minted the payload, on the same origin. Decided at every request from `HopResolution`; the transcript never stores it. |
| Display | §5 |

This fixes #2539 in three places (Phase 1 PR 1b):
- the overlay lists `ultra` in Codex's `client_presets`, so it is not a wire tier for Vibey;
- a catalog-sourced tier list is the backend row minus those presets (`low … max`, which matches models.dev's wire list), also when a released build persisted it with `ultra`;
- the relay's real `400 invalid_value` records `ultra: unsupported` for that Source.

Even without the overlay fix, the next request would skip that hop or fail clearly.

## 4. The overlay's `providers` section

The section is keyed by canonical Source vendor id (`openai`, `anthropic`, `xai`, …). Each vendor holds an ordered
list of model families. A family matches by `prefix` or exact `id` against the model id as Model Hub normalizes it
for Source models and route hops (`normalized_model_id`), and the first match wins. A new model of a family inherits
its row. Shape (`vibe/data/backend_models.json`, `providers`; the loader in `vibe/backend_model_catalog.py`
validates it):

```json
"providers": {
  "xai": {"models": [
    {"match": {"prefix": "grok-4"},
     "wire_efforts": null,
     "hosted_tools": {"openai_responses": {
       "web_search": {"declaration": {"type": "web_search"},
                      "price": {"unit": "call", "usd_per_1k": 5, "as_of": "2026-10-13", "url": "https://docs.x.ai/developers/pricing"}},
       "x_search": {"declaration": {"type": "x_search"},
                    "price": {"unit": "post", "usd_per_1k": 5, "extra": [{"unit": "profile", "usd_per_1k": 10}], "as_of": "2026-10-13", "url": "https://docs.x.ai/developers/pricing"}}}}}
  ]},
  "anthropic": {"models": [
    {"match": {"prefix": "claude-"},
     "hosted_tools": {"anthropic": {
       "web_search": {"declaration": {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
                      "price": {"unit": "search", "usd_per_1k": 10, "as_of": "2026-10-13", "url": "https://platform.claude.com/docs/en/about-claude/pricing"}}}}}
  ]}
}
```

Fields of one vendor entry, mirrored by
[`model-hub-contracts/provider-overlay.schema.json`](model-hub-contracts/provider-overlay.schema.json):

| Field | Rule |
| --- | --- |
| `models[]` | required; ordered, first match wins |
| `match` | exactly one of `prefix` or `id`, non-empty |
| `wire_efforts` | a list of effort names, or `null` (or absent) to defer to models.dev |
| `hosted_tools` | optional; protocol (`anthropic`, `openai_responses`, `openai_chat`, `google`) → search tool (`web_search`, `x_search`) → `{declaration, price?, include?}`. `declaration` is required, has a `type`, and is sent verbatim. `price` is optional: absent means no list price is known, never zero. `include` lists Responses `include` values the tool needs (OpenAI web search: `web_search_call.action.sources`). v1's OpenAI `image_generation` entry is removed: no chat request declares a hosted image tool (`vibey-hosted-tools.md` §7). |
| `executor_models` | new, optional, at vendor scope (beside `models[]`): the vendor's lightweight model ids that run proxied search calls on this vendor's Sources, preferred first (`vibey-hosted-tools.md` §6), e.g. Haiku, mini or fast tiers; a Source runs the first it lists. The executor call's declaration and `include` are those of the family row that model id matches, on the Source's protocol. A vendor without it is never an executor. |
| `image_models[]` | new, optional per vendor: `{id, description, params, size_forms, kinds?, request?, prices?, tokens_per_image?}`. Every image model runs through the engine's images endpoints (`/v1/images/generations`, or `/v1/images/edits` when the call carries reference images). `description` is the one line the `image_generate` enum shows; `params` lists which common parameters the model takes (`size`, `images`, `n`), and `size_forms` which forms of `size` it accepts (`pixels`, `ratio`); `kinds` names the credential kinds (`api_key`, `subscription`) its route was verified on, every kind when absent, so a subscription whose route is unverified is never offered the model; `request` holds the fixed scalar fields every call to it sends (an output format, base64 results), never a field the call names itself; `prices[]` is optional, each entry a `price` with an `image` unit keyed by the `size` and `quality` it applies to (absent keys match any), so a vendor that prices sizes or qualities apart is valued at the size the call resolved. `tokens_per_image` is optional: the documented output tokens of one image, which the pricing module values at the model's output token rate as an estimate (shown with ≈) where no per-image price exists. Only models whose route the implementation verified through the shipped engine are listed. |
| `price` | `unit` (`call`, `search`, `post`, `profile`, `image`), `usd_per_1k` ≥ 0, `as_of` (date) and `url` (https), all required; optional `extra[]` of `{unit, usd_per_1k}`. An `image` unit may carry `quality` and `size` when the vendor prices them apart. |
- The overlay records what the **provider** offers, and it is the prior every hop starts from: a hosted tool is
  assumed where the overlay offers it, until a real request refuses it. Whether a Source (a relay, a subscription)
  actually carries it is learned passively (§3.2).
- An invalid vendor entry disables that entry with a warning; startup never fails. A `providers` value that is not
  an object disables the whole overlay. Backend rows never depend on the section.
- The section is read from the bundled file only, never from the remote catalog copy that refreshes backend rows:
  its declarations go on the wire and must match the code that shipped with them. Released loaders ignore both the
  section and the rows' `client_presets`.

## 5. What Models shows per Source

Each model row on a Source detail panel (`SourceDetailPanel`) gets capability chips. They reuse `chips.tsx` badges
and the existing tone vocabulary (`TimeHint` for dates):

| Evidence | Chip | Example |
| --- | --- | --- |
| hosted tool, `call` verified (seen in a real response) | normal, with the time | "网页搜索 · 正常 · 10-15" |
| hosted tool, `call` unsupported | not supported | "X 搜索 · 不支持" |
| effort unsupported | not supported | "ultra · 不支持" |

- Unknown draws nothing. A healthy unknown is the normal case and must not look like a warning (V6 rule:
  exceptions only).
- A chip opens a detail popover (time, basis, the refusal's HTTP status and code). Every refusal chip carries
  "清除" (owner-approved mockup, 2026-10-15); a hosted-tool refusal clears through the Tools `clear` endpoint
  (`vibey-hosted-tools.md` §3.3). An effort refusal's "清除" clears that (Source, model, effort) record, so the next request at that effort tries the
  Source again (`POST /api/models/sources/<source_id>/evidence/clear` with `{model_id, feature}`, the same verb and
  shape as the hosted clear). An unknown `source_id` is 404; a missing record is a no-op that answers the Source;
  `feature` must be `{kind: "effort", name}`, and a hosted-tool feature is 400; the file is written first, and a
  failed write answers 5xx with nothing changed. A positive chip has no action (`vibey-hosted-tools.md` §8.2). Nothing re-checks: a
  cleared record comes back only if a real request is refused again.
- Because refusals no longer expire, the Agent route picker's refused-effort note says where to undo it:
  "当前没有供应商支持 · 可在模型 → 供应商中清除" / "No Source accepts it now · clear it under Models → Sources"
  (`chat.picker.effortRefused`, copy only, no action in the picker).

## 6. Pricing

### 6.1 What Avibe uses prices for today

- **A valuation only, never a bill** (`core/handlers/model_hub/pricing.py`):
  - "At API price" on the Usage tab and the quota cards;
  - the subscription payback comparison (MH-QUOTA scenarios).
- Prices come from the user's override file `model_hub_prices.json`, then from models.dev's first-party provider
  row.
- Resolution, admission, cooldown and provenance never read a price. Provenance and the usage ledger record tokens
  only.
- **Source billing.** `ModelHubSourceConfig.billing` is `monthly` (subscription) or `metered` (API key)
  (`source.schema.json`). It drives the billing chip, the payback view, and the failover note `entered_metered`. It
  carries no rate.

### 6.2 Where each pricing fact lives

| Fact | Lives in | Status |
| --- | --- | --- |
| Token prices: input, output, cache read, cache write | models.dev base `cost.*`; the user's override file | used |
| One-hour cache write | override file, or 2× input by default (`pricing.py`) | used; models.dev has no field |
| **Context-tier prices** (a higher rate past a context size) | models.dev `cost.tiers[]` with `{"tier": {"type": "context", "size": N}}`, plus the legacy `cost.context_over_200k` (size 200K, read only when no context tier is usable); an override entry in the same shape. On 2026-10-13: 630 and 517 models (e.g. `gpt-6-astra` above 272K: $20/$75; `grok-4.6` above 200K: $4/$12) | used. `model_price` reads each tier by the flat-price rules and ignores an unusable one. A call whose total context input (input plus cache-read and cache-write tokens, which the ledger's `input_tokens` already counts) exceeds a size is billed whole at that tier's rates; the highest tier exceeded wins. The ledger keeps no per-call rows, so the usage writer decides the tier when it records a call, from a price table it reads off the event loop and holds for five minutes, and the tier is part of the row's identity (`context_tier`, persisted only above the base tier, so released rows read as base). Valuation prices each row at its own tier and sums tiers per Source and model; a tier the table no longer lists is valued at the largest listed tier below it. |
| **Fast-tier prices** (the vendor's faster processing tier, docs/plans/fast-mode.md) | models.dev `experimental.modes.fast.cost`, read by the flat-price and context-tier rules into `ModelPrice.fast`. On 2026-10-31: e.g. `claude-opus-5-5` $8/$40 against $4/$20, `gpt-5.4` $5/$30 against $2.50/$15. Override entries carry none | used. The tier a call ran at is what its response says (Anthropic `usage.speed`, OpenAI `service_tier`), never what was asked; the usage writer records it as part of the row's identity (`speed: "fast"`, persisted only for the fast tier, so released rows read as standard, and a released reader folds a fast row into the standard one without losing a count). Valuation prices a fast row at the fast rates in the row's context tier, or at the standard rates as a lower bound where the table lists none. Summaries publish `fast_requests` beside `requests` |
| **Hosted-tool call prices** | overlay `providers.<vendor>.models[].hosted_tools.<protocol>.<tool>.price` (§4): unit, USD per 1,000 units, date and source URL | new. models.dev has none; the open proposal anomalyco/models.dev#8707 adds `cost.web_search = {search, prompt}` (no maintainer response yet) |
| Search-result tokens | the provider's usage (input tokens), priced by the token rows | already counted as input |
| Provider-reported cost | per-call usage, when present: xAI `cost_in_usd_ticks` (USD × 10¹⁰), `server_side_tool_usage_details`; Anthropic `server_tool_use.web_search_requests` | not read today; hosted tools read it (§6.4) |
| Per-Source billing mode | Source config `billing` | used |
| Relay markup or a different relay rate | not representable. A relay's bill is its own; Avibe shows list price and says so | out of scope (no billing system) |

**Hosted-tool list prices on 2026-10-13** (vendor pages; the overlay records each with its date and URL):

| Provider | Tool | Price |
| --- | --- | --- |
| OpenAI | `web_search` | $10 / 1k calls, plus search-content tokens at the model's input rate |
| Anthropic | `web_search` | $10 / 1k searches, plus result tokens as input, which are billed again on later turns while they stay in context |
| xAI | `web_search` | $5 / 1k calls |
| xAI | `x_search` | $5 / 1k posts and $10 / 1k profiles fetched (per-item billing since 2026-09-21) |

### 6.3 Per-Source billing semantics

- **`metered`.**
  - Price = list price, labelled "at API price".
  - When the provider reports the cost in the response (xAI), show that figure, labelled "reported by xAI".
- **`monthly` (subscription).** No per-call charge. Show "included in subscription" rather than a price, and keep
  the at-API-price valuation for payback.
- **A relay** is `metered` at list price. Its bill may differ, and the cost hint says "at API price" so it never
  claims to be the relay's bill.

### 6.4 What hosted tools need from pricing

1. **A per-call price** on the Tools tab (the overlay's list price per tool) and the real cost on the use-time card:
   - native: the tool fee plus the response's tokens;
   - proxy: the executor's lightweight-model call plus the tool fee;
   - `image_generate`: the image model's per-image price where listed, else its reported usage at its token rate.

   There is no estimate before a call: nothing runs before the model calls a tool. All of it is computed by the one pricing owner, `pricing.py` (`hosted_response_cost`, reached through
   `ModelHubService.hosted_call_cost`, which reads only the price table the usage writer already holds, so pricing a
   response does no I/O). It is stored once per response: as the message's
   `hosted_summary.cost` for native calls, and as `ProxyExecution.cost` for a proxied call, which is its own executor
   response. A provider-reported figure is never copied into individual calls. Neither the
   executor nor an adapter prices anything itself.
2. **Executor choice is automatic.** The chain is the Sources whose vendor offers the tool, in the user's optional
   executor order, then Vibey's Source order, each on its vendor's lightweight model (`vibey-hosted-tools.md` §6).
   Avibe does not auto-optimize cost, because relay prices are unknowable.
3. **Usage accounting.** The proxy executor's calls are metered like any gateway call (usage ledger, provenance),
   attributed to the executor Source.

No new billing store, budget, or cap.

## 7. Upstream models.dev

- **Prices:** support anomalyco/models.dev#8707 (`cost.web_search`) with a comment carrying our measured vendor
  units, including xAI's per-post/per-profile `x_search`, which the proposal does not cover. It is worth doing: it
  removes one overlay table, and OpenCode and others read the same data.
- **Availability** (which hosted tools a provider offers per model): a field would help other consumers, but for
  Avibe it only moves a prior. Relays and subscriptions differ from the provider, so evidence stays the truth. Low
  priority; propose it after the overlay has settled for one release.
- Neither proposal blocks Avibe.

## 8. Acceptance properties

1. A fact from models.dev or the overlay never blocks a request or removes a tier on its own. Only evidence does.
2. Only a narrow rule creates negative evidence. The rule names the (Source, model, feature) it learned. A generic
   error creates none.
3. Nothing probes. Every evidence record comes from a real request in a turn the user started.
4. Changing a Source's identity drops its evidence. Negative evidence, for efforts and hosted tools alike, has no time
   limit: it reads as unknown once its wire stamp differs from the current one or the user clears it. Positive
   evidence never changes routing.
5. #2539: a Source that rejected `ultra` shows "ultra · not supported on this Source". The next request at `ultra`
   either skips that hop or fails before invocation with `effort_unsupported`, never a provider 400.
6. Valuation applies context tiers when a call's input tokens exceed the tier size. A model with tiers and no flat
   price is still not priced as zero.
