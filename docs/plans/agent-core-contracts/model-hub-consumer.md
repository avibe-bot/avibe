# C-6 Model Hub consumer extension

What Vibey needs from Model Hub. Owned by Model Hub (`../model-hub-contracts/`); this file states the
consumer's requirements, and the Model Hub lane lands the change in both places.

## 1. Today

- Source `protocol` and backend `native_protocol` know `anthropic`, `openai_responses`, and `openai_chat`.
- `ModelHubLaunch` (`modules/agents/model_hub.py`) gives a consumer `gateway_base_url`, `gateway_token`,
  `gateway_request_metadata`, `runtime_model`, `context_window`, `max_output_tokens`, `supports_tools`,
  `supports_reasoning`, and `reasoning_efforts`, but not the protocol to speak.
- The gateway exposes `/<backend>/v1/{messages | chat/completions | responses}` and converts when the frontend protocol
  differs from the hop's. It fails over only before the first user-visible model output byte; after that a failure is
  terminal.

## 2. Required

1. **`google` protocol.** Gemini `generateContent` / `streamGenerateContent` joins the protocol vocabulary and gets a
   gateway frontend under the backend prefix.
2. **Backend id `vibey`** in every Model Hub enum that lists backends (C-8).
3. **Hop resolution** for the `vibey` backend returns `HopResolution` (`hop-resolution.schema.json`) through the Model
   Hub runtime router: the primary hop's protocol, the gateway URL for that protocol, the token, the runtime model,
   and capabilities. A capability Model Hub does not know is `null`, never guessed; `input_limit` is `null` until Model
   Hub stores one. The agent treats unknown values conservatively (C-2). It resolves once per run and again after a
   retryable error.
   - `capabilities.context_price_steps` lists the input sizes past which the target model's list price rises,
     ascending, read from the price table Model Hub's usage writer holds (the user's override file, then models.dev's
     first-party `cost.tiers`); it is empty when the price is flat or unknown. C-9 keeps the compaction threshold
     below the smallest one it can (`context.md` §1).
   - `request_headers` carries `x-avibe-turn-id`, the Turn the resolution was made for, and the agent sends it on
     every gateway request: model calls and proxied hosted calls.
   - `source_identity` is the primary hop Source's credential-free identity (`identity_digest`: kind, vendor,
     protocol, supply channel, base URL, credential reference, credential generation), taken from the snapshot its origin
     came from. The agent records it on each response its own Source served (C-1 `AssistantMessage.source_identity`)
     and replays opaque payloads only to a hop of the same identity (`cross-provider.md`). Its inputs are therefore
     a persisted comparison key: changing them later sends every stored message once in the different-origin form,
     which is safe degradation and needs no migration, but should be deliberate.
   - The agent infers the serving identity from the launch's: the served-hop report names the Source, never its
     identity. A key changed between resolution and admission therefore labels the response with the launch
     identity, although the new account served it. That label errs only toward dropping: an identity never recurs
     after a credential change, so it matches no later hop, and the response's payloads go once in the
     different-origin form. They never go to another account.
   - The token answers for the route, which later Turns of the Session reuse. The gateway credits a request only
     to the Turn it names, and only while that Turn holds the token's route and still admits work.
   - A model request from a finished Turn still routes but is credited to no Turn. A hosted call from one is
     `executor_not_bound`.
4. **Served-hop report.** Every gateway response for `vibey` carries `x-avibe-served-hop`: compact, ASCII-escaped JSON
   with exactly `provider`, `api`, `model`, `source` (since Model Hub contract v12), taken from the winning attempt
   (Source vendor, Source protocol, upstream target, Source id) and sent before the first model byte. The gateway does
   not commit early keepalive headers for `vibey`; other backends are unchanged. The same origin is recorded on
   `TurnProvenance.served`, beside its `source_id`. An `api` different from the frontend protocol means the gateway
   converted. The agent records `provider`, `api`, `model` as the message `origin` (C-1); `source` is not part of it. The header value is at most 4096 bytes; a hop whose origin cannot be represented within that limit is
   refused before invocation with a controlled local error (no model call, no header). Identifiers are never truncated.
