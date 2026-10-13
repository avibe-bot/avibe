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
  selection and filtering.
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

- **The catalog is the overlay's `hosted_tools`** (capability model §4): for each provider model and protocol, the
  native declaration and the list price.
- **What it lets the gateway answer, without a network call:**
  - which tools exist;
  - which Sources might run each one natively;
  - what a call costs.

### 3.2 Verification

- **Live probes are the only way to `verified`.** They run on an explicit user action (capability model §3.3).
- **The `call` probe.** One request to the (Source, model) with the native declaration forced through `tool_choice`
  and a minimal prompt ("search for 'Avibe local-first agent OS', reply with the first result URL").
  - **`verified`:** the response completes and carries the tool's result. That is a `web_search_call` with
    `action.query`, a `server_tool_use` + `web_search_tool_result` pair, or, for X search, an xAI
    `custom_tool_call` named `x_keyword_search`.
  - **`unsupported`:** an HTTP 400 that names the tool type (observed: `Unsupported tool type: x_search`), or a
    completed response with **no** tool result despite the forcing. The second case is a translated path that drops
    the tool, as observed with `image_generation` on an Anthropic route.
  - **Not evidence:** a transient failure (overload, 5xx, timeout). The UI reports "couldn't verify, try again"
    (observed: every OpenAI-relay web search ended with an in-stream "servers overloaded").
- **The `replay` probe.** One follow-up request that carries the first response's native items verbatim.
  - **`verified`:** accepted.
  - **`unsupported`:** a 400 that names the replayed hosted content. Observed on the Anthropic relay, 3 of 3:
    `Invalid encrypted_content in search_result block`. xAI accepted verbatim replay, 3 of 3.
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
- **What a toggle stores** (Model Hub config): `enabled` and the chosen proxy executor (`source_id`, `model_id`). It
  stores no per-Agent copy: Vibey reads it through hop resolution.

### 3.4 Proxy executor

- **Endpoint:** `POST /vibey/v1/hosted-tools/{tool}` on the gateway, authenticated with the turn's gateway token.
  The body is the canonical arguments.
