# Vibey hosted tools

Status: **design v2**, 2026-10-15, owner-approved direction (passive, default-on, simpler). It replaces design v1
(2026-10-13), whose active-check model the owner rejected after looking at rc4's Tools tab.
- Scope: Vibey only. Claude Code, Codex and OpenCode are unchanged, and there is no CLI.
- Foundation: [`model-hub-capability-model.md`](model-hub-capability-model.md), which covers priors, evidence and
  pricing.
- Evidence: the runtime spike of 2026-10-13 (40 + 10 upstream calls through the local engine on API-key relay
  Sources). Its tables are summarised where a rule depends on them.
- What is already built (rc4) and what v2 removes or changes is listed in §11; the implementation split is §12.

## 0. Owner decisions this design implements

- **No active detection.** Nothing calls a provider to find out whether a tool works: no user-started check, no
  check-all, no background probe. "Check all" would have probed hundreds of (Source, model) pairs: a large bill,
  rate limits and possibly provider risk control. Users never need to understand Source × model × tool.
- **One switch per tool, on by default.** The cost and privacy consequences are stated plainly in the public docs
  (§9), not in onboarding.
- **Assume from rules, call blind, learn passively.** A route is assumed to support a tool when its vendor offers it
  (§3.1). Real use teaches the rest: a real response that carries the tool's item is positive evidence, and a real
  error that names the tool is negative evidence (§3.2).
- **Show what real use learned.** The UI shows the last real execution and any refusal, never a guess (§8).
- **The agent decides what to declare; Model Hub supplies facts.** Model Hub states per hop and tool what the
  priors and evidence say. Vibey decides each request's declarations, and re-renders when the gateway reports a
  refusal (§3.5).
- **Image generation is one direct tool.** `image_generate` names its model explicitly and runs on the provider's
  image endpoint, never as a hosted declaration on a chat request (§7).
- **Pre-release state is not migrated.** 3.2.0 stable has no hosted tools. rc-era probe evidence and rc-era
  `hosted_tools` config are dropped or ignored, with no compatibility code (owner directive).

## 1. Example

The user has an Anthropic API key, an xAI API key and an OpenAI API key, and has changed no tool setting.

1. Vibey runs on Claude Opus through the Anthropic key. Anthropic offers web search on the `anthropic` protocol, and
   nothing says this Source refuses it, so `web_search` is declared natively. X search is not offered by Anthropic,
   so `x_search` is declared as an ordinary function with the canonical schema (§2).
2. Claude calls `x_search({"query": "from:OpenAI within_time:7d"})`. Vibey's tool runtime sends it to the gateway's
   executor. The gateway builds the executor chain for X search (§6): the xAI key is the only Source whose vendor
   offers it. It sends one short request to xAI's lightweight model with X search forced, and returns numbered
   posts with links and excerpts.
3. The tool card shows the query, "via xAI API key" with xAI's lightweight model, and the cost xAI reported. Models → Tools now
   says "X 搜索 · 最近一次由 xAI API key 执行 · 正常 · 10-15 14:02".
4. Claude then asks for a picture. It calls `image_generate({"model": "gpt-image-2", "prompt": "…"})`. The gateway
   sends the prompt to the OpenAI key's image endpoint, Vibey saves the image as a file, and the model receives the
   text path record (§7.3). The card shows the image and "gpt-image-2 · OpenAI API key".
5. Next turn the user switches Vibey to a relay Source that declares itself `openai` but refuses web search with
   `400 Unsupported tool type: web_search`. The gateway records that refusal for the relay (§3.2) and answers the
   pinned request `pin_unavailable` with the reason. Vibey's loop re-resolves the hop, which now says the relay does
   not run web search natively, and resends with the `web_search` function instead (§3.5). Models → Tools shows
   "网页搜索 · relay 不支持，已改用 Anthropic API key".

## 2. Concepts

- **One user-facing tool per capability.** `web_search`, `x_search` and `image_generate`.
- **Two execution modes for the search tools, one name and schema.**
  - **Native:** the provider's hosted declaration on the wire. The provider runs the search inside the model call.
  - **Proxy:** a function with the canonical schema. Vibey's tool runtime asks the gateway's executor to run it on
    another Source (§6).
- **`image_generate` has one mode: direct.** It is always a function, and the gateway calls an image endpoint. The
  model chooses the image model through the `model` argument (§7).
- **The canonical search schemas are the providers' trained interface.**
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
- **Three facts per hop and search tool** (§3.4), all derived by Model Hub, none chosen by the user:
  - `native`: `assumed` (the vendor offers it and nothing says this Source refuses it) or `unsupported` (negative
    evidence);
  - `replay`: `assumed` or `unsupported`, the same way, for sending a native payload back to its Source;
  - the executor chain is not a hop fact: the gateway resolves it when a proxy call arrives (§6).
- **Evidence is passive** (§3.2). It is written only from real traffic, by the gateway's one evidence owner.

## 3. Model Hub and gateway responsibilities

### 3.1 Priors: what each vendor offers

- **The catalog is the overlay's `providers` section** (capability model §4), bundled with the code that sends its
  declarations. For each vendor and model family, per protocol, it holds:
  - the native declaration of each search tool the vendor offers, with its `include` values and list price;
  - new: `executor_model`, the vendor's lightweight model that runs proxied search calls (§6), e.g. Anthropic's
    Haiku tier, OpenAI's mini tier, xAI's fast tier. The implementation PR picks the shipped ids;
  - new: `image_models`, the vendor's image models with a one-line description, the endpoint kind (§7.4), the
    parameters each supports, and a list price where one exists.
- **What priors decide.** Which tools a hop may declare natively, which Sources can run a proxied search, and which
  image models the user can pick. Priors are never an allowlist for anything a user sends: a vendor without a row
  simply gets the proxy function.
- **Eligibility is unchanged.** `runs_on` stays the one rule: a hub Source with an eligible credential and an
  untranslated path that serves the model (an API-key Source any model it does not list as retired; a subscription
  only its listed models), for which the overlay declares the tool. A Claude subscription Source is never eligible:
  not for native mode, not as an executor, not for images. Anthropic restricts OAuth credentials to Claude Code and
  its own apps ([`model-hub-tos-review.md`](model-hub-tos-review.md)).
