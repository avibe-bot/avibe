# Vibey hosted tools

Status: **design**, 2026-10-13, for owner review.
- Scope: Vibey only. Claude Code, Codex and OpenCode are unchanged, and there is no CLI.
- Foundation: [`model-hub-capability-model.md`](model-hub-capability-model.md), which covers evidence, the overlay and
  pricing.
- Evidence: the runtime spike of 2026-10-13 (40 + 10 upstream calls through the local engine on API-key relay
  Sources). Its tables are summarised where a rule depends on them.

## 0. Owner decisions this design implements

- **Native first.** A provider's hosted tool is used natively when the current route is verified for it. Otherwise
  Vibey offers a gateway-executed **proxy** tool with the **same name and schema**.
- **The gateway owns** the catalog, verification, the user toggles in Models, the proxy executor, and per-hop
  selection and filtering. Filtering means a pinned request is refused, never rewritten (§3.5).
- **The transcript stays portable.** Each hosted call keeps a canonical record, plus the origin-tagged native payload.
  The payload is replayed only to the same verified origin.
- **Phases.** Phase 1 is web search and X search. Phase 2 is image generation.

## 1. Example

The user enables X search in Models. Vibey is on Claude Opus through an Anthropic API key. An xAI API key is
verified for X search.

1. Anthropic has no X search, so Vibey's request declares `x_search` as an ordinary function with the canonical
   schema (§2).
2. Claude calls `x_search({"query": "from:OpenAI within_time:7d"})`. Vibey runs it through the gateway's proxy
   executor, which sends one short native request to the xAI Source and returns numbered posts with links and
   excerpts.
3. The tool card shows the query, "via proxy · xAI API key", and the cost reported by xAI. The answer's citations
   render as links.
4. Next turn the user switches Vibey to Grok on the xAI Source. That hop is verified for X search, so `x_search` is
   declared natively.
5. The earlier proxied call is an ordinary tool call and result, so it replays to Grok unchanged.

## 2. Concepts

- **One user-facing tool per capability.** Phase 1 has `web_search` and `x_search`.
- **Two execution modes behind one name and schema.**
  - **Native:** the provider's hosted declaration on the wire. The provider runs the search.
  - **Proxy:** a function with the canonical schema. Vibey's tool runtime asks the gateway's executor to run it.
- **The canonical schema is the providers' trained interface.**
  - `web_search` is `{"query": string}`, and only that. Anthropic's `server_tool_use.input` is exactly `{query}`.
    OpenAI's and xAI's `web_search_call.action` carries `query` for `type: "search"`.
    - The other knobs are declaration configuration, not per-call arguments: OpenAI's `open_page`/`find_in_page`
      actions and Anthropic's domain filters and `max_uses`.
    - A wider schema would teach the model arguments that no native mode accepts.
  - `x_search` mirrors the only provider that has it. xAI's model calls `x_keyword_search` with
    `{"query", "limit", "mode"}` (observed). The canonical schema is
    `{"query": string, "limit"?: integer, "mode"?: "Latest" | "Top"}`, named after the hosted tool `x_search`.
    `query` accepts X's advanced-search operators (`from:`, `within_time:`).
  - **Evidence that the same name and schema are safe:** a synthetic same-named call in history was accepted even
    when the target also declared the native tool under that name. Anthropic accepted a client `tool_use` named
    `web_search` next to `web_search_20250305`, and Responses accepted a `function_call` named `web_search` next to
    `{"type": "web_search"}`.
- **Phase 2:** `image_generation`, schema `{"prompt": string, "size"?: string}` (§7).

## 3. Gateway responsibilities

### 3.1 Catalog

- **The catalog is the overlay's `providers` section** (capability model §4): for each provider model family and
  protocol, the native declaration and the list price.
- **What it lets the gateway answer, without a network call:**
  - which tools exist;
  - which Sources might run each one natively;
  - what a call costs.

### 3.2 Verification

- **Live probes are the only way to `verified`.** They run on an explicit user action (capability model §3.3).
- **A probe is the production request.** It is built by the same Vibey request builder and with the same native
  declaration as a real turn on that (Source, model). Only the transcript differs: one fixed user message. So a
  probe cannot pass or fail for a shape that production never sends.
- **The `call` probe.** The fixed message is "search for 'Avibe local-first agent OS' and reply with the first
  result's URL".
  - **`verified`:** the response completes and carries the provider's own result item for that tool (§4.2).
  - **`unsupported`:** either an HTTP 400 that names the tool type (observed: `Unsupported tool type: x_search`), or a
    completed response without that item. The second case is a path that drops the tool silently, as observed with
    `image_generation` on a translated Anthropic route.
  - **Not evidence:** a transient failure (overload, 5xx, timeout). The UI reports "couldn't verify, try again"
    (observed: every OpenAI-relay web search ended with an in-stream "servers overloaded").
