# Native protocol conversion tables

This file records the C-2 wire mappings implemented by the `ai` lane. The
canonical types are the C-1 message and C-2 provider-event dataclasses. The
adapters use `httpx.AsyncClient` directly and stream SSE frames incrementally.
The protocol mappings are ported from tau (`tau_ai`, MIT) and Pi (`pi-ai`, MIT,
revision `7fbbd5f`); the table records Avibe-specific normalization and origin
rules on top of those mappings.

## Shared rules

| Concern | Rule |
| --- | --- |
| Request model | `ModelRequest.endpoint.model_id` is sent as the runtime model. The endpoint token is used only for request authentication. |
| System prompt | Sent from `ModelRequest.system`; it is never added to the persisted message history. |
| Images | `ImageBlock.media_token` is resolved by `MediaLoader` immediately before the request. Unknown `supports_images` is false, so the cross-provider transform replaces each image with `[image: <name or mime>]`. |
| Tool ids | A safe stored id is preserved when accepted by the target. Otherwise the per-request map uses `call_` plus the first 24 hex characters of SHA-256. The same map rewrites tool results. |
| Missing tool results | The transform inserts `[tool call interrupted; no result recorded]` with `is_error=true`. |
| Origin | Direct requests use the endpoint origin. Gateway requests parse only `x-avibe-served-hop`, whose JSON object must contain exactly `provider`, `api`, and `model`. A missing or invalid report makes the response unverified: thinking and tool-call signatures are nulled, and redacted thinking is dropped. |
| Cancellation | `CancelToken` ends the HTTP stream and yields one non-retryable `ProviderError(kind="aborted")`. Opening, stream reads, error-body reads, media loads, and served-hop resolution all use the shared cancellation-aware await owner. |
| Retry boundary | `ProviderError.retryable` is false once any model content delta or tool-call start/delta was emitted. A Responses `ToolCallStart` therefore makes a later provider failure non-retryable. |
| Partial and abort | The shared `partial_message` policy is used by all four adapters. Usage captured before the first visible delta is retained, and cancellation returns the assembled `AssistantMessage` whenever content or usage exists, including an empty message with usage only. |

## Pi branch audit

The following rows are the branch-by-branch audit against Pi revision
`7fbbd5f`. “Same” means the Avibe handler preserves Pi's state-machine
behavior. A stated deviation is limited to the C-1/C-2 canonical event,
origin, retry, cancellation, media, or message-shape rules.

| Protocol | Pi branch | Avibe handler | Result |
| --- | --- | --- | --- |
| Responses | `openai-responses-shared.ts:599-604` response id and output-item creation | `openai_responses.py:175-283` | Same slot creation order; Avibe stores canonical blocks. |
| Responses | `:605-634` reasoning summary/text deltas | `openai_responses.py:209-220` | Same; unknown slots are ignored. |
| Responses | `:635-654` output/refusal deltas | `openai_responses.py:182-208` | Same slot lookup; refusal is preserved as a canonical refusal stop. |
| Responses | `:655-671` function-call argument delta/done | `openai_responses.py:290-319` | Same suffix and replacement rules; malformed metadata is canonical `invalid_request`. |
| Responses | `:683-742` output-item finalization | `openai_responses.py:320-362` | Same arrival-order slots and final argument/signature capture. |
| Responses | `:743-744` completed/incomplete | `openai_responses.py:363-412` | Same terminal response ownership; canonical stop reasons and usage. |
| Responses | `:745-757` provider error/failed | `openai_responses.py:413-459` | Same classification, with Avibe's streamed retry boundary and partial message. |
| Responses | `:759-777` missing terminal/unfinished call | `openai_responses.py:474-487` and `_common.py:298-323` | Same terminal requirement; Avibe emits canonical `ProviderError`. |
| Responses | unknown event fallthrough | `openai_responses.py:460-473` | Same: ignored for forward compatibility. |
| Chat | `openai-completions.ts:553-565` chunk metadata and usage | `openai_chat.py:154-199` | Same relevant metadata/usage behavior. |
| Chat | `:567-585` choice finish reason | `openai_chat.py:199-207` and `_normalize_stop` | Same known mapping; unknown values become canonical `error`. |
| Chat | `:586-633` text and legacy reasoning fields | `openai_chat.py:199-209` and `530-536` | Same first non-empty reasoning field and visible deltas. |
| Chat | `:635-661` tool-call state machine | `openai_chat.py:210-273` | Same index/id correlation; an id arriving before the name is retained and reused. |
| Chat | `:665-675` reasoning-details accumulation | `openai_chat.py:210-212` and `538-578` | Same consecutive text/summary merge and opaque-entry preservation. |
| Chat | `:680-701` finish blocks and terminal checks | `openai_chat.py:285-318` | Same final argument parsing and terminal requirement. |
| Chat | top-level provider error | `openai_chat.py:175-198` | Same classification, with canonical partial usage and streamed retry boundary. |
| Chat | aborted/error cleanup | `openai-completions.ts:702-724` | Same assembled partial policy through `_common.partial_message`. |
| Anthropic | `anthropic-messages.ts:665-690` message start | `anthropic.py:192-194` and `_usage` | Same early usage capture; cache-write total is retained. |
| Anthropic | `:691-738` block start | `anthropic.py:194-257` | Same supported text/thinking/redacted/tool blocks; opaque payloads follow C-1 origin rules. |
| Anthropic | `:739-784` content deltas | `anthropic.py:257-301` | Same supported delta accumulation and signature append. |
| Anthropic | `:785-816` block stop | `anthropic.py:302-305` | Same block finalization. |
| Anthropic | `:817-860` message delta and usage merge | `anthropic.py:306-320` and `_usage` | Same non-null usage merge and stop normalization. |
| Anthropic | `:863-899` abort/error terminal path | `anthropic.py:381-392` and `623-635` | Same assembled partial behavior; cancellation is the C-2 non-retryable `aborted` kind. |
| Anthropic | unknown event/content block | `anthropic.py:194-257` and `346-352` | Same Pi ignore policy; later deltas for an unknown block are ignored. |
| Google | `google-generative-ai.ts:130-173` text/thought parts | `google.py:201-220` | Same thought marker; signatures are retained on their originating canonical block. |
| Google | `:175-220` function-call parts | `google.py:221-281` | Same call emission; ID-less parallel parts remain distinct as required by C-2 canonical history. |
| Google | `:224-251` finish reason and usage | `google.py:282-315` | Same usage accounting and stop mapping; failure reasons never become `stop`. |
| Google | `:254-283` final block/abort checks | `google.py:285-315` and `642-655` | Same terminal requirement and assembled partial policy. |
| Google | top-level error/prompt feedback | `google.py:148-199` | Same provider error classification with retryable transient status codes. |
| Google | unknown fields/parts | `google.py:201-281` | Same permissive ignore behavior for fields Pi does not consume. |

