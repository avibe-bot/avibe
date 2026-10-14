# Model Hub capability model

Status: **design**, 2026-10-13. Consumers: [`vibey-hosted-tools.md`](vibey-hosted-tools.md) and the effort menu
(avibe-bot/avibe#2539). It extends [`model-hub-reasoning-intent.md`](model-hub-reasoning-intent.md) and
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
| **Evidence** | (Source, model, feature) | Model Hub state, next to Source health | the outcome of a live probe, or negative evidence learned from a real upstream error, with time, basis and scope | priors |

Rules:

1. **Base and overlay are priors.** They decide what Avibe offers and shows before anything is known about a Source.
   They never block a request (reasoning-intent decision of 2026-09-08: capability declarations are not an
   allowlist).
2. **Evidence decides routing.** It is the only layer that may change what a hop receives.
3. **The #2505 rule applies to every fact.** Unknown is not negative. Only explicit negative evidence excludes, and
   only positive evidence enables a feature whose failure is terminal.
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

One record per (`source_id`, `model_id`, `feature`). Absence of a record is `unknown`. The persisted shape is
below, and [`model-hub-contracts/capability-evidence.schema.json`](model-hub-contracts/capability-evidence.schema.json)
is its JSON Schema. The store is `core/handlers/model_hub/capability_evidence.py`.

| Field | Rule |
| --- | --- |
| `version` | `1` |
| `source_id`, `model_id` | non-empty |
| `feature` | `{kind: "effort", name}` or `{kind: "hosted_tool", name: "web_search" \| "x_search" \| "image_generation", aspect: "call" \| "replay"}`; `image_generation` has `call` only |
| `status` | `verified` or `unsupported` |
| `basis` | `probe` (a live probe the user started) or `observed` (learned from a real request's error) |
| `observed_at` | UTC timestamp |
| `engine` | engine release at that time (the shipped engine manifest's asset release); a change makes `probe` evidence stale (§3.3) |
| `shape` | version of the request shape that produced the record; a change makes `probe` evidence stale |
| `detail` | required when `basis` is `observed`: the HTTP status and upstream machine code, e.g. `400 invalid_value`, at most 64 characters, never upstream text |
| `source_identity` | digest of `retry.source_identity` when the record was written |

`observed` evidence is always `unsupported`.

Records live in the Model Hub state directory (`model_hub_capability_evidence.json`). They follow Source identity:
`retry.source_identity` covers kind, vendor, protocol, channel, base URL, credential and verification marker.
Changing any of those drops the Source's evidence: a record whose stored digest no longer matches reads as unknown
and is pruned at the next write.

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
    tool `web_search` `replay`.
- **Generic errors never create evidence.** That covers 5xx, overload, rate limit, auth, and any 400 that does not
  name the feature.
- **What happens to the request.**
  - An effort rule match fails the request as it does today. The next request skips that hop (§3.4).
  - A hosted-content rule match on a pinned request becomes `pin_unavailable`. Vibey re-sends the same request in
    portable form at once (`vibey-hosted-tools.md` §3.5).
- Negative `observed` evidence expires after 7 days. Relays change, and an expired record falls back to the priors.
  A user's re-verify replaces it at once.

### 3.3 Probes and re-verification

- **A probe is a real upstream call.** It costs money (about $0.01–0.05; the 2026-10-13 spike measured it). It
  runs only on an eligible credential, and a Claude subscription Source is never eligible
  (`vibey-hosted-tools.md` §3.4). It runs only on an explicit user action:
  - enabling a hosted tool for a Source (or choosing it as the proxy executor);
  - the Source card's *Re-verify*;
  - confirming one pending re-verification that the UI lists (below).

  A probe never runs as a silent background job.
- **The probe spec belongs to the feature.**
  - Hosted tools: [`vibey-hosted-tools.md`](vibey-hosted-tools.md) §3.2.
  - Efforts: one minimal request at that effort. Only the efforts a user actually selects are ever probed.
- **Probe evidence becomes stale** on an engine upgrade, a probe-shape change, or when it is 30 days old.
  - **Current-wire staleness** (the record's `engine` or `shape` differs from the current one) matters for hosted
    tools. Their positive evidence is needed because a native failure can be silent (§3.4). Evidence taken on another
    wire cannot vouch for this one, and a silent drop would never trip a narrow rule. So stale hosted-tool `call` and
    `replay` evidence counts as unknown for routing: proxy mode and synthetic replay, both lossless.
  - **Age-only staleness** keeps routing as it did: the wire Avibe sends has not changed.
  - **Effort evidence is unchanged.** It is negative only, and stale negative evidence still skips the hop until it
    expires.
  - The Source card marks stale records "re-verify", and Models lists them with one confirm action. After an engine
    upgrade the Tools tab says why native turned into proxy (`vibey-hosted-tools.md` §6.1).
- **Positive evidence is demoted** by a narrow negative rule (§3.2) on a later real request.

### 3.4 How layers resolve

| Consumer | Rule |
| --- | --- |
| Effort menu for a model on an Agent | each hop's declared tiers, then the union across hops. A hop's declared tiers follow `model-hub-reasoning-tiers.md`'s ladder: an upstream reasoning signal (the protocol's ladder, bounded by the model's catalog row when it has one), else the bundled backend row's efforts minus that row's `client_presets` (provenance `catalog`; it outranks a user list, with a visible override event), else the user's list, else models.dev `reasoning_options` when the catalog row is built. A persisted `catalog` list is also read without `client_presets`, since released builds wrote them, and Vibey's own model rows always drop them at config load (3.2.0 saved rows with Codex's `ultra`; Vibey is never the client a preset belongs to, and no provider documents `ultra` as a wire effort). A user-authored Source list is never filtered. The overlay's `wire_efforts` slots in before the backend row once a vendor needs it; no shipped entry sets it yet. A tier is omitted only if **every** hop that can serve the model has negative evidence for it: every hop the route would try or wait for, exactly the hops whose refusal ends a request in `effort_unsupported`. A tier with negative evidence on some hops is shown with "not on: <Source>". Evidence is keyed by the token a client puts on the wire, which can differ from the menu value: Vibey sends menu `minimal` to Anthropic as `low`. So Model Hub decides both in menu values, where routing decides, and the picker's own read (`GET /api/models/agents/<backend>/models`, an Editor surface) carries the result per model as the response-only `effort_menu`. Vibey's adapters map each menu value through the one function their request builders use (`registry.sent_effort`); a native CLI sends the value as the menu names it. The chain API keeps each hop's read-only `refused_efforts` (`agent-chain.schema.json`) for the evidence detail. |
| Effort at request time | Forwarded unchanged (reasoning-intent). A hop with **negative** evidence for that effort is skipped for this request, as if it were not a candidate. If no candidate remains, the request fails before invocation with `effort_unsupported` naming the effort and the Sources, instead of a provider 400. Never clamped silently. |
| Hosted tool mode on a hop | `native` only with positive `call` evidence taken on the current engine and probe shape, and the user's toggle on; otherwise the gateway proxy (`vibey-hosted-tools.md`). A wrong native declaration is a terminal 400 or a silent drop; the proxy is lossless. So this feature needs positive evidence, unlike efforts. |
| Native payload replay | only to the same Source, and only while its `replay` evidence is positive and taken on the current engine and probe shape. The decision is made at every request from `HopResolution`; the transcript never stores it. |
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
| `hosted_tools` | optional; protocol (`anthropic`, `openai_responses`, `openai_chat`, `google`) → tool (`web_search`, `x_search`, `image_generation`) → `{declaration, price?, include?}`. `declaration` is required, has a `type`, and is sent verbatim. `price` is optional: absent means no list price is known, never zero. OpenAI prices image generation per image token, not per image, so its `image_generation` entry has none. `include` lists Responses `include` values the tool needs (OpenAI web search: `web_search_call.action.sources`). |
| `price` | `unit` (`call`, `search`, `post`, `profile`), `usd_per_1k` ≥ 0, `as_of` (date) and `url` (https), all required; optional `extra[]` of `{unit, usd_per_1k}`. A per-image unit, whose entries would also carry `quality` and `size`, waits for a vendor that lists a per-image price. |
- The overlay records what the **provider** offers. Whether a Source (a relay, a subscription) carries it is
  evidence.
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
| hosted tool, `call` verified | native, with date | "Web search · native · verified 10-13" |
| hosted tool, no evidence or unsupported, while the tool is enabled | via proxy | "X search · via proxy (xAI API key)" |
| hosted tool, `call` unsupported | not supported, with basis | "X search · not supported on this Source" |
| effort unsupported | not supported, with basis | "ultra · not supported on this Source (400, 10-13)" |
| probe evidence stale by age | re-verify | "Web search · verified 09-02 · re-verify" |
| hosted-tool evidence from another engine or probe shape | via proxy, re-verify | "Web search · via proxy · engine upgraded · re-verify" |

- Unknown draws nothing. A healthy unknown is the normal case and must not look like a warning (V6 rule:
  exceptions only).
- Each probe-backed chip has a *Re-verify* action with its cost hint ("about $0.02").

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

1. **A per-call cost hint** in Models (the Tools tab and the probe confirm) and on the use-time card:
   - native: the tool fee per call plus a token estimate;
   - proxy: the same plus the executor model's call.

   All of it is computed by the one pricing owner, `pricing.py` (`hosted_response_cost`, reached through
   `ModelHubService.hosted_call_cost`, which reads only the price table the usage writer already holds, so pricing a
   response does no I/O). It is stored once per response: as the message's
   `hosted_summary.cost` for native calls, and as `ProxyExecution.cost` for a proxied call, which is its own executor
   response. A provider-reported figure is never copied into individual calls. Neither the
   executor nor an adapter prices anything itself.
2. **Executor choice.** The Tools tab lists the verified executor candidates with their cost hints and billing mode.
   The user picks one; the default is the first verified candidate in Vibey's Source order. Avibe does not
   auto-optimize cost in Phase 1, because relay prices are unknowable.
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
3. No probe runs without a user action, and every probe action shows its cost before it runs.
4. Changing a Source's identity drops its evidence. An engine upgrade marks probe evidence stale; it does not
   delete it. Stale hosted-tool evidence from another engine or probe shape never authorizes native mode or native
   replay.
5. #2539: a Source that rejected `ultra` shows "ultra · not supported on this Source". The next request at `ultra`
   either skips that hop or fails before invocation with `effort_unsupported`, never a provider 400.
6. Valuation applies context tiers when a call's input tokens exceed the tier size. A model with tiers and no flat
   price is still not priced as zero.
