# Optional Parallel web search

In Settings → Models → Tools, open **Add search service**, choose **Parallel
(free)** and **Use for web search**. This enables anonymous search at
`https://mcp.parallel.ai/mcp`. Queries and the runtime connection IP are visible
to Parallel; no provider key is sent. Only search is exposed, not the server's
other tools. Requests use Avibe's direct connection with certificate verification
and ignore environment proxies.

Parallel starts outside the execution chain. Existing Exa defaults, saved
orders and opt-outs stay in effect. Once elected, use the existing service
switch and search order controls to disable or reorder it. The saved V2 setting
is `model_hub.hosted_tools.web_search.services.src_parallelkeyless` with
`enabled: true`; removing that override restores the unelected default.
There is no credentialed Parallel mode: explicit credentials are rejected.

The catalog service `parallel/mcp` uses the normal `service` role, result limit,
search records, usage and cooldown rules. It adds no persisted Source, model
supplier or migration. Historical records retain their provider attribution.