## Anthropic Messages

| Canonical field | Request mapping |
| --- | --- |
| `system` | `system`, as a text string or one text block with `cache_control` when `cache="default"`. |
| User text/image | `messages[].role="user"`; text blocks become `text`; images become `image` with a base64 `source`. |
| Assistant text | `messages[].role="assistant"` with `text` blocks. |
| Thinking | Same-origin signed thinking becomes `thinking` with `thinking` and `signature`; same-origin redacted thinking becomes `redacted_thinking` with `data`; unsigned thinking becomes text. |
| Tool call | `tool_use` with `id`, `name`, and parsed `input`. |
| Tool result | A user message containing `tool_result` with `tool_use_id`, `content`, and `is_error`. |
| Tools | `tools[]` with `name`, `description`, and `input_schema`; the final tool receives a cache breakpoint for `cache="default"`. |
| Reasoning effort | `none/off/disabled` → `thinking.type="disabled"`; other values → adaptive thinking and `output_config.effort` (`minimal` maps to `low`, `xhigh` to `max`). |
| Cache | At most four breakpoints: system, final tool, and the two latest eligible user-message tails. |

Anthropic stream events:

| Wire event | Outcome | Provider event / state |
| --- | --- | --- |
| `message_start.message.usage` | delta/state | Initial `Usage`; `input_tokens` excludes cache reads and writes. |
| `content_block_start` (`text`, `thinking`, `redacted_thinking`, `tool_use`) | delta/state | Creates the canonical block; `tool_use` emits `ToolCallStart`. Empty tool names are terminal `invalid_request`. |
| `content_block_delta.text_delta` | delta | `TextDelta`. |
| `content_block_delta.thinking_delta` | delta | `ThinkingDelta`. |
| `content_block_delta.signature_delta` | state | Appends to the thinking signature. |
| `content_block_delta.input_json_delta` | delta | `ToolCallDelta`; the complete JSON is parsed at `Done`. |
| `content_block_stop` | delta | `BlockEnd`. |
| `message_delta.stop_reason` | delta/state | Normalized stop reason; usage fields are merged. |
| `message_stop` | Done | `Done(AssistantMessage)`. |
| `ping` | state | Ignored keepalive. |
| `error` before deltas | retryable or terminal `ProviderError` | Provider error type selects the kind; `message_start` usage is retained in `partial` when present. |
| `error` after deltas | terminal `ProviderError` | Partial content and usage are retained; `retryable=false`. |
| unknown `content_block` type | state | Ignored like Pi; later deltas for an unknown block index are ignored. |
| malformed JSON, block, delta, or redacted payload | terminal `ProviderError` | `invalid_request` for malformed provider metadata, otherwise `unknown`; never a successful `Done`. |
| EOF without `message_stop` or response close failure | terminal `ProviderError` | Incomplete/transport failure, preserving partial content and usage. |