- **Executor choice.**
  - The configured executor, which must have `call: verified`.
  - Without one, the first verified (Source, model) in Vibey's Source order.
  - **Never a Claude subscription Source.** Anthropic restricts OAuth credentials to Claude Code and its own apps
    ([`model-hub-tos-review.md`](model-hub-tos-review.md)).
  - If no executor is verified, the call returns a tool error naming the fix ("verify X search on a Source in
    Models"). Vibey shows it like any tool error.
- **The request.** One short native call: the tool forced, a fixed instruction to search and list results, low
  effort, a small output cap.
- **The result** has two parts:
  - a **canonical record**: `queries`, `sources[]` with `url`, `title` and, where the provider returns them,
    `snippet` and `page_age`, `status`, and `cost`;
  - **the text the calling model sees**: numbered sources, each with title, URL and an excerpt.

  Excerpts are what make a result reusable. A model shown URLs alone treated the earlier answer as unsupported and
  said so; a model shown titles and snippets answered the follow-up without searching again. OpenAI and xAI return
  no page text, so the executor's instruction asks for a one-to-two-sentence excerpt per result. Anthropic supplies
  `cited_text`.
- **Cost and latency.**
  - Cost is one model call plus the tool fee: xAI about $0.01–0.11 per call; Anthropic $0.01 per search plus tokens.
    It is metered on the executor Source (usage ledger, provenance) and shown on the card.
  - Measured latency is 7 s (Anthropic) and 35–61 s (xAI at high effort). The executor runs at low effort; its
    latency is an acceptance measurement, not a promise.

### 3.5 Per-hop selection and failover

- **Hop resolution.** C-6 `HopResolution` gains `hosted_tools`: for each enabled tool, the primary hop's `mode`
  (`native` | `proxy`) and, for native, the wire declaration.
  - `native` requires the toggle on, `call: verified` for that (Source, model), and an untranslated path.
  - Vibey builds the request from it (§4.1).
- **Failover.** On failover to any other hop, the gateway re-applies the rule for that hop before sending:
  - A native declaration the hop is not verified for is replaced by the same-named proxy function.
  - Native hosted history whose recorded Source is not that hop, or whose replay is not verified there, is
    rewritten into the synthetic same-named call and result (§5). This is the same owner and rule as C-6 item 6, the
    opaque-payload strip.
- **Consequence:** a hop never receives a hosted tool or hosted payload it has no positive evidence for. Two
  observed failures become unreachable: `x_search` → OpenAI (`400 Unsupported tool type`) and `image_generation` →
  Anthropic (silently dropped).
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

- **New assistant content block `HostedToolBlock`** (`message.schema.json`):

  | Field | Meaning |
  | --- | --- |
  | `type` | `"hosted_tool"` |
  | `id` | canonical id |
  | `tool` | catalog name (`web_search`, `x_search`) |
  | `arguments` | canonical arguments, e.g. `{"query": "…"}` |
  | `record` | `HostedRecord`: `status` (`completed` / `failed` / `incomplete` / `pending`), `queries[]`, `sources[]` (`url`, `title?`, `snippet?`, `page_age?`), `cost?` (`usd`, `basis`: `reported` / `list` / `included`) |
  | `native` | `{source_id, origin, payload[], replay_verified}`: the provider's own items, verbatim and in order. `replay_verified` is the Source's `replay` evidence at write time. |
- **New optional `TextBlock.citations`:** `[{url, title?, cited_text?, start?, end?}]`, offsets in code points into
  `text`.
  - Citations belong to the text that cites them. Every provider attaches them there: OpenAI and xAI `url_citation`
    with offsets, and Anthropic `citations[]` on text blocks.
  - They are presentation only and are never replayed.
- **New optional `ToolResultMessage.hosted`:** a `HostedRecord` with `mode: "proxy"` and the executor's `origin`, so a
  proxied call renders like a native one.
- **Per adapter:**
  - **`openai_responses`:** `web_search_call` items, each a block with `arguments.query` from `action.query`.
    Annotations become citations.
    - xAI X search arrives as `custom_tool_call` `x_keyword_search` (`input` is a JSON string) followed by
      `reasoning` items with id `tco_…` that hold the tool output, encrypted.
    - Those reasoning items stay ThinkingBlocks as today. The `custom_tool_call` becomes the HostedToolBlock, and
      Vibey must not treat it as a local tool call. Today's adapter already ignores `custom_tool_call_input.*`.
  - **`anthropic`:** each `server_tool_use` and its `web_search_tool_result` become one block. Results become
    `sources`; `encrypted_content` is kept only in `native.payload`. Text-block `citations` become `TextBlock`
    citations.
  - **`openai_chat`:** no native hosted results in Phase 1.
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
  - C-2 still holds: exactly one `done` or `error`.
  - The paused partial and the continuation merge into one `AssistantMessage`.

### 4.4 In-flight turn replay

Within one turn, i.e. `pause_turn` or a client `tool_result` after server tools in the same assistant message, the
assistant content goes back to the same hop **verbatim**. Observed accepted:
- a pending `server_tool_use` (the server then ran it);
- ten `web_search_tool_result` blocks during a `pause_turn` continuation.

If a hop rejects in-flight verbatim replay (a narrow 400), the fallback replaces each server block pair **in place**
with one text block: `[web_search: <query>]` plus numbered titles and URLs. The signed thinking blocks stay
untouched; accepted, with no signature error. Dropping the pair is never used: the model then misread its own turn
("I called read_file before the search") and redid the work.

### 4.5 SSE caps

Phase 1 needs no change: the largest search event was 96 KB, against an 8 MiB line and 32 MiB event cap. For
images see §7.

## 5. Replay and conversion

These rules become C-1 rows in `cross-provider.md` and per-protocol rows in `conversion-tables.md`.

| History item | Target is the recorded Source with `replay: verified` | Any other target |
| --- | --- | --- |
| `HostedToolBlock` | `native.payload` verbatim, in place | synthetic same-named call plus result: Anthropic `tool_use` / `tool_result`, Responses `function_call` / `function_call_output`, Chat `tool_calls` / `tool` message. The result text is the record's numbered sources (title, URL, snippet). The assistant message is split around it. |
| `TextBlock.citations` | not sent | not sent |
| Proxy call (`ToolCallBlock` + `ToolResultMessage.hosted`) | ordinary tool call and result | same |

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
- **Vibey renders for the primary hop's origin. The gateway rewrites at failover** (§3.5), so a different Source or
  origin never receives `native.payload`.
- **C-6 change.** `x-avibe-served-hop` gains `source` (the serving Source id) so that `native.source_id` is exact.
  Two Sources can share an origin, e.g. a subscription and a relay serving the same model.
  - The served-hop parser and the gateway change in the same release.
  - The change ships in the deferred Model Hub contract v12 bump, the one #2483's closing spec describes
    (`terminal_error` for a pending attempt that never streamed). That makes one coordinated bump, not two
    (orchestrator decision, 2026-10-13).
  - Until v12, no hosted block is replayed natively across turns; every later turn gets the synthetic form. In-flight
    replay within one turn (§4.4) does not depend on the tag and is unaffected.

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
- **Citations** render through the existing citation contract: `core/citations.py` writes Markdown links plus the
  sidecar, which the Web renders as `CitationBadge`.
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
- **Anthropic OAuth policy:** no hosted tool runs natively or as proxy executor on a Claude subscription Source.
- **Relay findings on 2026-10-13:**
  - the OpenAI relay did not complete web search (overload, 5/5), so it is unverified;
  - the Anthropic relay rejects cross-turn verbatim replay, so replay there is unsupported;
  - xAI is fully verified for calls and replay.
- **Open:** OpenAI web search on a non-relay route, after the ChatGPT subscription's cooldown (2026-10-18).

## 9. Phasing

1. **Phase 0, capability foundation:**
   - the evidence store and narrow negative rules;
   - overlay `wire` / `client_preset` marking and `hosted_tools`;
   - Source chips for efforts;
   - context-tier valuation.
   This closes #2539.
2. **Phase 1, web search and X search:**
   - catalog, probes, the Tools tab, and the proxy executor;
   - `HopResolution.hosted_tools` and failover filtering;
   - the C-1 blocks and adapter parsing;
   - replay and conversion, `pause_turn` continuation, the tool card, and the IM line.
3. **Phase 2, image generation:** §7.

## 10. Acceptance properties

1. A hop receives a native hosted declaration only with the toggle on and positive `call` evidence for that
   (Source, model) on an untranslated path. Every other hop gets the same-named proxy function.
2. No hop receives `native.payload` unless it is the recorded Source with positive `replay` evidence.
3. Every completed hosted call, native or proxy, yields exactly one canonical record. The card, citations and IM line
   render the same for either mode.
4. Any transcript with hosted calls replays to any supported target without a history-shape error.
5. `pause_turn` never ends a turn. Continuations are bounded and merge into one message.
6. No probe and no proxy call runs without a user action or a model tool call in a turn the user started. Every
   probe shows its cost first.
7. A real error creates evidence only through a narrow rule for the named (Source, model, feature).
8. A shown cost says whether it is reported, at API price, or included in a subscription.

## 11. Test plan

The tests are hermetic: a local stub server plays each provider's hosted-tool responses from the spike's redacted
shapes. No live upstream and no real credentials.

| Layer | Owner boundary | Cases |
| --- | --- | --- |
| Adapter parsing | each adapter against stub SSE | Responses `web_search_call` + annotations; xAI `custom_tool_call` `x_keyword_search` + `tco_` reasoning; Anthropic `server_tool_use` / `web_search_tool_result` / citations; pending `server_tool_use` at `pause_turn`; a hosted call that fails |
| Cross-provider transform | `ai` request builders | each hosted block to each target, verified same Source versus other: native verbatim, synthetic call/result, split assistant message; citations never sent |
| `pause_turn` | Anthropic adapter, stub returns `pause_turn`, then `end_turn` | one logical response, the bound, the merged message |
| Gateway selection | real gateway + loopback fake engine | native only with evidence; failover to an unverified hop swaps native for proxy and rewrites history; `x_search` to OpenAI never sent |
| Evidence | classifier against recorded error bodies | `400 Unsupported tool type`, `invalid_value` effort and `Invalid encrypted_content` each create exactly one record; overload and 5xx create none |
| Proxy executor | gateway endpoint, stub provider | executor choice, the result text with excerpts, metering on the executor Source, error when no executor is verified, refusal of a Claude subscription |
| Probes | gateway, stub provider | verified / unsupported / dropped / transient outcomes; probes only from the API action |
| UI | Models Tools tab, Source chips, tool card | toggle off by default; Verify shows the cost; chips per evidence state; card modes; IM line |
| E2E, once per release | real engine + stub upstream | a native web search and a proxied X search through the shipped engine on each protocol |