- **Translated paths are never native.** The engine coerces web search across protocols and it worked in the spike,
  but its result shapes are hybrids (Anthropic `web_search_result_location` citations inside a Responses message),
  so they are not a contract.

### 3.2 Passive evidence

The evidence store, its record and its owner are the capability model's §3. v2 changes what writes it:

- **No probes.** `basis: probe` no longer exists. Every record is `basis: observed`, written by the gateway's
  classifier (`evidence_rules.py`) from a real request, and only for the exact (Source, model, feature) it names.
- **Positive (`verified`, display only).** A served response that carries the provider's own item for a tool the
  request declared natively: Responses `web_search_call` or a `custom_tool_call` named `x_keyword_search`,
  `x_semantic_search`, `x_user_search` or `x_thread_fetch`; Anthropic `server_tool_use` named `web_search` with its
  `web_search_tool_result`. A proxy executor call is itself a native call on the executor Source, so it writes the
  same record for the executor's (Source, model). A successful `image_generate` writes it for (Source, image model).
  - **Where the gateway sees it.** The served-stream observer (`ProtocolSSEState` / `ProtocolFactProjector`) keeps
    only bounded scalar projections today, and no item types. It gains projection paths: Responses
    `response.output_item.done` → `item.type`, `item.name`, `item.status`, and the buffered `output[*].type` and
    `output[*].name`; Anthropic `content_block_start.content_block.type` and `.name`. Buffered responses take the
    same paths through `observe_buffered_protocol_response`.
  - **Bounded writes.** Each write rewrites the evidence file, so a positive record is written when the state
    changes (no record, or a negative one) or when the existing positive record is older than one hour, never on
    every response. It is keyed on the serving Source's identity from the final admission snapshot.
  - `resolve()` reads a record by its `status`, no longer by its `basis`: today it reads every `observed` record as
    a refusal.
  - Positive evidence never changes routing. It feeds the "最近一次 … 正常" lines (§8) and nothing else.
  - A `google` hop has no native hosted tools, and the Chat route Vibey uses gives no hosted item to observe.
- **Negative (`unsupported`).** The narrow rules of #2551 on a real error before output, unchanged in form:
  - `Unsupported tool type: <type>` for a declared type → hosted tool `call`;
  - `Invalid encrypted_content in search_result block` while the request replays a web-search result → `web_search`
    `replay`;
  - **new:** xAI's plain-text refusal of a replayed payload, `Could not decrypt the provided encrypted_content`,
    which carries no code. Its evidence status waits for the Vibey lane's xAI A/B (stale ciphertext versus
    unsupported, §3.5). Until that settles, it writes no evidence and only turns replay off for the rest of the turn.
  - Generic errors never create evidence: 5xx, overload, rate limit, auth, and any 400 that does not name the
    feature.
- **Lifetime.** Negative evidence expires after 7 days (relays change). It also never outlives the wire it was
  learned on: every record carries the wire stamp it was observed under, the engine release plus a digest of the
  Source's rendered engine entry (credentials and the model list excluded), and a record whose stamp differs from
  the current one reads as unknown. So a refusal learned under one engine or one rendered config cannot survive a
  change that could reverse it, such as the dependency lane's `is-compat` flip for xAI (#2620), which changes what
  xAI receives for replayed reasoning. The 7-day TTL bounds whatever the stamp does not catch. A Source identity
  change drops the Source's evidence as before.
- **What v1's staleness rules become.** v1 needed `engine` and `shape` to stop positive probe evidence from vouching
  for a wire it was not taken on. v2 routing never reads positive evidence, so `shape` and probe staleness go
  (§11). The wire stamp keeps one job: retiring negative evidence. The `hosted_wire` manifest field is not
  introduced, so `engine_stamp()`'s `hosted_wire` branch (#2623) is removed.

### 3.3 User settings

- **Placement.** Models → Tools, grouped by capability (§8).
- **What the settings store.** The optional V2 section `model_hub.hosted_tools`, owned by the Model Hub API and
  written only through it:

  ```json
  "model_hub": {
    "hosted_tools": {
      "web_search": {"enabled": true},
      "x_search": {"enabled": false, "executor_order": ["src_…", "src_…"]},
      "image_generate": {"enabled": true, "default_model": {"source_id": "src_…", "model_id": "gpt-image-2"}}
    }
  }
  ```

  | Field | Rule |
  | --- | --- |
  | `enabled` | boolean; absent means **on** |
  | `executor_order` | search tools only, optional: Source ids in the order the user wants executors tried. Absent means fully automatic (§6). Ids that no longer name a Source are skipped. |
  | `default_model` | `image_generate` only, optional: `{source_id, model_id}`. Absent, or naming a model no longer available, means the first available model in catalog order (§7.2). |

  - **Absence means on.** The section is absent on every released config, so an upgrade from 3.2.0 turns every
    tool on (owner decision, §0). A tool key the loader does not know is ignored with a warning.
  - An rc-era section is not migrated. Its `image_generation` key is an unknown tool key and is ignored; an entry
    with v1's `executor` field is invalid, so the loader drops the whole section with one warning and the tools take
    their defaults. That is the existing safe-degradation rule for an optional feature, with no rc-specific code.
  - A settings change applies from the next turn, as every runtime config change does.
- **API.**
  - `GET /api/models/hosted-tools` → `{hosted_tools: HostedToolsStatus}` (§10.3): each tool's switch, its passive
    status lines, the search executor order as the gateway would try it now, and the image models with the default.
  - `PUT /api/models/hosted-tools/<tool>` with `{enabled}`, plus `executor_order` for a search tool or
    `default_model` for `image_generate` → the same read. It replaces that tool's entry.
  - The probe endpoint is removed (§11).

### 3.4 Hop facts