Anthropic stop reasons:

| Wire value | Canonical |
| --- | --- |
| `end_turn`, `stop_sequence`, `pause_turn` | `stop` |
| `max_tokens` | `length` |
| `tool_use` | `tool_use` |
| `refusal` | `refusal` |
| `safety` | `safety` |

## OpenAI Chat Completions

| Canonical field | Request mapping |
| --- | --- |
| `system` | First `messages` item with `role="system"`. |
| User text/image | `role="user"`; text is a string when alone, otherwise content parts; images use `image_url` data URLs. |
| Assistant text | `role="assistant"`, string `content`. |
| Thinking | Same-origin signed details are replayed as `reasoning_details` when the signature is structured; provider-compatible legacy signatures use `reasoning_content`, `reasoning`, or `reasoning_text`; unsigned thinking is text. |
| Tool call | `tool_calls[].type="function"` with `id`, `function.name`, and JSON `function.arguments`. |
| Tool result | `role="tool"` with `tool_call_id` and text content. |
| Tools | `tools[].type="function"` with `function.name`, `description`, and `parameters`. |
| Reasoning effort | `reasoning_effort`, with `xhigh/max` mapped to `high`; disabled values are omitted. |
| Usage request | `stream_options.include_usage=true`. |

Chat stream events:

| Wire field | Outcome | Provider event / state |
| --- | --- | --- |
| `choices[0].delta.content` | delta | `TextDelta`. |
| `choices[0].delta.reasoning_content/reasoning/reasoning_text` | delta | `ThinkingDelta`. |
| `choices[0].delta.reasoning_details` | state | Stored as a structured thinking signature. |
| `choices[0].delta.tool_calls[].id/function.name` | delta | `ToolCallStart`; missing/empty names are terminal `invalid_request`. |
| `choices[0].delta.tool_calls[].function.arguments` | delta | `ToolCallDelta`; fragments are parsed at `Done`. |
| `choices[0].delta.function_call.name/arguments` | delta | Legacy single-call form; normalized into the same tool-call state machine. |
| `choices[0].finish_reason` | state | Stop reason. Unknown values become terminal `error`, not `stop`. |
| `usage` | state | `Usage` from prompt/completion tokens, cached prompt tokens, and reasoning completion details. |
| top-level `error` frame before deltas | retryable or terminal `ProviderError` | Classified from error type/code. |
| top-level `error` frame after deltas | terminal `ProviderError` | Partial content and usage are retained; `retryable=false`. |
| `data: [DONE]` | Done | Completes the response. |
| malformed tool metadata, JSON, or EOF/close failure | terminal `ProviderError` | Invalid metadata is `invalid_request`; incomplete/transport failures retain partial state. |

Chat stop reasons:

| Wire value | Canonical |
| --- | --- |
| `stop` or absent | `stop` |
| `length` | `length` |
| `tool_calls`, `function_call` | `tool_use` |
| `refusal` | `refusal` |
| `content_filter`, `safety` | `safety` |

## OpenAI Responses

| Canonical field | Request mapping |
| --- | --- |
| System prompt | `instructions`. |
| User text/image | `input` message with `role="user"`, `input_text`, and `input_image` parts. |
| Assistant text | `input` message with `role="assistant"` and `output_text` parts. |
| Thinking | Same-origin signed reasoning becomes an input `reasoning` item with its `id` and `encrypted_content`; unsigned thinking becomes assistant text. |
| Tool call | Input `function_call` item with `id`, `call_id`, `name`, and JSON `arguments`. |
| Tool result | Input `function_call_output` with `call_id` and `output`. |
| Tools | `tools[]` with `type="function"`, `name`, `description`, and `parameters`. |
| Reasoning effort | `reasoning.effort`, with `xhigh/max` mapped to `high`, and `summary="auto"`. |
| Client-side state | `store=false`; `previous_response_id` is never sent. |

Responses stream events:

