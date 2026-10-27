# Keyless Exa as a default web search service

Status: **design**, 2026-10-22. This document is the keyless-Exa extension to
[`web-search-services.md`](web-search-services.md), which remains the source of
truth for Tavily, keyed Exa, official DeepSeek, the common search record, and the
service lifecycle. It also extends [`vibey-hosted-tools.md`](vibey-hosted-tools.md).
Where this document names keyless Exa, it is more specific than those documents.
It does not change the rule that a native-capable hop prefers the model's own
search.

This is a **new plan**, rather than another section of the services design,
because keyless Exa has a different custody boundary, protocol (Streamable HTTP
MCP rather than a keyed REST call), rate-limit signal, and built-in Source
representation. Keeping it separate lets the original vendor catalog and its
keyed-Exa row remain readable while this plan records the live upstream facts
that can change. The cross-references and the services-design §7 edit keep the
combined design unambiguous.

## Owner decisions

The following decisions are binding and are reproduced verbatim:

1. **Keyless Exa is a built-in web search service, on by default.** It needs no key and no setup, so every user has a search service even with none configured.
2. **Default automatic order:** the user's own search services (Tavily, keyed Exa, official DeepSeek) → **keyless Exa** → model executors. Like every row, the user can reorder it and turn it off.
3. **It stays a fallback even when the user has services**, including a keyed Exa.
4. **No "nothing new turns on by itself" promise.**
   - Delete the commitment in services-design §7 ("Nothing new turns on by itself except decision 4…").
   - Docs state the defaults and where a query goes as facts, with no general pledge about future defaults.
   - Keep §4.1's per-vendor `auto_enroll` mechanics unless the new design needs them changed.
5. **Terms risk is accepted.** Exa wants distribution, and we adapt quickly if it tightens. Identify our traffic honestly; don't disguise it.

`Prefer the model's own search` is unchanged: a native-capable hop still
searches natively first.

## 1. Recommendation at a glance

Implement keyless Exa as a **built-in Source at the search resolver boundary**:

- Stable Source id: `src_exakeyless`.
- Search service id: `exa/mcp`.
- Endpoint: `https://mcp.exa.ai/mcp` with the explicit tool selection
  `web_search_advanced_exa,web_fetch_exa`; the simple `web_search_exa` tool is
  still documented and probed, but phase 1 selects the advanced tool's
  structured result form to avoid parsing unescaped highlight delimiters.
- No credential, no Vault operation, no add flow, and no persisted credential
  Source row. It is still a Source for ordering, health/cooldown, attempts,
  usage, activity, and UI purposes.
- Saved setting: `model_hub.hosted_tools.web_search.services.src_exakeyless`.
  Missing means `{enabled: true, results: 5}`.
- Default automatic order: tool-only search Sources in `service_order`, then
  enabled auto-enrolled official model Sources with a search API (currently an
  official DeepSeek Source), then `src_exakeyless`, then model executors.
  Existing saved orders are augmented at read time under §4.1; no migration
  write is required.
- Search price and recorded cost are exactly `$0`; UI says **免费** rather than
  displaying a zero dollar amount. Every sent search `tools/call` increments
  usage `requests`, even though `billable_requests` stays zero.

This is not a second kind of search capability. The resolver, mixed order,
`executor_candidates`, `HostedRecord.attempts[]`, `ServiceExecutorOrigin`,
health projection, usage, and UI all treat it as a search Source. The only
storage exception is that a no-credential built-in is not forced into the
user-owned `model_hub.sources[]` list: there is no key to replace, no user
Source to remove, and no credential health to persist. That avoids creating a
fake key row while preserving the Source contract where it matters.

## 2. Official facts and dated evidence

Facts below were checked on **2026-10-22** unless a source commit or document
revision is explicitly named. URLs are included so the implementation lane can
re-check them at push time.

### 2.1 Hosted MCP and tool inventory

Sources:

