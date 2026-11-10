# Catalog-driven keyless MCP search

Status: implementation, 2026-11-09. Extends [web-search-services.md](web-search-services.md)
and [web-search-keyless-exa.md](web-search-keyless-exa.md); their settings, records,
usage, ordering, and cooldown contracts remain unchanged.

## Contract

- One bounded `aiohttp` MCP client serves catalog-owned anonymous search services.
  It retains single-flight setup, schema validation, admission at the send boundary,
  direct TLS egress, timeouts, an 8 MiB decoded response ceiling, and no redirects.
  JSON and SSE return only the matching RPC response; a completed SSE result does
  not wait for the server to close its stream. Stateful and stateless MCP both work.
- `vibe/data/api_key_vendors.json` owns each built-in's stable `source_id`, endpoint,
  `auth: none`, MCP tool and required arguments, request/result mappings, result
  limits, `default_enabled`, and `default_in_chain`. Adding another search service
  requires a catalog entry and tests, not a new transport or vendor-name branch.
- A synthetic identity is never persisted as a Source. A mismatched persisted
  identity or explicit credential cannot be silently replaced by an anonymous one.
- Exa keeps `src_exakeyless`, `exa/mcp`, automatic enrollment and the existing
  saved settings, usage, cooldowns, and shared search/fetch session. Parallel uses
  `src_parallelkeyless` / `parallel/mcp`, is off by default and initially outside
  the chain. Neither exposes arbitrary tools advertised by its MCP server.
- The Tools read supplies built-in identity, endpoint and default-enabled facts to
  the existing UI. Older reads retain their Exa fallback. No migration is needed:
  old versions ignore the unknown optional vendor while keeping Exa's settings.

## Optional Parallel search

In Settings → Models → Tools, open **Add search service**, choose **Parallel
(free)** and **Use for web search**. This enables anonymous search at
`https://mcp.parallel.ai/mcp`. Queries and the runtime connection IP are visible
to Parallel; no provider key is sent. Requests ignore environment proxies.

To try Parallel first, move it to the top of the search order. For models with
native search, turn off **Prefer the model's own search** to use that order.
Disable Parallel with its service switch. Its saved override is
`model_hub.hosted_tools.web_search.services.src_parallelkeyless`; removing it
restores the unelected default. Explicit credentials are rejected. Parallel's
MCP schema has no result-count input: Avibe caps the mapped output locally.

## Acceptance and verification

`MH-SEARCH-SERVICES-001` covers saved opt-in/order, disabled services, admission,
identity collisions, bounded responses, result mapping and credential isolation.
A synthetic third JSON entry exercises the real loader, chain and transport,
including stateless-session reuse. Existing Exa fixtures protect default-on,
saved opt-outs/order, usage/cooldowns and shared fetch behavior.

Before delivery, run the focused backend and UI tests, changed-file lint and UI
build. A one-at-a-time, normally paced comparison uses 20 representative queries
(code documentation, news, Chinese and product lookups), judging top-three
relevance and recording latency and rate limits. Results belong in the PR body,
not a durable product promise. This is not a load test. The owner decides merge;
no deployment or local runtime restart is included.