- **C-6 `HopResolution.hosted_tools`** gives, for each enabled search tool, the primary hop's facts:
  - `native`: `assumed` or `unsupported`;
  - `declaration` and `include` (the overlay entry's Responses `include` values, possibly empty), present exactly
    when `native` is `assumed`;
  - `replay`: `assumed` or `unsupported`.

  `native` is `assumed` when all of these hold, and `unsupported` otherwise:
  - `runs_on` holds for the hop's (Source, model) and the tool (§3.1);
  - no current negative `call` evidence exists for it (§3.2).

  `replay` is `assumed` when `native` is, the protocol replays natively (`anthropic`, `openai_responses`), and no
  current negative `replay` evidence exists.
- **`image_generate`** gets `{models: [{source_id, model_id, description}], default: {source_id, model_id}}`: every
  image model the user's Sources make available (§7.2), in the order the enum uses.
- **What goes away:** `mode` (Vibey decides), `executor` (the gateway resolves the chain per call), and every
  dependence on positive evidence.
- **The turn freezes the tool list.** A turn's first resolution fixes which tools the turn declares, one name per
  tool whatever the mode, and the `image_generate` enum. Later hops of the turn change only `native` and `replay`.
  A mid-turn tool-list change would break the cached prompt prefix and the model's view of its tools.

### 3.5 Pinning, refusal and re-render

v1's pinning stays: a request carrying Source-specific hosted content (a native declaration, a replayed native
payload, or an in-flight continuation) is pinned with `x-avibe-pin-source`, is served only by that Source with no
failover, and is refused unpinned (C-6 item 7). What changes is what happens after a refusal.