- Exa MCP reference and quickstart: [`/docs/reference/exa-mcp`](https://exa.ai/docs/reference/exa-mcp),
  [`/docs/get-started/exa-mcp`](https://exa.ai/docs/get-started/exa-mcp), retrieved
  2026-10-22.
- Exa MCP landing page: [`exa.ai/mcp`](https://exa.ai/mcp), retrieved 2026-10-22.
- Official source: [`exa-labs/exa-mcp-server`](https://github.com/exa-labs/exa-mcp-server),
  commit `0b499f095c4ca145eee07b09137641c037d58449`, authored 2026-10-19.

The hosted endpoint is Streamable HTTP at `https://mcp.exa.ai/mcp`. Exa's docs
say no API key is required to get started. The default hosted tools are:

| Tool | Current availability | Input shape relevant to Avibe | Phase |
| --- | --- | --- | --- |
| `web_search_exa` | Enabled by default | `query: string`; `numResults: number` is optional. The live endpoint on 2026-10-22 also advertised a required `objective: string`; adapt to the schema returned by `tools/list` and send `objective = query` only when it is required. | Recorded upstream shape; not selected for phase 1 |
| `web_fetch_exa` | Enabled by default | `urls: string[]`; optional `maxCharacters: number`. It calls Exa contents/crawling and returns clean text/Markdown. | 2 |
| `web_search_advanced_exa` | Opt-in with a `tools` URL parameter | `query`, `numResults`, filters, dates, highlights, summaries, freshness, and subpage-crawl controls. | 1 (selected canonical tool) |

The source implementation of `web_search_exa` sends an Exa Search request with
`type: "auto"`, a default of 10 results, and `contents.highlights: true`.
The public Search API schema caps `numResults` at **100**. Avibe's setting is
independent of the model's arguments: default 5, user-configurable 1–100, and
sent as `numResults`.

The source implementation of `web_search_exa` returns one MCP text content
block. Its text is a sequence of result sections containing `Title`, `URL`,
`Published`, `Author`, and `Highlights`; the source falls back to `Text` only
when highlights are absent. Avibe records this as a supported upstream shape,
but does not use it as the canonical phase-1 wire format because its unescaped
separator cannot distinguish a page highlight from a new result section.

Phase 1 selects `web_search_advanced_exa` by including it in the endpoint's
`tools` parameter. The official source returns that tool's payload as JSON text
with a `results` array; its schema supports `enableHighlights` and
`highlightsMaxCharacters`. Avibe sets `enableHighlights: true`, bounds
`highlightsMaxCharacters`, sets `textMaxCharacters: 1` as a defensive
one-character wire bound, and ignores the returned `text`, summary, context,
and subpage fields. The canonical record therefore remains highlights-only and
never stores page content, while the JSON result shape is unambiguous.

The source implementation of `web_fetch_exa` accepts one or more URLs and an
optional per-page character cap, calls `/contents`, and formats returned page
text as Markdown. Avibe reserves this for phase 2; see §9.

The official source's `web_search_advanced_exa` schema says `numResults` is
1–100; its input also includes `enableHighlights`,
`highlightsMaxCharacters`, and `textMaxCharacters`. The live keyless
`tools/list` response on 2026-10-22 advertised the advanced tool only when it
was selected through the endpoint's `tools` URL parameter. The simple
`web_search_exa` remains the documented/probed fallback shape, but phase 1
requires the advanced tool so the adapter can consume its structured JSON
payload without trusting unescaped text separators.

### 2.2 Live keyless probe

The probe ran from the Avibe development machine on **2026-10-22**, without an
API key, OAuth token, or `x-api-key`. It used twelve JSON-RPC requests in total
(the original seven-request probe plus a five-request follow-up for
`web_fetch_exa`), well below the requested limit of 20, and did not try to
exhaust a limit.

1. The default endpoint initialized with HTTP 200 and
   `Content-Type: text/event-stream`; it returned `Mcp-Session-Id`, protocol
   version `2025-06-18`, and server `exa-search-server` version `3.5.0`.
2. The default `tools/list` returned `web_search_exa` and `web_fetch_exa`.
   The live `web_search_exa` schema required `query`, `objective`, and allowed
   optional `numResults`, even though the checked source commit's local tool
   definition only requires `query` and optionally accepts `numResults`.
   The client must therefore read the live schema and adapt rather than
   hard-code one version.
3. A keyless `web_search_exa` call with two results and an honest
   `x-exa-source: avibe-keyless-probe` header returned HTTP 200 SSE. The JSON-RPC
   result contained `content[0].type = "text"`, and the text contained title,
   URL, published/author fields, and highlights. It also carried `_meta.searchTime`.
4. A keyless `web_fetch_exa` call for the official Exa MCP documentation returned
   HTTP 200 SSE and a text/Markdown result with the requested short character
   limit.
5. An opt-in `web_search_advanced_exa` `tools/list` returned the advanced schema,
   including `numResults` 1–100 and `enableHighlights`. The subsequent keyless
   call was rate-limited without deliberately exhausting the service: the HTTP
   response was 200 SSE, but the JSON-RPC result contained
   `_meta.ai.exa/rateLimited: true` and a human-readable free-MCP rate-limit
   message. This is a live example of a JSON-RPC/application-level limit signal
   in addition to the HTTP 429 path documented and implemented by Exa.

6. A five-request follow-up reused the same keyless endpoint with a fresh
   session: `initialize` returned HTTP 200 SSE and protocol `2025-06-18`,
   `notifications/initialized` returned HTTP 202 with an empty body,
   `tools/list` advertised `web_fetch_exa` with required `urls` and optional
   positive `maxCharacters`, and the two fetch calls below stayed keyless.
7. Fetching `https://exa.ai/docs/get-started/exa-mcp` with
   `maxCharacters: 240` returned HTTP 200 SSE and one text block containing
   Markdown-style `# title`/`URL`/page-text output plus `_meta.searchTime`.
8. Fetching `https://example.invalid/avibe-keyless-exa-probe` with the same
   bound returned HTTP 200 SSE with `isError: true` and
   `Error fetching URL(s): ...: CRAWL_UNKNOWN_ERROR`; the server did not turn a
   failed crawl into an empty successful result.

The live probe establishes the search and fetch shapes plus the observed
application-level rate-limit signal; it is not a CI fixture and must not be
repeated in CI.

### 2.3 Rate limits and headers

Official facts are not one single numeric contract:

- Exa's hosted-MCP documentation says anonymous use is free and rate-limited,
  and its troubleshooting section describes a 429 free-rate-limit error, but it
  does **not** publish a numeric anonymous MCP quota.
- The official hosted-server source commit named above configures anonymous
  IP-based defaults of **2 QPS** using a sliding one-second window and **50
  requests/day** using a fixed one-day window. Both are environment-configured
  defaults, not a public guarantee; the source returns HTTP 429 with JSON-RPC
  error code `-32000`, `Retry-After` seconds, and `X-RateLimit-Reset` as a
  Unix-epoch **millisecond** value on that path (the hosted source passes its
  Upstash reset timestamp through unchanged).
- Exa's general API billing documentation, retrieved 2026-10-22, documents a
  **10 QPS** default for authenticated `/search` and other API endpoints. That
  is not the anonymous hosted-MCP limit and must not be used as the keyless
  service's promise.
- Third-party claims of 3 QPS/150 per day and approximately 2 QPS/50 per day
  are therefore not equivalent sources. The design relies on no fixed quota.
- The same official `api/mcp.ts` source applies those buckets to every JSON-RPC
  `tools/call` regardless of tool name and counts batched tool calls; it skips
  `initialize`, `tools/list`, and other protocol methods. Therefore keyless
  `web_search_exa` and `web_fetch_exa` share the per-IP buckets rather than
  having separate search/fetch quotas.

The client recognizes all of these observed/officially specified signals:

1. HTTP 429, honoring `Retry-After` (seconds or HTTP date) and, when present,
   the later of the relevant reset headers. For the checked Exa hosted server,
   `X-RateLimit-Reset` is an absolute Unix-epoch timestamp in **milliseconds**,
   not seconds: parse a finite integer as milliseconds, convert it to the
   cooldown deadline, and ignore malformed or already-past values;
2. a JSON-RPC error with the hosted-MCP rate-limit message, or with code
   `-32000` **and** that bounded message. The generic `-32000` code alone is
   not sufficient: the checked hosted source also uses it for HTTP 401
   authentication errors;
3. a successful HTTP response whose result metadata contains
   `_meta.ai.exa/rateLimited: true`, as observed on 2026-10-22;
4. the hosted-MCP free-rate-limit text when a deployed version omits the
   metadata marker, but only in an error-shaped result (`isError: true`, a
   JSON-RPC error, or an otherwise response-level error envelope). Never match
   that phrase inside a valid result section or highlight.

A rate-limit signal is a **cooldown**, never a credential failure. It maps to
`service_rate_limited` (the operational failover class is
`service_unavailable`) with a rate-limit detail and `cooldown_until`. If no
server advice is present, reuse services-design §4.2's conservative 30-second
cooldown, doubling on consecutive failures to 300 seconds. This is a client
backoff, not a claim about Exa's window. A later success resets it.

The hosted source accepts `x-exa-source` and passes an integration identity to
Exa's Search API as `x-exa-integration`; it also supports client/session
metadata headers. Avibe sends `x-exa-source: avibe` and MCP `clientInfo.name:
"avibe"` (with the Avibe version), and does not imitate another plugin or hide
its traffic. The adapter may additionally send the MCP session id and the
bounded `x-exa-mcp-client` metadata that the official source supports.

### 2.4 Terms and privacy facts

Exa's current Terms of Service, retrieved from [`exa.ai/terms`](https://exa.ai/terms)
on **2026-10-22**, govern API and Search Engine use. They require API use to
follow the documentation and call-volume limits. They also say that submitted
User Input and Output may be accessed, used, hosted, cached, stored,
transmitted, displayed, modified, and disclosed as needed to provide, develop,
and improve Exa products and services or meet legal obligations. The terms
contain restrictions on misuse, overloading, unauthorized automation, resale,
and competitive-product use. This design accepts the distribution risk under the
owner decision and makes the integration identifiable; it does not promise
that these terms will remain unchanged.

For keyless Exa specifically:

- The query, requested result count, and the MCP protocol metadata are sent
  from the Avibe runtime host directly to `mcp.exa.ai`; they do not go through
  `avibe.bot` and no key is sent.
- For the normal local install, Exa sees the public source IP of the user's
  machine/network egress. The hosted source's anonymous limiter keys on the IP
  it receives. If the runtime is explicitly managed in an Enterprise VM, the
  VM's egress IP is the source instead.
- Exa returns result titles, URLs, publication/author metadata, and highlights.
  Avibe stores only the existing bounded `HostedSource` fields; it does not
  request or retain full page text in phase 1.
- Exa can see the honest integration identity `avibe`, the MCP client name and
  version, the MCP session id, and the request's normal network metadata. Avibe
  adds no separate Avibe account or user identifier and sends no credential;
  the query contents themselves may contain identifying information.

These are facts and current upstream terms, not a general promise that future
Avibe defaults or Exa processing practices cannot change.

## 3. Representation and persisted settings

### 3.1 Built-in Source and catalog row

Add a built-in search-service catalog row to the service catalog consumed by
`service_vendors[]`; do not overload the keyed REST vendor row `exa/search`.
The row has these stable values:

| Field | Value |
| --- | --- |
| Source id | `src_exakeyless` |
| Catalog/vendor id | `exa-mcp` (synthetic built-in vendor id, distinct from keyed vendor `exa`) |
| Search service id | `exa/mcp` |
| Display label | `Exa（免费）` in Chinese; `Exa (free)` in English |
| Endpoint | `https://mcp.exa.ai/mcp` with `tools=web_search_advanced_exa,web_fetch_exa` |
| Transport | Streamable HTTP MCP |
| Auth | none |
| Default results | 5 |
| Maximum results | 100 |
| Price | `usd_per_1k: 0`, unit `search`, no extras |
| Free-tier fact | `免费（按 IP 限流）` / `Free; IP rate-limited` |
| Free check | none |
| Tool-only | true |
| Built-in | true; always available in the catalog, not addable or removable |

The built-in row uses vendor id `exa-mcp` and search `service_id: exa/mcp`.
The slash-free vendor id is required by the catalog; the slash-bearing service
id is the adapter/usage identity. Persisted `executor_order` and its role map
use the Source id `src_exakeyless`, never `exa/mcp`; `exa/mcp` is not a saved
executor id. It is not an overload of the keyed REST vendor row `exa` /
`exa/search`.

Its `source_ids` contains `src_exakeyless`; its `facts` adds `built_in: true`
and `requires_setup: false` (unknown fields remain ignored by older readers).
The candidate's `model_id` is `exa/mcp`, not a model name. The candidate keeps
the existing service-candidate shape and `billing: "metered"` for schema
compatibility, but its zero catalog price and every record cost are exact zero;
the UI renders **免费**, never “按量付费”. If the contract can add a
non-breaking `free: true` display fact, use it; it must not change usage
semantics.

A built-in Source has a synthetic standby/active/cooling state in the resolver,
not a credential health state. It can be disabled, cooling, or unavailable due
to transport/server/malformed-output errors, but it can never be
`needs_action: invalid_key`, `balance`, or `limit` because it has no credential.

### 3.2 Config

Use the existing per-service settings map. No new top-level switch is needed:

```json
{
  "model_hub": {
    "hosted_tools": {
      "web_search": {
        "services": {
          "src_exakeyless": {"enabled": true, "results": 5}
        }
      }
    }
  }
}
```

The effective defaults when the section, entry, or field is absent are
`enabled: true` and `results: 5`. Writes validate `results` from 1 through 100;
reads clamp a saved value to the current maximum without failing startup, as in
services-design §3.2. `enabled: false` is the ordinary service opt-out and
keeps the row in the order/read as `disabled`. The settings read and PUT paths
recognize `src_exakeyless` through the built-in `exa-mcp` catalog branch before
resolving ordinary `config.sources`; a PUT round-trip may save its
`{enabled, results}` override without creating a credential Source, and a read
returns the saved partial override while effective defaults remain in the
candidate settings.

#### Synthetic identity boundary

The synthetic identity is one cross-boundary contract, not a collection of API
exceptions. Define one catalog identity for (`src_exakeyless`, `exa-mcp`,
`exa/mcp`) and use it at every read/write and reconciliation boundary while
continuing to omit a credential row from `model_hub.sources[]`:

- V2 config loading must treat `src_exakeyless` as a live synthetic id when
  parsing `services`, so a saved partial override such as
  `{"enabled": false}` is retained and clamped across restart. It must do the
  same for `executor_order_kinds` when a saved `executor_order` names the
  built-in service role. V2 serialization must emit those entries again from
  the synthetic identity rather than filtering them through `config.sources`.
- The hosted-tools GET/PUT paths, candidate resolution and admission, and the
  saved-order role map use the same Source id for settings, placement, and
  disabled/cooling state; `exa/mcp` remains only the adapter/usage service id.
- Cooldown reconciliation and the built-in Retry now branch use the synthetic
  identity; capability-evidence write/prune, usage-label resolution, and
  hosted-activity current-state projection do too. No config save, unrelated
  settings write, later-candidate health write, restart, or read-time projection
  may drop the saved opt-out, deliberate role placement, active cooldown,
  positive evidence, usage row, or current activity state.

The required acceptance is a V2 serialize/reload round trip for the disabled
override and a second unrelated config/health write while that override and a
keyless cooldown exist. This inventory is the implementation boundary; adding
only another API branch is incomplete.

The synthetic cooldown is keyed by (`src_exakeyless`, `exa/mcp`) and has a
catalog identity independent of `model_hub.sources[]`. `ServiceCooldowns.reconcile`
receives that synthetic identity alongside `config.sources`, and every config-save
reconciliation and later candidate-health write must retain the identity and its
active cooldown. Saving settings or settling a later candidate must not
silently re-admit keyless Exa before `cooldown_until`. A successful keyless
attempt clears only its own cooldown generation.

The built-in Source is not appended to persisted `service_order` on creation; it
has no creation event. Read-time automatic resolution places it after
`service_order`'s tool-only Sources and enabled auto-enrolled model Sources.
If a user explicitly reorders the mixed list, the server saves
`src_exakeyless` in `executor_order` and
`executor_order_kinds[src_exakeyless] = "service"`.

### 3.3 Older builds and saved orders

A release before this design treats `src_exakeyless` as an unknown Source id.
Its existing `executor_order` reader skips the id and continues with the named
Sources it knows; it does not call Exa and it does not fail configuration load.
Its next settings write may drop the unknown id and the new `services` fields,
which is the existing lossy pre-feature write boundary. A new build re-inserts
keyless Exa at read time when the saved order does not deliberately name it;
there is no migration write.

A deliberate saved placement is recognized only when the current entry names
`src_exakeyless` and its role map says `service`. A released order without
`executor_order_kinds` is treated as a model-only historical order, so keyless
Exa is placed by the automatic read-time rule rather than accidentally freezing
an old order.

## 4. Placement and candidate read

### 4.1 Automatic and saved order

With no saved `executor_order`, the effective order is exactly:

1. all tool-only search Sources in `service_order` (Tavily, keyed Exa, and any
   other tool-only services);
2. enabled official model Sources whose catalog search API is `auto_enroll`
   (currently official DeepSeek), in the existing Source order;
3. enabled `src_exakeyless`;
4. model executors in the existing automatic order;
5. disabled search rows at the existing disabled tail, including
   `src_exakeyless` when its `enabled` setting is false. Cooling, blocked, and
   temporarily unavailable enabled rows stay in their service position; state
   controls admission, not the editable order.

If the user saved an order, use services-design §4.1 unchanged:

- named entries in their deliberate saved positions stay there;
- unnamed or role-changed search services, including a newly available
  `src_exakeyless` and an auto-enrolled official DeepSeek search service, are
  inserted immediately before the first saved model executor, in automatic
  service order;
- unnamed model executors follow at the end;
- a saved keyless row can therefore be placed before a user's own service or
  after a model executor, because user reordering wins;
- `services.src_exakeyless.enabled: false` remains a visible disabled row and
  is never requested.

A keyed Exa and keyless Exa are distinct rows and distinct attempts. The
keyless row remains in the fallback chain even when keyed Exa exists, unless
the user turns it off or deliberately moves it.

`prefer_native` remains the first decision: a native-capable hop searches
natively and never reaches this chain for that hop. With `prefer_native: false`,
the function call enters the entire saved/effective order, including keyless
Exa.

### 4.2 Tools read

For `GET /api/models/hosted-tools`, `web_search` returns:

```json
{
  "services": {},
  "executor_order": null,
  "effective_order": ["src_tavily001", "src_exa000001", "src_deepseek001", "src_exakeyless", "src_model001"],
  "executor_candidates": [
    {
      "kind": "service",
      "source_id": "src_exakeyless",
      "model_id": "exa/mcp",
      "billing": "metered",
      "relay": false,
      "price": {"unit": "search", "usd_per_1k": 0, "extra": [], "as_of": "2026-10-22", "url": "https://exa.ai/docs/get-started/exa-mcp#authentication"},
      "settings": {"enabled": true, "results": 5},
      "state": {"status": "active"},
      "trial_pending": false,
      "cooldown_until": null
    }
  ],
  "service_vendors": [
    {
      "id": "exa-mcp",
      "name": "Exa（免费）",
      "price": {"unit": "search", "usd_per_1k": 0, "extra": [], "as_of": "2026-10-22", "url": "https://exa.ai/docs/get-started/exa-mcp#authentication"},
      "free_tier": "免费（按 IP 限流）",
      "results_max": 100,
      "source_ids": ["src_exakeyless"],
      "facts": {"tool_only": true, "auto_enroll": false, "free_check": false, "built_in": true, "requires_setup": false}
    }
  ]
}
```

`services` is the saved partial-override map, not a resolved-default map. In
this no-override example it is empty; the keyless candidate's
`settings: {"enabled": true, "results": 5}` carries its effective defaults.
`effective_order` always includes the built-in keyless row, even when the saved
map has no entry for it. An enabled row stays in its automatic position while
cooling, blocked, or temporarily unavailable so admission can record the skip.
A disabled keyless row remains in the disabled tail, and its candidate/settings
stay visible so the user can turn it back on. For `src_exakeyless`, an active
service cooldown is serialized in `cooldown_until` even when less than the
generic 60-second visibility threshold; the UI must be able to show the
rate-limit state and **Retry now** for every active keyless cooldown. No empty
search-service state is inferred from an empty `sources[]`.


## 5. Execution

### 5.1 MCP session lifecycle

The Model Hub owns the MCP client and keeps one in-memory session per Avibe
runtime, endpoint, authentication mode, and honest client identity—not per
MCP tool name. This makes the same initialized client reusable for the phase-2
`web_fetch_exa` tool without creating a second session or rate-limit burst. It
is not written to disk and is not shared with another runtime. A per-session
async lock covers only session establishment (`initialize`,
`notifications/initialized`, and the first `tools/list`) plus any required
schema refresh. Once setup is ready, `tools/call` requests run concurrently
under independent JSON-RPC ids; the service layer caps active keyless Exa
calls at three per runtime to respect the shared per-IP limit.

The first call, within the one service attempt's existing **15-second search**
cap (or the designed **30-second fetch** cap), does:

1. connect over HTTPS to the pinned `mcp.exa.ai` host;
2. send MCP `initialize` with the supported protocol version, no capabilities
   beyond what Avibe uses, and honest `clientInfo` (`name: "avibe"`, the
   running Avibe version);
3. retain the returned `Mcp-Session-Id` and negotiated `protocolVersion`,
   reject a version Avibe does not support, and send
   `notifications/initialized`;
4. call `tools/list`, retain the complete tool schema for this session, and
   require `web_search_advanced_exa` for phase 1; phase 2 may dispatch
   `web_fetch_exa` through the same session after validating that tool; then
5. send one `tools/call` for the selected tool.

A `notifications/initialized` notification has no JSON-RPC result body; accept
its conforming HTTP 202 response with an empty body as success. Do not apply
that empty-body exception to `initialize`, `tools/list`, or `tools/call`, which
must return a matching JSON-RPC response.

Later calls reuse the session and cached `tools/list`; a warm call sends no
second listing. A `notifications/tools/list_changed` message, a re-established
session, or a tool-call schema error invalidates the cache, and the next call
refreshes it under the setup lock before mapping arguments. The refresh is not
a new search request and does not increment usage. A missing/expired session is
reinitialized once before the request. If a session-bound
`notifications/initialized`, `tools/list`, or `tools/call` exchange receives
the MCP session-not-found response (the standard HTTP 404/session error, not
an endpoint/protocol 404), clear the stale session, initialize again, resend
`notifications/initialized`, and replay the affected pre-tool request once
within the original deadline; this is safe because the server has not accepted
the tool call. Once the replay's `tools/call` request bytes begin, Avibe never
retries it automatically: a retry could duplicate a search and a usage request.

Every exchange uses `Accept: application/json, text/event-stream`,
`Content-Type: application/json`, and the retained negotiated
`MCP-Protocol-Version`, plus the session header when present and
`x-exa-source: avibe`. The response parser accepts both the Streamable HTTP SSE
`event: message` / `data: <JSON-RPC>` shape and a single `application/json`
JSON-RPC response, matching the response id in either form. A
`notifications/tools/list_changed` notification is not discarded as
unrelated: it invalidates any cached schema, and the next call refreshes
`tools/list` before building arguments. A schema error from `tools/call` uses
the same invalidation path. The implementation does not depend on an always-
open idle receive stream; the cached session schema remains authoritative until
one of these invalidation events. The 8 MiB existing response-body cap applies
to either response form; notifications are processed only within that cap.

No API key, bearer token, Vault reference, or user credential is present in the
MCP request. The adapter is still the custody/transport boundary and returns
only status, bounded headers, receipt time, and bounded JSON/text data to the
search producer.

### 5.2 Request mapping and schema adaptation

The canonical Avibe call remains exactly `{query}`. The frozen per-service
`results` setting is mapped to the advanced tool's `numResults` and never
exposed as a model argument.

Build the phase-1 MCP arguments from `tools/list`:

```json
{
  "query": "<canonical query>",
  "numResults": 5,
  "type": "auto",
  "enableHighlights": true,
  "highlightsMaxCharacters": 1000,
  "textMaxCharacters": 1
}
```

Send only fields present in the live advanced schema. `enableHighlights` is
required by this design; `textMaxCharacters: 1` is the smallest positive bound
accepted by the source wrapper and prevents full page text from crossing the
wire. Do not send filters, summaries, subpage, crawl, or context fields in
phase 1. If the selected tool is absent or does not advertise all required
highlight and `textMaxCharacters` controls, classify the service as
`service_unavailable` rather than silently switching to the ambiguous text
formatter.

Use a **15-second** overall search service deadline, including a cold-session
initialize/list and any capped admission wait. Fetch keeps its designed
**30-second** service deadline. Connection/TLS setup has the existing 5-second
transport budget; each exchange receives only the remaining deadline. A warm
session gives `tools/call` the remaining budget without another `tools/list`.
The search chain's single 180-second budget remains authoritative.

### 5.3 Response mapping

`web_search_advanced_exa` currently returns one MCP text content block whose
text is a JSON object, not a stable JSON-RPC result object. Prefer
`structuredContent` if a future server provides it; otherwise parse the text as
bounded JSON and require an object with a `results` array. Before treating any
payload as a valid result, classify an error-shaped envelope in this order:

1. an exact catalog-named query-refusal tag/code in a JSON-RPC error or MCP
   tool error result, such as `PROHIBITED_CONTENT` or
   `CONTENT_FILTER_ERROR`, is `service_refused` with no cooldown;
2. a bounded free-rate-limit message in that same error-shaped envelope, or
   JSON-RPC code `-32000` paired with that message, or the
   `_meta.ai.exa/rateLimited` marker on an otherwise valid response, is
   `service_rate_limited` with cooldown. The generic `-32000` code alone is not
   a rate-limit signal;
3. any other JSON-RPC error, `isError: true` result, or response-level error is
   `service_unavailable` with cooldown.

This classification applies to HTTP 200 MCP tool errors as well as non-200
responses. Never search valid result titles, URLs, or highlights for these
phrases: arbitrary page content is not an error envelope. For each non-error
result, accept only an HTTP(S) `url` within the existing bounds, map bounded
`title`, `publishedDate`, and `highlights[]`, and convert the ordered highlight array to
one canonical snippet by joining non-empty highlights with `\n\n` and truncating
the final string to the existing **1,000-character** snippet bound. Do not emit
an array or silently keep only the first highlight. Preserve Exa ranking order
and cap to the frozen `results` count. Ignore `text`, `summary`, `context`,
`subpages`, and unknown fields even when present. A notice-bearing JSON payload
may contain a `notices` array before `results`; preserve no notice as a source
and do not confuse notice text with result content.

A valid JSON object with `results: []` is a completed record with no sources.
Malformed JSON, a missing/non-array `results`, a non-empty result without a
valid HTTP(S) URL, missing expected content/text, and an error-shaped envelope
with no recognized refusal or rate-limit signal are `service_unavailable`; never
return partial or fabricated sources. A result with a valid URL but no highlight
is allowed with no snippet; Avibe still has not stored page content.

## 6. Outcomes, records, and usage

Apply services-design §4.2 with the following keyless-specific clarifications:

| MCP/HTTP result | Chain | State |
| --- | --- | --- |
| Valid result, including empty results | Ends with `completed`; cost `$0` | No state change |
| Any rate-limit signal from §2.3: HTTP 429, `_meta.ai.exa/rateLimited: true`, or a bounded hosted-MCP rate-limit message in an error-shaped envelope; never JSON-RPC `-32000` alone | Next candidate; attempt code `service_rate_limited` (operationally a `service_unavailable` cooldown) | Service cooldown; never credential health |
| Stale MCP session-not-found 404 before the tool call is accepted | Reinitialize/list and replay once within the original deadline; if the replay fails, apply its actual outcome | Clear only the in-memory session; no credential health |
| Timeout, connection/TLS failure, or closed session after the admitted call begins | Next candidate, `service_unavailable` | Service cooldown; no credential health |
| Any HTTP 5xx or non-rate-limit JSON-RPC/server error | Next candidate, `service_unavailable` | Service cooldown; no credential health |
| 403 `PROHIBITED_CONTENT`, `CONTENT_FILTER_ERROR`, or another catalog-named query refusal, including the same tag inside an HTTP 200 MCP `isError: true` result | Next candidate, `service_refused` | No credential health and no cooldown |
| 3xx, 401, 403, 404 not recognized as stale-session or query refusal, 405, or other unexpected 4xx | Next candidate, `service_unavailable` | Service cooldown; never `invalid_key` |
| Malformed text, missing expected content, invalid URL-only result, or an error-shaped `isError`/JSON-RPC result with no recognized refusal or rate-limit signal | Next candidate, `service_unavailable` | Service cooldown; no credential health |

A rate limit is not `service_key_invalid`, `service_balance`, or
`service_limit`. It has no key to invalidate and no account balance to infer.
Persist it as the distinct `service_rate_limited` error code (with the
localized rate-limit copy) while applying the same cooldown/failover mechanics
as `service_unavailable`. Add that code to `HOSTED_ERROR_CODES`, the C-1
attempt readers/writers, the hosted-activity projection, and the English/Chinese
copy. This is needed so a later projection can still say
**Exa（免费）限流，已改用下一个** rather than guessing from ephemeral cooldown
state; it is never a credential blocker. `Retry-After` is honored in full; for
this Exa host, `X-RateLimit-Reset` is an absolute Unix epoch timestamp in
milliseconds, so the parser divides it by 1,000 (or compares in milliseconds)
and ignores malformed or past values. When both are valid, the later deadline
wins; for example, a fixture value such as `1800000000000` is parsed as epoch
milliseconds, not epoch seconds. The generic no-advice backoff is the existing
30-second then doubling cooldown, capped at 300 seconds.

A successful keyless attempt writes the existing positive service evidence with
`mode: "service"`, keyed by (`src_exakeyless`, `exa/mcp`, `web_search`). The
capability-evidence writer and pruner receive the synthetic built-in identity
alongside `config.sources`, so this success is retained and later reads expose
its latest service-mode evidence instead of dropping it as an unknown Source.
The hosted-activity projection uses the same synthetic catalog identity for
`_source_display`/current-state lookup. It must not treat an absent
`hub.sources` entry as superseded: `service_rate_limited` and
`service_unavailable` attempts keep their distinct fallback reasons and render
against `Exa（免费）` / `Exa (free)`.
The execution origin is the existing service variant:

```json
{"provider": "exa-mcp", "api": "search_api", "model": "exa/mcp"}
```

Every reached attempt has one `attempts[]` entry. The label is
`Exa（免费）` / `Exa (free)`, the kind is `service`, and every sent call has an
explicit `{ "usd": 0, "basis": "list" }` cost. A skipped cooldown or disabled
entry has no cost because it sent nothing. The activity projection renders
`通过 Exa（免费）`, not a model name.

Usage records one request for every sent search `tools/call`; initialize,
`tools/list`, and notifications are not search requests. Keyless Exa's
`billable_requests` and `reported_requests` remain zero, `api_cost_usd` is
zero, and no lower-bound flag is introduced by an interrupted free request.
The usage reader resolves `src_exakeyless` through the built-in catalog and
synthesizes its stable `Exa（免费）` / `Exa (free)` label and service identity;
it must never classify these rows as a removed Source merely because the
Source is not in `model_hub.sources[]`. Usage still exposes the request count
so the user can see how often the built-in fallback was used.

## 7. UI specification

This section is intentionally precise enough to draw in `design.pen`; this lane
does not edit that file. The implementation uses the existing localized i18n
keys and the existing Models → Tools card primitives.

### 7.1 Tools card and search order

Keep the existing Web Search card header, overall enable switch, status line,
and native-preference switch. In the visible **搜索顺序 / Search order** list:

- Always show **Exa（免费） / Exa (free)**, even when the user has no configured
  service. It has the kind pill **搜索服务 / Search service**.
- Its row has the free status **免费 · 无需密钥 / Free · no API key**, a second
  line **5 个结果 · 按 IP 限流 / 5 results · IP rate-limited**, and the price
  label **免费 / Free**.
- The row has the same up/down reorder controls as every other row and an
  inline switch labelled **用于网页搜索 / Use for web search**. Turning it off
  moves it to the disabled tail in the read and shows **未用于搜索 / Not used
  for web search**; it does not remove the row or erase its usage.
- A cooldown row remains in place and says, for example,
  **Exa（免费）暂时限流 · 稍后重试 / Exa (free) rate-limited · retry later**,
  with the existing `cooldown_until` detail and **立即重试 / Retry now** action.
  The action calls `POST /api/models/hosted-tools/web_search/services/src_exakeyless/retry`
  with the existing empty `{}` body; `service_id: "exa/mcp"` is derived inside
  the controller's built-in catalog branch, which clears only its in-memory
  cooldown and returns the normal empty success. It must not call the ordinary
  `_source` lookup, create a credential Source, or show a key repair. It never
  says “密钥无效 / invalid key”.
- The caption explicitly says the current automatic order:
  **已添加的搜索服务 → 已启用的官方 DeepSeek → Exa（免费） → 模型代搜；可拖动调整。/ Your added
  search services → enabled official DeepSeek → Exa (free) → model
  executors; drag to reorder.**
  The caption is a current behavior statement, not a promise about future
  defaults.
- Keep **添加搜索服务 / Add search service** below the order. The existence of
  built-in Exa means the no-service state does not claim that no search service
  exists.
- Add the egress fact below the order and before the custody note:
  **查询会从此 Avibe 运行时直接发送到 Exa（mcp.exa.ai）；Exa 可看到该连接的
  IP。/ Queries go directly from this Avibe runtime to Exa (mcp.exa.ai); Exa
  can see the connection's IP.**
- Keep the custody note, amended to say that keyless Exa has no key:
  **已添加服务的密钥由网关保管；Exa（免费）无需密钥，也不会把密钥交给模型或
  Agent。/ Added-service keys stay in the gateway; Exa (free) needs no key, and
  no key is given to the model or Agent.**

When the overall Web Search switch is off, the card's existing off state wins;
the per-row switch is still shown as saved configuration and does not imply a
request will run.

### 7.2 Add dialog and Source surfaces

The add dialog retains the keyed Exa REST option (`exa/search`) with its API-key
flow. Its fact line may say **Exa API key uses your Exa team plan; official Exa
docs say new teams receive $10 in monthly Free-tier credits.** This statement
is only for the keyed REST path; never present those credits as the keyless
MCP quota or as a promise that keyless requests spend the user's Exa account.

The built-in `exa/mcp` row is not a second addable key card. If the dialog
shows it in the vendor grid, render it as a non-actionable **已内置 · 免费 /
Built in · free** card with **无需密钥或设置 / No key or setup required** and
an **查看搜索顺序 / View search order** link. Do not offer Remove or Replace.

If the user opens the built-in detail from the order row, show:

- status: **免费 · 无需密钥** or the current cooldown;
- endpoint fact: `mcp.exa.ai`;
- settings: result count 1–100;
- this month's request count and `$0` cost rendered as **免费**;
- the enable switch and **立即重试** only during a cooldown;
- no key field, no credential-generation field, no free-check button, and no
  “重新启用” credential action.

The Sources list may show a non-removable **搜索服务 / Search service** row for
this built-in Source if the existing UI needs a row to open its detail. It must
say **不提供模型 / No models**, never appear in model route candidates, and
must not look like a user-entered credential.

### 7.3 Chat and IM

Reuse the shared hosted-activity projection. A successful card/line reads:

- Web: **通过 Exa（免费） · 5 个来源 · 免费 / via Exa (free) · 5 sources ·
  Free**.
- IM: **🔎 网页搜索 "…" · Exa（免费） · 5 个来源 · 免费 / 🔎 Web search "…" ·
  Exa (free) · 5 sources · Free**.

A fallback caused by a rate limit reads:

- **Exa（免费）限流，已改用 DeepSeek / Exa (free) rate-limited; switched to
  DeepSeek**.

A fallback caused by a malformed response or server error reads
**Exa（免费）暂时不可用，已改用下一个 / Exa (free) unavailable; tried the
next service**. The line must not suggest that an API key is missing or
invalid. A final all-candidates failure names the cooldown/unavailable remedy
from `attempts[]`.

## 8. Privacy and public-document copy

The public English and Chinese documentation shipped with activation states
these current facts:

1. Web search normally prefers a native-capable model's own search.
2. When the proxy chain is used, the current automatic order is tool-only
   search services in their saved order, enabled auto-enrolled official model
   Sources with search APIs (currently official DeepSeek), built-in keyless Exa
   (on by default), then model executors. Users can reorder the rows and turn
   off search-service rows; model-executor opt-out is not expanded by this plan.
3. Keyless Exa requires no key or setup, costs `$0`, and is rate-limited by Exa.
   Usage counts requests even though cost is free.
4. The query is sent directly from the Avibe runtime host to Exa's hosted MCP
   endpoint. For a local install, that is normally the user's machine and its
   public egress IP; it is not an `avibe.bot` proxy hop.
5. Exa receives the query, requested result count, MCP/client identification,
   normal connection metadata, and returns titles, URLs, metadata, and
   highlights. Phase 1 sends those query-relevant highlights/snippets to the
   model and stores them in the bounded search record; it does not send or
   retain the full page text from Exa.
6. Exa's current Terms govern the service and permit the processing/disclosure
   described in §2.4; users should not send information they are not permitted
   to disclose. The docs link to Exa's Terms rather than interpreting them.
7. Keyed Exa REST is separate: it is addable, uses the user's key/account, and
   its official monthly free credits are not the keyless service's quota.

Do not add a general statement that nothing else will turn on automatically.
Document the current default and routing facts only.

## 9. Phase 2 `web_fetch`

This section records only the upstream facts needed to keep the phase-1 client
reusable; [`web-fetch.md`](web-fetch.md) owns the phase-2 tool, local-first order,
address policy, bounded content/replay and fetch UI. It reuses this plan's
`src_exakeyless` / `exa/mcp` identity, advanced-search-plus-fetch endpoint,
shared MCP session and cooldown.

- The exact keyless MCP tool is `web_fetch_exa`. The checked source also
  registers `crawling_exa` as a deprecated alias that points to the same
  implementation; Avibe should use `web_fetch_exa`.
- The live keyless `tools/list` on **2026-10-22** advertised the input schema
  `{urls: string[], maxCharacters?: number}`: `urls` is required and batches
  one or more URL strings; `maxCharacters` is optional, positive, and applied
  per page. The checked source defaults it to **3,000** characters when absent
  and passes it as Exa `/contents` `text.maxCharacters`. The official Exa
  schema retrieved **2026-10-22** permits `text.maxCharacters` from 1 through
  **1,000,000**; the MCP wrapper itself does not declare a finite maximum, so a
  future caller must impose its own bounded value.
- The output is one MCP text content block formatted as clean Markdown:
  `# title`, `URL: ...`, optional `Published:`/`Author:` lines, then extracted
  page text; a successful live call returned HTTP 200 SSE with that shape and
  `_meta.searchTime`. Multiple URLs are batched into the same text result. A
  no-content response is the text `No content found for the provided URL(s).`
  If Exa reports per-URL crawl errors, the source appends `Error fetching <id>:
  <tag>`; when there are no successful results and an error status exists, it
  returns `isError: true` with `Error fetching URL(s): <id>: <tag>`.
- A keyless fetch of `https://example.invalid/avibe-keyless-exa-probe` on
  **2026-10-22** returned HTTP 200 SSE with `isError: true` and the text
  `Error fetching URL(s): ...: CRAWL_UNKNOWN_ERROR`. This is an MCP tool error
  envelope, not an empty successful result.
- The official hosted-server source commit
  `0b499f095c4ca145eee07b09137641c037d58449` (2026-10-19) applies the anonymous
  IP limiter to every `tools/call`, counts the calls in the JSON-RPC batch, and
  explicitly excludes `initialize`, `tools/list`, and other protocol methods.
  It does not create separate search and fetch buckets: `web_search_exa` and
  `web_fetch_exa` both consume the same per-IP QPS/daily limiter. The source
  documents defaults of 2 QPS and 50 calls/day when the limiter is configured,
  but these are deployment defaults rather than a public quota promise. The
  live fetch responses exposed no rate-limit headers; do not infer a separate
  fetch allowance. Phase 1 therefore reuses the same cooldown for any tool-call
  rate-limit signal.


## 10. Contract version and freeze rule

The Model Hub master contract is **v20**. It first shipped in prerelease tag
`gh-v3.2.3rc3` on **2026-10-22**; the latest prerelease is
`gh-v3.2.3rc7`, published **2026-10-24**, and the checked rc4, rc5, rc6 and rc7
contract mirrors still advertise v20. Therefore this keyless-Exa design must
not append new contract meanings to v20. The implementation lanes must open
the next coordinated contract event as **v21** for any keyless-Exa shapes.
Before every push and again before close-out, the lane must re-check tags
containing the current head and the latest RC/release tags; if v21 becomes
shipped before the next contract change, that change opens v22 instead. The
freeze rule still applies: one tested head owns all affected schema mirrors and
consumers for a version event.

The affected contract surfaces are the existing hosted-tools status/read and
settings shapes, the built-in Source/candidate discriminator, service catalog
facts, C-1 service execution/attempt readers, usage service rows, and the
localized activity/UI types. No live upstream response is a contract fixture.

## 11. Implementation split

Use the same layering as S2: **readers first, producer behind a flag, then one
flip plus public docs together**. The intermediate KEX-1 and KEX-2 heads kept the
new path dormant; KEX-3 is the coordinated activation event documented here.

| Lane | Scope | Gate |
| --- | --- | --- |
| KEX-1 readers and catalog | Add the built-in catalog/read shape, `src_exakeyless` defaulting and old-order reader behavior; extend status/candidate, attempts, service-origin, usage, activity, and UI readers/types/fixtures; add contract mirrors, the built-in cooldown-retry RPC/route branch, and released-shape compatibility tests. No network call. | Intermediate reader head only; activation remains with KEX-3. |
| KEX-2 producer | Add the adapter's keyless MCP session boundary, schema adaptation, SSE/JSON-RPC parser, highlights-only mapping, rate-limit detection/cooldown, service outcomes, usage writes, and UI actions behind the same flag. Use only stub MCP servers. | Intermediate producer head only; hermetic tests prove the path before activation. |
| KEX-3 activation and public docs | Re-check contract tags, flip the producer/read predicate together, publish the English/Chinese privacy/default/UI copy, and run exact-head review/CI. | Completed in contract v21: `KEYLESS_EXA_ACTIVE = true`; public disclosure ships with activation. |

KEX-1 and KEX-2 must not make a live Exa call in tests or CI. KEX-3 owns the
coordinated activation; no lane merges this design PR.

## 12. Acceptance properties and test plan

### 12.1 Acceptance properties

1. A native-capable hop still searches natively first; keyless Exa is reached
   only by the proxy chain.
2. With no user search service, the read and automatic order still contain
   enabled `src_exakeyless`; with user services, it remains after them by
   default; a deliberate saved order can move it anywhere or disable it.
3. `src_exakeyless` is stable across restart, has no credential material, is
   not in model supply/discovery/engine config, and cannot produce a credential
   failure.
4. An old build skips the unknown saved id and loads safely; a new build
   re-inserts the built-in candidate at read time without a migration write.
5. Only the canonical `{query}` reaches the agent. Result count is frozen per
   turn/service and mapped to advanced `numResults`; the adapter sends only the
   selected highlight controls and never forwards model-supplied filters,
   summaries, or crawl fields.
6. A reused MCP session performs one initialize lifecycle and caches one
   `tools/list` response for the session; setup is single-flight, while
   `tools/call` requests are concurrent under a three-call per-runtime cap; the
   negotiated protocol version is validated and reused; `tools/list_changed`,
   re-establishment, or a schema error causes a fresh list; an expired session
   can reinitialize and replay once only before the server accepts the tool
   call, but an admitted call is never automatically duplicated. The same
   session/client boundary is reusable for `web_fetch_exa` in phase 2.
7. Keyless Exa search work fits its 15-second service cap, fetch retains its
   30-second cap, and both retain the 180-second chain deadline; no blocking
   parse runs on the controller loop.
8. Valid results are highlights-only, bounded, ordered, and become one
   canonical completed record. The advanced adapter requests at most the
   defensive one-character text field, ignores it, and never requests or
   stores full page text.
9. HTTP 429, JSON-RPC/application rate-limit signals, malformed output, server
   errors, and transport failures fail over as specified in §6; rate limits
   persist as `service_rate_limited`, install a cooldown, and never become a
   credential blocker.
10. A successful or failed sent call records exact `$0`; every sent search call
    increments request usage; skips do not increment usage.
11. The Tools card always makes the built-in service, default, egress, free
    price, switch, and reorder behavior clear. Chat and IM name the free
    service and distinguish rate-limit/unavailable fallback from key failure.
12. CI has no live Exa calls and no real credentials.

### 12.2 Hermetic tests

Use a stub MCP Streamable HTTP server, not Exa, with deterministic SSE and
JSON-RPC fixtures:

- `initialize` returns a session id, a supported negotiated protocol version,
  server info, and capabilities; assert the version is retained on later
  requests, `notifications/initialized`, `MCP-Protocol-Version`, honest
  `x-exa-source`, and `clientInfo.name` are sent, and its empty HTTP 202
  response is accepted. An unsupported negotiated version is rejected.
- `tools/list` variants cover the selected advanced tool with its required
  `query`/`numResults`/highlight controls and `textMaxCharacters`; a missing
  required control fails as `service_unavailable`. The simple tool's live
  required `objective` shape remains a compatibility evidence fixture, not a
  phase-1 dispatch path.
- `tools/call` returns valid advanced JSON with one result, multiple results,
  empty results, a valid URL with no highlights, multiple highlights that are
  joined in order and capped at 1,000 characters, text fields that must be
  ignored, malformed JSON, an invalid URL, and oversized/bounded fields. A
  normal multi-result JSON payload is accepted without parsing unescaped text
  delimiters. Cover a valid highlight containing the free-rate-limit phrase;
  it must not trigger cooldown.
- The advanced JSON payload's notice-bearing and empty `results` forms are
  normalized to a completed empty record when appropriate.
- `web_fetch_exa` is a phase-2 parser fixture only; the shared session/client
  boundary accepts it as a second validated tool without a second initialize or
  session key; no phase-2 production activation is implied by the test.
- Session reuse sends one initialize lifecycle and one `tools/list` total for
  two warm calls; an expired session returns the standard session-not-found 404
  during initialization notification or before the tool call, reinitializes,
  resends the notification, and reconnects/replays the affected pre-tool
  request once before admission; a connection failure after the call is sent is
  not retried. The stub also proves keep-alive connection reuse and two
  concurrent `tools/call` ids after one setup.
- The stub can emit `notifications/tools/list_changed`; the cached schema is
  invalidated and the next call performs `tools/list` before mapping arguments.
- The service-chain fixtures prove at most three keyless Exa calls are active,
  that search uses a 15-second attempt cap, and that a cooldown published by
  one attempt is visible to new admissions without allowing an in-flight call
  to clear it.
- Two parallel cold starts share one initialize/list lifecycle under the setup
  lock; calls do not queue behind a turn-wide lock, and semaphore wait consumes
  the service attempt deadline.
- The transport fixture covers both SSE and direct `application/json` JSON-RPC
  responses.
- HTTP 429 with `Retry-After`, HTTP 429 with reset headers (including
  `X-RateLimit-Reset: 1800000000000` interpreted as Unix epoch milliseconds;
  malformed and past values are ignored, and the later valid deadline wins),
  JSON-RPC `-32000` **with the bounded free-rate-limit message**,
  `_meta.ai.exa/rateLimited`, and error-shaped free-rate-limit text all produce
  `service_rate_limited` plus cooldown, never `needs_action`. A generic
  JSON-RPC `-32000` without that message, including an HTTP 401 authentication
  envelope, maps to `service_unavailable`; a valid highlight quoting the
  rate-limit text does not.
- 5xx, redirects, timeouts, closed sessions, generic HTTP 200 `isError`, and
  malformed content map to `service_unavailable` and fail over. HTTP 200
  `isError: true` containing `PROHIBITED_CONTENT`, `CONTENT_FILTER_ERROR`, or
  another catalog-named query refusal maps to `service_refused` without a
  cooldown before the generic `isError` branch. Recognized Exa query refusals
  in non-200 responses follow the same rule; unknown endpoint/protocol 4xx
  responses remain `service_unavailable` with cooldown.
- Default order, saved order, role-map insertion, explicit disable (including a
  visible disabled-tail row), V2 serialize/reload of the disabled synthetic
  override and deliberate synthetic service role, older-build unknown-id
  reading, keyed-Exa-plus-keyless fallback, auto-enrolled official-DeepSeek-plus-keyless
  ordering, and native-preference behavior are table-driven.
- Attempts, service origin, exact-zero cost, request-only usage, capability
  evidence, activity projection of rate-limit/unavailable fallback reasons,
  no lower-bound charge, and cooldown projection (including an active cooldown
  shorter than 60 seconds) are asserted at their owning boundaries.
- The synthetic (`src_exakeyless`, `exa/mcp`) cooldown and saved partial
  override survive config saves, V2 reload, and later-candidate health writes;
  the role map survives when the user deliberately placed the service; a
  successful keyless attempt clears only its own cooldown generation.
- The Tools read/UI fixtures cover the always-present row, saved-partial versus
  effective settings, disabled-tail visibility, free label, egress fact, inline
  switch, reorder controls, add-dialog keyed Exa distinction, detail panel, Chat
  card, IM line, and both locales.

No live call is permitted in CI. The 2026-10-22 probe facts above are evidence
for this plan, not an automated dependency.

## 13. Known by design and owner-level questions

- The anonymous hosted-MCP quota is intentionally treated as an unknown,
  provider-controlled window. The implementation honors explicit advice and
  uses the existing bounded client cooldown when advice is absent; it does not
  promise 2 QPS/50 per day or 10 QPS.
- Exa's free-MCP response can be HTTP 200 with rate-limit metadata/text, even
  though the docs and source also define HTTP 429. Both are supported.
- The live default `web_search_exa` schema and the checked source commit differed
  on the required `objective` field on 2026-10-22. That difference is recorded
  for compatibility, but phase 1 selects the opt-in advanced JSON tool and does
  not dispatch the simple text formatter.
- The checked source's simple `web_search_exa` text formatter remains an
  upstream compatibility fact, but advanced search is the phase-1 canonical
  tool because its JSON payload is unambiguous and can be mapped to
  highlights-only results. Fetch is reserved for phase 2.
- No owner-level question remains open in this lane. The owner decisions above
  settle default-on, ordering, fallback, terms risk, and honest identity. The
  orchestrator still owns the final design.pen work, merge decision, and any
  later change in Exa's terms or hosted behavior.

## Sources checked

- Exa MCP reference/quickstart: https://exa.ai/docs/reference/exa-mcp and https://exa.ai/docs/get-started/exa-mcp (2026-10-22).
- Exa MCP landing page: https://exa.ai/mcp (2026-10-22).
- Exa hosted MCP source: https://github.com/exa-labs/exa-mcp-server, commit `0b499f095c4ca145eee07b09137641c037d58449` (2026-10-19), especially `api/mcp.ts`, `src/tools/webFetch.ts`, `src/tools/config.ts`, and `src/mcp-handler.ts`.
- Exa Search API schema: https://exa.ai/docs/exa-spec.yaml (retrieved 2026-10-22).
- Exa Highlights: https://exa.ai/docs/search/highlights (retrieved 2026-10-22).
- Exa MCP authentication/free access: https://exa.ai/docs/get-started/exa-mcp#authentication (retrieved 2026-10-22).
- Exa Terms of Service: https://exa.ai/terms (retrieved 2026-10-22).
- Keyless live probe: `https://mcp.exa.ai/mcp`, 2026-10-22, no key, twelve JSON-RPC requests, five `tools/call` attempts, no deliberate exhaustion.
