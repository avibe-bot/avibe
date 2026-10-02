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
| Cancellation | `CancelToken` ends the HTTP stream and yields one non-retryable `ProviderError(kind="aborted")`. |
| Retry boundary | `ProviderError.retryable` is false once any model content delta or tool-call start/delta was emitted. |

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

| Wire event | Provider event / state |
| --- | --- |
| `message_start.message.usage` | Initial `Usage`; `input_tokens` excludes cache reads and writes. |
| `content_block_start` (`text`, `thinking`, `redacted_thinking`, `tool_use`) | Creates the canonical block; `tool_use` emits `ToolCallStart`. |
| `content_block_delta.text_delta` | `TextDelta`. |
| `content_block_delta.thinking_delta` | `ThinkingDelta`. |
| `content_block_delta.signature_delta` | Appends to the thinking signature. |
| `content_block_delta.input_json_delta` | `ToolCallDelta`; the complete JSON is parsed at `Done`. |
| `content_block_stop` | `BlockEnd`. |
| `message_delta.stop_reason` | Normalized stop reason; usage fields are merged. |
| `message_stop` | `Done(AssistantMessage)`. |
| `error` | Classified `ProviderError`, retaining a partial message after deltas. |

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

| Wire field | Provider event / state |
| --- | --- |
| `choices[0].delta.content` | `TextDelta`. |
| `choices[0].delta.reasoning_content/reasoning/reasoning_text` | `ThinkingDelta`. |
| `choices[0].delta.reasoning_details` | Stored as a structured thinking signature. |
| `choices[0].delta.tool_calls[].id/function.name` | `ToolCallStart`. |
| `choices[0].delta.tool_calls[].function.arguments` | `ToolCallDelta`; fragments are parsed at `Done`. |
| `choices[0].finish_reason` | Stop reason. |
| `usage` | `Usage` from prompt/completion tokens, cached prompt tokens, and reasoning completion details. |
| `data: [DONE]` | Completes the response. |

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

| Wire event | Provider event / state |
| --- | --- |
| `response.output_text.delta` | `TextDelta`. |
| `response.refusal.delta` | Refusal text delta; terminal `stop_reason="refusal"`. |
| `response.reasoning_summary_text.delta`, `response.reasoning_text.delta` | `ThinkingDelta`. |
| `response.output_item.added` (`reasoning`) | Captures reasoning item id and encrypted content. |
| `response.output_item.added` (`function_call`) | `ToolCallStart`. |
| `response.function_call_arguments.delta` | `ToolCallDelta`. |
| `response.output_item.done` | Completes reasoning signatures or tool arguments. |
| `response.completed.response.usage` / `response.incomplete.response.usage` | `Usage`; terminal status is normalized. |
| `response.failed` / `error` | Classified `ProviderError`, retaining partial content. |

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
| Reasoning effort | Gemini 3/Gemma 4 use `thinkingLevel`; Gemini 2.5 uses model-specific `thinkingBudget`; disabled uses zero budget where supported. |
| Output limit | `generationConfig.maxOutputTokens`. |

Gemini stream events:

| Wire field | Provider event / state |
| --- | --- |
| `candidates[0].content.parts[].text` | `TextDelta`, unless `thought=true`. |
| `candidates[0].content.parts[].text` with `thought=true` | `ThinkingDelta`. |
| `candidates[0].content.parts[].thoughtSignature` | Thinking or tool-call signature. |
| `candidates[0].content.parts[].functionCall` | `ToolCallStart` and `ToolCallDelta`; argument fragments are accumulated and parsed at `Done`. |
| `candidates[0].finishReason` | Normalized stop reason. |
| `usageMetadata` | `Usage` from prompt, candidate, cached-content, and thought token counts. |
| `promptFeedback.blockReason` | `safety` when no candidate is returned. |

Gemini stop reasons:

| Wire value | Canonical |
| --- | --- |
| `STOP` or absent | `stop` |
| `MAX_TOKENS` | `length` |
| `SAFETY`, `IMAGE_SAFETY`, `BLOCKLIST`, `PROHIBITED_CONTENT`, `RECITATION` | `safety` |
| `MALFORMED_FUNCTION_CALL`, `OTHER_ERROR` | `error` |
| Any function call in the response | `tool_use` |