- **The `replay` probe.** A second production request with the native declaration still present. Its history is
  the call probe's turn, with the native payload verbatim, followed by "give the URL of the second result you found,
  without searching again".
  - **`verified`:** the answer contains a URL from the first response's results, and the response makes no new
    call to the tool.
  - **`unsupported`:** a 400 that names the replayed hosted content (observed on the Anthropic relay, 3 of 3:
    `Invalid encrypted_content in search_result block`), or an answer that does not use the earlier results (a new
    tool call, or no URL from them). Transport acceptance alone is never verification. xAI accepted verbatim
    replay, 3 of 3.
- **Cost and when probes run.** A probe pair costs about $0.02–0.15 (spike: xAI $0.01–0.11 per call, Anthropic $0.01
  per search plus tokens). The Tools tab shows the estimate before running.
- **What makes probe results stale** (capability model §3.3): a Source identity change, an engine upgrade, a probe
  shape change, or age.
- **Failures in real use demote evidence** through the narrow rules in capability model §3.2.
- **Relation to #2539.** It is the same class (a prior taken as a Source fact), solved by the same evidence store.
  Efforts need only negative evidence. Native hosted tools need positive evidence, because their failure is terminal
  or silent while the proxy is lossless.

### 3.3 User toggles

- **Placement.** One **Tools** tab in Models, next to Sources, Quota, Usage and Logs (§6).
- **Each tool is off by default** (§6.3).
- **What a toggle stores.** A new optional V2 config section, owned by the Model Hub API and written only through
  it:

  ```json
  "model_hub": {
    "hosted_tools": {
      "web_search": {"enabled": true, "executor": {"source_id": "src_…", "model_id": "grok-4.6"}},
      "x_search": {"enabled": false, "executor": null}
    }
  }
  ```

  - Fields, keyed by catalog tool name (`web_search`, `x_search`):

    | Field | Rule |
    | --- | --- |
    | `enabled` | boolean, required |
    | `executor` | required: `null`, or `{source_id, model_id}` with both non-empty |
  - The section is absent on every released config, and absence means every tool is off. No migration is needed:
    `ModelHubConfig`'s loader accepts the new optional section. An unknown tool key disables that entry and logs a
    warning (safe degradation); it never fails startup.
  - An executor whose Source or model no longer exists reads as `null`.
  - It stores no per-Agent copy: Vibey reads it through hop resolution.

### 3.4 Proxy executor

- **Endpoint:** `POST /vibey/v1/hosted-tools/{tool}` on the gateway, authenticated with the turn's gateway token.
  The body is the canonical arguments and the turn's executor binding (§3.4).