| Wire event | Outcome | Provider event / state |
| --- | --- | --- |
| `response.created`, `response.in_progress`, `response.queued` | state | Records status; no visible delta. |
| `response.output_text.delta` | delta | `TextDelta`. |
| `response.refusal.delta` | delta | Refusal text delta; terminal `stop_reason="refusal"`. |
| `response.reasoning_summary_text.delta`, `response.reasoning_text.delta` | delta | `ThinkingDelta`. |
| `response.reasoning_summary_part.added/done` | state | Recognized no-op lifecycle markers; future unknown event types remain ignored. |
| `response.output_item.added` (`reasoning`) | state | Captures reasoning item id and encrypted content; does not mark output streamed by itself. |
| `response.output_item.added` (`function_call`) | delta/state | `ToolCallStart`; because a canonical provider event was emitted, a later error is non-retryable under C-2. |
| `response.function_call_arguments.delta` | delta | `ToolCallDelta`. |
| `response.function_call_arguments.done` | delta/state | Emits any suffix not already seen and replaces the final argument buffer. |
| `response.output_item.done` | state | Completes reasoning signatures or tool arguments. |
| `response.content_part.added/done`, `response.output_text.done`, reasoning `*.done` | state | Recognized no-op completion markers. |
| `response.completed.response.usage` | Done | `Usage`; terminal status is normalized. |
| `response.incomplete.response.usage` without an error | Done | `max_output_tokens`/length becomes `length`; safety/content filtering becomes `safety`; other incomplete reasons become terminal `error`. |
| `response.incomplete.response.error` | retryable or terminal `ProviderError` | Error code and usage are retained; after visible deltas `retryable=false`. |
| `response.failed` / top-level `error` | retryable or terminal `ProviderError` | Error code classification with partial content and usage. |
| malformed output item, tool metadata, JSON, or EOF/close failure | terminal `ProviderError` | Invalid metadata is `invalid_request`; transport/incomplete failures retain partial state. |

Responses terminal states:

| Wire state | Canonical |
| --- | --- |
| Completed with function calls | `tool_use` |
| Completed without function calls | `stop` |
| Refusal deltas | `refusal` |
| `incomplete_details.reason=max_output_tokens` or `length` | `length` |
| `incomplete_details.reason=content_filter` or `safety` | `safety` |
| Failed | `error` |

## Google Gemini

| Canonical field | Request mapping |
| --- | --- |
| System prompt | `systemInstruction.parts[].text`. |
| User text/image | `contents[].role="user"`; images use `inlineData` with loader-resolved bytes. |
| Assistant text | `contents[].role="model"` and text parts. |
| Thinking | Same-origin signed thinking uses `thought=true` and `thoughtSignature`; unsigned thinking is plain text. |
| Tool call | `functionCall` with `id`, `name`, and `args`; the same-origin `thoughtSignature` is stored on `ToolCallBlock.signature`. |
| Tool result | A user `functionResponse` part with `name`, `id`, and `response.output` or `response.error`. |
| Tools | `tools[].functionDeclarations[]` with Gemini's sanitized OpenAPI subset. |
| Reasoning effort | Gemini 3/Gemma 4 use `thinkingLevel`; Gemini 2.5 uses a model-specific minimum `thinkingBudget` when disabled; unknown models omit `thinkingConfig`. |
| Output limit | `generationConfig.maxOutputTokens`. |

Gemini stream events:

| Wire field | Outcome | Provider event / state |
| --- | --- | --- |
| `candidates[0].content.parts[].text` | delta | `TextDelta`, unless `thought=true`. |
| `candidates[0].content.parts[].text` with `thought=true` | delta | `ThinkingDelta`. |
| `candidates[0].content.parts[].thoughtSignature` | state | Thinking or tool-call signature. |
| `candidates[0].content.parts[].functionCall` | delta | `ToolCallStart` and `ToolCallDelta`; argument fragments are accumulated and parsed at `Done`. Empty names or malformed args are terminal `invalid_request`. |
| `candidates[0].finishReason` | Done | Normalized stop reason. |
| `usageMetadata` | state | `Usage` from prompt, candidate, cached-content, and thought token counts. |
| `promptFeedback.blockReason` | Done | `safety` when no candidate is returned. |
| top-level `error` before deltas | retryable or terminal `ProviderError` | Error status/code classification. |
| top-level `error` after deltas | terminal `ProviderError` | Partial content and usage are retained; `retryable=false`. |
| malformed JSON/parts, EOF, or close failure | terminal `ProviderError` | Invalid metadata is `invalid_request`; incomplete/transport failures retain partial state. |

Gemini stop reasons:

| Wire value | Canonical |
| --- | --- |
| `STOP` or absent | `stop` |
| `MAX_TOKENS` | `length` |
| `SAFETY`, `IMAGE_SAFETY`, `BLOCKLIST`, `PROHIBITED_CONTENT`, `RECITATION`, `SPII`, `IMAGE_PROHIBITED_CONTENT` | `safety` |
| `MALFORMED_FUNCTION_CALL`, `UNEXPECTED_TOOL_CALL`, `TOO_MANY_TOOL_CALLS`, `OTHER_ERROR`, `FINISH_REASON_UNSPECIFIED`, `IMAGE_RECITATION`, `IMAGE_OTHER`, `LANGUAGE`, `NO_IMAGE`, `OTHER`, or any unknown non-normal value | `error` |
| A normal `STOP` response containing function calls | `tool_use` |
