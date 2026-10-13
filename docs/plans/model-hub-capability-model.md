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
| **Overlay** | model id, per protocol | Avibe. `vibe/data/backend_models.json` becomes the one overlay | corrections and additions to the base: wire effort tiers where models.dev is wrong or late; **client presets** that never go on the wire (Codex `ultra`); **hosted tools** the provider offers for the model on that protocol (§4); hosted-tool prices (§6) | per-Source truth; an allowlist |
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
   - It gains per-entry `wire` versus `client_preset` effort marking and a `hosted_tools` map.
   - No second catalog file is introduced for Vibey.
   - Native backends keep reading their menu from the same rows; Vibey reads only wire values.

## 3. Evidence

### 3.1 Record

One record per (`source_id`, `model_id`, `feature`). `feature` is `{"kind": "effort", "name": "ultra"}` or
`{"kind": "hosted_tool", "name": "web_search", "aspect": "call" | "replay"}`.

| Field | Meaning |
| --- | --- |
| `status` | `verified` or `unsupported`. Absence of a record is `unknown`. |
| `basis` | `probe` (a live probe the user started), or `observed` (learned from a real request's error) |
| `observed_at` | when the evidence was produced |
| `engine` | engine release at that time; an engine upgrade makes `probe` evidence stale (§3.3) |
| `shape` | version of the request shape that produced it; a probe-shape change makes it stale |
| `detail` | bounded machine summary: HTTP status and upstream machine code, e.g. `400 invalid_value`, `400 unsupported_tool_type`. Never upstream text beyond a code. |

Records live with the Source's runtime state (`model_hub` state dir). They follow Source identity:
`retry.source_identity` covers kind, vendor, protocol, channel, base URL, credential and verification marker.
Changing any of those drops the Source's evidence.

### 3.2 Learning from real errors (negative evidence)

- **Narrow rules, one per feature.** A classifier rule maps an upstream error to exactly one (Source, model,
  feature) only when the error names that feature:
  - `invalid_request_error` / `invalid_value` whose message names the request's `reasoning.effort` value → effort;
  - `Unsupported tool type: <type>` for a type the request declared → hosted tool `call`;
  - `Invalid encrypted_content` inside a hosted result block the request replayed → hosted tool `replay`.
- **Generic errors never create evidence.** That covers 5xx, overload, rate limit, auth, and any 400 that does not
  name the feature.
- **The request still fails as it does today.** Evidence changes the next request: the same mistake is not repeated.
- Negative `observed` evidence expires after 7 days. Relays change, and an expired record falls back to the priors.
  A user's re-verify replaces it at once.

### 3.3 Probes and re-verification

- **A probe is a real upstream call.** It costs money (about $0.01–0.05; the 2026-10-13 spike measured it), so it runs only on an
  explicit user action:
  - enabling a hosted tool for a Source (or choosing it as the proxy executor);
  - the Source card's *Re-verify*;
  - confirming one pending re-verification that the UI lists (below).

  A probe never runs as a silent background job.
- **The probe spec belongs to the feature.**
  - Hosted tools: [`vibey-hosted-tools.md`](vibey-hosted-tools.md) §3.2.
  - Efforts: one minimal request at that effort. Only the efforts a user actually selects are ever probed.
- **Probe evidence becomes stale** on an engine upgrade, a probe-shape change, or when it is 30 days old.
  - Stale evidence still routes as it did.
  - The Source card marks it "re-verify", and Models lists the stale records with one confirm action.
- **Positive evidence is demoted** by a narrow negative rule (§3.2) on a later real request.

### 3.4 How layers resolve

| Consumer | Rule |
| --- | --- |
| Effort menu for a model on an Agent | each hop's declared tiers, then the union across hops. A hop's declared tiers are its Source model's persisted `reasoning_efforts` when their provenance is `user` or `upstream`; otherwise overlay `wire` tiers; otherwise models.dev `reasoning_options`. User-authored lists, including free-form names, are never replaced or filtered by a prior; only an overlay-sourced list drops `client_preset` names. A tier is omitted only if **every** hop that can serve the model has negative evidence for it. A tier with negative evidence on some hops is shown with "not on: <Source>". |
| Effort at request time | Forwarded unchanged (reasoning-intent). A hop with **negative** evidence for that effort is skipped for this request, as if it were not a candidate. If no candidate remains, the request fails before invocation with `effort_unsupported` naming the effort and the Sources, instead of a provider 400. Never clamped silently. |
| Hosted tool mode on a hop | `native` only with positive `call` evidence and the user's toggle on; otherwise the gateway proxy (`vibey-hosted-tools.md`). A wrong native declaration is a terminal 400 or a silent drop; the proxy is lossless. So this feature needs positive evidence, unlike efforts. |
| Native payload replay | only to the same Source, and only while its current `replay` evidence is positive. The decision is made at every request from `HopResolution`; the transcript never stores it. |
| Display | §5 |

This fixes #2539 in three places:
- the overlay marks `ultra` a Codex `client_preset`, so it is not a Vibey wire tier;
- models.dev's wire list (`low … max`) is the base;
- the relay's real `400 invalid_value` records `ultra: unsupported` for that Source.

Even without the overlay fix, the next request would skip that hop or fail clearly.

## 4. Hosted tools in the overlay

`hosted_tools` per model row, per protocol. The value is the wire declaration the provider trains on and the
catalog's tool name (`vibey-hosted-tools.md` §2):

```json
"hosted_tools": {
  "openai_responses": {"web_search": {"type": "web_search"}},
  "anthropic": {"web_search": {"type": "web_search_20250305", "name": "web_search"}}
}
```

- Rows are keyed by model family through the existing model-id normalization, so a new model of a family inherits
  them.
- For xAI the row adds `{"x_search": {"type": "x_search"}}` under `openai_responses`.
- A provider that offers a tool only on some models lists it only on those.
- The overlay records what the **provider** offers. Whether a Source (a relay, a subscription) carries it is
  evidence.

## 5. What Models shows per Source

Each model row on a Source detail panel (`SourceDetailPanel`) gets capability chips. They reuse `chips.tsx` badges
and the existing tone vocabulary (`TimeHint` for dates):

| Evidence | Chip | Example |
| --- | --- | --- |
| hosted tool, `call` verified | native, with date | "Web search · native · verified 10-13" |
| hosted tool, no evidence or unsupported, while the tool is enabled | via proxy | "X search · via proxy (xAI API key)" |
| hosted tool, `call` unsupported | not supported, with basis | "X search · not supported on this Source" |
| effort unsupported | not supported, with basis | "ultra · not supported on this Source (400, 10-13)" |
| probe evidence stale | re-verify | "Web search · verified 09-02 · re-verify" |

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
| **Context-tier prices** (a higher rate past a context size) | models.dev `cost.tiers[]` with `{"tier": {"type": "context", "size": N}}`, plus the legacy `cost.context_over_200k`. On 2026-10-13: 630 and 517 models (e.g. `gpt-6-astra` above 272K: $20/$75; `grok-4.6` above 200K: $4/$12) | **not used.** `model_price` reads only the flat fields, so valuation understates long-context use. Fix: when a call's input tokens exceed `size`, the tier's rates apply to the whole call, as the vendors bill. The ledger already has per-call input tokens. |
| **Hosted-tool call prices** | overlay `hosted_tools.<protocol>.<tool>.price`: unit and USD per 1,000 units | new. models.dev has none; the open proposal anomalyco/models.dev#8707 adds `cost.web_search = {search, prompt}` (no maintainer response yet) |
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
   delete it.
5. #2539: a Source that rejected `ultra` shows "ultra · not supported on this Source". The next request at `ultra`
   either skips that hop or fails before invocation with `effort_unsupported`, never a provider 400.
6. Valuation applies context tiers when a call's input tokens exceed the tier size. A model with tiers and no flat
   price is still not priced as zero.
