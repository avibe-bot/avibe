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
- **Phases.** Phase 1 is web search, X search and image generation (owner decision, 2026-10-13 19:10). There is no
  Phase 2.

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

- **One user-facing tool per capability.** Phase 1 has `web_search`, `x_search` and `image_generation`.
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
- `image_generation` is `{"prompt": string, "size"?: string}` (§7). That is the argument surface of OpenAI's
  image tool calls; `quality` and `background` are declaration configuration.

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
  declaration as a real turn on that (Source, model). Two things differ:
  - the transcript, which is one fixed user message;
  - for the `call` probe, `tool_choice` forcing the hosted tool, where the protocol allows it.

  So a probe cannot pass or fail for a shape that production never sends.
- **A probe has its own resolution.** Normal resolution gives native mode only after `call: verified` (§3.5), so a
  first verification could never send a native declaration. Model Hub therefore builds a probe resolution, and only
  for the user's probe action. It names the probed (Source, model) and carries the overlay's native declaration
  whatever the current evidence, with the endpoint built as the Vibey launch builds it (the Source's vendor as its
  provider). Vibey's adapter runs unchanged, and Model Hub serves every request it sends with its own invocation
  of exactly that Source, never the turn gateway, so there is no route and no failover. A resend the adapter makes
  for a field the endpoint refused before answering (Anthropic's one-hour cache lifetime) is one more such call;
  each probe step (call, then replay) sends at most two requests and runs the model once. A result is recognized only
  as the hosted call a real turn would keep. Normal turns never receive a probe resolution.
- **The `call` probe.** The fixed message is "search the web for a news article published today about technology and
  reply with its URL" (X search: "search X for posts about 'Avibe local-first agent OS' and reply with the first
  post's URL"). The web search asks for fresh articles because the replay probe asks about the other results: a
  fixed topic's other results (its site's other pages) can be inferred from the first, while a page published after
  the model was trained cannot, and a post's URL carries its id.
  - **`verified`:** the response completes and carries the provider's own result item for that tool (§4.2).
  - **`unsupported`:** only an HTTP 400 that names the tool type (observed: `Unsupported tool type: x_search`).
  - **Inconclusive, not evidence:** a completed response without that item. The model may simply not have used the
    tool, which looks the same as a silent drop. The UI says "the model didn't use the tool; try again".
    - Silent drops happen on translated paths, as observed with `image_generation` on an Anthropic route. Those
      paths are never native anyway (§3.5).
  - **Not evidence:** a transient failure (overload, 5xx, timeout). The UI reports "couldn't verify, try again"
    (observed: every OpenAI-relay web search ended with an in-stream "servers overloaded").
- **One action, both aspects.** `POST /api/models/hosted-tools/<tool>/probe` runs the `call` probe. When it
  verifies and is recorded, and the tool has a `replay` aspect on a protocol that replays natively, the `replay`
  probe follows at once, on the call's own turn. The `image_generation` call probe reads the image through Vibey's own
  parsing, which keeps it only when it is an image; the probe keeps no file. The Tools tab's estimate covers both
  requests.
- **The `replay` probe.** A second production request with the native declaration still present. Its history is
  the call probe's turn, with the native payload verbatim, followed by "list the URLs of the other results you
  found, without using any tool again". Nothing is forced.
  - **`verified`:** the answer names a result URL the call returned that its visible answer does not show (only the
    replayed payload holds it), and the response makes no new call to the tool.
  - **`unsupported`:** a 400 that names the replayed hosted content (observed on the Anthropic relay, 3 of 3:
    `Invalid encrypted_content in search_result block`), or a new call to the tool (§12 item 23). Transport
    acceptance alone is never verification. xAI accepted verbatim replay, 3 of 3.
  - **Inconclusive:** an answer naming no such URL, or a refusal, and any request that did not carry the native
    payload verbatim. With no hidden URL to ask about, no replay runs.
  - **Matching:** a result's URL is matched exactly but for the equivalences RFC 3986 allows (the case of its scheme
    and host, an empty path as `/`); only where prose ends a written URL is read both ways.
  - **Known by design:** a result that is a section or home page can still be guessed by a model whose payload was
    dropped. The fresh-article query makes such results rare, and a re-probe replaces a wrong record.
  - **Within the UI's wait:** the whole probe answers inside the UI's RPC timeout. Each call's deadline is the
    tool's (as for a turn's proxied call) or what the probe has left, whichever is shorter. A replay with under a
    minute left is not sent and reads as transient: try again.
