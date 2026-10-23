# Vibey hosted tools

Status: **design v2**, 2026-10-15, owner-approved direction (passive, default-on, simpler). It replaces design v1
(2026-10-13), whose active-check model the owner rejected after looking at rc4's Tools tab.
- Scope: Vibey only. Claude Code, Codex and OpenCode are unchanged, and there is no CLI.
- Foundation: [`model-hub-capability-model.md`](model-hub-capability-model.md), which covers priors, evidence and
  pricing.
- Evidence: the runtime spike of 2026-10-13 (40 + 10 upstream calls through the local engine on API-key relay
  Sources). Its tables are summarised where a rule depends on them.
- What is already built (rc4) and what v2 removes or changes is listed in §11; the implementation split is §12.
- Extension: [`web-search-services.md`](web-search-services.md) adds search APIs (Tavily, Exa, DeepSeek, …) as
  `web_search` executors, the "prefer the model's own search" switch, and moves the web search order into the card
  body. Where the two documents differ for `web_search`, that one holds.

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
- **Pre-release state is not migrated.** 3.2.0 stable has no hosted tools. rc-era probe evidence is pruned,
  and an rc-era `hosted_tools` section's unknown keys and fields are ignored, with no compatibility code (owner
  directive).

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
  - new, at vendor scope: `executor_models`, the vendor's lightweight models that run proxied search calls, preferred first (§6), e.g.
    Anthropic's Haiku tier, OpenAI's mini tier, xAI's fast tier; the executor call uses the declaration of the family
    row that model matches. The implementation PR picks the shipped ids;
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
- **A custom Source takes each model's own vendor's priors (amended 2026-10-20, #2873).** A Source added under
  自定义 has `vendor: custom`, which has no overlay row, so it used to be excluded from every hosted tool however
  clearly its models were one vendor's. Each model it serves now takes the priors of the one overlay vendor that
  claims it (a model family of that vendor matches it, or it is one of that vendor's image models), on one
  condition: the Source speaks that vendor's catalog protocol (`api_key_vendors.json`), the protocol the vendor's
  declarations and image route were verified on. Everything else follows from those priors, exactly as for a
  `vendor: openai` relay: the tool is assumed, called blind, and a refusal is learned (§3.2).
  - Per model, not per Source: one relay that serves several vendors' models runs each vendor's tools on that
    vendor's models, as far as its one protocol allows (a Responses relay runs OpenAI's and xAI's search; Claude's
    is declared on `anthropic` only, so Claude models there use the proxy function).
  - A model that no vendor claims, or that several do, takes no priors: its vendor is not guessed.
  - Executor and image models come only from the Source's own listing. A catalog vendor's API key may be offered
    its image models unlisted, but nothing except a relay's inventory says that it serves one.
  - `prior_vendor` in `vibe/backend_model_catalog.py` is the one owner. `hosted_tool_spec`, the executor models, the image models, the
    engine's image registration and the narrow rules all read through it, so what is declared, offered,
    registered and learned stays one set.
- **Translated paths are never native.** The engine coerces web search across protocols and it worked in the spike,
  but its result shapes are hybrids (Anthropic `web_search_result_location` citations inside a Responses message),
  so they are not a contract.

### 3.2 Passive evidence

The evidence store, its record and its owner are the capability model's §3. v2 changes what writes it:

- **No probes.** `basis: probe` no longer exists. Every record is `basis: observed`, written by the gateway's
  classifier (`evidence_rules.py`) from a real request, and only for the exact (Source, model, feature) it names.
- **Positive (`verified`, display only).** A served response that carries the provider's own completed item for a
  tool the request declared natively: Responses `web_search_call` with `status: completed`, or a `custom_tool_call`
  named `x_keyword_search`, `x_semantic_search`, `x_user_search` or `x_thread_fetch`; Anthropic a
  `web_search_tool_result` whose `content` is a result list, not the error object
  (`web_search_tool_result_error`), which arrives under the same block type. The block type is web search's own,
  so the `server_tool_use` that precedes it is not read. A proxy executor call is itself a native call on the executor Source, so it writes the
  same record for the executor's (Source, model). A successful `image_generate` writes it for (Source, image model).
  - **Where the gateway sees it.** The served-stream observer (`ProtocolSSEState` / `ProtocolFactProjector`) keeps
    only bounded scalar projections today, and no item types. It gains projection paths: Responses
    `response.output_item.done` → `item.type`, `item.name`, `item.status`, and the buffered `output[*].type` and
    `output[*].name` and `output[*].status`; Anthropic `content_block_start.content_block.type`, `.content` (whether it
    is a list) and `.content.type`, and the buffered `content[*]` the same. Buffered responses take the same paths
    through `observe_buffered_protocol_response`, keeping a few short fields for at most 256 members.
  - **Bounded writes, off the response path.** Each write rewrites the evidence file, so a positive record is written
    at settlement, off the loop, after the response completes, and only when there is no record or the existing
    positive record is older than one hour, never on every response. **A positive write never replaces a negative
    record:** a success that settles after a concurrent refusal must not re-enable native routing, so only a clear
    or a stamp change removes a refusal. It is keyed on
    the serving Source's identity from the final admission snapshot: a Source re-identified while the response ran is
    not vouched for.
  - `resolve()` reads a record by its `status`, no longer by its `basis`: today it reads every `observed` record as
    a refusal.
  - **A positive record carries the observed `mode`** (`native`, `executor` or `direct`) and is never retired by the
    wire stamp: it is display-only history, so an upgrade does not hide it. The stamp retires `unsupported` records
    only. A positive and a negative record for the same (Source, model, feature) coexist (the store keys by status
    too), so a later refusal never erases the success history kept for a restart.
  - **Memory is the one source of success history.** Every success updates its (Source, model, tool) record in
    memory at once; the file takes it at most hourly per key and seeds memory at startup. `latest_ok` is the newest
    of those records whose Source still has the identity it ran under, compared by time, so the status line is
    current while file writes are throttled, and a Source that changes identity hands the line to the newest other
    success rather than to an older file copy. There is no separate per-tool projection to drift from the records.
  - Positive evidence never changes routing. It feeds the "最近一次 … 正常" lines (§8) and nothing else.
  - A `google` hop has no native hosted tools, and the Chat route Vibey uses gives no hosted item to observe.
- **Negative (`unsupported`) is routing state.** When the file write fails, the narrow rule still updates the
  in-memory store and logs the loss, so the re-resolve that follows (§3.5) cannot declare the refused tool natively
  again. The narrow rules of #2551 fire on a real error before output, unchanged in form:
  - `Unsupported tool type: <type>` for a declared type → hosted tool `call`;
  - `Invalid encrypted_content in search_result block` while the request replays a web-search result → `web_search`
    `replay`;
  - **new:** an upstream image endpoint that refuses the model or the endpoint before producing anything →
    `image_generate` `call`. A 400 or 404 whose machine code names the model (`model_not_found`, `unknown_model`) is
    written for that (Source, model); any other 404 is a refusal of the endpoint itself (the path is not served, as a
    relay without the vendor's image endpoint answers) and is written for every image model the Source registers
    there, so sibling models do not each fail once. Only an upstream refusal counts: the images path goes through
    `_is_local_model_registration_failure`, which recognizes the engine's own refusals as local, so they never
    teach: neither "unknown provider for model <routed>" nor its images handler's "Model <routed> is not supported
    on /v1/images/generations or /v1/images/edits". The record
    is stamped and retired by the wire stamp like any negative. The enum drops that pair from the next turn (§7.2), and the refusal shows and
    clears on the image card like a search refusal (§8.1);
  - **new:** xAI's plain-text refusal of a replayed payload, `Could not decrypt the provided encrypted_content`,
    which carries no code. It does not say which ciphertext failed, and a stale or foreign blob draws it as well as
    an unsupported replay, so it writes no evidence unless the Vibey lane's xAI A/B shows that xAI refuses fresh
    ciphertext from the same account. The gateway does not classify it: it is no failover-class failure and no narrow
    rule fires, so it passes through as the provider's 400 on pinned and unpinned requests alike, and Vibey's adapter
    is its one classifier (§3.5). If the A/B makes it evidence, it becomes a narrow rule here and its pinned case a
    `hosted_unsupported` reason.
  - Generic errors never create evidence: 5xx, overload, rate limit, auth, and any 400 that does not name the
    feature.
- **Lifetime of negative evidence (owner decision, 2026-10-15 01:20).** Negative evidence, for hosted tools and
  efforts alike (capability model §3.2), has no time limit. It stays until
  one of these clears it:
  - the user clears it in Models → Tools, for one (Source, tool) or for every Source of a tool (§8.1);
  - its Source identity or wire stamp no longer matches, which happens on its own when the Source's config, the engine
    release or the Avibe release changes.

  With `source_identity`, every record carries the wire stamp it was observed under: the engine release, the Avibe
  release (which covers the bundled overlay's declarations, the adapters' request shape and vendor-wide engine
  flags), and the model's rendered fields the identity does not cover. A record whose identity or stamp differs from
  the current one reads as unknown. Capability model §3.3 lists every renderer input and what covers it. The stamp is
  computed from the config held in memory, never from the engine state on a request path, as `engine_stamp()` is
  never called on one. So a refusal learned under one engine or one rendered config cannot survive a
  change that could reverse it, such as the dependency lane's `is-compat` flip for xAI (#2620), which changes what
  xAI receives for replayed reasoning. A change on the provider's side that the stamp cannot see (a relay that starts
  supporting a tool) waits for the user's clear. A Source identity change drops the Source's evidence as before.
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
  - The loader ignores, with a warning, an unknown tool key and an unknown field inside an entry, and keeps the
    fields it knows. So an rc-era section keeps each tool's `enabled`, and a user's explicit opt-out survives the
    upgrade; its `image_generation` key and v1's `executor` field are ignored. This is one generic loader rule, not
    rc-specific code.
  - A broken section (not an object) or entry (a value of the wrong type) turns its tools **off** with a warning:
    hosted tools reach third parties and cost money, so damage never turns one on. Sources and Agents still load
    (the persisted-shape rule in `AGENTS.md`). `executor_order` holds Source ids (`src_…`), and `default_model`
    names one.
  - A settings change applies from the next turn, as every runtime config change does.
- **API.** Saved and effective values are separate fields, and the UI never writes back an effective value: v1's
  toggle echoed the resolved executor and so froze "automatic" into a saved choice.
  - `GET /api/models/hosted-tools` → `{hosted_tools: HostedToolsStatus}` (§10.3). Per tool:
    - `enabled`;
    - `status: {latest_ok, refused[]}` (§8.1): `latest_ok` is `{source_id, model_id, mode: "native" | "executor" |
      "direct", at}` or null (`direct` for an image call), the latest positive record; `refused[]` is `[{source_id, model_id, aspect, at, reason}]`, the
      current negative records, with `aspect` `call` or `replay` and `reason` the record's detail (HTTP status and
      upstream machine code, never provider prose); `verified[]` is `[{source_id, model_id, mode, at}]`, the positive
      records in memory (current, not as of the last hourly file write), one per pair, from which the Source chips derive (hosted evidence lives on this read, which
      the clears answer; the Source read keeps effort evidence);
    - `latest_ok` is the newest identity-valid success in memory (§3.2), not the newest file record;
    - search tools: `executor_order` (saved, null when automatic), `effective_order` (as the gateway would try now),
      and `executor_candidates: [{source_id, model_id, billing, relay, price}]` in effective order (`model_id` the
      vendor's executor model; `price` the overlay's list price with its units, or null) (`relay` true for a Source on a
      non-official endpoint, labelled "at API price"), every eligible executor Source (a Claude
      subscription never is) with its per-call price, so the order can be edited over the full list;
    - `image_generate`: `default_model` (saved, null when default; a saved model no longer available, or whose pair
      is refused, stays saved, flagged `available: false`); `effective_default` (the saved default if available,
      else the same `model_id` on another Source that serves it, since the user picked a model, not a relay, else
      the first available model in catalog order); `models: [{source_id, model_id, description, billing, price}]`,
      where `billing` is the serving Source's (`monthly` | `metered`, as executor candidates carry it, so the card
      prices a subscription's images as included without another read) and `price` is
      `{usd, basis: "list" | "estimated"}` per image or null ("暂无单价"), exactly
      the enum's candidate pairs, so a refused pair leaves it and the card's refusal line says why (clearing the
      refusal brings the pair back, and a saved default with it, with no write); and `image_offered_by: [vendor]`,
      a catalog fact independent of configuration: the vendors whose bundled overlay lists image models, sorted,
      which the empty state names as the keys that would add one.
  - `PUT /api/models/hosted-tools/<tool>` is per field: an absent field is unchanged, and an explicit `null` resets
    `executor_order` to automatic or `default_model` to the default. `enabled` is a boolean. It answers the same
    read.
  - `POST /api/models/hosted-tools/<tool>/clear` with `{source_id, model_id?}` clears that Source's negative records
    for the tool (`call` and `replay`): for that model when `model_id` is given (a Source chip on a model row), else
    for every model (the Tools tab's per-Source line); with `{}` it clears them for every Source of the tool. A
    `model_id` with no records is a no-op like the others. It answers the same
    read, writes nothing else, and from the next request (hop facts are resolved per request, §3.4) the tool is
    assumed again on those Sources. A request in a running turn may therefore try it again; if it still refuses, the
    re-resolve bounds of §3.5 apply.
    - An unknown tool is 404, as for the PUT. A `source_id` that names no Source, or a Source with no records, is a
      no-op that answers the read, so a double click, two tabs or a Source removed meanwhile all succeed. Any other
      body is 400.
    - **A clear persists before it takes effect.** It writes the evidence file first and then memory; if the write
      fails it answers 5xx and changes nothing, so what the UI shows is true after a restart too. (A refusal is the
      reverse: it updates memory even when its write fails, because it is routing safety, §3.2.)
    - The UI sends clears through the same serialized write queue as the PUT.
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
- **`image_generate`** gets `{models: [{source_id, model_id, description, params, size_forms}], default: {source_id,
  model_id}}` (the endpoint kind and price stay gateway-side): every image model the user's Sources make available (§7.2), in the order the enum uses. The entry is
  absent when the tool is off or no image model is available, so `models` is never empty and `default` never null.
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
    it. Re-resolving applies only to a request whose response has produced no output, so it never discards streamed
    content: a `pause_turn` continuation answered `pin_unavailable` still ends `interrupted` with its partial (§4.3). The fresh evidence turns exactly the refused tool into the proxy function or synthetic replay; other native
    content stays native and pinned.
  - This applies to every `pin_unavailable`, not only `hosted_unsupported`: a new identity drops the old payloads,
    a Source in cooldown yields another hop with the in-place form. Fully portable rendering stays only as the last
    resort.
  - **Bounds.** At most one re-resolve per (turn, Source, tool, aspect) for `hosted_unsupported`, and at most two
    re-resolves per turn across all reasons. When the fresh hop comes back on the same Source with the same facts,
    Vibey renders portable at once instead of resending the same request.
  - **In-flight content in the next request** (client tool results after server tools in the same assistant
    message) keeps its hosted items verbatim only while the fresh hop still declares that tool natively. The
    adapter's own `pause_turn` continuations are not re-rendered at all (§4.3). Otherwise the items take the
    in-place text form (§4.4), because a provider refuses `server_tool_use` without its declaration.
  - **A decrypt refusal** (§3.2, xAI) is mostly unpinned: xAI's encrypted reasoning (ThinkingBlock signatures,
    `rs_…`/`tco_…` items) rides ordinary requests, and only hosted content pins. The gateway passes it through as the
    provider's 400 on either path, so Vibey's adapter, its one classifier, recognizes that 400 before any output as
    an opaque-payload refusal, and the loop re-resolves and renders with
    **every** opaque payload of that Source identity dropped for the rest of the turn: signatures, encrypted reasoning
    and native hosted payloads, since the refusal does not say which one failed. Once per (turn, identity); no
    evidence is written (§3.2).