- **Executor choice, bound per turn.**
  - Hop resolution picks the executor and returns it in `HopResolution.hosted_tools[tool].executor` (§3.5). The pick
    is the configured executor if it has `call: verified`, otherwise the first verified (Source, model) in Vibey's
    Source order.
  - The proxy call sends that binding with the turn token, and the gateway runs that executor and no other.
  - A config change (another executor, a disabled tool, a removed Source) applies from the next turn, as every
    runtime config change does (AGENTS.md).
  - If the bound executor can no longer run (its Source was removed or disabled mid-turn), the call returns a tool
    error. It never silently switches Source or billing.
  - **Only an eligible credential.** A Claude subscription Source is never eligible: not as executor, not for a
    probe, and not for native mode (§3.5). Anthropic restricts OAuth credentials to Claude Code and its own apps
    ([`model-hub-tos-review.md`](model-hub-tos-review.md)).
  - If no executor is verified, the call returns a tool error naming the fix ("verify X search on a Source in
    Models"). Vibey shows it like any tool error.
- **The request.** One short native call with the tool forced, a fixed instruction to search and list results, and
  a small output cap.
  - It sends **no reasoning effort**. The executor (Source, model) is verified for the tool, not for any effort, and
    an unverified effort could fail a verified tool.
- **The result is the canonical record only:**
  - the `HostedRecord`, with the action, `status`, `error` and per-call `sources[]` (`url`, `title`, and, where
    available, `snippet` and `page_age`);
  - the `ProxyExecution`: executor origin, Source, billing mode, and the call's cost. A proxied call is a whole
    executor response, so its cost is per call.
  - The executor does not write model-facing text. Vibey's tool runtime renders the tool result from the record with
    the **same function** that renders a synthetic replay (§5), so the text a model sees for a proxied call and for a
    replayed native call never diverge.
  - Excerpts are what make a result reusable. A model shown URLs alone treated the earlier answer as unsupported and
    said so; a model shown titles and snippets answered the follow-up without searching again.
  - OpenAI and xAI return no page text, so the executor's instruction asks for a one-to-two-sentence excerpt per
    result, stored as `snippet`. Anthropic supplies `cited_text`.
- **Cost and latency.**
  - Cost is one model call plus the tool fee: xAI about $0.01–0.11 per call; Anthropic $0.01 per search plus tokens.
    It is metered on the executor Source (usage ledger, provenance) and shown on the card.
  - Money is computed by one owner, Model Hub's pricing module (capability model §6), for both proxied and native
    calls. Neither the executor nor a Vibey adapter prices anything itself.
  - Measured latency is 7 s (Anthropic) and 35–61 s (xAI at high effort). The executor's own latency is an acceptance
    measurement, not a promise.

### 3.5 Per-hop selection and failover

- **Hop resolution.** C-6 `HopResolution` gains `hosted_tools`. For each enabled tool it gives:
  - the primary hop's `mode` (`native` | `proxy`);
  - for `native`, the wire declaration;
  - for `proxy`, the bound executor `{source_id, model_id}` (§3.4), which may be `null` when none is verified;
  - `replay`, the **current** `replay` evidence for the primary hop's Source and model.

  `native` requires all of these:
  - the toggle on;
  - an eligible credential (§3.4);
  - `call: verified` for that (Source, model);
  - an untranslated path.

  Vibey builds the request from these fields (§4.1, §5).
- **One owner for every conversion: Vibey.** Vibey renders every request from its canonical records. The gateway
  never rewrites hosted content: no declaration swap and no history conversion. It sees only wire bytes, and the
  canonical record (for xAI X search, the response's citations) is not in them.
- **Pinned requests.** A request that carries Source-specific hosted content carries the gateway request header
  `x-avibe-pin-source: <source_id>`. Source-specific hosted content means:
  - a native declaration;
  - a `native.payload`;
  - an in-flight continuation (§4.3, §4.4).

  The value is `HopResolution.source_id` for the first two. For a continuation it is the Source the previous
  response came from, according to the served-hop report.
  - The gateway serves a pinned request only from that Source, with no failover and no recovery wait.
  - The gateway answers the local error `pin_unavailable` in three cases:
    - the Source cannot be admitted for this content, checked before invoking anything: it is cooling down, it was
      removed, it has no route, or its evidence for a declared native tool or replayed payload is no longer
      positive;
    - its attempt ends before model output with a failure that normal routing would fail over from;
    - its attempt ends before model output with an error that a narrow hosted-content rule names (capability model
      §3.2). That rule also records the negative evidence.

    In the last two cases `pin_unavailable` replaces the failover. The answer is HTTP 409 with
    `{"type": "error", "error": {"type": "model_hub_pin_unavailable", "code": "pin_unavailable"}}` and is never
    shown to the user. Any other error passes through as today.
  - Vibey then re-renders that same request in **portable form** and sends it unpinned. Portable form means:
    proxy declarations, synthetic history (§5), and the in-place text form for in-flight content (§4.4).
  - An unpinned request fails over exactly as today.
  - The cost is one local round trip on failover for a request with native content. The upstream is not called
    again for nothing: a failure before output is a failover attempt either way.
- **Consequence:** a hop never receives a hosted tool or hosted payload it has no positive evidence for. Two
  observed failures become unreachable: `x_search` → OpenAI (`400 Unsupported tool type`) and `image_generation` →
  Anthropic (silently dropped).
- **Not changed here:** C-6 item 6. The gateway still strips opaque thinking payload at failover for unpinned
  requests, which is a second owner of that conversion. Hosted content no longer depends on it; unifying it is a
  separate decision.
- **Translated paths are never native.** The engine coerces web search across protocols and it worked in the spike,
  but its result shapes are hybrids (Anthropic `web_search_result_location` citations inside a Responses message),
  so they are not a verified contract.

## 4. Vibey responsibilities

### 4.1 Request building

| Protocol | Native declaration | Proxy declaration |
| --- | --- | --- |
| `openai_responses` (OpenAI, xAI) | `{"type": "web_search"}`, `{"type": "x_search"}` | `{"type": "function", "name": "web_search", "parameters": <canonical>}` |
| `anthropic` | `{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}` | `{"name": "web_search", "input_schema": <canonical>}` |
| `openai_chat`, `google` | none in Phase 1 | function / functionDeclaration with the canonical schema |

- Vibey takes the mode from `HopResolution.hosted_tools`. It never decides nativity itself.
- Its four local tools are unchanged (C-7).

### 4.2 Parsing: the C-1 change

The field tables below are the contract. The JSON Schema for them lands with the Phase 1 implementation PR, where a
producer validates it (§12 lists every invariant it must encode).

**Mapping rule.** Every item the provider emits for a declared hosted tool becomes exactly one `HostedToolBlock`,
in stream order, with a provider-neutral `action`. An action this design does not know becomes `other` and is kept,
never dropped.

| Provider item | `tool` | `action` |
| --- | --- | --- |
| OpenAI/xAI `web_search_call`, `action.type: search` | `web_search` | `{type: "search", queries[]}`, taken from `action.queries`, else from the deprecated singular `action.query` |
| `web_search_call`, `action.type: open_page` | `web_search` | `{type: "open_page", url}` |
| `web_search_call`, `action.type: find_in_page` | `web_search` | `{type: "find_in_page", url, pattern}` |
| Anthropic `server_tool_use` (`name: web_search`) plus its `web_search_tool_result` | `web_search` | `{type: "search", queries: [input.query]}` |
| xAI `custom_tool_call` `x_keyword_search` / `x_semantic_search` | `x_search` | `{type: "x_search", variant: "keyword" \| "semantic", query}` |
| xAI `custom_tool_call` `x_user_search` | `x_search` | `{type: "x_user_search", query}` |
| xAI `custom_tool_call` `x_thread_fetch` | `x_search` | `{type: "x_thread_fetch", ref}` |
| any other item under a declared hosted tool | that tool | `{type: "other", name}`, with the provider's item name |

**`HostedToolBlock`** (assistant content):

| Field | Rule |
| --- | --- |
| `type` | `"hosted_tool"` |
| `id` | canonical id |
| `tool` | catalog name: `web_search` or `x_search` (Phase 2 adds `image_generation`) |
| `action` | one of the mapping rule's actions, with its fields required |
| `record` | the `HostedRecord`, per-call facts only |
| `native` | required: `{source_id, payload[]}`. `payload` holds the provider's own items, verbatim and in order, and is never empty. The payload's origin is the message's `origin`, not stored twice. No replay authorization is stored; replay is decided at every request from current evidence (§5). |

A native hosted call always has its native payload. A proxied call is a `ToolCallBlock` plus
`ToolResultMessage.hosted`, never a `HostedToolBlock`.

**`HostedRecord`** (per call):

| Field | Rule |
| --- | --- |
| `status` | `completed` / `failed` / `incomplete` / `pending` |
| `error` | present exactly when `status` is `failed` or `incomplete`; bounded and redacted |
| `sources[]` | this call's own results: `url` required; `title`, `snippet`, `page_age` optional. Empty when the provider returns none per call (xAI X search). |

**`AssistantMessage.hosted_summary`** (per response, present when the message has hosted blocks). This is where
response-scoped facts live, so nothing is copied into calls or split across them:

| Field | Rule |
| --- | --- |
| `cost` | `{basis, usd}`: `basis` is `reported` (the provider's figure for the whole response, e.g. xAI `cost_in_usd_ticks`) or `list` (computed by the pricing module), and both carry `usd`; or `basis: included`, with no `usd` |
| `counts` | provider-reported counters, by name (`web_search_calls`, `x_search_calls`, `x_posts_fetched`, `x_users_fetched`, Anthropic `web_search_requests`) |
| `cited_sources[]` | sources the response cites when the provider gives none per call: the xAI X-search `url_citation`s to `x.com` / `twitter.com` |

**`TextBlock.citations`** (optional): `[{url, title?, cited_text?, start?, end?}]`, with `start` and `end` together
as a half-open code-point range into `text`.
- Citations belong to the text that cites them. Every provider attaches them there: OpenAI and xAI `url_citation`
  with offsets, and Anthropic `citations[]` on text blocks.
- They are presentation only and are never replayed.
- **One materialization boundary.** At delivery, `core/citations.py` gains one builder from (text, citations) to
  the existing delivered shape: Markdown links written into the text, plus the sidecar the Web renders as
  `CitationBadge` (numbered sources, UTF-16 ranges, `body_sha256`). Adapters only parse; IM gets the Markdown links.

**`ToolResultMessage.hosted`** (optional): `{record, execution}`, both required.
- `execution` is a `ProxyExecution`, every field required: `mode: "proxy"`, `executor_origin`,
  `executor_source_id`, `executor_billing`, `cost`.
- `is_error` is `true` exactly when `record.status` is `failed` or `incomplete`.
- So a proxied call renders like a native one, and the card names the exact Source even when two Sources share an
  origin.

**Per adapter notes.**
- **`openai_responses`.**
  - xAI's X-search `reasoning` items with id `tco_…`, which hold the tool output encrypted, stay ThinkingBlocks as
    today.
  - The `custom_tool_call` becomes the HostedToolBlock, and Vibey must not treat it as a local tool call. Today's
    adapter already ignores `custom_tool_call_input.*`.
- **`anthropic`.** `encrypted_content` is kept only in `native.payload`. Text-block `citations` become `TextBlock`
  citations.
- **`openai_chat`.** No native hosted results in Phase 1.
- **Assembled-output budget.** Hosted payloads count against the existing 32 MiB `StreamAssembler` budget. Search
  events measured at most 96 KB.

### 4.3 `pause_turn`

- **Observed:** a 12-step dependent search chain paused after 10 server-side searches. The response ended with a
  `server_tool_use` that had no result.
- **Continuing works:** resending the assistant content as is, with no new user message, was accepted. The server
  ran the pending search, and the answer completed.
- **Today** `anthropic.py` maps `pause_turn` to `stop`, so that turn would end without its answer.
- **Rule.** The Anthropic adapter continues internally: it re-issues the request with the paused content appended
  and keeps streaming as one logical response, at most 5 continuations.
  - Each continuation is pinned to the Source the paused response came from (§3.5). On `pin_unavailable`, the
    continuation is re-rendered in portable form, with the paused server blocks converted in place (§4.4), and goes
    out unpinned.
  - C-2 still holds: exactly one `done` or `error`.
  - The paused partial and the continuation merge into one `AssistantMessage`.
  - Its `usage` is the **sum** of every continuation response's counters: input, output, and cache read and write.
    Today's parser resets usage at each `message_start`. The gateway already meters each continuation as its own
    call, so the usage ledger and provenance need no change.

### 4.4 In-flight turn replay

Within one turn, i.e. `pause_turn` or a client `tool_result` after server tools in the same assistant message, the
next request is pinned to the Source the previous response came from (§3.5), and the assistant content goes back
**verbatim**. This is the provider's own continuation contract, so it needs no `replay` evidence. Observed accepted:
- a pending `server_tool_use` (the server then ran it);
- ten `web_search_tool_result` blocks during a `pause_turn` continuation.

**Fallback form.** It applies when the pinned Source rejects in-flight verbatim replay (a narrow 400), or answers
`pin_unavailable`. Each server block pair is replaced **in place** by one text block: `[web_search: <query>]` plus
numbered titles and URLs.
- On the same origin, the signed thinking blocks stay untouched. This was accepted, with no signature error.
- On a different origin, the C-1 cross-origin rules then apply to the rest of the message as usual.
- Dropping the pair is never used: the model then misread its own turn ("I called read_file before the search")
  and redid the work.

### 4.5 SSE caps

Phase 1 needs no change: the largest search event was 96 KB, against an 8 MiB line and 32 MiB event cap. For
images see §7.

## 5. Replay and conversion

These rules become C-1 rows in `cross-provider.md` and per-protocol rows in `conversion-tables.md`.

| History item | Target hop is `native.source_id` and `HopResolution.hosted_tools[tool].replay` is true now | Any other target |
| --- | --- | --- |
| `HostedToolBlock` | `native.payload` verbatim, in place | a synthetic same-named call plus result (Anthropic `tool_use` / `tool_result`, Responses `function_call` / `function_call_output`, Chat `tool_calls` / `tool` message), with the assistant message split around it. One record-to-text function (§3.4) renders the result, under the rules below. |
| `TextBlock.citations` | not sent | not sent |
| Proxy call (`ToolCallBlock` + `ToolResultMessage.hosted`) | ordinary tool call and result | same |

- **Synthetic rendering rules**, by action:
  - A `search` action becomes one call with arguments `{query: queries[0]}`. Its result lists any further queries,
    then the record's numbered sources (title, URL, snippet).
  - An `x_search` or `x_user_search` action becomes one `x_search` call with `{query}`.
  - A non-search action (`open_page`, `find_in_page`, `x_thread_fetch`, `other`) becomes a line in the result of the
    preceding synthetic call of the same tool in that response, e.g. "opened <url>" or "fetched thread <ref>".
    Without a preceding call, it becomes its own call with `{query: "<action type> <target>"}`.
  - The response's `hosted_summary.cited_sources` are listed once, in the first synthetic result of that tool, as
    "posts cited in the answer".
  - A `failed` or `incomplete` record renders as an error result: Anthropic `is_error: true`, or text starting
    `[<tool> failed: <error>]` where the protocol has no error flag.
  - A `pending` record (a call cut off with the turn) renders as the C-1 interrupted-call result.
- **Why synthetic beats plain text.**
  - Anthropic and Responses targets accepted the synthetic form, with the proxy or the native tool declared.
  - With snippets in the result, the target answered from it without searching again.
  - With plain-text rendering, Claude searched again.
- **What is lost across providers: page text.**
  - Anthropic returns page content only as `encrypted_content`, which no other origin can read. OpenAI and xAI never
    return it.
  - The target sees queries, titles, URLs and snippets, and an honest model treats claims beyond them as unverified,
    as observed.
  - Provider-internal search state (opened pages, follow-up actions) is lost too.
- **Vibey renders for the primary hop.** A request with native content is pinned, and its failover comes back to
  Vibey as `pin_unavailable` for a portable re-render (§3.5). So a different Source or origin never receives
  `native.payload`, and only Vibey ever converts it.
- **C-6 change: the served-hop report gains `source`**, the serving Source id. Two Sources can share an origin, e.g.
  a subscription and a relay serving the same model. Native mode depends on this field three times: to tag
  `native.source_id` (§4.2), to authorize replay (above), and to pin in-flight continuations (§3.5).
  - It ships as a coordinated Model Hub contract bump, v12.
    - The orchestrator decided on 2026-10-13 to share the bump that #2483's provenance spec was deferred to.
    - That provenance fix then landed in #2544 within v11, without a bump. So this change now owns v12, and any other
      contract change pending at that time joins it.
  - The gateway and the served-hop parser change in that same release.
  - **Phase 1's native mode therefore lands with or after v12.** Proxy mode does not depend on it.
  - **The v12 bump edits these exact-shape contracts together:**
    - `model-hub-contracts/hop-origin.schema.json`: add `source` (required);
    - `model-hub-contracts/vibey-consumer.md`, Response origin;
    - `agent-core-contracts/provider.md`, the served-hop sentence;
    - `agent-core-contracts/conversion-tables.md`, the Origin row ("exactly `provider`, `api`, `model`");
    - `agent-core-contracts/model-hub-consumer.md`, item 4.

    These files are frozen at v11, so this PR states the change and leaves their text alone.

## 6. User interface

### 6.1 Models: the Tools tab

- **One row per tool** (Phase 1: Web search, X search), reusing the Sources tab's row and chip vocabulary
  (`chips.tsx`, `SourceRow`, `TimeHint`):
  - **toggle**, off by default;
  - **runs natively on:** chips per verified (Source, model), e.g. "xAI API key · grok-4.6 · verified 10-13";
  - **proxy executor:** a picker of verified candidates, each with billing mode and cost hint;
  - **cost per call:** "≈ $0.01 at API price" for metered, "included in subscription" for monthly, or "reported by
    xAI" when the last call returned a cost.
- **Verify on…** lists the Sources whose overlay row offers the tool but that have no evidence. Each entry has an
  explicit **Verify** button showing the probe's estimated cost. Nothing probes on its own.
- **Turning a tool on with no verified Source** opens that list. It never auto-probes.

### 6.2 Source detail chips

Each model row on a Source's detail panel shows capability chips (capability model §5):
- "Web search · native · verified 10-13";
- "X search · via proxy";
- "X search · not supported on this Source";
- "ultra · not supported on this Source".

Each has a **Re-verify** action with its cost. Unknown shows nothing.

### 6.3 Defaults and onboarding

- **Every tool is off by default.**
  - A call costs money on metered Sources.
  - It sends the user's queries to a third-party search.
  - Turning it on needs a cost-bearing probe.
  - No Source the owner has today makes it free. The ChatGPT subscription could, but it is untested.
- **No onboarding step mentions tools.** The Tools tab is the only entry point.

### 6.4 Use time in Vibey

- **Tool card** (the existing `AgentActivityGroup` row; its `web` kind already shows a globe and a query chip):
  - the query;
  - the mode ("native · xAI API key" or "via proxy · Anthropic API key, claude-haiku-4-5");
  - the cost when metered;
  - an expandable source list (title, domain, age);
  - a failed status with its reason.
- **Citations** render through the existing citation contract, using the one builder in §4.2: Markdown links plus
  the sidecar, which the Web renders as `CitationBadge`.
- **IM** gets a compact single line, `🔎 Web search "query" · native (xAI) · 6 sources · $0.01`, and the answer
  carries plain Markdown links.

## 7. Phase 2: image generation

- **Today:** the engine injects `{"type": "image_generation"}` into every Codex-executor request (CPA
  `ensureImageGenerationTool`; `disable-image-generation` unset in `vibe/model_hub_runtime/config.py`), xAI included.
  GPT calls it when asked for a picture from a Vibey-shaped request; the image is billed and then dropped by Vibey.
- **Size on the wire:**
  - One image is about 4.1–4.2 MB of base64, sent twice: in `response.output_item.done` and again in the
    engine-rebuilt `response.completed`.
  - The relay forced `quality: low` and capped the size (1370×1148 and 1536×1024 against requests for 1024×1536 and
    3840×2160).
  - High quality would sit at the 8 MiB line cap.
- **Storage and display.**
  - The adapter decodes each image to the session's media store as the event is parsed. History keeps a media
    reference, never base64, and replays no image bytes to the producing origin.
  - Cross-origin: the synthetic tool result carries the image as an `ImageBlock`, for vision targets.
  - The UI and IM show the file.
- **Caps.** Image events get their own line limit of 32 MiB, decoded straight to the file and never assembled. Other
  events keep 8 MiB.
- **Idle bound.**
  - A failing generation went 35.4 s without a byte after `generating`, and healthy ones took 64–130 s.
  - Phase 2 acceptance measures successful generations through the gateway. If a gap can exceed 120 s, #2538's bound
    must treat an open `image_generation_call` as expected silence.
- **The injected tool, options:**
  - **(a) `disable-image-generation: passthrough`:** the engine never injects or strips, for every backend.
    - It stops hidden billed images for OpenCode and for Claude Code on Codex-executor Sources, which cannot show them.
    - For Codex it must first be verified that Codex's own image tool declaration still works under passthrough.
  - **(b) A Vibey-only per-request opt-out:** an engine patch Avibe would carry. The only built-in switch, the
    internal `X-OpenAI-Internal-Codex-Responses-Lite` header, also forces `parallel_tool_calls: false`, so it is
    rejected.
  - **Recommendation:** (a), after the Codex check, because it removes a cost leak for every backend. Choose (b) only
    if Codex depends on injection.
  - Either way, Vibey declares `image_generation` itself when the user enables it.

## 8. Cost and limits

- **Per-call prices** live in the overlay with date and source (capability model §6.2):
  - OpenAI web search: $10 / 1k calls plus content tokens;
  - Anthropic: $10 / 1k searches plus result tokens, re-billed while they stay in context;
  - xAI: web search $5 / 1k calls, X search $5 / 1k posts and $10 / 1k profiles.
- **Relays** show list price, labelled as such, and may bill differently.
- **Anthropic OAuth policy:** a Claude subscription Source is never probed and never runs a hosted tool, whether
  natively or as proxy executor. Credential eligibility is checked before the probe, the native mode and the executor
  choice (§3.4, §3.5).
- **Relay findings on 2026-10-13:**
  - the OpenAI relay did not complete web search (overload, 5/5), so it is unverified;
  - the Anthropic relay rejects cross-turn verbatim replay, so replay there is unsupported;
  - xAI is fully verified for calls and replay.
- **Open:** OpenAI web search on a non-relay route, after the ChatGPT subscription's cooldown (2026-10-18).

## 9. Phasing

1. **Phase 0, capability foundation:**
   - the evidence store and narrow negative rules;
   - overlay `client_presets` on backend rows and the provider-neutral `providers` section (wire efforts, hosted
     tools, prices), with its loader and schema;
   - Source chips for efforts;
   - context-tier valuation.
   This closes #2539.
2. **Phase 1, web search and X search:**
   - catalog, probes, the Tools tab, and the proxy executor;
   - `HopResolution.hosted_tools`, request pinning and `pin_unavailable`, and the V2 `hosted_tools` config;
   - the C-1 blocks and adapter parsing;
   - replay and conversion, `pause_turn` continuation, the tool card, and the IM line.
3. **Phase 2, image generation:** §7.

## 10. Acceptance properties

1. A hop receives a native hosted declaration only when all of these hold: the toggle is on; the credential is
   eligible; the (Source, model) has positive `call` evidence; and the path is untranslated. Every other hop gets
   the same-named proxy function.
2. The gateway never rewrites hosted content. A request with native content is pinned, and every failure to serve
   it pinned comes back to Vibey as `pin_unavailable`. Vibey then sends it again in portable form.
3. No hop receives `native.payload` unless it is the recorded Source and its `replay` evidence is positive at
   request time. The transcript stores no replay authorization.
4. Every hosted call, native or proxy, yields exactly one canonical record. The card, citations and IM line render
   the same for either mode. One function renders a record to model-facing text, for proxied results and for
   synthetic replay alike. A failed record never renders as a successful empty result.
5. Any transcript with hosted calls replays to any supported target without a history-shape error.
6. `pause_turn` never ends a turn. Continuations are bounded, pinned to the paused response's Source, merged into
   one message, and their usage is summed.
7. No probe and no proxy call runs without a user action or a model tool call in a turn the user started. Every
   probe shows its cost first.
8. A real error creates evidence only through a narrow rule for the named (Source, model, feature).
9. A shown cost says whether it is reported, at API price, or included in a subscription.

## 11. Test plan

The tests are hermetic: a local stub server plays each provider's hosted-tool responses from the spike's redacted
shapes. No live upstream and no real credentials.

| Layer | Owner boundary | Cases |
| --- | --- | --- |
| Adapter parsing | each adapter against stub SSE | the mapping rule, one case per row of §4.2's table, including `queries[]` versus the deprecated `query`, every xAI X-search variant, and an unknown item becoming `other`; `hosted_summary` with one and with several X searches (reported cost once, no per-call copies); Anthropic citations; pending `server_tool_use` at `pause_turn`; a hosted call that fails |
| Cross-provider transform | `ai` request builders | each hosted block to each target, verified same Source versus other: native verbatim, synthetic call/result, split assistant message; citations never sent |
| `pause_turn` | Anthropic adapter, stub returns `pause_turn`, then `end_turn` | one logical response; the bound; the merged message with summed usage; the pin header; `pin_unavailable` falls back to the in-place text form |
| Gateway selection and pinning | real gateway + loopback fake engine | native only with evidence and an eligible credential; a pinned request is never failed over; an inadmissible pinned Source and a pre-output failure both answer `pin_unavailable`; Vibey's portable re-render is then served by failover; `x_search` to OpenAI never sent |
| Evidence | classifier against recorded error bodies | `400 Unsupported tool type`, `invalid_value` effort and `Invalid encrypted_content` each create exactly one record; overload and 5xx create none |
| Proxy executor | gateway endpoint, stub provider | executor choice; no effort sent; the record with excerpts; metering on the executor Source; error when no executor is verified; refusal of a Claude subscription |
| Record rendering and citations | the record-to-text function; `core/citations.py` builder | completed, failed, incomplete and pending records; every action's synthetic rule; summary cited sources listed once; provider annotations through to the Web sidecar and the IM Markdown link (end to end) |
| Config | `ModelHubConfig` loader | a released config without `hosted_tools` loads with all tools off; an unknown tool key degrades with a warning |
| Probes | gateway, stub provider | verified / unsupported / dropped / transient outcomes; a replay probe whose answer lacks the first search's URL is `unsupported`; probes only from the API action |
| UI | Models Tools tab, Source chips, tool card | toggle off by default; Verify shows the cost; chips per evidence state; card modes; IM line |
| E2E, once per release | real engine + stub upstream | a native web search and a proxied X search through the shipped engine on each protocol |

## 12. Contract checklist for the Phase 1 schemas

The JSON Schemas land with the Phase 1 implementation PR. There, a producer validates them, through the existing
`_validate` and mandatory-field tests for `HopResolution`, and new ones for the C-1 blocks. Each invariant below is
encoded either as a schema clause or as a producer test. The PR names which for each item.

**C-1 `message.schema.json`:**
1. `HostedToolBlock` requires `type`, `id`, `tool`, `action`, `record` and `native`.
   - `native` requires `source_id` and a non-empty `payload`.
   - It has no origin of its own and no replay flag.
2. `action` is one of the mapping rule's types (§4.2), with that type's fields required:
   - `search` with `queries`, at least one;
   - `open_page` with `url`;
   - `find_in_page` with `url` and `pattern`;
   - `x_search` with `variant` and `query`;
   - `x_user_search` with `query`;
   - `x_thread_fetch` with `ref`;
   - `other` with `name`.
3. `HostedRecord`: `status` is in its enum. `error` is present exactly for `failed` and `incomplete`. Each
   `sources[]` entry requires `url`.
4. `AssistantMessage.hosted_summary`:
   - it is present exactly when the message has a `HostedToolBlock`;
   - `cost.usd` is present exactly when `basis` is not `included`.
5. `TextBlock.citations`: `start` and `end` come together.
6. `ToolResultMessage.hosted` requires `record` and `execution`.
   - `execution` requires `mode: "proxy"`, `executor_origin`, `executor_source_id`, `executor_billing` and `cost`.
   - `is_error` is `true` exactly when `record.status` is `failed` or `incomplete`.

**C-6 `HopResolution.hosted_tools` (both copies):**
7. Each entry requires `mode` and `replay`.
   - `declaration` is present exactly for `native`.
   - `executor` (`{source_id, model_id}` or `null`) is present exactly for `proxy`.

**Model Hub:**
8. V2 `model_hub.hosted_tools`: keys are catalog tool names, and each entry requires `enabled` and `executor`.
   - `executor` is `null` or a pair of non-empty ids.
   - An unknown key degrades with a warning.
9. Overlay `providers` entries and the evidence record follow the field tables in the capability model (§3.1, §4),
   including: `observed` ⇒ `unsupported` with `detail`.
10. The `pin_unavailable` answer is exactly the 409 envelope in §3.5.