- **The reason is machine-readable.** When the gateway answers a pinned request `pin_unavailable` because a narrow
  hosted rule fired, the 409 body says which:

  ```json
  {"type": "error", "error": {"type": "model_hub_pin_unavailable", "code": "pin_unavailable",
    "reason": {"kind": "hosted_unsupported", "tool": "web_search", "aspect": "call", "source_id": "src_…"}}}
  ```

  `reason` is present only when a narrow rule fired; the other `pin_unavailable` causes (the Source cannot be
  admitted, a failover-class failure before output, #2626's identity hand-back) carry none, and `type` and `code`
  are unchanged. The rule has already written the negative evidence (§3.2) when the answer leaves the gateway.
  Vibey matches the 409 on `type` and `code` and tolerates extra fields (today's client matches the body exactly;
  it relaxes in the same change).
- **Vibey re-resolves; it does not render everything portable.**
  - The adapter surfaces `pin_unavailable` as a terminal error with no partial and no output. The loop then
    resolves a fresh hop, as it already does per model attempt (C-6 item 3), and renders that request normally for
    it. The fresh evidence turns exactly the refused tool into the proxy function or synthetic replay; other native
    content stays native and pinned.
  - This applies to every `pin_unavailable`, not only `hosted_unsupported`: a new identity drops the old payloads,
    a Source in cooldown yields another hop with the in-place form. Fully portable rendering stays only as the last
    resort.
  - **Bounds.** At most one re-resolve per (turn, Source, tool, aspect) for `hosted_unsupported`, and at most two
    re-resolves per turn across all reasons. When the fresh hop comes back on the same Source with the same facts,
    Vibey renders portable at once instead of resending the same request.
  - **In-flight continuations** (Anthropic `pause_turn`, or client tool results after server tools) keep their
    hosted items verbatim only while the fresh hop still declares that tool natively. Otherwise the items take the
    in-place text form (§4.4), because a provider refuses `server_tool_use` without its declaration.
  - **A replay decrypt refusal** (§3.2, xAI) turns replay off for that Source for the rest of the turn, without
    evidence, so the re-resolved request carries synthetic replay.
- **The cost is one cold prompt prefix.** Dropping a native declaration changes the tools block, so the resend and
  the next turns start a new cache prefix: once per (Source, tool), when the refusal is first learned, and again only
  after the negative evidence expires and the tool is tried natively once more. The upstream is not called twice
  for nothing: a refusal before output costs no output tokens.
- **The gateway never rewrites hosted content.** Vibey renders every request from its canonical records; the gateway
  never swaps declarations or converts history. C-6 item 6's opaque thinking strip still applies to unpinned
  requests only.

## 4. Vibey responsibilities

### 4.1 Request building

| Protocol | Native declaration (`native: assumed`) | Function (otherwise, and always for `image_generate`) |
| --- | --- | --- |
| `openai_responses` (OpenAI, xAI) | `{"type": "web_search"}`, `{"type": "x_search"}`, plus the overlay entry's `include` values. OpenAI's web search requires `include: ["web_search_call.action.sources"]`, merged with Vibey's existing `reasoning.encrypted_content`. | `{"type": "function", "name": <tool>, "parameters": <canonical>}` |
| `anthropic` | `{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}` | `{"name": <tool>, "input_schema": <canonical>}` |
| `openai_chat`, `google` | none | a function with the canonical schema. Vibey reaches a `google` hop over Chat Completions at the gateway (C-1 conversion tables), and the engine translates it to Gemini `functionDeclarations`. |

- Vibey declares a search tool natively exactly when it is enabled and `native` is `assumed`; otherwise it declares
  the same-named function. It never declares a hosted image tool.
- Nothing forces a tool: `ModelRequest.tool_choice` and `forced_tool` existed only for probes and go (§11).
- Its four local tools are unchanged (C-7).

### 4.2 Parsing: the C-1 blocks

The v1 C-1 shapes stay, minus native images. The field tables are the contract; `message.schema.json` encodes them.

**Mapping rule.** Every item the provider emits for a declared hosted tool becomes exactly one `HostedToolBlock`,
in stream order, with a provider-neutral `action`. An action this design does not know becomes `other` and is kept,
never dropped.

| Provider item | `tool` | `action` |
| --- | --- | --- |
| OpenAI/xAI `web_search_call`, `action.type: search` | `web_search` | `{type: "search", queries[]}`, taken from `action.queries`, else from the deprecated singular `action.query` |
| `web_search_call`, `action.type: open_page` | `web_search` | `{type: "open_page", url}` |
| `web_search_call`, `action.type: find_in_page` | `web_search` | `{type: "find_in_page", url, pattern}` |
| Anthropic `server_tool_use` (`name: web_search`) plus its `web_search_tool_result` | `web_search` | `{type: "search", queries: [input.query]}` |
| xAI `custom_tool_call` `x_keyword_search` / `x_semantic_search` | `x_search` | `{type: "x_search", variant: "keyword" \| "semantic", query, limit?, mode?}` |
| xAI `custom_tool_call` `x_user_search` | `x_search` | `{type: "x_user_search", query}` |
| xAI `custom_tool_call` `x_thread_fetch` | `x_search` | `{type: "x_thread_fetch", ref}` |
| any other item under a declared hosted tool | that tool | `{type: "other", name}`, with the provider's item name |

- **`HostedToolBlock`** (assistant content): `type` `"hosted_tool"`, `id`, `tool` (`web_search` or `x_search`),
  `action`, `record`, and `native: {source_id, payload[]}` with the provider's own items verbatim and in order,
  never empty. No replay authorization is stored; replay is decided at every request (§5).
- **`HostedRecord`** (per call): `status` (`completed` / `failed` / `incomplete` / `pending`); `error` exactly when
  `failed` or `incomplete`, bounded and redacted, with a stable `error_code` when Avibe wrote it (#2589);
  `sources[]` (`url` required; `title`, `snippet`, `page_age` optional); `images[]` only on an `image_generate`
  result (§7.3).
- **`AssistantMessage.hosted_summary`** (per response, present exactly when the message has hosted blocks):
  `cost` (`{basis, usd}` with `basis` `reported` or `list`, or `included` with no `usd`; absent when unknown),
  `counts` (provider counters by name), `cited_sources[]` (the xAI X-search `url_citation`s).
- **`TextBlock.citations`** (optional): `[{url, title?, cited_text?, start?, end?}]`, presentation only, never
  replayed, materialized at delivery by `core/citations.py`.
- **`ToolResultMessage.hosted`** (optional): `{record, execution?}` for a proxied search call and for every
  `image_generate` call. `execution` (`ProxyExecution`: `executor_origin`, `executor_source_id`, `executor_billing`,
  `cost?`) is present exactly when an executor or image endpoint ran; `is_error` is true exactly when the record is
  `failed` or `incomplete`.
- A native hosted call always has its native payload. A proxied search or an image call is a `ToolCallBlock` plus
  `ToolResultMessage.hosted`, never a `HostedToolBlock`.
- **Native image items are no longer mapped.** Vibey never declares `image_generation`, so an
  `image_generation_call` can only appear if an upstream injects it; it then maps to `other` and its bytes are never
  decoded. The #2589 codes `image_not_streamed` and `provider_no_image` lose their producers and stay in
  `HOSTED_ERROR_CODES` for records already written.
- **Per adapter notes.** xAI's X-search `reasoning` items (`tco_…`) stay ThinkingBlocks. The `custom_tool_call`
  becomes the HostedToolBlock, never a local tool call. Anthropic's `encrypted_content` is kept only in
  `native.payload`. Hosted payloads count against the 32 MiB `StreamAssembler` budget; search events measured at
  most 96 KB, so every stream uses the 8 MiB line cap.

### 4.3 `pause_turn`

Unchanged from v1 as implemented (orchestrator decision, 2026-10-14):
- The Anthropic adapter continues a paused response internally, at most 5 continuations, each pinned to the Source
  that served the paused response. It never re-renders a continuation in portable form.
- A continuation that Source answers `pin_unavailable`, a pause of a send that carried no pin, or a 6th pause ends
  the response with one terminal `ProviderError(kind="interrupted")` whose partial keeps all assembled content.
- The merged message carries the final continuation's usage; `hosted_summary` sums counts and cost and deduplicates
  `cited_sources` by URL. Pending calls carry over by tool-call id, so each call stays one block.
- The merged message is verified only when every response in it was verified and reported the same origin.

### 4.4 In-flight turn replay

Within one turn (`pause_turn`, or a client `tool_result` after server tools in the same assistant message), the
next request is pinned to the Source the previous response came from, and the assistant content goes back
verbatim. This is the provider's own continuation contract, so it needs no `replay` evidence, but it does need the
tool still declared natively on that hop (§3.5).

**Fallback form.** It applies when the pinned Source refuses in-flight verbatim replay, when the fresh hop no
longer declares the tool natively, or on `pin_unavailable`. Each server block pair is replaced **in place** by one
text block: `[web_search: <query>]` plus numbered titles and URLs.
- On the same origin, signed thinking blocks stay untouched (accepted, with no signature error).
- On a different origin, the C-1 cross-origin rules apply to the rest of the message.
- Dropping the pair is never used: the model then misread its own turn and redid the work.

## 5. Replay and conversion

These rules are C-1 rows in `cross-provider.md` and per-protocol rows in `conversion-tables.md`.

| History item | All three hold: the target hop is `native.source_id` under the identity that minted the payload (#2626); the target's origin equals the message's `origin`; and the hop's `replay` is `assumed` now | Any other target |
| --- | --- | --- |
| `HostedToolBlock` | `native.payload` verbatim, in place | a synthetic same-named call plus result, with the assistant message split around it. One record-to-text function renders the result. |
| `TextBlock.citations` | not sent | not sent |
| Proxied search or image call (`ToolCallBlock` + `ToolResultMessage.hosted`) | ordinary tool call and result | same |

- **Google targets** take the Chat pair; the engine translates it to Gemini `functionCall` / `functionResponse`
  parts. A failed record becomes `response: {error: <text>}`.
- **Synthetic rendering rules**, by action, unchanged from v1: a `search` becomes one call with `{query:
  queries[0]}` whose result lists further queries and the numbered sources; an `x_search` or `x_user_search` becomes
  one `x_search` call; every other action is its own call with `{query: "<action type> <target>"}`; the response's
  `cited_sources` are listed once; a `failed` or `incomplete` record renders as an error result; a `pending` record
  renders as the C-1 interrupted-call result.
- **Replay is assumed, so a silent drop is possible.** A Source that accepts a replayed payload but silently drops
  it leaves the model without the earlier results' page content (the answer text and citations stay in history).
  Nothing in the response reveals it, so passive learning cannot see it. Known by design (§15); #2620 was this class
  (the engine stripped xAI ciphertext for months without an error).
- **What is lost across providers: page text.** Anthropic returns page content only as `encrypted_content`, which
  no other origin can read; OpenAI and xAI never return it. The target sees queries, titles, URLs and snippets.
- **The served-hop report carries `source`** (Model Hub contract v12), which tags `native.source_id`, authorizes
  replay, and pins in-flight continuations.

## 6. The search executor

- **Endpoint:** `POST {base_url}/hosted-tools/{tool}` on the gateway, with the turn's Bearer token and its
  `request_headers`, and the body `{"arguments": <canonical arguments>}`. There is no executor in the body: the
  gateway resolves the chain from the caller's live turn (`x-avibe-turn-id`). A call without a live turn, for a tool
  not enabled in that turn, or from a turn that no longer admits work is 409 `executor_not_bound`, and nothing runs.
- **The turn still owns the call.** The endpoint stays authorized by the live named turn (#2586's `x-avibe-turn-id`
  and closed admission); only the per-turn executor binding goes. Stopping the turn cancels the call, and it is
  still metered. A candidate whose identity changed since the chain was built is not used.
- **The chain, built at call time.**
  1. Candidates are the Sources whose vendor offers the tool natively (§3.1), with `runs_on` holding for the
     vendor's `executor_model` and no current negative `call` evidence for that pair. So a subscription that does
     not list the vendor's lightweight model is excluded when the chain is built, not found by a 404.
  2. Order: the user's `executor_order` when set, then the remaining candidates in Vibey's Source order.
  3. The gateway calls the first candidate: one short native request on that Source's `executor_model`, with the
     overlay's declaration, the tool forced where the protocol allows it (`tool_choice: "required"` on Responses;
     Anthropic is asked in the message alone), a fixed instruction to search and list numbered results with one or
     two cited sentences each, a small output cap, and no reasoning effort.
  4. On a failure that normal routing fails over from, or a narrow-rule refusal (which also writes negative
     evidence), it tries the next candidate. A model refusal (404 `model_not_found`, "model not supported") skips
     that Source for this call without settling its health, as `_InvocationUnsupported` does: it is inventory, not
     capability, and writes evidence only if a narrow rule names it. Any other outcome ends the chain.
  5. With no candidate, or every candidate failed, the call returns a `failed` record naming what to add ("add an
     xAI Source to use X search").
  - **One deadline for the whole chain**, the tool's call timeout (180 s for search), not one per candidate.
  - Each candidate tried is metered on its own Source; the result's `ProxyExecution` names the Source that served
    it, with that call's cost.
  - The executor request is a short model request because no vendor exposes search without a model: Anthropic,
    OpenAI's public API, xAI and Google all run search inside a model call.
- **The result is the canonical record** (v1's rules, unchanged): a `HostedRecord` with `sources[]` in the
  executor's order, a snippet from the answer text each citation covers (Anthropic keeps `cited_text`), X search
  listing the cited posts, and the `ProxyExecution` naming the executor Source, model and billing. Every outcome of
  a well-formed call is a 200 in the `ToolResultMessage.hosted` encoding; success is an allow-list on the response's
  own completion status. Vibey's tool runtime renders the model-facing text with the same function that renders a
  synthetic replay (§5).
- **Cost.** One lightweight-model call plus the tool fee (xAI about $0.01 per call; Anthropic $0.01 per search plus
  tokens), valued by the one pricing owner and shown on the card.

## 7. Image generation: `image_generate`

### 7.1 No caller gets an image tool it did not declare

Unchanged from v1 §7.1: the engine runs with `disable-image-generation: passthrough`, nothing adds an image tool
back for any caller (Codex CLI included), and the gateway regression (MH-HOSTED-GATEWAY-001) and the once-per-release
E2E stay.

### 7.2 The tool

- **One function tool**, declared on every protocol when enabled:

  ```json
  {"name": "image_generate",
   "parameters": {"type": "object", "required": ["model", "prompt"], "properties": {
     "model": {"enum": ["gpt-image-2", "grok-imagine-image", "gemini-3.1-flash-image"],
               "description": "gpt-image-2: … (default) · grok-imagine-image: … · gemini-3.1-flash-image: …"},
     "prompt": {"type": "string"},
     "size": {"type": "string", "description": "e.g. 1024x1024, 1536x1024; or an aspect ratio such as 16:9"},
     "images": {"type": "array", "items": {"type": "string"}, "description": "paths of reference images to edit"},
     "n": {"type": "integer", "minimum": 1, "maximum": 4}}}}
  ```

  - **The enum is the image models actually available**: the overlay's `image_models` (§3.1) for each Source where
    `runs_on` holds for that model, deduplicated by model id (the first Source in Vibey's order serves an id that
    two Sources offer, unless `default_model` names the other). Each entry's one-line description goes into the
    `model` description. The default is listed first and marked "(default)"; the rest follow sorted by id. So the
    enum is a pure function of configuration and changes only on a config event, and the turn freezes it (§3.4).
  - **Common parameters only.** `prompt`, `size` (a size or an aspect ratio, mapped per endpoint), `images`
    (reference images for an edit) and `n` where the model supports more than one. A model-specific option stays
    optional and is ignored by a model without it.
  - **No tool without a model.** With no image model available, `image_generate` is not declared at all, and the
    Tools card says what to add (§8).
- **The default** is `model_hub.hosted_tools.image_generate.default_model` (§3.3), else the first available model in
  catalog order.

### 7.3 Files and a text path record

Unchanged from v1 §7.3 (owner decision, 2026-10-13 19:14), with the producer now the direct call:
- **The model is not given the image.** The gateway returns the bytes to Vibey's tool runtime over the local hop;
  Vibey saves them through the session media sink, the only writer of session media, and builds the `HostedRecord`
  with `images[]` path records. The bytes are never in a transcript.
- **The model gets a text path record:** path, model, size, mode and cost, e.g.
  `[image saved: /…/image-3f2a.jpeg · 1536×1024 · gpt-image-2 (OpenAI API key) · ≈ $0.04] prompt: "…"`. It opens the
  file with `read` if it wants to look. A request asks the endpoint for JPEG where the model allows it, so a file
  fits `read`'s inline image cap; a file still over it gets a record saying it is too large to view inline.
- **Media registration and the sweep** (#2588) are unchanged: the committed row registers each image it names, and
  Vibey's first recovery pass removes unregistered content-addressed files older than 24 hours.
- **The user sees the image.** The Workbench renders it inline from the file, and IM attaches it.
- **History and replay never contain base64.** An `image_generate` call is an ordinary tool call and result, so it
  replays as one to every target.

### 7.4 Direct execution

- **Never through a chat model.** Vibey never declares a hosted image tool on a chat request; v1's native
  `image_generation` declaration, its partial images and its 32 MiB line cap are removed (§11).
- **Endpoint:** the same executor endpoint, `POST {base_url}/hosted-tools/image_generate`, with `{"arguments":
  {model, prompt, size?, images?, n?}}`. Vibey reads each reference image itself, through the same checks as `read`
  (a supported image type, within `read`'s inline limit), and sends the bytes base64 in `images`.
- **Per model kind** (the overlay entry says which):
  - **OpenAI gpt-image family** (an OpenAI API key, or the Codex subscription backend) and **xAI grok-imagine**:
    the engine's `/v1/images/generations`, or `/v1/images/edits` when `images` is present.
  - **Gemini image models** (Nano Banana): the chat route with the image response modality, on the Source's own
    protocol.
- **Every route is unverified today** (gateway lane, 2026-10-15). The pinned engine (v8.0.23-3) contains the images
  code (the Codex executor's direct Image API path, `gpt-image-2` entries, `grok-imagine-image-quality`, and
  "model %s is only supported on /v1/images/generations and /v1/images/edits"), and `passthrough` keeps the images
  endpoints enabled. None of it is reachable from Avibe yet:
  - the engine adapter and `EngineClient` post only to the protocol endpoints, so they gain an images invoke path;
  - `stream_wire` has no images taxonomy (outcome and usage), so it gains one;
  - a custom (OpenAI-compatible chat) Source's image model needs the engine's per-model `image: true` flag, which
    `config.py` never writes.
- **Route verification is the implementation's job, not the user's.** Each image path gets one hermetic engine E2E
  (the real engine, a stub upstream), like §7.1's harness: Codex OAuth images, an OpenAI API key, xAI
  grok-imagine, and a Gemini image model (e.g. Nano Banana 2, `gemini-3.1-flash-image`) through the chat route
  with the image modality. A model whose route is not verified ships without an overlay entry, so it is never
  offered and the UI never needs an "unverified" tag.
- **No silent fallback.** A failed call returns a `failed` record whose error names the alternatives ("gpt-image-2
  failed: <reason>. Available: grok-imagine-image, gemini-3.1-flash-image"). The model may call again with another
  model; the gateway never switches on its own.
- **The result names the model used**, in `ProxyExecution` and in the path record.

## 8. User interface

### 8.1 Models → Tools

- **Grouped by capability**, one card per tool, each with one switch, on by default:
  - **搜索**: 网页搜索, X 搜索;
  - **生成**: 图片生成.
- **The subtitle says where content goes**, plainly, because tools are on by default: a search sends the query to
  the vendor that runs it (the hop's own provider, or the executor Source's), and an image prompt goes to the image
  model's vendor; metered Sources are billed per call.
- **One status line per tool, from passive evidence only, and only after something happened:**
  - "最近一次由 <Source> 执行 · 正常 · 10-15 14:02": the latest positive record for the tool, native or executor;
  - "<Source> 不支持，已改用 <Source>": a current negative record, with the later call that ran elsewhere;
  - nothing before first use. No "unknown", "unverified" or "unchecked" wording anywhere.
- **Search cards** show the per-call list price where known, and an optional **Advanced** section with the executor
  order: up/down buttons (keyboard- and screen-reader-accessible, the route chain editor's pattern), collapsed and
  fully automatic until touched, with "Reset to automatic".
- **The image card** lists the available image models with their one-line descriptions and a default-model picker
  (`default_model`, §3.3). A model that failed shows the fact after use ("最近一次失败：…"), never a tag before it.
  With no image model available it says which Source to add.
- **Removed:** check and check-all, the confirm and result dialogs, Re-verify, the probe estimate, the executor
  picker, and "engine upgraded" headers (§11).

### 8.2 Source detail chips

Each model row on a Source's detail panel shows only passive state, with the detail popover (time, basis, reason)
and no action:
- "网页搜索 · 正常 · 10-15";
- "X 搜索 · 不支持";
- "ultra · 不支持" (efforts, unchanged).

Unknown shows nothing. The chips reuse `EvidenceChip`'s locale-composed spoken name and popup semantics, without the
recheck action and without the keyboard path into a detail that has no action.

### 8.3 Use time in Vibey

Unchanged: the tool card (query, mode "native · xAI API key" or "via Anthropic API key · claude-haiku-5-5", cost
when metered, sources, failure reason); citations through the existing citation contract; the IM one-liner
`🔎 Web search "query" · native (xAI) · 6 sources · $0.01`. The image card shows the prompt, the image, the model
used and the cost; IM attaches the file.

## 9. Defaults, cost and privacy

- **Every tool is on by default** (owner decision, 2026-10-15). This reverses v1's default, whose premise was that a
  call costs money, sends queries to a third party, and needed a cost-bearing check to turn on. v2 removes the check;
  the other two remain true and are stated in the public docs:
  - a search sends the model's query to the vendor that runs it: the hop's provider natively, or the executor
    Source's vendor via proxy;
  - an image prompt and any reference images go to the chosen image model's vendor;
  - on metered Sources each call costs the tool fee plus tokens (§10.4); on subscriptions it is included.
- **No onboarding step mentions tools**, and nothing calls a provider unless the model calls a tool in a turn the
  user started.
- **Per-call prices** live in the overlay with date and source (capability model §6.2): OpenAI web search $10 / 1k
  calls plus content tokens; Anthropic $10 / 1k searches plus result tokens, re-billed while they stay in context;
  xAI web search $5 / 1k calls, X search $5 / 1k posts and $10 / 1k profiles. Image models are priced per image or
  per image token as each vendor lists them; a model with no list price shows its reported cost or none, never zero.
- **Relays** show list price, labelled "at API price", and may bill differently.

## 10. Contract changes

The contract edits land with the implementation PRs as one coordinated Model Hub version event after #2626's v14,
under the freeze protocol (`model-hub-contracts/README.md`).

### 10.1 C-1 (`agent-core-contracts/message.schema.json`, `cross-provider.md`, `conversion-tables.md`)

- `HostedToolName` for `HostedToolBlock` is `web_search` | `x_search`; the `generate` action and `images[]` are no
  longer valid on a `HostedToolBlock`.
- `ToolResultMessage.hosted.record` may carry `images[]` and the `generate` action only for `image_generate`;
  `generate` records the `model` used, with `prompt` required for a `completed` record.
- The conversion rows drop native image replay; an `image_generate` call is an ordinary tool call everywhere.

### 10.2 C-6 (`agent-core-contracts/model-hub-consumer.md`, both `hop-resolution.schema.json` copies)

- `HopResolution.hosted_tools` entries become `{native: "assumed" | "unsupported", declaration?, include?, replay:
  "assumed" | "unsupported"}` for search tools (§3.4), and `image_generate: {models[], default}`; `mode` and
  `executor` are removed. `declaration` and `include` are present exactly when `native` is `assumed`.
- Admission of a pinned request reads negative evidence only: a declared native tool needs `runs_on` and no current
  negative `call` evidence; a replayed item needs no current negative `replay` evidence.
- The `pin_unavailable` envelope gains `reason` (§3.5).
- The executor body is `{"arguments"}` only; the gateway resolves the chain from the live turn (§6).

### 10.3 Model Hub contracts

- `capability-evidence.schema.json`: `basis` is `observed` only; `status` `verified` (display only) or
  `unsupported`; `shape` is removed and `engine` becomes `wire`, the stamp of §3.2; `feature.name` is `web_search`
  | `x_search` | `image_generate`, and `image_generate` has `call` only.
- `hosted-tools-status.schema.json` is rewritten for §3.3's read: per tool `enabled`, `status` (the latest positive
  and current negative records as status lines), `executor_order` (search) or `models` and `default` (image), and
  the per-call `price`. `reverify`, `executor`, `bound`, `candidates[].call`/`replay`/`probe_estimate` go.
- `hosted-tool-probe-result.schema.json` is deleted, with its `api-response.schema.json` branch and `api.md` row.
- `provider-overlay.schema.json` gains `executor_model` per vendor family and `image_models[]`.
- V2 config: `model_hub.hosted_tools` as §3.3.

### 10.4 Pricing

- No `probe_estimate`. Cost is reported or valued per real call: `hosted_summary.cost` for native calls, and
  `ProxyExecution.cost` for executor and image calls.
- Image calls are valued from the overlay's image price where the vendor lists one, else from reported usage at the
  image model's token rate; unknown stays unknown, never zero.

## 11. Removal list

| Area | Removed or changed |
| --- | --- |
| Model Hub | `core/handlers/model_hub/hosted_probe.py`; `ModelHubService.probe_hosted_tool`, `_record_probe_evidence`, `_probe_estimate`, `_probe_rejection`, `_probe_aspect`; the RPC and UI-server routes for `POST /api/models/hosted-tools/<tool>/probe`; `MODEL_HUB_RPC_TIMEOUT_SECONDS`' probe dependency (it moves back to `vibe/model_hub_client.py`); `HOSTED_PROBE_SHAPE` and every `shape` parameter; the evidence `probe` basis and probe staleness; `engine_stamp()`'s `hosted_wire` branch (#2623); the per-turn executor binding and `_executor`; `UserTestCall`'s probe-only callers |
| Gateway | `stream_wire`'s `response.image_generation_call.partial_image` model-output envelope; the executor's native image path (`ResponsesStreamReader(partial_images=…)`, the 32 MiB image line cap, the image event and byte budget, `_image_result`, the `_IMAGE_*` constants, the 300 s image `call_timeout`), replaced by what the images endpoint needs; the OpenAI `image_generation` overlay declaration (#2580's JPEG q90 becomes the images-endpoint request parameters); `executor` in the proxy body; the `hosted_tools_status` candidates and bound-executor projection. Kept: `disable-image-generation: passthrough`; `hosted_admission`'s "image calls are never replayed" rule until no transcript can carry a native image call; #2616's per-turn identity binding and #2586's turn id. |
| Vibey | the native `image_generation` declaration, its parsing (`responses_hosted_tool`'s `image_generation_call` mapping, `image=`/`image_error=` in `responses_hosted_block`), `_ImageBudget`, `_saved_image`, `_PARTIAL_IMAGE_EVENT`, `IMAGE_STREAM_LINE_BYTES`, the large-event JSON offload, `drive_sse_stream`'s `max_line_size` and `iter_sse_events`' offload (every stream returns to 8 MiB); `ModelRequest.tool_choice` and `_common.forced_tool`; `registry.build_wire_request` (its tests move to each adapter's `wire_request`); the adapters' probe-only `media_sink` parameter. `HostedRecord.images`, `HostedImage`, `generate_action` and the `generate` action stay, on `ToolResultMessage.hosted` only. |
| UI | `ProbeAnswerNote.tsx` (with `ReplayNote`); in `ToolsTab.tsx` `ProbeCell`, the executor picker and its notes, the `needsCheck`/`needsExecutor` states, the "unchecked" chip and the probe column; in `HostedCapabilityChips.tsx` the recheck action, the keyboard focus-into-detail, and the reverify, engineUpgraded and unsupportedAged states; `useEstimateLabel.ts`; the `probeHostedTool` wiring and the Sources re-read after a probe in `SettingsModelsPage.tsx`; in `useHostedTools` `probe`, `probing`, `lostProbeOutcome`, `recordedOutcome` and `PROBE_NOT_RECORDED` (the serialized writes, identity fencing and re-read after Source mutations stay); in `hostedTools.ts` `ProbeTarget`, `probeKey`, `probeUnanswered`, `verifiedCandidates`, `sameExecutor` and the `aged`/`engineUpgraded` branches of `evidenceChip`; `modelsApi.probeHostedTool`; the types `HostedToolProbeResult`, `HostedProbeAspect`, `HostedCostEstimate`, `probe_estimate`, and `executor` in `HostedToolPut`. Check-all is removed by the UI lane's pending PR. |
| i18n (`settings.models`) | `tools.probe.*`; `tools.state.needsCheck` and `needsExecutor`; `tools.executor.*`; `tools.chip.unchecked`, `reverify`, `engineUpgraded`, `unsupportedAged`; `tools.reverify.*`; `capability.recheck`, `recheckCost`; `capability.chip.*` and `capability.explain.*` for reverify, engineUpgraded, viaProxyAged and unsupportedAged; `capability.expiryProbe` |
| Tests and scenarios | `tests/test_model_hub_hosted_probe.py`; the probe and executor-binding cases in `test_model_hub_hosted_tools_resolution.py`, `test_model_hub_hosted_executor.py`, `test_model_hub_hosted_pinning.py` and `test_model_hub_evidence_rules.py`; the native image cases in the Responses adapter tests; UI `ToolsTab.test` MH-HOSTED-UI-013 and 019, the executor cases of 001 and the probe parts of 003 and 018, `useHostedTools.test` 013 and parts of 007, `hostedTools.test` 013, `SourceDetailPanel.test` 013 and 017, `SettingsModelsPage.render` 016; catalog rows MH-HOSTED-007 (probe), UI-013, UI-016 and UI-019 retire, and the rows they share shrink |
| Contracts | as §10 |

## 12. Implementation split

Each phase is one or more PRs under the normal gates. The contract version event (§10) rides the first PR that
edits a frozen file, and later phases join it if it has not shipped.

| Phase | Lane | Work | Depends on |
| --- | --- | --- | --- |
| A. Evidence and priors | foundation | passive positive rule and the xAI decrypt rule (status per the A/B) in `evidence_rules.py`; `wire` stamp and negative-only routing; drop `probe`, `shape`, the `hosted_wire` branch; overlay `executor_model` and `image_models` with loader and schema; `hosted_tools` config v2 with absence = on; hop facts (§3.4); `hosted-tools-status` v2 read and PUT; remove the probe stack (§11 Model Hub) | — |
| B. Gateway | gateway | the served-stream observer's projection paths for hosted items, streamed and buffered (§3.2), feeding phase A's positive rule; `pin_unavailable.reason`; admission on negative evidence only; the search executor chain at call time with `executor_model`, `executor_order` and failover (§6); `image_generate` direct execution per kind (§7.4), with recorded stub tests; route verification of each shipped image model | A (hop facts, overlay) |
| C. Vibey | Vibey | declarations from facts (§4.1); the turn freeze (§3.4); `pin_unavailable` surfaced to the loop and re-resolve with its bounds (§3.5); in-flight rule; `image_generate` tool with the enum, reference images and the path record; remove native image parsing and the probe-only seams (§11 Vibey) | A; B for the executor body and images |
| D. UI | UI | the grouped Tools tab, status lines, executor order, image models and default; passive Source chips; removals (§11 UI, i18n); the Tools-tab and chip frames in `avibe-docs/design.pen`, edited through Pencil | A (read shape) |
| E. Docs | foundation | public docs: what each tool does, default-on, where content goes, cost (§9) | A–D |

## 13. Acceptance properties

1. Nothing calls a provider to learn about a tool. Every provider call comes from a model's tool call or a model
   request in a turn the user started.
2. Every tool is on unless the user turned it off; a released config without the section has every tool on.
3. A hop declares a search tool natively exactly when it is enabled, `runs_on` holds, and no current negative
   evidence exists; every other hop gets the same-named function.
4. Positive evidence never changes routing. Negative evidence comes only from a narrow rule, names one (Source,
   model, feature), and stops applying after 7 days or when the wire stamp it was learned under changes.
5. A refusal a narrow rule names reaches Vibey as `pin_unavailable` with its reason; Vibey re-resolves and resends
   with that tool as a function, within the bounds of §3.5. The gateway never rewrites hosted content.
6. No hop receives `native.payload` unless it is the recorded Source under the identity that minted it, on the same
   origin, with `replay` assumed now.
7. A proxied search tries executors in the user's order, then Vibey's Source order, each on its vendor's
   lightweight model, and fails over on failover-class failures and narrow refusals only.
8. `image_generate` runs on exactly the model the call names; a failure names the alternatives and never switches
   model. The result names the model, and no image byte enters model context or history.
9. Every hosted call, native or proxy, yields exactly one canonical record, rendered the same way on the card, in
   citations and on IM.
10. Any transcript with hosted calls replays to any supported target without a history-shape error.
11. The UI shows only what real use learned; no state reads "unknown", "unverified" or "unchecked".
12. A shown cost says whether it is reported, at API price, or included in a subscription.

## 14. Test plan

Hermetic: a local stub server plays each provider's responses from the spike's redacted shapes. No live upstream
and no real credentials.

| Layer | Owner boundary | Cases |
| --- | --- | --- |
| Evidence | `evidence_rules` against recorded served streams and error bodies | a served native web search writes one positive record; an executor call writes one for the executor; `Unsupported tool type` and `Invalid encrypted_content` each write one negative record; overload, 5xx and other 400s write none; a negative record under another wire stamp reads as unknown; one older than 7 days reads as unknown |
| Hop facts | `hosted_tools` resolution | `assumed` from priors with no evidence; `unsupported` with a current negative; a Claude subscription never `assumed`; a translated path never `assumed`; the image enum and default, deterministic |
| Config | `ModelHubConfig` loader | a 3.2.0 config without the section has every tool on; an rc-era section is dropped with one warning; `executor_order` and `default_model` round-trip |
| Gateway refusal | real gateway + loopback fake engine | a pinned native request refused with `Unsupported tool type` answers 409 with `reason`, and the evidence is written first; admission refuses a declared tool with current negative evidence |
| Re-render | Vibey loop with a stub gateway | re-resolve after `hosted_unsupported` resends with the function, other native content still pinned; the per-key and per-turn bounds; same facts go portable; an in-flight continuation whose tool turned proxy uses the in-place form |
| Executor chain | gateway endpoint, stub providers | order from `executor_order` then Source order; failover on 5xx and on a narrow refusal; `model_not_found` moves on; no candidate gives a `failed` record naming the fix; metering on the executor Source; a Claude subscription is never a candidate |
| Images | gateway endpoint + Vibey tool runtime, stub endpoints | generations and edits per kind; JPEG requested where allowed; a failure names the alternatives and no other model is called; bytes saved once through the media sink; the path record; no base64 in history |
| Parsing and replay | adapters and transforms | v1's mapping and replay cases minus native images; an injected `image_generation_call` maps to `other` without decoding |
| UI | Tools tab, Source chips, tool card | default on; status lines appear only after use; executor order up/down and reset; image models and default; chips without actions; no probe UI |
| E2E, once per release | real engine + stub upstream | a native web search, a proxied X search, and one `image_generate` per shipped image kind through the shipped engine |

## 15. Known by design

1. **Silent drops are invisible.** A Source that accepts a native declaration but never runs the tool looks like a
   model that chose not to search, and one that accepts a replayed payload but drops it loses the results' page
   content. Passive learning cannot see either. The status line simply never shows that Source executing.
2. **The first refusal costs one request and one cold prefix** per (Source, tool), and again after the negative
   evidence expires (§3.5).
3. **The executor's lightweight model is a catalog choice.** A Source that does not serve it moves the chain on; the
   user cannot pick a different executor model in v2.
4. **rc-era state is dropped**: probe evidence reads as `basis: probe`, which v2's loader does not accept, so those
   records are pruned; an rc-era `hosted_tools` section resets to the defaults.