- **Cost and when probes run.** A probe pair costs about $0.02–0.15 (spike: xAI $0.01–0.11 per call, Anthropic $0.01
  per search plus tokens). The Tools tab shows the estimate before running.
- **What changes probe results** (capability model §3.3). A Source identity change drops them. An engine upgrade or
  a probe-shape change makes them **current-wire stale**: they no longer describe the request being sent, so for
  routing they count as unknown (proxy and synthetic replay) until the user re-verifies. Age alone (30 days) only
  marks them for re-verification and keeps routing.
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

  - Fields, keyed by catalog tool name (`web_search`, `x_search`, `image_generation`):

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
  - Hop resolution picks the executor and returns it in `HopResolution.hosted_tools[tool].executor` (§3.5), for
    every enabled tool and in either mode. The pick is the configured executor if it has `call: verified`, otherwise
    the first verified (Source, model) in Vibey's Source order.
  - The proxy call sends that binding with the turn token, and the gateway runs that executor and no other.
  - A config change (another executor, a disabled tool, a removed Source) applies from the next turn, as every
    runtime config change does (AGENTS.md).
  - The binding is held for the turn. Ordinary edits to the executor's Source (models, order, name) apply from the
    next turn. Only removing or disabling that Source, which takes it off every hop at once, makes a bound call
    return a tool error. The call never silently switches Source or billing.
  - **Only an eligible credential.** A Claude subscription Source is never eligible: not as executor, not for a
    probe, and not for native mode (§3.5). Anthropic restricts OAuth credentials to Claude Code and its own apps
    ([`model-hub-tos-review.md`](model-hub-tos-review.md)).
  - If no executor is verified, the call returns a tool error naming the fix ("verify X search on a Source in
    Models"). Vibey shows it like any tool error. A call that never reached an executor has a `failed` record and no
    `execution` (§4.2).
- **The request depends on the tool.** For a search tool, it is one short native call with the tool forced, a fixed
  instruction to search and list results, and a small output cap. For `image_generation`, it is one call with the
  tool forced that renders the supplied `prompt` at the supplied `size` (§7.2).
  - It sends **no reasoning effort**. The executor (Source, model) is verified for the tool, not for any effort, and
    an unverified effort could fail a verified tool.
- **The result is the canonical record only:**
  - the `HostedRecord`, with the action, `status`, `error` and `sources[]` (`url`, `title`, and, where available,
    `snippet` and `page_age`).
    - A proxied call is exactly one executor response. So when the provider returns no per-call results (xAI X
      search), the record's `sources` are that response's cited posts. A native response with several calls keeps
      those in `hosted_summary` instead (§4.2).
    - Either way the calling model gets numbered posts with links and excerpts;
  - the `ProxyExecution`, when an executor ran: executor origin, Source, billing mode, and the call's cost. A
    proxied call is a whole executor response, so its cost is per call.
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
  - the executor bound for this turn, `{source_id, model_id}` or `null` when none is verified (§3.4). It is present
    for **every** enabled tool, whatever the mode, because a native request that comes back `pin_unavailable` is
    re-rendered with the proxy function within the same turn;
  - `replay`, the **current** `replay` evidence for the primary hop's Source and model.

  `native` requires all of these:
  - the toggle on;
  - an eligible credential (§3.4);
  - `call: verified` for that (Source, model), taken on the current engine and probe shape (§3.2);
  - an untranslated path.

  `replay` is likewise true only for `replay: verified` evidence taken on the current engine and shape.

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

  **As implemented (orchestrator decision, 2026-10-14):** every model attempt resolves a fresh hop (C-6 item 3), so
  verbatim content goes only to the hop whose own Source served it, on the same origin, and every pin is
  `HopResolution.source_id`. A continuation whose fresh hop is another Source takes the in-place text form (§4.4),
  unpinned. Pinning it to the previous Source would build the request for one hop (declarations, model, efforts,
  signature target) and serve it from another. A continuation also sends `x-avibe-pin-model` with its served model
  when that id is ASCII. Without the header, the gateway takes the pinned Source's own model, which is the same hop.
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
| `openai_responses` (OpenAI, xAI) | `{"type": "web_search"}`, `{"type": "x_search"}`, `{"type": "image_generation", "partial_images": 2, "output_format": "jpeg", "output_compression": 90}`, plus the overlay entry's `include` values. OpenAI's web search requires `include: ["web_search_call.action.sources"]`, merged with Vibey's existing `reasoning.encrypted_content`; without it, `action.sources` is not returned. xAI's `web_search_call` carried `action.sources` without it (spike). | `{"type": "function", "name": "web_search", "parameters": <canonical>}` |
| `anthropic` | `{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}` | `{"name": "web_search", "input_schema": <canonical>}` |
| `openai_chat`, `google` | none in Phase 1 | a function with the canonical schema. Vibey reaches a `google` hop over Chat Completions at the gateway (C-1 conversion tables), so the Chat form applies, and the engine translates it to Gemini `functionDeclarations`. |

- Vibey takes the mode from `HopResolution.hosted_tools`. It never decides nativity itself.
- The probe and the proxy executor use these same declarations, `include` values included (§3.2, §3.4).
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
| xAI `custom_tool_call` `x_keyword_search` / `x_semantic_search` | `x_search` | `{type: "x_search", variant: "keyword" \| "semantic", query, limit?, mode?}`; `limit` and `mode` are kept when the call carries them |
| xAI `custom_tool_call` `x_user_search` | `x_search` | `{type: "x_user_search", query}` |
| xAI `custom_tool_call` `x_thread_fetch` | `x_search` | `{type: "x_thread_fetch", ref}` |
| OpenAI `image_generation_call` | `image_generation` | `{type: "generate", prompt: revised_prompt, size?}`; the image is saved to a file (§7.3) |
| any other item under a declared hosted tool | that tool | `{type: "other", name}`, with the provider's item name |

**`HostedToolBlock`** (assistant content):

| Field | Rule |
| --- | --- |
| `type` | `"hosted_tool"` |
| `id` | canonical id |
| `tool` | catalog name: `web_search`, `x_search` or `image_generation` |
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
| `sources[]` | this call's own results: `url` required; `title`, `snippet`, `page_age` optional. Empty when the provider returns none per call (xAI X search), except in a proxied call's record (§3.4). |
| `images[]` | `image_generation` only: `{path, mime, width?, height?}` for each saved file. Never base64. |

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

**`ToolResultMessage.hosted`** (optional): `{record, execution?}`.
- `execution` is a `ProxyExecution`: `mode: "proxy"`, `executor_origin`, `executor_source_id`,
  `executor_billing`, and `cost` when the pricing module can value the call. It is present exactly when an executor ran. A call that never
  reached one (none verified, or the bound Source removed) has none, and its `record.status` is `failed`.
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
  - **As implemented (orchestrator decision, 2026-10-14):** a continuation goes only to the Source that served the
    paused response, pinned there, and is never re-rendered in portable form. A continuation that Source answers
    `pin_unavailable`, or a pause of a send that carried no pin, ends the response with one terminal
    `ProviderError(kind="interrupted")` whose partial keeps all assembled content, as at the bound. The merged
    message is then always one origin's, so no paused part needs a stored form other than the one it streamed in.
    The merged message is verified only when every response in it was verified and reported the same origin;
    otherwise it is sanitized as any unverified response. The case given up is rare: the pinned Source becoming
    unavailable seconds into a paused turn. The user keeps the partial answer and can retry. A turn's first request
    still gets its one portable resend on `pin_unavailable` (§3.5).
  - **After the 5th continuation**, if the response is still `pause_turn`, the adapter ends with one terminal
    `ProviderError(kind="interrupted")`. Its partial keeps all assembled content and the summed usage, and its
    `error_message` is "the provider paused the turn more than 5 times". C-2's existing rule then applies: terminal
    after text or a tool call, retried once after thinking alone.
  - C-2 still holds: exactly one `done` or `error`.
  - The paused partial and the continuation merge into one `AssistantMessage`.
  - Its `usage` is the **sum** of every continuation response's counters: input, output, and cache read and write.
    Today's parser resets usage at each `message_start`. The gateway already meters each continuation as its own
    call, so the usage ledger and provenance need no change.
  - **As implemented (orchestrator decision, 2026-10-14):** the merged message carries the **final** continuation's
    usage, not the sum. The context estimate anchors on a response's usage as the size of the request that produced
    it, and the final request's input already holds every paused part, so a sum would roughly double the next
    estimate. Billing and the ledger come from the gateway's per-call metering, and `hosted_summary` still sums
    counts and cost across continuations. Vibey-side token sums that read message usage, such as side-turn audit
    totals, undercount the earlier continuations.
  - **Pending calls carry over by id.** The adapter keeps its hosted blocks keyed by tool-call id across
    continuations. A result arriving in a continuation for a `pending` block finalizes that block, its `record` and
    its `native.payload`, so each call stays exactly one block.
  - **One `hosted_summary` for the merged message.** `counts` and `cost.usd` are summed over the continuation
    responses. `cost.basis` is `reported` only when every response reported a figure, and stays `included` on a
    subscription Source; otherwise the pricing module values the whole message at list price. `cited_sources` are
    deduplicated by URL in first-seen order.

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
images see §7.5.

## 5. Replay and conversion

These rules become C-1 rows in `cross-provider.md` and per-protocol rows in `conversion-tables.md`.

| History item | All three hold: the target hop is `native.source_id`; the target's origin (provider, api, model) equals the message's `origin`; and `HopResolution.hosted_tools[tool].replay` is true now | Any other target |
| --- | --- | --- |
| `HostedToolBlock` | `native.payload` verbatim, in place | a synthetic same-named call plus result (Anthropic `tool_use` / `tool_result`, Responses `function_call` / `function_call_output`, Chat `tool_calls` / `tool` message), with the assistant message split around it. One record-to-text function (§3.4) renders the result, under the rules below. |
| `TextBlock.citations` | not sent | not sent |
| Proxy call (`ToolCallBlock` + `ToolResultMessage.hosted`) | ordinary tool call and result | same |

- **Google targets.** Vibey speaks Chat Completions to a `google` hop (C-1 conversion tables), so the Chat pair
  applies. The engine translates it to a Gemini `functionCall` part in the model turn and a `functionResponse` part
  in the next user turn, in the same order. A failed record becomes `response: {error: <text>}`.
- **Synthetic rendering rules**, by action:
  - A `search` action becomes one call with arguments `{query: queries[0]}`. Its result lists any further queries,
    then the record's numbered sources (title, URL, snippet).
  - An `x_search` or `x_user_search` action becomes one `x_search` call with `{query}`, plus `limit` and `mode` when
    the action has them.
  - An `image_generation` action becomes one `image_generation` call with `{prompt}`, plus `size` when the action
    has it. Its result is the **text path record** (§7.3). Image bytes are never resent: no base64 and no image
    block, even to the same Source.
  - A non-search action (`open_page`, `find_in_page`, `x_thread_fetch`, `other`) is its own call with
    `{query: "<action type> <target>"}`. A completed one's result says what it did, e.g. "Opened <url>" or "Fetched
    thread <ref>".
    - **Deviation (orchestrator decision, PR #2561 review).** The design first folded such an action into the result
      of its tool's preceding call. Each rendering defect found in review came from that folding, where a line could
      land on a result that does not show it. So every hosted block is exactly one call, with its own status.
  - The response's `hosted_summary.cited_sources` are listed once, as "posts cited in the answer". They go on the
    first call that was not cut off, whether it is replayed as a synthetic call or in place.
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
  - **The v12 bump edited these exact-shape contracts together** (Phase 1 foundation PR):
    - `model-hub-contracts/served-hop.schema.json` (new): the header is exactly `provider`, `api`, `model`,
      `source`. `hop-origin.schema.json` stays the persisted provenance origin (`provider`, `api`, `model`), because
      provenance attempts already carry `source_id` beside it and records written at v5–11 must keep validating;
    - `model-hub-contracts/vibey-consumer.md`, Response origin;
    - `agent-core-contracts/provider.md`, the served-hop sentence;
    - `agent-core-contracts/conversion-tables.md`, the Origin row;
    - `agent-core-contracts/model-hub-consumer.md`, item 4.

    A message's C-1 `origin` stays exactly `provider`, `api`, `model`; the served `source` tags `native.source_id`.

## 6. User interface

### 6.1 Models: the Tools tab

- **One row per tool** (Phase 1: Web search, X search, Image generation), reusing the Sources tab's row and chip
  vocabulary (`chips.tsx`, `SourceRow`, `TimeHint`):
  - **toggle**, off by default;
  - **runs natively on:** chips per verified (Source, model), e.g. "xAI API key · grok-4.6 · verified 10-13";
  - **proxy executor:** a picker of verified candidates, each with billing mode and cost hint;
  - **cost per call:** "≈ $0.01 at API price" for metered, "included in subscription" for monthly, or "reported by
    xAI" when the last call returned a cost.
- **Verify on…** lists the Sources whose overlay row offers the tool but that have no evidence. Each entry has an
  explicit **Verify** button showing the probe's estimated cost. Nothing probes on its own.
- **Turning a tool on with no verified Source** opens that list. It never auto-probes.
- **After an engine upgrade**, native evidence from the previous engine counts as unknown, so those tools run
  through the proxy. The tab says why in one header line, "Engine upgraded · N need re-verification" (zh:
  "引擎已升级 · N 项需重新检测"), with the existing check action listing those entries. Nothing re-probes in the
  background.

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
- **Image generation card:** the prompt, the mode and the cost, with the image rendered inline from its file. IM
  attaches the file, with the same one-line form. The model sees only the path record (§7.3).

## 7. Image generation (Phase 1)

### 7.1 The injected tool moves from the engine to the gateway, per caller

- **Today.** The engine injects `{"type": "image_generation", "output_format": "png"}` into every Codex-executor
  request: CPA `ensureImageGenerationTool`, with `disable-image-generation` unset in
  `vibe/model_hub_runtime/config.py`. That covers OpenAI API keys, the ChatGPT subscription and xAI keys, for any
  caller, Claude Code and OpenCode included. GPT calls it from a Vibey request when asked for a picture; the image is
  billed and then dropped.
- **How Codex consumes it today.**
  - Codex maps a hosted `image_generation_call` item to a `TurnItem::ImageGeneration` (`codex-rs/core/src/event_mapping.rs`)
    and emits app-server `imageGeneration` thread items.
  - Avibe's Codex backend persists those items as files: `modules/agents/codex/event_handler.py`
    `_persist_generated_image`, covered by `tests/test_codex_event_handler.py`.
  - Codex's own `image_gen` extension tool is gated to OpenAI-auth providers (`codex-rs/core/src/tools/spec_plan.rs`
    `image_generation_available`). So through Model Hub, the injected hosted tool is Codex's only image path, and it
    must be preserved exactly.
- **Decision (orchestrator, 2026-10-13):**
  1. The engine runs with `disable-image-generation: passthrough`, a fixed key in `config.py`. CPA then forwards
     requests with exactly the tools the client declared, never injecting and never stripping.
  2. The gateway reproduces CPA's injection **only for Codex CLI requests** (`backend == "codex"`), byte for byte,
     under CPA's own conditions:
     - the hop's Source is served by CPA's Codex executor (an `openai_responses` API key or the ChatGPT subscription);
     - the model id does not end in `spark`;
     - the credential is not a ChatGPT free plan, read from the same plan attribute CPA reads;
     - the body's `client_metadata.ws_request_header_x_openai_internal_codex_responses_lite` is not true. The header
       form is not forwarded by the gateway today, so it is not consulted.

     It then appends the same tool to `tools`, or sets `tools` to it when absent. It leaves the body unchanged when
     an `image_generation` tool, or the `image_gen` function or namespace, is already present.
  3. Vibey declares `image_generation` itself, only when the user's toggle is on.
- **Effect on other backends.** Claude Code and OpenCode requests routed to a Codex-executor Source stop receiving
  the hidden image tool. They could not show the result, so this removes billed images that nobody saw.
- **Rule boundary.** "The gateway never rewrites hosted content" (§3.5) is about Vibey's requests. This injection is
  a compatibility step for one native caller and adds the tool CPA added.
- **Regression tests.**
  - **Gateway, table-driven from CPA's injection cases:** for a Codex request, the fake engine receives exactly the
    bytes CPA would have produced. One row per condition: no tools, other tools, an existing image tool, the
    `image_gen` function and namespace, `spark`, free plan, and lite metadata.
  - **Other backends:** Claude, OpenCode and Vibey requests reach the engine with no image tool unless they declared
    one.
  - The existing Codex image persistence tests stay unchanged.
  - **E2E once per release:** the real engine in passthrough, a stub upstream, a Codex request. The upstream sees
    exactly one image tool and the Codex backend persists the image.

### 7.2 Native and proxy

- **Native** on verified OpenAI-protocol Sources. The probe is an `image_generation` call probe with a small image.
- **Proxy** for other models: the executor is a verified image-capable Source. The executor saves the image as a
  file and returns its path record. A proxied call's result is the same text path record as a native one (§7.3).

### 7.3 Files and a text path record (owner decision, 2026-10-13 19:14)

- **The model is not given the image by default.** Each image is saved as a local file in the session's media
  store, decoded as the event is parsed.
- **The model gets a text path record:** the file path, the revised prompt, the size, and the mode and cost, e.g.
  `[image saved: /…/image-3f2a.png · 1536×1024 · native (OpenAI API key) · ≈ $0.04] prompt: "…"`.
  - If the model wants to look, it opens the file with its `read` tool, the same as any other file.
  - The file the record names must fit `read`'s inline image cap. `MAX_INLINE_IMAGE_BYTES` in
    `core/agent_core/tools/read.py` is about 3.4 MiB, and `read` does not resize. A high-quality PNG of 4–6 MB would
    come back as "Image omitted". Phase 1 bounds it in the declaration: OpenAI's asks for `output_format: "jpeg"`
    at `output_compression: 90`, so a 1.6 MP image is about 0.3–1 MB, and every IM platform shows JPEG. An image
    still over the cap gets a record saying it is too large to view inline.
  - Known limitation: no transparent backgrounds in Phase 1. A later per-call format choice (an
    `output_format`/`background` argument on the tool) restores them.
  - Known limitation (orchestrator decision, 2026-10-14): a file is saved before the row that names it commits. A
    cancel, crash or kill in between leaves a file no row references. Such files are content-addressed, so they never
    duplicate, and the per-response image budget bounds them. A startup sweep of the agent media root removes them
    (#2588, before the 3.2.1 RC).
  - Avibe-authored failure reasons in a record's `error` are English model-facing text. Each carries a stable
    `error_code` (C-1 `HostedRecord.error_code`, one of `HOSTED_ERROR_CODES`), by which the display boundary localizes
    it; a provider's own reason carries none and shows as it is (#2589).
- **The user sees the image.** The Workbench renders it inline from the file, and IM attaches it. That is display,
  separate from model context.
- **History and replay never contain base64.**
  - `HostedRecord.images` holds the path records.
  - On the native OpenAI path, the transcript's `image_generation_call` item has its `result` replaced by the path
    record in `native.payload`.
  - Because a replaced `result` is no longer a valid image result, an `image_generation` block is never replayed
    verbatim. Every target, the producing Source included, gets the synthetic call plus the text path record (§5).
    The `replay` aspect is never probed for this tool.

### 7.4 Byte flow and the idle bound

- **Partial images.** Vibey's declaration requests `partial_images: 2`, so `response.image_generation_call.partial_image`
  events carry bytes during generation.
  - The gateway counts a partial image as model output (`stream_wire.py`), so response headers are released at the
    first partial.
  - Phase 1 drops previews: Vibey counts each one against the response's image budget and keeps none, so no preview
    reaches a file, history or model context, and the user sees the final image. Showing previews while an image is
    drawn is a later display change: a temporary file the final image replaces.
- **What was measured.** A failing generation went 35.4 s without a byte after `generating`; healthy generations took
  64–130 s.
- **Phase 1 acceptance gate.** Through the gateway, with partials, the largest byte gap of every successful test
  generation stays under #2538's 120 s bound on `openai/openai_responses`. If it does not, the gateway treats an
  open `image_generation_call` as expected silence before Phase 1 ships.

### 7.5 SSE caps

- **Sizes on the wire.** One final image is about 4.1–4.2 MB of base64 at the relay's forced `quality: low`, and
  arrives **twice**: in `response.output_item.done` and in the engine-rebuilt `response.completed`. Each partial is
  another full image. High quality is plausibly 5.5–8 MB per copy.
- **Vibey's limits for a Responses stream that declares `image_generation`:**
  - the line cap is raised from 8 MiB to the 32 MiB event cap;
  - image base64 is decoded to file as it is parsed and is excluded from the 32 MiB cumulative assembled-output
    budget;
  - the adapter takes the image from `output_item.done` and discards the duplicate `result` in `response.completed`
    without decoding it.
- Other streams keep the 8 MiB line cap.

## 8. Cost and limits

- **Per-call prices** live in the overlay with date and source (capability model §6.2):
  - OpenAI web search: $10 / 1k calls plus content tokens;
  - Anthropic: $10 / 1k searches plus result tokens, re-billed while they stay in context;
  - xAI: web search $5 / 1k calls, X search $5 / 1k posts and $10 / 1k profiles;
  - OpenAI image generation: OpenAI lists no per-image price; image generation is billed per image token at the
    image model's rate (2026-10-13 pricing page). The overlay's `image_generation` entry therefore has no list
    price, and the cost comes from the call's image-token usage at that rate (Phase 1 pricing PR).
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
2. **Phase 1, web search, X search and image generation:**
   - catalog, probes, the Tools tab, and the proxy executor;
   - `HopResolution.hosted_tools`, request pinning and `pin_unavailable`, and the V2 `hosted_tools` config;
   - the C-1 blocks and adapter parsing;
   - replay and conversion, `pause_turn` continuation, the tool card, and the IM line;
   - image generation (§7): engine passthrough, the gateway's per-caller injection for Codex, files instead of
     base64, partial images, and the SSE caps.

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
6. `pause_turn` never ends a turn while its Source can continue it. Continuations are bounded, pinned to the
   paused response's Source, and merged into one message that carries the final request's usage and summed hosted
   counters (§4.3 as implemented). Past the bound, or when that Source cannot continue it, the turn ends with one
   `interrupted` error that keeps the partial.
7. A generated image never enters model context or history as bytes. The model gets the text path record, and the
   user sees the image.
8. A Codex CLI request reaches the upstream with exactly the image tool CPA would have injected. No other caller
   gets one unless it declared it.
9. No probe and no proxy call runs without a user action or a model tool call in a turn the user started. Every
   probe shows its cost first.
10. A real error creates evidence only through a narrow rule for the named (Source, model, feature).
11. A shown cost says whether it is reported, at API price, or included in a subscription.

## 11. Test plan

The tests are hermetic: a local stub server plays each provider's hosted-tool responses from the spike's redacted
shapes. No live upstream and no real credentials.

| Layer | Owner boundary | Cases |
| --- | --- | --- |
| Adapter parsing | each adapter against stub SSE | the mapping rule, one case per row of §4.2's table, including `queries[]` versus the deprecated `query`, every xAI X-search variant, and an unknown item becoming `other`; `hosted_summary` with one and with several X searches (reported cost once, no per-call copies); Anthropic citations; pending `server_tool_use` at `pause_turn`; a hosted call that fails |
| Cross-provider transform | `ai` request builders | each hosted block to each target, verified same Source versus other: native verbatim, synthetic call/result, split assistant message; citations never sent |
| `pause_turn` | Anthropic adapter, stub returns `pause_turn`, then `end_turn` | one logical response; the bound; the merged message with the final request's usage and summed hosted counters; the pin header; `pin_unavailable` on a continuation ends `interrupted` with the partial (§4.3 as implemented) |
| Gateway selection and pinning | real gateway + loopback fake engine | native only with evidence and an eligible credential; a pinned request is never failed over; an inadmissible pinned Source and a pre-output failure both answer `pin_unavailable`; Vibey's portable re-render is then served by failover; `x_search` to OpenAI never sent |
| Evidence | classifier against recorded error bodies | `400 Unsupported tool type`, `invalid_value` effort and `Invalid encrypted_content` each create exactly one record; overload and 5xx create none |
| Proxy executor | gateway endpoint, stub provider | executor choice; no effort sent; the record with excerpts; metering on the executor Source; error when no executor is verified; refusal of a Claude subscription |
| Record rendering and citations | the record-to-text function; `core/citations.py` builder | completed, failed, incomplete and pending records; every action's synthetic rule; summary cited sources listed once; provider annotations through to the Web sidecar and the IM Markdown link (end to end) |
| Config | `ModelHubConfig` loader | a released config without `hosted_tools` loads with all tools off; an unknown tool key degrades with a warning |
| Image injection | gateway + loopback fake engine | the §7.1 table (CPA's conditions, byte for byte); Claude, OpenCode and Vibey requests get no image tool unless declared; the existing Codex image persistence tests unchanged |
| Images | Responses adapter against stub SSE | partials then a final image twice: one file written, no base64 in history, the duplicate in `response.completed` not decoded, a 9 MiB image line accepted under the image cap, the text path record as model context |
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
   - `x_search` with `variant` and `query`, and optional `limit` and `mode`;
   - `x_user_search` with `query`;
   - `x_thread_fetch` with `ref`;
   - `generate` with `prompt`, and optional `size`;
   - `other` with `name`.
   - `generate` is valid only when `tool` is `image_generation`, and `images[]` is present exactly then.
   - No field anywhere holds image base64.
3. `HostedRecord`: `status` is in its enum. `error` is present exactly for `failed` and `incomplete`. Each
   `sources[]` entry requires `url`.
4. `AssistantMessage.hosted_summary`:
   - it is present exactly when the message has a `HostedToolBlock`;
   - `counts` and `cited_sources` are required (possibly empty); `cost` is optional, absent when no price is known,
     because unknown is never priced as zero;
   - `cost.usd` is present exactly when `basis` is not `included`.
5. `TextBlock.citations`: `start` and `end` come together.
6. `ToolResultMessage.hosted` requires `record`. `execution` is present exactly when an executor ran; without it,
   `record.status` is `failed`.
   - `execution` requires `mode: "proxy"`, `executor_origin`, `executor_source_id` and `executor_billing`; its
     `cost` is optional on the same rule as item 4.
   - `is_error` is `true` exactly when `record.status` is `failed` or `incomplete`.

**C-6 `HopResolution.hosted_tools` (both copies):**
7. Each entry requires `mode`, `replay` and `executor`.
   - `executor` is `{source_id, model_id}` or `null`, and is present for every entry whatever the mode.
   - `declaration` and `include` (the overlay entry's Responses `include` values, possibly empty) are present exactly
     for `native`.
   - `hosted_tools` itself is required, and empty when no tool is enabled.

**Model Hub:**
8. V2 `model_hub.hosted_tools`: keys are catalog tool names, and each entry requires `enabled` and `executor`.
   - `executor` is `null` or a pair of non-empty ids.
   - An unknown key degrades with a warning.
9. Overlay `providers` entries and the evidence record follow the field tables in the capability model (§3.1, §4),
   including: `observed` ⇒ `unsupported` with `detail`.
10. The `pin_unavailable` answer is exactly the 409 envelope in §3.5.
11. Native replay requires all three: same Source, same origin, and current `replay` evidence. `image_generation`
    blocks are never replayed verbatim, and their synthetic call carries `size` when the action has it.
12. A probe with no tool item is inconclusive and writes no evidence.
13. The engine config renders `disable-image-generation: passthrough`. The gateway's Codex image injection is a
    single function covered by the §7.1 table.

**Added after review of the final design head (implementation-level, binding on the Phase 1 PR):**

14. A probe resolution exists only for the user's probe action. It pins the probed (Source, model), carries the
    overlay's native declaration whatever the current evidence, and allows no failover (§3.2).
15. The executor request depends on the tool: search tools search and list results; `image_generation` renders the
    supplied `prompt` and `size` (§3.4).
16. Across `pause_turn` continuations, a result for a `pending` block finalizes that block by tool-call id. The
    merged message has one `hosted_summary`, with summed `counts` and `cost.usd` and URL-deduplicated
    `cited_sources` (§4.3).
17. Image cost is valued from the call's image-token usage at the image model's per-token rate, because OpenAI
    lists no per-image price (capability model §4). If a vendor later lists per-image prices, those entries are
    keyed by the resolved `quality` and `size` (item 22).
18. The image file a path record names fits `read`'s `MAX_INLINE_IMAGE_BYTES`, tested with a 6 MB generated image
    (§7.3).
19. The executor binding is held for the turn. Only removal or disablement of the bound Source makes a call fail,
    with a tool error and no Source switch (§3.4).
20. Hosted-tool `call` and `replay` evidence whose `engine` or `shape` differs from the current one resolves as
    unknown: proxy mode and synthetic replay. Age-only staleness keeps routing. Effort evidence is unchanged
    (capability model §3.3, §3.4).

**Added after review of the merged design (implementation-level, binding on the Phase 1 PRs):**

21. Continuations merge into one `AssistantMessage` only when the served-hop report shows the same origin. A
    `pin_unavailable` continuation served by another origin makes the merged message take the serving origin, and
    the paused part is kept in the portable form that was actually sent (§4.4 fallback, C-1 cross-origin rules), so
    no payload or signature from the first origin remains.
    **As implemented (orchestrator decision, 2026-10-14):** there is no portable continuation, so no cross-origin
    merge (§4.3). A continuation whose response reports another origin, or none, leaves the merged message
    unverified, so it keeps no payload or signature of any response.
22. The `generate` action records the output's resolved `quality` and `size`; pricing uses those output values, not
    the declaration's.
23. The replay probe asks for a result URL the call returned that its visible answer does not show, since only the
    replayed native payload holds it. With no such URL (a single result, or results that stay encrypted, such as
    xAI's X search) no replay runs and replay evidence stays unknown, so turns replay synthetically. An answer without
    such a URL, or a refusal, is inconclusive; `unsupported` comes only from a 400 naming the replayed content or a new
    call to the tool.
24. For OpenAI and xAI executors, each source's `snippet` is the answer text its `url_citation` span covers, extended
    to the enclosing sentence; the executor instruction asks for one or two cited sentences per result so the spans
    align. A source without a span has no snippet; Anthropic keeps `cited_text`. Tested against a recorded executor
    response.
25. Each response has a maximum number of image events (final plus requested partials, plus a small margin) and a
    cumulative decoded-byte budget; exceeding either ends the stream as a provider error. The terminal duplicate is
    still discarded undecoded.
26. Context-tier thresholds use total context input: input plus cache-read and cache-write tokens.
27. The proxy endpoint returns a generated image's bytes to Vibey's tool runtime over the local hop. Vibey saves them
    through the same session media sink the native path uses, then builds the `HostedRecord` with the path record.
    The bytes are never persisted in a transcript, and Vibey stays the only writer of session media.
28. `generate.prompt` is required only for a `completed` record; a `failed` or `incomplete` record may omit it when
    the provider's `revised_prompt` is absent or null.