5. **Conversion stays a fallback.** The agent speaks the primary hop's protocol. When failover reaches a hop with
   another protocol, the gateway converts (degraded, not broken) and the served-hop report says so.
6. **No opaque payload crosses origins at failover.** The agent builds the request for the primary hop's origin, so it
   may carry that origin's opaque payloads (C-1: any block's signature, redacted thinking). When the gateway fails
   over to a hop whose origin `(provider, api, model)`, Source, or Source identity differs from the primary's, it
   removes every such payload before sending, applying the same rule as `cross-provider.md`, whether or not it
   converts protocols. Opaque payloads are bound to the account that issued them, the Source identity, as well as
   the origin: a provider refuses another account's ciphertext. The serving identity is read on the final admission
   snapshot, so a key rotated mid-turn counts. A pinned request is never rewritten: one pinned to the primary hop
   whose identity has changed since the launch answers `pin_unavailable`, and the agent re-resolves and renders it
   for the fresh hop, whose identity drops the old payloads. A Gemini `functionCall` part whose signature is removed
   gets Google's documented stand-in `skip_thought_signature_validator` instead, since Gemini 3+ refuses a
   current-turn call without one (`../vibey-gemini-native.md`).
   The identity stays in memory: it is never in the served-hop report or persisted provenance. Known limit:
   the next request targets the primary origin again, so payloads returned by a fallback origin are not replayed if a
   later request fails over to that same fallback; a provider that then rejects the call surfaces as a provider error.