- **The cost is one cold prompt prefix.** Dropping a native declaration changes the tools block, so the resend and
  the next turns start a new cache prefix: once per (Source, model, tool), since evidence is per model (another
  model of the same Source may run the tool), when the refusal is first learned, and again only
  after the record is cleared (by the user or a stamp change) and the tool is tried natively once more. The upstream is not called twice
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
  `counts` (provider counters by name, each at most 2^53 - 1), `cited_sources[]` (the xAI X-search `url_citation`s).
  A provider's counter or `cost_in_usd_ticks` past its bound is refused where it is read
  (`core/agent_core/ai/hosted.py`), for the native and the proxied call alike, so it reads as absent and never
  fails a response (#2572).
- **`TextBlock.citations`** (optional): `[{url, title?, cited_text?, start?, end?}]`, presentation only, never
  replayed, materialized at delivery by `core/citations.py`.
- **`ToolResultMessage.hosted`** (optional): `{record, execution?, action?}` for a proxied search call and for every
  `image_generate` call.
  - `execution` (`executor_origin`, `executor_source_id`, `executor_billing`, `cost?`) is present exactly when an
    executor or image endpoint ran. Its `mode` is `proxy` for a search executor and `direct` for an image endpoint.
    `executor_source_id`, `executor_billing` and `cost` are the attempt whose response the record came from;
    `failed_attempts[]` lists the earlier billable attempts with their own billing and cost (§6).
  - `action` is what the executor reports it did, beside the record, as today: for `image_generate`, the `generate`
    action with the `model` used, the prompt it rendered, and the resolved size.
  - `is_error` is true exactly when the record is `failed` or `incomplete`.
- A native hosted call always has its native payload. A proxied search or an image call is a `ToolCallBlock` plus
  `ToolResultMessage.hosted`, never a `HostedToolBlock`.
- **Native image items are not hosted items.** Vibey never declares `image_generation` (and §7.1 keeps any caller
  from receiving it), so an `image_generation_call` is ignored like any undeclared item, and a line over 8 MiB fails
  the stream like any oversized line. The #2589 codes `image_not_streamed` and `provider_no_image` lose their
  producers and stay in `HOSTED_ERROR_CODES` for records already written.
- **Stored history survives hosted-tool registry changes.** An unrecognized native `HostedToolBlock` loads as
  portable text with its query or URL and sources when renderable, otherwise a one-line unavailable placeholder.
  The projection keeps a readable non-completed status and error, including an explicit no-result marker for pending
  calls, so a failed or interrupted call is never presented as a successful result.
  This includes an rc-era `image_generation` block without restoring its removed native shape or replay path.
  A proxied result for an unrecognized tool keeps its `content` and `is_error`, with `hosted` dropped. New writes
  and known-tool rows remain strict; no migration or stored-row rewrite is performed, and each degraded row logs
  one warning without content.
  If any native block is degraded, response-wide counts, cited sources and cost are discarded: they cannot be
  partitioned between the unknown call and the remaining calls. Calls are decoded before that aggregate, and its
  discarded contents are not validated against this build's summary/source schema. A known block still requires a
  summary; an existing summary becomes empty when known blocks remain and is omitted when none remain (§15).
  The message envelope and retained known blocks remain strictly validated.
- **Per adapter notes.** xAI's X-search `reasoning` items (`tco_…`) stay ThinkingBlocks. The `custom_tool_call`
  becomes the HostedToolBlock, never a local tool call. Anthropic's `encrypted_content` is kept only in
  `native.payload`. Hosted payloads count against the 32 MiB `StreamAssembler` budget; search events measured at
  most 96 KB, so every stream uses the 8 MiB line cap.

### 4.3 Anthropic server-tool continuation

**`pause_turn`.** Unchanged from v1 as implemented (orchestrator decision, 2026-10-14):
- The Anthropic adapter continues a paused response internally, at most 5 continuations, each pinned to the Source
  that served the paused response. It never re-renders a continuation in portable form.
- A continuation that Source answers `pin_unavailable`, a pause of a send that carried no pin, or a 6th pause ends
  the response with one terminal `ProviderError(kind="interrupted")` whose partial keeps all assembled content.
- The merged message carries the final continuation's usage; `hosted_summary` sums counts and cost and deduplicates
  `cited_sources` by URL. Pending calls carry over by tool-call id, so each call stays one block.
- The merged message is verified only when every response in it was verified and reported the same origin.

**Mixed parallel groups** (C-1 clarification approved 2026-10-22). A response with `stop_reason: tool_use`, client
`tool_use` blocks, and a `server_tool_use` lacking its result has **not run** that server tool. After the client
results, the next response starts with a `web_search_tool_result` whose `tool_use_id` names that earlier use; it
never repeats the use. The adapter carries the pending use by id, as for `pause_turn`, but this continuation crosses
C-5's commit boundary, so its completion is append-only:

- Response 1 keeps its ordinary pending `HostedToolBlock`, with the use alone in `native.payload`.
- Response 2 holds an ordinary completed (or failed) `HostedToolBlock` with the **same id, tool and action**, and
  the result alone in `native.payload`. These are two immutable fragments of **one logical call**. No committed
  `content_json.model`, `context_seq` or trace row is rewritten. The v1 shape round-trips through the actual
  v3.2.2 and gh-v3.2.3rc2 readers; no schema version or new persisted message kind is needed.
- Pairing requires the preceding mixed response, its client results, and the same origin, Source and Source
  identity. A result from a re-resolved hop cannot complete an old Source's call.
- Usage and hosted counters remain on the response reporting them, unchanged. Parsing a completion never adds a
  charge inferred from a block. A missing reported counter remains missing; it is not manufactured as zero or one.
- Web and IM follow one rule: a deferred use produces no card; response 2's result fragment renders as an ordinary
  hosted card at its own position, after the client-tool activity, live and on reload. A call that never completes
  produces no card. Old pending trace cards of this exact deferred shape are also hidden at read time; other pending
  calls (for example a stream cut with `stop_reason: error`) retain interrupted rendering. No cross-response card
  merge, provisional state, held-card map, audit receipt or special delivery guarantee exists.

Provider source, fetched **2026-10-22**: [Server tools — Mixing server tools and client tools in one turn](https://platform.claude.com/docs/en/agents-and-tools/tool-use/server-tools.md).

### 4.4 In-flight turn replay

Within one turn, a client `tool_result` after server tools in the same assistant message makes the next request
pinned to the Source the previous response came from, and the assistant content goes back verbatim. (The adapter's
own `pause_turn` continuations follow §4.3 and never take the fallback below.) This is the provider's own continuation contract, so it needs no `replay` evidence, but it does need the
tool still declared natively on that hop (§3.5).

For a mixed pending group (§4.3), the assistant content is sent in its original order, followed by **one user
message containing only all client `tool_result` blocks**, with the same tool declarations. No user text follows
those results: it would close the still-open server-tool turn and the provider rejects it. Queued steers therefore
wait until the deferred result has arrived, including after a restart: T3 derives the open barrier from the
persisted transcript and owning Turn, leaves accepted inputs unconsumed, and restores them to `Agent.resume`'s
queue before it opens admission. A stopped/settled Turn has no continuation barrier and admits them normally.
A subsequent internal `pause_turn` preserves the entire linked exchange
natively without requiring historical replay evidence.

A failed partial and every client-tool result owned by that failed response are omitted together from the effective request history used by in-flight discovery, completion pairing and wire compilation. Their durable rows remain audit facts, and reused client call ids become eligible again only after the next valid assistant response resets that failed-attempt scope. For the same
Anthropic origin/account's deferred exchange, the partial neither closes the in-flight request nor completes a pair for
replay eligibility. An already-authorized transcript retry therefore sends the preceding valid mixed response
and its client results unchanged; a later successful response pairs across the failed attempts. The failed
partials and their reported usage stay immutable in storage. This does not grant retry permission or make a
failed stream resumable like `pause_turn`; Stop/`aborted`, a new user input, or another origin/account still closes
the exchange.

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

- **Cross-response Anthropic completions** (§4.3) are joined before replay eligibility is decided. The earlier use
  counts as finished when its later fragment completed it. Native replay retains the exact response order:
  assistant use → client-result-only user → assistant server result. Both halves go native or neither does.
  Portable replay emits **one** same-named synthetic call in the first response's parallel group and its result
  alongside that group's client results; it suppresses the completion fragment in response 2, retaining its other
  content and its usage. An unmatched pending call still renders interrupted.
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
  gateway resolves the chain from the caller's live turn (`x-avibe-turn-id`). A call without a live turn, or from a
  turn that no longer admits work, is 409 `turn_not_live`; a tool outside the turn's frozen tool set (§3.4) is 409
  `tool_not_enabled`. Nothing runs. The gateway answers `turn_not_live` before it validates the body, and
  `tool_not_enabled` after. v1's `executor_not_bound` is retired with the binding.
- **The frozen tool set lives on the turn.** The turn's first resolution writes its tool set, each search tool's
  effective executor order, and the `image_generate` enum, once to the turn trace (`hosted_tools`, replacing the
  write-once `hosted_executors`). The endpoint authorizes every call against it, and a settings or Source-order change
  applies from the next turn, as every runtime config change does.
- **The turn still owns the call.** The endpoint stays authorized by the live named turn (#2586's `x-avibe-turn-id`
  and closed admission); only the per-turn executor binding goes. Stopping the turn cancels the call, and it is
  still metered. A candidate whose identity changed since the chain was built is not used.
- **The chain, built at call time.**
  1. Candidates are the Sources whose vendor offers the tool natively (§3.1) and that list one of the vendor's
     `executor_models`: the first one the Source lists that runs the tool, exactly or as a dated snapshot, never a
     retired one (a custom relay may list several vendors' executor models, §3.1). `runs_on`
     must hold for it, with no current negative `call` evidence for that pair. A Source that lists none is excluded
     when the chain is built, not found by a 404: an API-key Source admits unlisted models on a turn's hop, but a
     call nobody asked for must not guess.
  2. Order: the turn's frozen effective order, which is the user's `executor_order` when set, then the remaining
     candidates in Vibey's Source order, as they stood at the turn's first resolution. A Source added mid-turn joins
     from the next turn.
  3. The gateway admits each candidate on the final snapshot, under the mutation lock, at the moment it calls it
     (`runs_on`, identity, no negative `call` evidence), so a config change during the chain moves on instead of
     calling a removed or re-identified Source. This is the same per-request admission every model request of the
     turn already gets (C-6 item 3, #2616's identity rule): the turn freezes the order, not the Sources' runtime, and a
     removed Source cannot be served at all. It calls the first admitted candidate: one short native request on
     that Source's executor model, with the
     overlay's declaration, the tool forced where the protocol allows it (`tool_choice: "required"` on Responses;
     Anthropic is asked in the message alone), a fixed instruction to search and list numbered results with one or
     two cited sentences each, a small output cap, and no reasoning effort.
  4. On a failure that normal routing fails over from (a transient one such as an overload or a server error
     included), or a narrow-rule refusal (which also writes negative evidence), it tries the next candidate. A model
     refusal (the upstream's machine code naming the model, such as `model_not_found`, or a 404 `not_found_error`, as
     routing's classifier reads it; L1 hands no prose to it) skips that Source for this call without settling its
     health, as `_InvocationUnsupported` does. It is not a refusal of the tool and writes no evidence: only the
     narrow rule, an error naming the feature, does. With the executor model taken from the Source's listing, it
     means a stale listing, so a discovered row marks that Source for the background schedule (#2682). The
     schedule's own due rule lists a marked Source once its listing is 15 minutes old, under its eligibility,
     backoff and shutdown. The row leaves only if that listing, under the identity that refused it, no longer
     names it either and its removal moves no route; the rest of the listing merges as the schedule's always
     does. A manual row is the user's, and stays. A failure mid-stream moves on too: nothing
     reaches Vibey until the gateway returns the record, so "before output" here means before the result is
     returned, and the partial call is still metered on its Source. A completed response that never ran the tool
     (the model chose not to; Anthropic is asked in the message alone) ends the chain with a `failed` record: it is
     a model choice, not a Source failure, and moving on would double the cost. Any other outcome ends the chain.
  5. With no admitted candidate, the call returns a `failed` record naming the providers whose Sources can run the
     tool ("add a Source from one of these providers: xAI"). When Sources that would run it exist but each has a
     current refusal of the tool, the record names those Sources and points at clearing the refusal in Models › Tools
     instead (amended 2026-10-20, #2873): an upstream refusal is not a missing credential. With candidates that were all tried and failed, it
     returns the last failure's reason and names the Sources tried, so the user sees that retrying, or those
     Sources, is the issue.
  - **One deadline for the whole chain**, the tool's call timeout (180 s for search), not one per candidate.
  - Each candidate tried is metered on its own Source. The result's `execution` names the Source whose response the
    record came from (the last one tried when all failed), with that attempt's `executor_billing` and `cost`; earlier
    billable attempts (those that reported tokens or produced a response; a refusal before output that reported
    none is not billed) are listed in `execution.failed_attempts: [{source_id, billing, cost?}]`, each with its own
    billing and cost basis, so a metered failure before a subscription success keeps both. A subscription attempt
    carries `cost: {basis: "included"}`, so an absent `cost` always means unpriced.
  - **The total is computed once**, in `core/hosted_activity`'s projected `cost`, which the Web card and the IM line
    both render: `usd` is the sum of the known figures over the record's attempt and `failed_attempts[]`; every
    attempt `included` gives `included`; the basis is `reported` only when every summed figure is reported, else
    `list` (shown with ≈); an attempt without a `cost` makes the total a lower bound, shown as "at least $X" /
    "至少 $X", never as complete. The projection carries the lower bound as `cost.at_least`; `cost.calls` stays the
    number of calls one native response's figure covers. With no priced attempt to sum, the cost is unknown rather
    than "at least $0". No attempt count is shown: the producer lists billable attempts only and does not say whether
    the last attempt was billed, so any count would be inferred, and the total already says what the call cost.
  - A model executor's request is a short model request because these vendors expose search only inside a model
    call: Anthropic, OpenAI's public API, xAI and Google. Search APIs that need no model join the `web_search` chain
    as search services ([`web-search-services.md`](web-search-services.md) §4).
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
     "model": {"enum": ["gpt-image-2", "gemini-2.5-flash-image", "gemini-3-pro-image-preview"],
               "description": "gpt-image-2: … (default) · gemini-2.5-flash-image: … · gemini-3-pro-image-preview: …"},
     "prompt": {"type": "string"},
     "size": {"type": "string", "description": "e.g. 1024x1024, 1536x1024; or an aspect ratio such as 16:9"},
     "images": {"type": "array", "items": {"type": "string"}, "maxItems": 4, "description": "paths of reference images to edit"},
     "n": {"type": "integer", "minimum": 1, "maximum": 4}}}}
  ```

  - **The enum is the image models actually available**: the overlay's `image_models` (§3.1; on a custom Source only
    those it lists) for each Source where `runs_on` holds for that model and no current negative `image_generate` evidence exists for the pair (§3.2: a
    relay without the vendor's image endpoint refuses once and is then left out), deduplicated by model id (the first Source in Vibey's order serves an id that
    two Sources offer, unless `default_model` names the other). Each entry's one-line description goes into the
    `model` description; every line stays short, because the tool block is in every request's prefix. The default is listed first and marked "(default)"; the rest follow sorted by id. So the
    enum is a pure function of configuration and current negative evidence, and the turn freezes it (§3.4). A
    change (a config event, an image refusal or a clear) costs one cold prompt prefix on the next turn, as §3.5 says
    for search.
  - **Common parameters only.** `prompt`, `size` (a size or an aspect ratio, mapped per endpoint), `images`
    (reference images for an edit) and `n`. The overlay's `params` say which of `size`, `images` and `n` each model
    takes, and `size_forms` which forms of `size` it accepts (`pixels`, `ratio`, or both); the gateway maps the given
    form to the endpoint's parameter. A `size` in a form the model does not accept is an unsupported argument. A call that passes one the chosen model does not take returns a `failed` record naming the argument and
    the models that take it, before anything runs: silently dropping `images` would turn an edit into an unrelated
    generation. Vibey's tool runtime refuses it before the call, from the turn's frozen `params`, so a bad call costs
    nothing; the gateway validates again as the execution boundary.
  - **Avibe-written failures carry stable codes** (#2589, `HOSTED_ERROR_CODES`): `image_param_unsupported` for an
    argument the model does not take, and `image_model_unavailable` when the named model is no longer available
    mid-turn. The UI adds copy for each.
  - **No tool without a model.** With no image model available, `image_generate` is not declared at all, and the
    Tools card says what to add (§8).
- **The default** is `model_hub.hosted_tools.image_generate.default_model` (§3.3), else the first available model in
  catalog order.

### 7.3 Files and a text path record

Unchanged from v1 §7.3 (owner decision, 2026-10-13 19:14), with the producer now the direct call:
- **The model is not given the image.** The gateway returns the bytes to Vibey's tool runtime over the local hop;
  Vibey saves them through the session media sink, the only writer of session media, and builds the `HostedRecord`
  with `images[]` path records. The bytes are never in a transcript.
- **The model gets a text path record:** path, model, size, mode and cost, rendered from the record, the result's
  `generate` action (which now names the `model`) and its `execution`, e.g.
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
  {model, prompt, size?, images?, n?}}`. Vibey reads each reference image itself, exactly as `read` resolves a path
  (relative to the session's cwd, under the same file permissions) and with its checks (a supported image type,
  within `read`'s inline limit), and sends the bytes base64 in `images`. A path the agent could not `read` is
  refused before any call, with a `failed` record whose error names the path.
- **One path: the engine's images endpoints.** Every image model runs through `/v1/images/generations`, or
  `/v1/images/edits` when the call carries reference images, as a buffered JSON call (`EngineClient`'s
  `openai_images` request protocol, with its own `stream_wire` taxonomy for the outcome and usage). The overlay
  entry's `request` holds the fixed fields the model needs (JPEG at quality 90 for GPT Image, so a file fits `read`'s
  inline cap; base64 results for Gemini), and the call adds `prompt`, `n`, `size` in pixels or `aspect_ratio` for a
  ratio, and each reference image as a `data:` URL. An API-key Source's engine entry registers its vendor's image
  models, and an OpenAI-compatible entry flags each with the engine's per-model `image: true`, without which the
  engine's images handler refuses a non-GPT-Image name.
- **What was verified** (gateway lane, 2026-10-15, the pinned engine v8.0.23-3 against a stub upstream,
  `tests/e2e/test_model_hub_images.py`):
  - **An OpenAI API key** (`gpt-image-2`): generations and edits reach `{base}/images/generations|edits` with the
    body unchanged but for the bare model id, and the answer comes back byte for byte. The engine serves it through
    its Codex executor's direct Image API path.
  - **A Gemini API key** (`gemini-2.5-flash-image`, `gemini-3-pro-image-preview`): the flagged entry reaches
    Google's OpenAI-compatible `images/generations`, the only image path Google documents on that layer (its chat
    route does not return images for these models). No reference images: Google documents generation only.
  - **xAI grok-imagine is not reachable**: the engine's images handler accepts an xAI image model only under an
    empty, `xai` or `grok` prefix and then routes the bare id, which `force-model-prefix` never registers (unchanged
    in v8.0.28). That 400 is the engine's own refusal, recognized as local, never learned. xAI ships with no image
    models until the engine or the xAI rendering changes.
  - **A Codex (ChatGPT) subscription** (`gpt-image-2`): the engine runs it through the same direct Image API
    function against `https://chatgpt.com/backend-api/codex`, a host no stub can stand in for (an OAuth auth file
    takes no base URL), so no hermetic E2E verifies it. One real call with the owner's approval verified it on
    2026-10-18. The call went through engine v8.0.23 with a Codex subscription credential, and its body was the
    one the gateway sends. It returned one `b64_json` image with HTTP 200, plus `usage` with 515 output image
    tokens, and no refusal. Its cost reads as `included`, as for every monthly Source. The subscription path
    ignored `output_format`, `output_compression` and `size`: it answered with a PNG at 1254×1254 and `quality: low`.
    The result reader takes any supported image type, and the size an answer reports is the size recorded. So
    `gpt-image-2` lists both credential kinds. The overlay's other image models stay `api_key` only until a call
    verifies them.
- **Route verification is the implementation's job, not the user's.** A model whose route is not verified ships
  without an overlay entry, or without the credential kind it was not verified on, so it is never offered and the
  UI never needs an "unverified" tag.
- **Authorized by the turn's enum.** The gateway resolves the Source from the turn's frozen `image_generate` enum
  (§3.4): a model outside it is 409 `tool_not_enabled`, and the chosen (Source, model) is admitted on the final
  snapshot (`runs_on`, identity, and no current refusal: a pair refused earlier in the turn fails locally with
  `image_model_unavailable` while the declared enum stays frozen). So a caller cannot bill an image model or Source
  the turn was not offered.
- **Bounds.** The endpoint's 1 MiB request bound becomes per tool: `image_generate` allows its reference images,
  at most 4, each within `read`'s inline limit, plus 1 MiB; the size check stays before parsing and the JSON parse
  stays off the loop. `n` is at most 4, and decoded bytes are capped per image and per call as the executor caps
  them today; a buffered response with several base64 images is parsed and size-checked off the loop.
- **No idle cut for a buffered call.** The engine client's 120 s socket-read bound applies only to streamed calls on
  the measured Sources, so a buffered images call longer than 120 s is not cut; the tool's call deadline bounds it.
  If an images path ever streams partial images, those partials must keep feeding the idle bound.
- **No silent fallback.** A failed call returns a `failed` record whose error names the alternatives ("<reason>.
  Available: gemini-2.5-flash-image, gemini-3-pro-image-preview"). The model may call again with another
  model; the gateway never switches on its own.
- **The result names the model used**, in `ProxyExecution` and in the path record.

## 8. User interface

### 8.1 Models → Tools

- **Grouped by capability**, one card per tool, each with one switch, on by default:
  - **搜索**: 网页搜索, X 搜索;
  - **生成**: 图片生成.
- **The subtitle says where content goes**, plainly, because tools are on by default:
  - en: "Tools Vibey can use in a conversation. Searches send the query to the vendor that runs them; if one fails,
    the query may be retried with another of your Sources. Image prompts go to the image model's vendor. On metered
    Sources each call is billed."
  - zh: "Vibey 在对话中可使用的工具。搜索会把查询发给执行它的厂商，失败时可能改由你的其他供应商重试；图片提示词会发给图片模型的厂商。按量付费的供应商每次调用都会计费。" ("按量付费" is the Models vocabulary for metered billing.)
- **One status line per tool, from the read's `status` (§3.3), and only after something happened:**
  - "最近一次由 <Source> 执行 · 正常 · 10-15" from `latest_ok`. It shows a date, not a time: positive records are
    written at most hourly (§3.2). A call line that carries "已改用" stands in for it, since it already says where
    the tool runs now;
  - refusals come from `refused[]` grouped by aspect and then by `source_id` (the read stays per record), each group
    with its newest `at` and its refused models, since evidence is per (Source, model) and the same Source may run
    the tool on another model. Call refusals and replay refusals each get their own line under the rules below; a
    Source with a call refusal appears only in the call line, since its replay refusal is moot while the tool is not
    declared natively there. The two-or-more threshold counts distinct Sources across both lines:
    - **one Source refused:** the status line reads "<Source> 不支持 · <models>" (joined with `Intl.ListFormat`), with
      "，已改用 <Source>" appended when that newest `at` is older than `latest_ok` and `latest_ok` is on another
      Source, and has a "清除" action for that (Source, tool);
    - **two or more Sources refused** (owner-approved mockup, 2026-10-15): the status line names the Sources
      ("A、B 不支持", with "，已改用 <Source>" under the same rule taken over every group), and the clears sit in the
      card's **Advanced** section, in a "各供应商状态" list: one row per (Source, model) with a record, a refusal over
      a success as on the chips (§8.2), each refusal row with "清除" for that pair (`{source_id, model_id}`), and
      "全部清除" for the tool (`{}`);
  - the replay line reads "<Source> 不支持回放历史结果" (or "A、B 不支持回放历史结果"), since those Sources still run
    the tool, and clears the same way;
  - every clear uses the `clear` endpoint (§3.3). A cleared Source is tried natively again from its next request; if
    it still refuses, the line comes back after that call. Refusals do not expire on their own (§3.2);
  - nothing before first use. No "unknown", "unverified" or "unchecked" wording anywhere.
- **Search cards** show an optional **Advanced** section (which also holds the clear actions when two or more
  Sources refused) with the executor order over `executor_candidates`: up/down
  buttons (keyboard- and screen-reader-accessible, the route chain editor's pattern), each row with its Source's
  executor model and its unit rates as the overlay lists them ("$5 / 1k posts + $10 / 1k profiles", "at API price"
  for a relay), never a single per-call figure where the count is unknown before the call, collapsed and fully automatic until touched, with "Reset to
  automatic" (a `null` PUT). The price shows per row because it depends on who runs the search. A move
  rearranges the shown executors within the saved order: an id the saved order names but the read does not show (a
  refused or absent Source) keeps its place, and a shown executor the order lacks follows it.
- **The image card** lists the available image models with their one-line descriptions and a per-image price
  (owner-approved mockup): the vendor's unqualified list price where it has one (Grok Imagine "$0.02"; a price for
  one size or quality is not shown as every image's); an estimate from token
  pricing, marked "≈", where the image's tokens are known: the pricing module values the overlay's
  `tokens_per_image` at the model's output token rate (Nano Banana "≈$0.067"); "暂无单价" / "no unit price" where
  neither is (gpt-image-2, which OpenAI prices per image token at a varying count). It has a default-model picker over `default_model` / `effective_default`; a saved default that is no longer available
  shows as itself, "(unavailable)", with a note naming the model in use (`effective_default`), and stays visible and
  resettable even when no model is offered. The picker's "Automatic" option saves `null`; it names the model in use
  while nothing is saved, and otherwise says how automatic picks (the first available in catalog order), since the
  read does not say which model that would be. A model on a subscription Source (`billing: monthly`) shows "订阅内含". Each shipped model
  has its own description in every locale (UI copy keyed by model id, guarded by a test over the bundled overlay);
  the overlay's `description`, which is model-facing, shows only for a model without copy. Image-endpoint refusals (§3.2) show and clear like search refusals; the image card
  gets an Advanced section only when its clears move there (two or more Sources refused), holding only those; any other
  failed image call shows only on its tool card in chat, since passive evidence keeps no other image failures. With no image model available,
  the card says why from the read: with a current image refusal, a neutral line ("No image model is available
  now; clear a refusal to try it again"), the refusal lines and their clears saying the rest; otherwise the vendors
  from `image_offered_by` as the keys that would add one ("图片生成需要 OpenAI、Google 其中一家的 API Key").
- **Removed:** check and check-all, the confirm and result dialogs, Re-verify, the probe estimate, the executor
  picker, and "engine upgraded" headers (§11).

### 8.2 Source detail chips

Each model row on a Source's detail panel shows only passive state, with the detail popover (time, basis, reason).
There is no "via proxy" chip: proxying is the tool's business, not the Source's.
- "网页搜索 · 正常 · 10-15", "图片生成 · 正常": a positive chip, with no action;
- "X 搜索 · 不支持", "图片生成 · 不支持", "网页搜索 · 不支持回放", "ultra · 不支持": a refusal chip, which carries
  "清除" as its own trailing segment (owner-approved mockup, 2026-10-15); its popover states the facts (time,
  reason, how long it stays, the last success) and a hint, and holds no action. A hosted-tool refusal chip clears
  both aspects of that tool for that model, through the Tools `clear` endpoint with its `source_id` and `model_id`
  (§3.3); an effort refusal through the Source evidence clear (capability model §5). Each clear answers the read
  that owns its evidence, so nothing else is read again: hosted evidence lives only on the hosted-tools read, which
  the hosted clear answers, and effort evidence only on the Source, which the effort clear answers.

A model row shows one chip per feature: when a positive and a negative record coexist (§3.2), the refusal chip
wins, because it is routing state, and its popover may still name the last success ("曾于 10-15 正常"). A call
refusal also hides that tool's replay chip, which is moot while the tool is not declared natively.

Unknown shows nothing. The chips reuse `EvidenceChip`'s locale-composed spoken name and popup semantics, without the
recheck action. Every chip stays focusable and opens its detail from the keyboard, positive or refusal, because the
detail (time, basis, HTTP status and code) is content, not only a place for an action.

### 8.3 Use time in Vibey

Unchanged: the tool card (query, mode "native · xAI API key" or "via Anthropic API key · claude-haiku-5-5", cost
when metered, sources, failure reason); citations through the existing citation contract; the IM one-liner
`🔎 Web search "query" · native (xAI) · 6 sources · $0.01`. The image card shows the prompt, the image, the model
used and the cost; IM attaches the file.

Who ran a call is its vendor's catalog label. A Source whose vendor the catalog does not name, as a compatible
endpoint's `custom` does not, is named by its Source name in the vendor's place on both surfaces, and is not repeated
beside it (amended 2026-10-20, #2873).

**When a search ran by proxy because the turn's own Source refused it** (`hosted.fallback_from`, set by Vibey on a
hop whose C-6 facts say `refused`), both surfaces say so, so the switch is never silent:
- the Web card keeps a note in view, collapsed or expanded: "relay-gpt 不支持网页搜索，已改用 Anthropic（relay-claude）"
  with "之后自动沿用，可在 模型 › 工具 中清除" for a reader who can manage the instance, and "之后自动沿用，实例管理员可在
  模型 › 工具 中清除" for anyone else, who cannot open Models;
- the IM line names it in place of how the call ran: "🔎 网页搜索 “…” · relay-gpt 不支持，已改用 Anthropic · 6 个来源 · $0.01".
Like every claim about how a call ran, the note shows only for a completed call. The refusing Source is named by
its current name (`source_names`), and the note
disappears once the refusal is cleared and the hop is native again.

**Untrusted text on the IM line.** The subject (a model's query or prompt), a provider's error, and a user's Source or
model name are not Avibe's words. `hosted_line` passes each through `core.message_output.plain_untrusted_text`
(mentions and masked links made inert on every platform) and then the destination's escape. A platform escape alone
leaves Discord's `@everyone`, `<@id>` and `[label](url)` live.

## 9. Defaults, cost and privacy

- **Every tool is on by default** (owner decision, 2026-10-15). This reverses v1's default, whose premise was that a
  call costs money, sends queries to a third party, and needed a cost-bearing check to turn on. v2 removes the check;
  the other two remain true and are stated in the public docs:
  - a search sends the model's query to the vendor that runs it: the hop's provider natively, or the executor
    Source's vendor via proxy; when an executor fails, the same query may go to the user's next executor Source,
    which can be another vendor or the same vendor through another key or relay (§6);
  - an image prompt and any reference images go to the chosen image model's vendor;
  - on metered Sources each call costs the tool fee plus tokens (§10.4); on subscriptions it is included.
- **No onboarding step mentions tools**, and nothing calls a provider unless the model calls a tool in a turn the
  user started.
- **Per-call prices** live in the overlay with date and source (capability model §6.2): OpenAI web search $10 / 1k
  calls plus content tokens; Anthropic $10 / 1k searches plus result tokens, re-billed while they stay in context;
  xAI web search $5 / 1k calls, X search $5 / 1k posts and $10 / 1k profiles. Image models show a per-image price on
  the Tools card: the vendor's list price (Grok Imagine $0.02), or an estimate from token pricing marked "≈" (Nano
  Banana ≈$0.067), or "暂无单价" (gpt-image-2); a real call shows its reported or valued cost, never zero for
  unknown.
- **Relays** show list price, labelled "at API price", and may bill differently.

## 10. Contract changes

The contract edits land with the implementation PRs as one coordinated Model Hub version event after #2626's v14,
under the freeze protocol (`model-hub-contracts/README.md`).

### 10.1 C-1 (`agent-core-contracts/message.schema.json`, `cross-provider.md`, `conversion-tables.md`)

- A new `HostedToolBlock` has `tool` `web_search` | `x_search`, and known-tool rows are read strictly. Stored
  unrecognized tools, including rc-era `image_generation`, load as portable text rather than native blocks (§4.2, §15).
- `ToolResultMessage.hosted` keeps `{record, execution?, action?}`. `record.images[]` and `action: generate` occur
  only for `image_generate`; `generate` gains the `model` used, with `prompt` required for a `completed` record.
- `ProxyExecution` becomes the result's `execution` with `mode: "proxy" | "direct"` (direct for an image endpoint)
  and `failed_attempts[]` (`{source_id, billing, cost?}`) for the earlier billable attempts of a search chain. `core/hosted_activity` maps `direct` beside `proxy` for the
  tool card and the IM line.
- `HOSTED_ERROR_CODES` gains `image_param_unsupported` and `image_model_unavailable` (§7.2), with UI copy for each.
- The conversion rows drop native image replay; an `image_generate` call is an ordinary tool call everywhere.

### 10.2 C-6 (`agent-core-contracts/model-hub-consumer.md`, both `hop-resolution.schema.json` copies)

- `HopResolution.hosted_tools` entries become `{native: "assumed" | "unsupported", declaration?, include?, replay:
  "assumed" | "unsupported"}` for search tools (§3.4), and `image_generate: {models[{source_id, model_id,
  description, params, size_forms}], default}`; `mode` and
  `executor` are removed. `declaration` and `include` are present exactly when `native` is `assumed`.
- Admission of a pinned request reads negative evidence only, through the hop's own facts (`hosted_tool_facts`): a
  declared native tool needs `native` assumed and the overlay's declaration type; a replayed item needs `replay`
  assumed, which a current `call` refusal withdraws as well as a `replay` one.
- The `pin_unavailable` envelope gains `reason` (§3.5).
- The executor body is `{"arguments"}` only; the gateway resolves the chain from the live turn (§6). The endpoint
  keeps turn ownership; it authorizes each call against the turn's frozen tool set and, for `image_generate`, its
  enum. Errors are 409 `turn_not_live` and `tool_not_enabled`; `executor_not_bound` is retired.

### 10.3 Model Hub contracts

- `capability-evidence.schema.json`: `basis` is `observed` only; `status` `verified` (display only) or
  `unsupported`; `shape` is removed and `engine` becomes `wire`, the stamp of §3.2; `feature.name` is `web_search`
  | `x_search` | `image_generate`, and `image_generate` has `call` only.
- `hosted-tools-status.schema.json` is rewritten for §3.3's read: per tool `enabled` and `status {latest_ok,
  refused[], verified[]}`; search tools `executor_order`, `effective_order`, `executor_candidates[]`; `image_generate`
  `default_model`, `effective_default`, `models[]`, `image_offered_by[]`. `reverify`, `executor`, `bound` and
  `candidates[]` (with `call`, `replay`, `probe_estimate`) go. The PUT body is per field (§3.3).
- `hosted-tool-probe-result.schema.json` is deleted, with its `api-response.schema.json` branch and `api.md` row.
- `refused_efforts[].changes_at` (`agent-chain.schema.json`, `source.schema.json`) loses its meaning, since nothing
  expires by age: it is removed with its UI expiry hint (`capability.expiryProbe` and the observed-expiry copy); the
  hosted-tools read already drops it (§3.3).
- `api.md` gains `POST /api/models/hosted-tools/<tool>/clear` (`{source_id?, model_id?}`, `model_id` only with
  `source_id`, → the read, §3.3) and
  `POST /api/models/sources/<source_id>/evidence/clear` (`{model_id, feature}` with an effort feature → the Source,
  for an effort refusal; capability model §5), each with its `api-response.schema.json` branch.
- `provider-overlay.schema.json` gains `executor_models[]` per vendor and `image_models[]`.
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
| Gateway | `stream_wire`'s `response.image_generation_call.partial_image` model-output envelope; the executor's native image path (`ResponsesStreamReader(partial_images=…)`, the 32 MiB image line cap, the image event and byte budget, `_image_result`, the `_IMAGE_*` constants, the 300 s image `call_timeout`, and the image-sized 128 MiB response budget, now 16 MiB for a search answer), replaced by what the images endpoint needs; the OpenAI `image_generation` overlay declaration (#2580's JPEG q90 becomes the images-endpoint request parameters); `executor` in the proxy body; `executor_request`'s image branch (its `include`/`partial_images` read of the overlay image declaration); the `hosted_executor_bindings` / `turn_hosted_executors` plumbing, replaced by the turn's frozen `hosted_tools` (§6); the `hosted_tools_status` candidates and bound-executor projection. `_own_turn_call` stays. Kept: `disable-image-generation: passthrough`; `hosted_admission`'s "image calls are never replayed" rule until no transcript can carry a native image call; #2616's per-turn identity binding and #2586's turn id. |
| Vibey | the native `image_generation` declaration, its parsing (`responses_hosted_tool`'s `image_generation_call` mapping, `image=`/`image_error=` in `responses_hosted_block`), `_ImageBudget`, `_saved_image`, `_PARTIAL_IMAGE_EVENT`, `IMAGE_STREAM_LINE_BYTES`, the large-event JSON offload, `drive_sse_stream`'s `max_line_size` and `iter_sse_events`' offload (every stream returns to 8 MiB); `ModelRequest.tool_choice` and `_common.forced_tool`; `registry.build_wire_request` (its tests move to each adapter's `wire_request`); the adapters' probe-only `media_sink` parameter and the `media_sink` argument of `registry_providers(media_loader=…, media_sink=…)`; `HostedTool`'s `executor` field and its `executor` body; the agent's `_with_hosted_images` / `_emit_orphan_images` path for native images (`agent.py`), kept for `image_generate` results. `HostedRecord.images`, `HostedImage`, `generate_action` and the `generate` action stay, on `ToolResultMessage.hosted` only. |
| UI | `ProbeAnswerNote.tsx` (with `ReplayNote`); in `ToolsTab.tsx` `ProbeCell`, the executor picker and its notes, the `needsCheck`/`needsExecutor` states, the "unchecked" chip and the probe column; in `HostedCapabilityChips.tsx` the recheck action, the recheck-only keyboard path (every chip keeps its keyboard path into the detail), and the reverify, engineUpgraded and unsupportedAged states; `useEstimateLabel.ts`; the `probeHostedTool` wiring in `SettingsModelsPage.tsx` and its Sources re-read after a probe (no clear needs one: §8.2); in `useHostedTools` `probe`, `probing`, `lostProbeOutcome`, `recordedOutcome` and `PROBE_NOT_RECORDED` (the serialized writes, identity fencing and re-read after Source mutations stay); in `hostedTools.ts` `ProbeTarget`, `probeKey`, `probeUnanswered`, `verifiedCandidates`, `sameExecutor` and the `aged`/`engineUpgraded` branches of `evidenceChip`; `modelsApi.probeHostedTool`; the types `HostedToolProbeResult`, `HostedProbeAspect`, `HostedCostEstimate`, `probe_estimate`, `HostedToolCandidate`, `bound`, and `executor` in `HostedToolPut`. Rebuilt on the new read rather than only trimmed: `modelCapabilities` and the Tools-tab rows on `status`; `cost.imageTokens` ("no per-image price yet") by per-image prices; `capability.chip.viaProxy` and `explain.viaProxy` go with the "via proxy" chip. Check-all is removed by the UI lane's pending PR. |
| i18n (`settings.models`) | `tools.probe.*`; `tools.state.needsCheck` and `needsExecutor`; `tools.executor.*`; `tools.chip.unchecked`, `reverify`, `engineUpgraded`, `unsupportedAged`; `tools.reverify.*`; `capability.recheck`, `recheckCost`; `capability.chip.*` and `capability.explain.*` for reverify, engineUpgraded, viaProxy, viaProxyAged and unsupportedAged; `capability.expiryProbe`; `tools.col.*` and `tools.chip.native`/`unsupported`/`executor` (the candidate table goes). Rewritten: `tools.subtitle` (§8.1), `tools.empty` and `tools.noNative` against the new read; `tools.footnote` keeps its relay and subscription sentences. |
| Tests and scenarios | `tests/test_model_hub_hosted_probe.py`; the probe and executor-binding cases in `test_model_hub_hosted_tools_resolution.py`, `test_model_hub_hosted_executor.py`, `test_model_hub_hosted_pinning.py` and `test_model_hub_evidence_rules.py`; the native image cases in the Responses adapter tests; UI `ToolsTab.test` MH-HOSTED-UI-013 and 019, the executor cases of 001 and the probe parts of 003 and 018, `useHostedTools.test` 013 and parts of 007, `hostedTools.test` 013, `SourceDetailPanel.test` 013 and 017, `SettingsModelsPage.render` 016; catalog rows MH-HOSTED-007 (probe), UI-004 (the engine-upgraded and aged header), UI-013, UI-016 and UI-019 retire; UI-002 shrinks to the passive states; UI-018's "locked until verified" becomes "on by default, nothing locks"; UI-001's write contract becomes the per-field PUT |
| Contracts | as §10 |

## 12. Implementation split

Each phase is one or more PRs under the normal gates. The contract version event (§10) rides the first PR that
edits a frozen file, and later phases join it if it has not shipped. The C-1 additions the display reads (`execution.mode: "direct"`
and the codes `image_param_unsupported` and `image_model_unavailable`) land in the producer PR (phase C), before
phase D's display work; that PR adds the codes' en and zh copy in `vibe/i18n` and `ui/src/i18n`, because
`test_every_known_hosted_error_code_has_copy_on_every_surface` requires copy for every known code.

| PR | Lane | Work | Depends on |
| --- | --- | --- | --- |
| **A1** Evidence, priors, settings | foundation | the evidence model: status-keyed records with `mode`, negative-only routing, no time limit (efforts included), the in-memory `wire` stamp computed at `sync_sources`, memory updated when a refusal's write fails, no `probe` basis or `shape`, the `hosted_wire` branch gone; the positive rule's entry point, which B1's observer calls; the clear endpoints (hosted and effort) with their edge and persistence rules; overlay `executor_models` and `image_models` (schema and loader; image entries land with B2); the `hosted_tools` config v2 with the generic loader rule; the hop-fact functions (§3.4) and the image enum function; the `hosted-tools-status` v2 read and per-field PUT with the `latest_ok` projection; removal of the probe stack (§11 Model Hub); the Model Hub contracts of §10.3 and the coordinated version event | — |
| **B1** Signal, refusal, executor chain | gateway | the observer's projection paths and the positive write at settlement (§3.2); `pin_unavailable.reason` and admission on negative evidence only; `HopResolution.hosted_tools` v2 from A1's facts, frozen in the turn trace with the executor order and the image enum; the executor chain (§6) with `{arguments}` bodies, `turn_not_live` / `tool_not_enabled`, failover, one deadline, `failed_attempts`, and the executor-model refusal; the C-6 text (§10.2); gateway removals (§11) | A1 |
| **C1** Declarations and re-render | Vibey | declarations from facts and the turn freeze; the 409 matched on `type` and `code`; `pin_unavailable` surfaced to the loop and re-resolve with its bounds; the decrypt refusal; the in-flight rule; the C-1 changes of §10.1 (`execution.mode`, `failed_attempts`, `generate.model`, the two image error codes with en and zh copy); the localized context-load error for a row that no longer validates; Vibey removals (§11) | B1's HopResolution and executor body: developed on B1's branch and merged together with it |
| **B2** Image endpoints | gateway | the engine images invoke path, `stream_wire`'s images taxonomy, the per-model `image` flag, the image narrow rule (upstream refusals only), request and response bounds, enum authorization; one hermetic engine E2E per image path; the overlay `image_models` entries for the paths that pass | A1, B1 |
| **C2** `image_generate` | Vibey | the tool with the enum and `params` / `size_forms` validation before the call, reference images read as `read` reads them, the path record from record, action and execution | C1, B2 |
| **D1** Tools tab and chips | UI | the grouped Tools tab, status lines per aspect with their clears, Advanced (executor order, clears), the per-field PUT, Source chips with clears and precedence, the picker's refused-effort note; removals (§11 UI, i18n); the Tools-tab and chip frames in `avibe-docs/design.pen` through Pencil (the Pencil MCP must be connected on that host) | A1's read and endpoints; B1 for live positive records |
| **D2** Image card and direct calls | UI | the image card (models, prices, default, Advanced clears), `core/hosted_activity`'s cost total and the `direct` mode on the card and the IM line | C2, B2 |
| **E** Public docs | foundation | what each tool does, default-on, where content goes, cost (§9) | D1, D2 |

## 13. Acceptance properties

1. Nothing calls a provider to learn about a tool. Every provider call comes from a model's tool call or a model
   request in a turn the user started.
2. Every tool is on unless the user turned it off; a released config without the section has every tool on.
3. A hop declares a search tool natively exactly when it is enabled, `runs_on` holds, and no current negative
   evidence exists; every other hop gets the same-named function.
4. Positive evidence never changes routing. Negative evidence comes only from a narrow rule, names one (Source,
   model, feature), and stops applying only when the user clears it or the wire stamp it was learned under changes
   (the Source's config, the engine release or the Avibe release). It never expires by age.
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
| Evidence | `evidence_rules` against recorded served streams and error bodies | a served native web search writes one positive record; an executor call writes one for the executor; `Unsupported tool type` and `Invalid encrypted_content` each write one negative record; overload, 5xx and other 400s write none; a negative record under another wire stamp reads as unknown; a negative record does not expire with age; the clear endpoint removes one (Source, tool)'s records, or every Source's for the tool, and nothing else; an effort refusal does not expire, and clearing it lets the next request at that effort reach the Source |
| Hop facts | `hosted_tools` resolution | `assumed` from priors with no evidence; `unsupported` with a current negative; a Claude subscription never `assumed`; a translated path never `assumed`; the image enum and default, deterministic |
| Config | `ModelHubConfig` loader | a 3.2.0 config without the section has every tool on; an rc-era section keeps each tool's `enabled` and ignores the unknown key and fields with a warning; `executor_order` and `default_model` round-trip |
| Gateway refusal | real gateway + loopback fake engine | a pinned native request refused with `Unsupported tool type` answers 409 with `reason`, and the evidence is written first; admission refuses a declared tool with current negative evidence; a refusal whose evidence write fails still refuses the next native declaration; an Anthropic `web_search_tool_result_error` writes no positive record |
| Re-render | Vibey loop with a stub gateway | re-resolve after `hosted_unsupported` resends with the function, other native content still pinned; the per-key and per-turn bounds; same facts go portable; an in-flight continuation whose tool turned proxy uses the in-place form; a decrypt 400 on an unpinned request re-renders once without that identity's opaque payloads; a `pause_turn` continuation's `pin_unavailable` still ends `interrupted` |
| Executor chain | gateway endpoint, stub providers | order from `executor_order` then Source order; failover on 5xx and on a narrow refusal; a mid-stream failure moves on and is metered; a response that never ran the tool ends the chain; a candidate removed during the chain is skipped; `model_not_found` moves on; no candidate gives a `failed` record naming the fix; metering on the executor Source; a Claude subscription is never a candidate |
| Images | gateway endpoint + Vibey tool runtime, stub endpoints | a model outside the turn's enum is refused; generations and edits per kind; a buffered call over 120 s is not cut; reference images within the per-tool request bound; JPEG requested where allowed; a failure names the alternatives and no other model is called; bytes saved once through the media sink; the path record; no base64 in history |
| Parsing and replay | adapters and transforms | v1's mapping and replay cases minus native images; an injected `image_generation_call` is ignored like any undeclared item, without decoding |
| UI | Tools tab, Source chips, tool card | default on; status lines appear only after use; "清除" and "全部清除" on refusals call the clear endpoint; executor order up/down and reset; image models and default; chips without actions; no probe UI |
| E2E, once per release | real engine + stub upstream | a native web search, a proxied X search, and one `image_generate` per shipped image kind through the shipped engine |

## 15. Known by design

1. **Silent drops are invisible.** A Source that accepts a native declaration but never runs the tool looks like a
   model that chose not to search, and one that accepts a replayed payload but drops it loses the results' page
   content. Passive learning cannot see either. The status line simply never shows that Source executing.
2. **The first refusal costs one request and one cold prefix** per (Source, model, tool), and again after the record is
   cleared (§3.5).
3. **A refusal stays until cleared.** A relay that later starts supporting a tool or an effort keeps getting the
   proxy function, or keeps being skipped at that effort, until the user clears the refusal, or its config or the
   engine or Avibe release changes (owner decision: no time limit).
4. **The executor's lightweight model is a catalog choice.** A Source that does not serve it moves the chain on; the
   user cannot pick a different executor model in v2.
5. **A decrypt refusal drops a whole identity's opaque payloads for the turn**, reasoning included, because xAI's
   refusal does not say which ciphertext failed (§3.5). The turn continues with portable history for that Source.
6. **An rc-era image opt-out is not carried over.** v1's tool was `image_generation`; v2's is `image_generate`, and the
   loader treats the old key as unknown rather than mapping it (owner directive: no compatibility code for
   pre-release state). Only rc testers who turned image generation on and then off are affected; rc4 kept it off
   unless enabled.
7. **rc-era state is not migrated** (owner directive): probe evidence carries `basis: probe`, which v2's loader does not
   accept, so those records are pruned; an rc-era `hosted_tools` section keeps each tool's `enabled`, and its unknown
   keys and fields are ignored (§3.3). An rc session holding a removed native `image_generation` block is not restored
   as a native image tool: the current reader degrades any unrecognized native hosted block to portable text, as it does
   for a future hosted tool this build does not know.
8. **History is readable across hosted-tool registry changes.** A stored hosted result for a tool added by a newer build
   loads as an ordinary tool result, and a native call loads as portable text with its query or URL and sources when
   available, otherwise a one-line unavailable placeholder. A degraded native block also clears unpartitionable
   response-wide facts (§4.2). The row is not rewritten, new writes stay strict, and known tools retain strict
   validation—including a result-only fragment for a known deferred completion; only an unrecognized tool name
   degrades portably. This is the intentional downgrade path before a new hosted tool ships.
9. **A deferred call without a completion stays unresolved in model history.** Stop, an error before its result,
   `pin_unavailable`, or re-resolution to a different Source do not synthesize a success or retry the search elsewhere.
   The existing §4.4 fallback remains unchanged. Already dropped result blocks in old transcripts cannot be recovered.
10. **Deferred calls have no card on any surface.** Response 1's use-only pending block with `stop_reason: tool_use`
    is a protocol fact, not a search that ran. Web and IM omit it, including the old interrupted cards emitted for this
    exact shape by earlier builds. A later completion uses the ordinary hosted-card path at response 2's position;
    a call that never completes has no card. Other pending blocks retain interrupted rendering. No Web merge,
    provisional searching state, held-card state, restart flush, audit receipt or special delivery guarantee exists;
    the ordinary commit-to-trace/send crash gap remains unchanged.
11. **Unreported hosted usage is unknown.** Counters/cost are read from wire usage only, never inferred from the
    number of parsed calls or completion fragments. The 2026-10-22 live probe's installed parser dropped the result
    block and its hosted summary, so that transcript cannot establish which response reported the search counter.
    Neither the mixed-group contract nor [web search usage and pricing](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool.md)
    (both fetched 2026-10-22) specifies attribution across this split; the latter defines the
    `usage.server_tool_use.web_search_requests` counter. Both present and absent counters are covered by
    raw-stream-shaped fixtures; the fix never adds a second charge.
12. **An existing authorized retry can repeat upstream work.** A failed partial is not a valid native response
    prefix, so a retry resends the last valid continuation rather than inventing a new provider resume protocol.
    Each attempt's reported usage stays on its own response: a partial reporting one search followed by a retry
    reporting two records all three, without synthesizing or deduplicating real charges. No new retry layer is added.
