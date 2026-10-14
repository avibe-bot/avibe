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
   - `request_headers` carries `x-avibe-turn-id`, the Turn the resolution was made for, and the agent sends it on
     every gateway request: model calls and proxied hosted calls.
   - `source_identity` is the primary hop Source's credential-free identity (`identity_digest`: kind, vendor,
     protocol, supply channel, base URL, credential reference, verification), taken from the snapshot its origin
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
   whose identity has changed since the launch answers `pin_unavailable`, and the agent renders it again portably.
   The identity stays in memory: it is never in the served-hop report or persisted provenance. Known limit:
   the next request targets the primary origin again, so payloads returned by a fallback origin are not replayed if a
   later request fails over to that same fallback; a provider that then rejects the call surfaces as a provider error.

7. **Hosted tools** (planned, [`../vibey-hosted-tools.md`](../vibey-hosted-tools.md) §3.5).
   - `HopResolution.hosted_tools` names, for each tool the user enabled:
     - the primary hop's `mode` (`native` or `proxy`);
     - for `native`, the wire declaration, which is required exactly then;
     - the executor bound for this turn (`{source_id, model_id}` or `null`), present for every entry whatever the
       mode, because a native request that comes back `pin_unavailable` is re-rendered with the proxy function in the
       same turn. A config change applies from the next turn;
     - `replay`, true only for the primary hop Source's positive replay evidence taken on the current engine and
       probe shape.
   - `native` requires all of these ([`../model-hub-capability-model.md`](../model-hub-capability-model.md) §3):
     - an eligible credential (never a Claude subscription);
     - positive `call` evidence for that (Source, model), taken on the current engine and probe shape;
     - an untranslated path.
   - **The gateway never rewrites hosted content.** Vibey pins every request that carries a native declaration, a
     native hosted payload or an in-flight continuation, with `x-avibe-pin-source`.
   - A pinned request is served only by that Source, with no failover and no recovery wait. The gateway answers HTTP
     409 `{"type": "error", "error": {"type": "model_hub_pin_unavailable", "code": "pin_unavailable"}}` in three
     cases:
     - the Source cannot be admitted, checked before invoking anything;
     - its attempt ends before model output with a failover-class failure;
     - its attempt ends before model output with an error that a narrow hosted-content evidence rule names.
   - Vibey then renders the same request in portable form and sends it unpinned; it fails over as today.
   - **The pin names an exact hop.** It is the pinned Source with the model the content was rendered for:
     - the model in `x-avibe-pin-model`, which a continuation sets to the served-hop `model` of the response it
       continues;
     - otherwise the launch's primary hop model, when the pin is the primary Source. The route credential fixes
       that model.
     - A pin that is neither matches no hop.
     - Nothing turn-wide is read, because a turn may have concurrent requests.
   - **Admission** reads the overlay and the evidence at the current engine and probe shape, on the same snapshot the
     call is then made on (after the engine is prepared, for every attempt):
     - the pinned hop must be a runnable hop of the route;
     - "the tool runs on the hop" is the one rule hop resolution uses for native mode (`hosted_tools.runs_on`): a hub
       Source with an eligible credential and an untranslated path that serves the model (an API-key Source any
       model it does not list as retired; a subscription only its listed models), for which the overlay declares the
       tool;
     - every hosted declaration in `tools` needs the tool to run on the hop with that declaration `type`, and
       `call` evidence. A declaration is a tool whose `type` the overlay declares for a catalog tool on this
       protocol, for any vendor; a function never is one;
     - every replayed hosted item outside the in-flight response needs its tool to run on the hop, and `replay`
       evidence. A generated image never has it.
     - **One identity per turn and Source.** Native content goes to a Source only under one identity (credential,
       base URL, protocol, vendor, kind, credential generation) per turn. The turn's first pinned request admitted to the
       Source binds it, atomically with that request's final admission, and every later one must match it. After a
       mid-turn change, such as a rotated key or an edited relay URL, every pinned request to that Source is
       `pin_unavailable` for the rest of the turn, even with evidence taken anew, including one overlapping the
       binding request. An in-flight response carrying hosted items needs an identity already bound, so it is
       `pin_unavailable` when none is. A request without a live turn binds nothing and is `pin_unavailable`.
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
       never exempts a hosted call from evidence.
   - A pinned request is forwarded with nothing changed but `model` and `stream`, so Vibey renders all of it, earlier
     history included, for the pinned Source's origin.
   - **Unpinned hosted content is refused.** Vibey pins every request carrying Source-specific hosted content:
     native declarations, replayed native items, and in-flight continuations. An unpinned Vibey request carrying
     any of it therefore has no correct hop, and the gateway answers 422 `hosted_content_unpinned` before anything
     runs.
   - **Native content is never translated.** The pinned hop must speak the request's protocol; otherwise the
     answer is `pin_unavailable`.
   - A `pin_unavailable` carries no routing state into the resend. Every failover-class failure that is health
     evidence settles the Source as normal routing does, so the resend skips it. An unrecognized error frame inside
     a 2xx is not health evidence, so the resend may try that Source once more.
   - Item 6 (the opaque thinking strip) is unchanged and applies only to unpinned requests.
   - The served-hop report carries `source`, the serving Source id (item 4, Model Hub contract v12), so that a
     hosted block's payload is tagged with the exact Source; two Sources can share an origin. The gateway and the
     parser in `conversion-tables.md` changed together in that bump. Native hosted mode lands with or after v12.
   - **The proxy executor.** `POST {base_url}/hosted-tools/{tool}` with the turn's Bearer token, its
     `request_headers` (item 3), and the body
     `{"arguments": <canonical arguments>, "executor": HopResolution.hosted_tools[tool].executor}`. The executor
     may be `null`.
     - The executor must be the one the caller's live turn bound for that tool. The turn's first resolution
       binds the enabled tools and their executors, with each executor Source's identity. Later resolutions in the
       turn keep that binding and recompute only `mode`, `declaration`, `include` and `replay` for their hop, so a
       settings change applies from the next turn.
       - Any other executor, a tool not enabled this turn, a credential without a live turn, or a turn that no
         longer admits work (stopping or finalizing) gets 409 `executor_not_bound`, and nothing runs.
       - The turn owns an admitted call: stopping the turn cancels it, and the call is still metered.
       - An executor Source whose identity changed since launch (base URL, credential) is "no longer available".
     - The gateway runs that executor and no other: one native request in the executor Source's own protocol, with
       the overlay's declaration and no reasoning effort. Search tools ask for a numbered list of results with one
       or two cited sentences each. On Responses the tool is forced (`tool_choice: "required"`), as the call probe
       that verified it forces it; Anthropic is asked in the message alone. `image_generation` renders the prompt at
       the given size and requests partial images.
     - Every outcome of a well-formed call is a 200 with `{"record", "execution"?}`, in the
       `ToolResultMessage.hosted` encoding. `execution` is present exactly when an executor ran. A call that
       reaches none (no verified executor, or the bound Source was removed or is ineligible) has a `failed` record.
       Success is an allow-list: a call is `completed` only when the response says it completed (Responses
       `status: completed`; Anthropic `end_turn` or `stop_sequence`) and nothing failed. A response that stopped
       early (Responses `incomplete`; Anthropic `max_tokens` or `pause_turn`) is `incomplete`. Any reported failure
       (a failed call or outcome, an Anthropic search error), or any other or missing ending, is `failed`. Both keep
       any partial sources. An unreadable response, including one nested past the decoder's limit or one whose
       fields are not of their kind, is `failed`. The record's `error` is in the user's language.
     - The record's search `sources` keep the executor's order. A snippet is the answer text its citation belongs
       to: the list item when that item cites only that source, else its sentence, with the citation markers
       removed. An Anthropic result keeps its `cited_text`. X search lists the cited posts. A response's first 256
       citations are read.
     - For `image_generation`, the record has no `images` key. `generation` carries the resolved `revised_prompt`,
       `size` and `quality`, and `images` carries `[{mime, data}]` in base64. Vibey saves the bytes through its
       session media sink and adds the path records itself. The image must decode, within 24 MiB. The stream may
       carry only the requested partials and the final image, plus two, within 64 MiB decoded across all of them.
       Anything else is a `failed` record.
     - A malformed or unbound call is the only error status: 400 `invalid_request_error` (including a body over
       1 MiB), 401, 404 for an unknown tool, or 409 `executor_not_bound`, as
       `{"type": "error", "error": {"type", "code"}}`.
     - A search call is bounded at 180 s and an image at 300 s. The call is metered on the executor Source and never
       changes Source health.
     - `execution.cost` is the pricing module's value of the executor's response (`hosted_call_cost`): `included`
       on a monthly Source, the provider's own figure as `reported` (xAI `cost_in_usd_ticks`), else `list` from the
       call's metered tokens and the provider's hosted counters (`usage.server_tool_use`,
       `usage.server_side_tool_usage_details`). It is absent when the call cannot be valued, never zero.

## 3. Not required

Failover, credentials, quota, and Source health stay in Model Hub. The agent never contacts a vendor directly.