7. **Hosted tools** (v2, [`../vibey-hosted-tools.md`](../vibey-hosted-tools.md) §3.4, §3.5, §6, §7).
   - `HopResolution.hosted_tools` has one entry per hosted tool the turn declares:
     - **`web_fetch`** (WF-1/2/3, v21) has exactly
       `{native: "unsupported", replay: "unsupported"}`: always function-only,
       never Claude-native. WF-3 enables its server entry, Vibey registration and
       producer with `WEB_FETCH_ACTIVE = True`; an explicit false hides them and
       rejects execution. The first resolution
       freezes its canonical `{url}`-only function beside the other turn tools.
       Vibey posts `{"arguments": {"url": ...}}` under the live turn to
       `/hosted-tools/web_fetch`; the gateway owns normalization and the budget.
       The validated record produces C-1's complete capped ordinary quotation
       through the same deterministic frame used by readers. Replay preserves
       the stored call/result without a fetch or settlement. The owner chose to
       ship readers and activation together (`../web-fetch.md` §8).
     - **a search tool** (`web_search`, `x_search`) gives the primary hop's facts:
       - `native` is `assumed` when the tool runs on the hop (`hosted_tools.runs_on`: a hub Source with an eligible
         credential, never a Claude subscription, and an untranslated path, that serves the model, for which the
         overlay declares the tool) and no current negative `call` evidence exists for it; otherwise `unsupported`.
         Positive evidence never decides it;
       - `declaration` and `include` (the overlay's wire declaration and Responses `include` values), present
         exactly when `native` is `assumed`;
       - `replay` is `assumed` when `native` is, the protocol replays natively (`anthropic`, `openai_responses`),
         and no current negative `replay` evidence exists; otherwise `unsupported`;
       - `refused: true`, present exactly when the tool runs on the hop but current negative `call` evidence for the
         hop's (Source, model) makes `native` `unsupported`: that Source refused the tool, so a proxied call is a
         fallback from it. It is absent when the tool does not run on the hop;
     - **`image_generate`** gives `{models: [{source_id, model_id, description, params, size_forms}], default:
       {source_id, model_id}}`: the image models the user's Sources make available, default first. It is absent when
       the tool is off or no image model is available, so `models` is never empty.
     - The turn's first resolution freezes the tool set, each search tool's executor order and the image models on
       the turn trace; later hops of the turn change only `native`, `replay` and `refused`. A settings change applies from the
       next turn.
   - **The gateway never rewrites hosted content.** Vibey pins every request that carries a native declaration, a
     native hosted payload or an in-flight continuation, with `x-avibe-pin-source`.
   - A pinned request is served only by that Source, with no failover and no recovery wait. The gateway answers HTTP
     409 `{"type": "error", "error": {"type": "model_hub_pin_unavailable", "code": "pin_unavailable"}}` in three
     cases:
     - the Source cannot be admitted, checked before invoking anything;
     - its attempt ends before model output with a failover-class failure;
     - its attempt ends before model output with an error that a narrow hosted-content evidence rule names. Only
       then does the body carry `error.reason: {kind: "hosted_unsupported", tool, aspect, source_id}`, and the rule
       has already written the negative evidence (memory first) when the answer leaves the gateway.
   - Vibey matches the 409 on `type` and `code` and tolerates extra fields. It then resolves a fresh hop and renders
     the request for it: the fresh facts turn exactly the refused tool into the proxy function or synthetic replay,
     and other native content stays native and pinned (bounds in `vibey-hosted-tools.md` §3.5).
   - **The pin names an exact hop.** It is the pinned Source with the model the content was rendered for:
     - the model in `x-avibe-pin-model`, which a continuation sets to the served-hop `model` of the response it
       continues;
     - otherwise the launch's primary hop model, when the pin is the primary Source. The route credential fixes
       that model.
     - A pin that is neither matches no hop.
   - **Admission** reads the overlay and the negative evidence on the same snapshot the call is then made on (after
     the engine is prepared, for every attempt):
     - the pinned hop must be a runnable hop of the route;
     - every hosted declaration in `tools` needs the tool to run on the hop with that declaration `type`, and no
       current negative `call` evidence. A declaration is a tool whose `type` the overlay declares for a catalog tool
       on this protocol, for any vendor; a function never is one;
     - every replayed hosted item outside the in-flight response needs its tool to run on the hop, and no current
       negative `replay` evidence. A generated image call is never replayed.
     - **One identity per turn and Source.** Native content goes to a Source only under one identity (credential,
       base URL, protocol, vendor, kind, credential generation) per turn. The turn's first pinned request admitted to
       the Source binds it, atomically with that request's final admission, and every later one must match it. After
       a mid-turn change, such as a rotated key or an edited relay URL, every pinned request to that Source is
       `pin_unavailable` for the rest of the turn, including one overlapping the binding request. An in-flight
       response carrying hosted items needs an identity already bound, so it is `pin_unavailable` when none is. A
       request without a live turn binds nothing and is `pin_unavailable`.
     - A `type` or `name` that is not non-blank text is absent, so its item is no hosted item and ends the
       in-flight run. A named call (`function_call`, `custom_tool_call`, `mcp_call`) without one cannot be
       classified and ends it too.
     - The hosted items are Responses `web_search_call`, `image_generation_call`, and `custom_tool_call` named
       `x_keyword_search`, `x_semantic_search`, `x_user_search` or `x_thread_fetch`; and Anthropic `server_tool_use`
       named after a catalog tool, and `web_search_tool_result`.
     - The **in-flight response** is structural. On Anthropic it is the last assistant message, when the request ends
       with it or with user messages made only of `tool_result` blocks. On Responses it is the trailing run of model
       output items (an assistant `message`, `reasoning`, and the known call item types), followed only by
       `function_call_output` or `custom_tool_call_output` items. Any other item ends the run, so an unknown item
       never exempts a hosted call.
   - A pinned request is forwarded with nothing changed but `model` and `stream`, so Vibey renders all of it, earlier
     history included, for the pinned Source's origin.
   - **Unpinned hosted content is refused.** An unpinned Vibey request carrying native declarations, replayed native
     items or an in-flight continuation has no correct hop, and the gateway answers 422 `hosted_content_unpinned`
     before anything runs.
   - **Native content is never translated.** The pinned hop must speak the request's protocol; otherwise the
     answer is `pin_unavailable`.
   - A `pin_unavailable` carries no routing state into the resend beyond the evidence a narrow rule wrote. Every
     failover-class failure that is health evidence settles the Source as normal routing does.
   - Item 6 (the opaque thinking strip) is unchanged and applies only to unpinned requests.
   - **Passive positive evidence.** A served response that carries the provider's own completed item for a tool the
     request declared natively (Responses `web_search_call` with `status: completed`, or a `custom_tool_call` named
     after an X search; Anthropic `server_tool_use` named `web_search` with a `web_search_tool_result` whose
     `content` is a result list) records a success for (serving Source, model, tool) at settlement, off the loop.
     It is display only and never changes routing.
   - **The executor endpoint.** `POST {base_url}/hosted-tools/{tool}` with the turn's Bearer token, its
     `request_headers` (item 3) and the body `{"arguments": <canonical arguments>}`.
     - The call belongs to the live turn the request names (`x-avibe-turn-id`). Without one, or from a turn that no
       longer admits work, it is 409 `turn_not_live`; a tool outside the turn's frozen tool set, or for
       `image_generate` a model outside the turn's frozen models, is 409 `tool_not_enabled`. Nothing runs. Stopping
       the turn cancels an admitted call, which is still metered.
     - **A search call runs on a chain** built from the turn's frozen candidate list. For `web_search`, a candidate
       is a search-service Source (including a qualifying official DeepSeek API-key Source) or a model executor;
       one Source holds one role. Automatic order puts services before model executors. A saved order preserves
       deliberate placements; unnamed or role-changed services, identified by `executor_order_kinds`, are placed
       before its first model executor without a config write, unnamed models follow, and disabled entries go last
       (`web-search-services.md` §4.1). The turn freezes `prefer_native`, candidates and their result counts.
       `prefer_native: false` states native/replay unsupported on every hop, so native history replays synthetically.
       A candidate is skipped by its first applicable reason: unavailable, disabled, blocked, cooling, query too long.
       Skips write no state and cost nothing. A service sends its catalog request through the adapter custody
       boundary, never the model engine. A model executor is a Source whose vendor offers the tool natively,
       called with the first of its vendor's `executor_models` the Source
       lists (a Source listing none is no candidate), and admitted on the final snapshot when the gateway reaches it
       (`runs_on`, its identity, no current negative `call` evidence). The gateway sends one short native request
       (the overlay's declaration; on Responses `tool_choice: "required"`; Anthropic is asked in the message alone;
       no reasoning effort) and moves to the next candidate on a failure normal routing fails over from (an overload
       or server error included), a narrow-rule refusal, a refusal of the executor model (`model_not_found`), or a
       failure mid-stream. Only the narrow rule writes negative `call` evidence; a model refusal is not a refusal of
       the tool. A completed response that never ran the tool ends the chain. One deadline covers the whole chain
       (180 s), with at most 30 s per service attempt; model executors retain the remaining chain budget.
     - **An `image_generate` call** runs directly on the image endpoint of the (Source, model) the turn's frozen
       models name (`vibey-hosted-tools.md` §7.4), admitted on the final snapshot; there is no chain and no
       fallback. Its arguments are `{model, prompt, size?, images?, n?}`, `images` being at most 4 base64 strings,
       each a supported image within `read`'s inline limit. An argument the model does not take, or a `size` in a
       form it does not accept, is a `failed` record with `error_code: "image_param_unsupported"` naming the models
       that take it; a model the snapshot no longer admits is one with `"image_model_unavailable"`. Nothing runs in
       either case. The answer is `{"record", "execution"?, "images"?, "generation"?}`: `execution.mode` is
       `direct`, with no `failed_attempts`; `images` is `[{"data": <base64>}]`, in order, only on a completed call,
       and Vibey saves each one through its media sink and fills `record.images`; `generation` holds the
       endpoint's own `size`, `quality` and `revised_prompt` when it reported them. A failed record names the
       turn's other models. An endpoint that answers with URLs only is `failed`: the gateway never fetches one.
     - Every outcome of a well-formed call is a 200 with `{"record", "execution"?}` in the
       `ToolResultMessage.hosted` encoding. `execution` names the attempt whose response the record came from (the
       last one tried when all failed), with its billing and cost; earlier billable attempts are listed in
       `execution.failed_attempts: [{source_id, billing, cost?}]`, including an earlier possibly billed service
       attempt whose charge is unknown (no cost). A service chain's `record.attempts[]` carries every reached
       candidate, tried or skipped, even with no execution. Model-only chains retain the released shape.
       With no candidate, the failed record names both remedies: add a search service or a search-capable API key.
       Otherwise its terminal reason prioritizes query-too-long (`search_query_too_long`, largest reachable limit
       in `error`), query-specific refusal, blocked Sources (`search_sources_blocked` with per-Source remedies;
       disabled decides only if every candidate is disabled), last tried failure, then cooling/unavailable.
     - A service response is `completed` exactly for HTTP 200 with a results array that is empty or retains at
       least one row after mapping bounds: HTTP(S) URLs at most 2,048 characters, title 300, excerpt 1,000,
       page age 64, at most the frozen result count. Missing/unreadable arrays or non-empty arrays with no usable
       rows are `service_unreadable` and fail over. JSON decoding and mapping run off the controller loop.
     - Model success is an allow-list: a call is `completed` only when the response says it completed (Responses
       `status: completed`; Anthropic `end_turn` or `stop_sequence`) and nothing failed; a response that stopped
       early is `incomplete`; any reported failure, or any other or missing ending, is `failed`. An unreadable
       response is `failed`. The record's `error` is in the user's language, and every reason the gateway words
       carries its `error_code` (`HOSTED_ERROR_CODES`), the key the surfaces show it by in their reader's language;
       the provider's own detail, the Sources tried and an image call's other models stay in `error`. One reason has
       no code: with no candidate, the record names the providers to add, which only that call knows, so it is shown
       as written.
     - The record's search `sources` keep the executor's order. A snippet is the answer text its citation belongs
       to: the list item when that item cites only that source, else its sentence, with the citation markers
       removed. An Anthropic result keeps its `cited_text`. X search lists the cited posts. A response's first 256
       citations are read.
     - A malformed call is 400 `invalid_request_error` (including a body over the tool's request bound), an
       unauthenticated one 401, an unknown tool 404, as `{"type": "error", "error": {"type", "code"}}`.
     - Each sent attempt is metered on its own Source; skips and pre-send failures are not. Model executor
       attempts never change Source health. Service credential, balance and catalog-named limit failures do,
       fenced by the Source attempt-start settlement generation. Transient, query-specific, redirect and unreadable
       service failures never write health; transient/unavailable/unreadable failures use a separately fenced
       (Source, service) cooldown. A service writes no negative hosted-tool evidence. Completed service requests
       write positive `mode: service` evidence. A re-enable trial admits one request immediately before sending.
     - A model attempt's cost uses the pricing module as before. A service attempt uses its catalog list price or
       a finite non-negative reported price no larger than 100 times that price, with the catalog's estimate flag.
       Known-unbilled outcomes carry explicit list $0: pre-send failure, non-200, empty DeepSeek results, or a
       catalog-declared unbilled outcome. Received status survives body failure or stop: a non-200 is $0, an Exa/
       Tavily 200 is billed, and a DeepSeek 200 with an unreadable result count is uncertain. Otherwise sent/no-status
       failures are uncertain unless the catalog declares them unbilled. Unknown costs remain absent and make
       known chain totals lower bounds. Stopped calls write no record but meter their observed billing facts.
       Stop itself creates no cooldown or failure: no-status and 200 stops settle only billing/authentication/trial
       facts, while an observed failure status still settles its health/cooldown row.

## 3. Not required

Failover, credentials, quota, and Source health stay in Model Hub. The agent never contacts a vendor directly.
