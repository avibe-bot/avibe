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
| Request URL | `join_endpoint_url` parses the endpoint with `urllib.parse`, joins the protocol path before any existing query, preserves the base query, and appends protocol query fields such as Gemini `alt=sse` only when that key is not already present. A complete protocol URL is reused by operation suffix. Adapters do not concatenate endpoint strings. |
| System prompt | Sent from `ModelRequest.system`; it is never added to the persisted message history. |
| Images | `ImageBlock.media_token` is resolved by `MediaLoader` immediately before the request. Unknown `supports_images` is false, so the cross-provider transform replaces each image with `[image: <name or mime>]`. |
| Tool ids | A safe stored id is preserved when accepted by the target. Otherwise the per-request map uses `call_` plus the first 24 hex characters of SHA-256. The same map rewrites tool results. |
| Missing tool results | The transform inserts `[tool call interrupted; no result recorded]` with `is_error=true`. |
| Origin | Direct requests use the endpoint origin. Gateway requests parse only `x-avibe-served-hop`, whose JSON object must contain exactly `provider`, `api`, and `model`. A missing or invalid report makes the response unverified: thinking and tool-call signatures are nulled, and redacted thinking is dropped. |
| Cancellation | `CancelToken` ends the HTTP stream and yields one non-retryable `ProviderError(kind="aborted")`. Opening, stream reads, error-body reads, media loads, and served-hop resolution all use the shared cancellation-aware await owner. |
| Retry boundary | `ProviderError.retryable` is false once any model content delta or tool-call start/delta was emitted. A Responses `ToolCallStart` therefore makes a later provider failure non-retryable. |
| Partial and abort | The shared `partial_message` policy is used by all four adapters. Usage captured before the first visible delta is retained, and errors/abort carry an assembled `AssistantMessage` whenever visible content or usage exists; empty placeholder slots alone do not count as streamed output, while non-empty terminal snapshots do. Consumer/task cancellation is preserved even if response cleanup fails. |
| Retry admission | `RetryPolicy.delay` in `core/agent_core/agent/models.py` applies only the bounded retry count/time budget and obeys `ProviderError.retryable`; it does not reject a usage-only partial or infer a second streamed-output boundary. `test_retry_obeys_provider_flag_and_allows_usage_only_partial` is the loop proof. |
| Tool-call stop normalization | `StreamAssembler.finalize` changes only `stop` with one or more tool calls to canonical `tool_use`, following Pi. The loop admits execution only for `stop_reason="tool_use"`; calls attached to `length`, `safety`, `refusal`, or `error` are settled as `is_error` results explaining the stop reason. |

## Shared stream assembler and dispatch ledger

`StreamAssembler` in `core/agent_core/ai/_common.py` is the only owner of
stream-boundary state, and `drive_sse_stream` is the only lifecycle owner.
Adapter modules translate wire fields from the driver's shared SSE iterator into
assembler calls; they do not open/close responses, classify retryability,
construct partials, mark terminal state, or parse final tool arguments.

| Concern | Single owner | Adapter call sites |
| --- | --- | --- |
| Content accumulation and visible-output flag | `StreamAssembler.text_delta`, `thinking_delta`, `tool_start`, `tool_arguments` | `anthropic.py` content-block branches; `openai_chat.py` delta branches; `openai_responses.py` output/reasoning/tool branches; `google.py` candidate-part branches |
| Stable tool id and native id map | `StreamAssembler.tool_start`, `tool_state` | The four adapters' tool-call translation branches only |
| Final JSON argument validation | `StreamAssembler.finalize` → `parsed_arguments` | The four adapters call `finalize` once after their protocol terminator; malformed arguments are a C-2 terminal `invalid_request` deviation from Pi's permissive partial parser |
| Usage and early usage retention | `set_usage` | Anthropic `message_start`/`message_delta`; Chat usage chunks; Responses terminal response; Gemini `usageMetadata` |
| Partial on every error and abort | `StreamAssembler.partial`, `error`, `exception`, `aborted`, `incomplete` | `drive_sse_stream` owns HTTP, transport-read, error-body, cancellation, and translator exceptions; protocol translators only request assembler errors |
| Exactly one terminal event before response cleanup | `drive_sse_stream` → `StreamAssembler.terminal` | The driver commits the translator's candidate or its own transport/incomplete outcome before `response.aclose`; cleanup cannot replace a consumer/task cancellation |
| Endpoint redaction | `sanitize_endpoint_text` in `StreamAssembler.terminal`, `error`, and `exception` | Includes preparation errors, HTTP error bodies, stream errors, network errors, and close failures |
| Unknown wire event policy | `dispatch_wire_event` plus protocol frame validation | Anthropic and Responses use explicit protocol event sets; Chat and Gemini use implicit frame kinds and validate their payload shapes directly. Unknown explicit types are ignored like Pi. Anthropic also accepts a valid data-only SSE frame whose JSON `type` is known; this is a rule (b) data-loss prevention deviation because SSE `event` is optional. |

The shared dispatch helper only identifies a known event. Each adapter then
validates the shape of that known event and emits exactly one terminal
`invalid_request` error for malformed metadata. Thus a future event type is
forward-compatible, while a known event with an invalid payload cannot become a
successful response.

## Wire-shape validation tables

These tables enumerate every known event handled by each adapter. A required
field is present in every valid frame. An optional field may be omitted or
`null` only where the table says so. A present field with the wrong JSON type is
a terminal `ProviderError(kind="invalid_request")`; unknown event types and
unknown fields follow Pi and are ignored.

### Anthropic Messages

| Event | Required fields and types | Optional fields and types |
| --- | --- | --- |
| `message_start` | `message: object` | `message.usage: object` |
| `content_block_start` | `index: non-negative integer`, `content_block: object`, `content_block.type: string` | text/thinking `text`/`thinking`/`signature: string`; redacted `data: non-empty string`; tool `id`/`name: string`, `input: object` |
| `content_block_delta` | `index: non-negative integer`, `delta: object`, `delta.type: string` | `text_delta.text`, `thinking_delta.thinking`, `signature_delta.signature`, `input_json_delta.partial_json: string` |
| `content_block_stop` | `index: non-negative integer` | none |
| `message_delta` | none | `delta: object`, `delta.stop_reason: string`, `usage: object` |
| `message_stop` | none | none |
| `error` (named SSE or data-only) | `error: object` when an envelope is present | `error.type`/`error.message: string`; provider extension fields such as `code` are ignored |
| `ping` | none | none |

### OpenAI Chat Completions

| Wire frame | Required fields and types | Optional fields and types |
| --- | --- | --- |
| normal chunk | JSON object | `choices: array`, `usage: object`, `error: object` |
| choice | `choice: object` | `delta: object`, `finish_reason: string`, `usage: object` |
| delta text/refusal/reasoning | `content`/`refusal`/`reasoning_content`/`reasoning`/`reasoning_text: string` | each field may be omitted or `null` |
| `reasoning_details` | array when present | entries are provider objects |
| `tool_calls` | array when present | entries: `object`; `index: integer`, `id: string`, `function: object`; function `name`/`arguments: string` |
| legacy `function_call` | object when present | `name`/`arguments: string` |
| top-level `error` | object when present | `type`/`message: string`, `code: string or integer` (integer codes are ignored for classification) |
| `[DONE]` | sentinel data | no JSON fields |

### OpenAI Responses

| Event | Required fields and types | Optional fields and types |
| --- | --- | --- |
| `response.created`, `response.in_progress` | `type: string` | `response: object`, `response.status: string` |
| `response.queued` | `type: string` | none |
| output/refusal/reasoning delta | `type: string`, `delta: string` | `output_index: non-negative integer` |
| `response.output_item.added` | `item: object`; `item.type: string` | `output_index: non-negative integer`; function item `id`/`call_id`/`name`/`arguments: string` |
| `response.output_item.done` | `item: object` when present; `item.type: string` | same function fields; `output_index: non-negative integer` |
| function-call argument delta/done | `delta` or `arguments: string` when present | `item_id: string`, `output_index: non-negative integer` |
| custom-tool input delta/done | `type: string` | provider-specific fields are ignored |
| content/output/reasoning done events | `type: string` | `output_index: non-negative integer` |
| `response.completed`, `response.incomplete` | `response: object` | `response.status: string`, `output: array`, `usage: object`, `incomplete_details: object`, `error: object` |
| `response.failed` | `type: string` | `response: object`, `response.error: object`, `response.usage: object` |
| top-level `error` | `error: object` or `message: string` | `error.type`/`error.message: string`, `error.code: string or integer` |

`response.output` is validated even when empty. If it is present and is not an
array, the terminal is malformed rather than a successful empty response.

### Google Gemini

| Wire shape | Required fields and types | Optional fields and types |
| --- | --- | --- |
| normal frame | JSON object | `candidates: array`, `usageMetadata: object`, `promptFeedback: object` |
| top-level `error` | object when present | `status`/`message: string`; provider `code` is preserved in the envelope but ignored for classification |
| candidate | object | `finishReason: string`, `content: object` |
| candidate content | `parts: array` when present | none |
| part | object | `text: string`, `thought: boolean`, `thoughtSignature: string`, `functionCall: object` |
| function call | object when present | `id`/`name: string`, `args: object` or JSON string |
| prompt feedback | object when present | `blockReason: string` |
| `usageMetadata` | object when present | token counters are non-negative integers |

### Provider error classification

All four adapters route named error frames, data-only error frames, and HTTP
error bodies through `StreamAssembler.error` and the shared `ProviderError`
classifier. Each protocol translator supplies the provider-specific envelope
or extracted message/code pair that its wire format makes available.

| Protocol | Auth | Rate limit | Overload/server | Overflow/context |
| --- | --- | --- | --- | --- |
| Anthropic | `authentication_error`, `invalid_api_key`, `permission_error`, `permission` | `rate_limit_error` | `overloaded_error` → `overloaded`; `api_error` → `server` | `invalid_request_error` with context text; `context_length_exceeded` → `overflow` |
| OpenAI Chat | `authentication_error`, `invalid_api_key`, `permission` | `rate_limit_error`, `too_many_requests` | `server_error`, `api_error` | `context_length_exceeded`, `request_too_large` |
| OpenAI Responses | `unauthenticated`, `permission_denied`, `invalid_api_key` | `rate_limit`, `rate_limit_error` | `overloaded`, `server_error`, `service_unavailable` | `context_length_exceeded`, `request_too_large` |
| Gemini | `UNAUTHENTICATED`, `PERMISSION_DENIED`, `API_KEY_INVALID` (including nested `ErrorInfo.reason`) | `RESOURCE_EXHAUSTED` | `UNAVAILABLE`, `DEADLINE_EXCEEDED`, `INTERNAL` | `context_length_exceeded`, `INVALID_ARGUMENT` with overflow text |

HTTP `401/403` always map to `auth`, `413` to `overflow`, `429` to
`rate_limit` with `Retry-After`, and `5xx` to `server` or `overloaded`.
Transport errors map to `network`. Every classification is non-retryable once
the assembler has emitted visible content or a tool-call event.

## Endpoint identity and redaction ledger

Unnamed custom endpoints use `credential_free_endpoint_identity`: scheme,
hostname, explicit port, and path only. Userinfo, query, and fragment are never
part of `Origin.provider`; distinct endpoints remain distinct even when their
provider field is empty.

| Path that can carry endpoint text | Redaction owner | Proof |
| --- | --- | --- |
| Origin construction for an unnamed endpoint | `endpoint_origin` → `credential_free_endpoint_identity` | `test_unnamed_custom_endpoint_origin_is_credential_free` |
| HTTP/provider error body | `StreamAssembler.error` → `sanitize_endpoint_text` | `test_error_message_redacts_quoted_json_nested_in_message` plus adapter error cases |
| Network, cancellation, parser, and close exceptions | `StreamAssembler.exception`/`terminal` → `sanitize_endpoint_text` | `test_adapter_error_redaction_preserves_json_classification`, `test_close_failure_cannot_emit_a_second_terminal_event`, `test_all_adapters_cancel_at_every_http_lifecycle_phase` |
| Media preparation and served-hop resolver failures | adapter preflight plus `drive_sse_stream` | `test_media_loader_failure_is_a_provider_error` and cancellation lifecycle matrix |
| Adapter logs | none: adapters do not log endpoint URLs | source audit of all four adapter modules |

The driver call sites are exactly `AnthropicAdapter._stream`,
`OpenAIChatAdapter._stream`, `OpenAIResponsesAdapter._stream`, and
`GoogleAdapter._stream`. The delayed transport-read regression exercises all
four call sites and proves terminal delivery precedes response cleanup.

| Shared driver branch | Canonical outcome | Recorded proof |
| --- | --- | --- |
| transport read raises after a visible delta, while response close is observable | one non-retryable `ProviderError(kind="network", partial=...)`, delivered before `response.aclose`; no second terminal | `test_delayed_transport_read_emits_terminal_before_close` across all four adapters |

## Pi branch audit

The following rows are the branch-by-branch audit against Pi revision
`7fbbd5f`. “Same” means the Avibe handler preserves Pi's state-machine
behavior. A stated deviation is limited to the C-1/C-2 canonical event,
origin, retry, cancellation, media, or message-shape rules.

| Protocol | Pi branch | Avibe handler | Result |
| --- | --- | --- | --- |
| Responses | `openai-responses-shared.ts:599-604` response id and output-item creation | `OpenAIResponsesAdapter._stream` → `response.output_item.added` | Same slot creation order; Avibe stores canonical blocks. |
| Responses | `:605-634` reasoning summary/text deltas | `OpenAIResponsesAdapter._stream` → reasoning delta branches | Same; unknown slots are ignored. |
| Responses | `:635-654` output/refusal deltas | `OpenAIResponsesAdapter._stream` → text/refusal branches | Same slot lookup; refusal is preserved as a canonical refusal stop for an existing message slot, while orphan refusal deltas are ignored like Pi. |
| Responses | `:655-671` function-call argument delta/done | `OpenAIResponsesAdapter._stream` → argument branches | Same suffix and replacement rules; malformed metadata is canonical `invalid_request`. |
| Responses | `:683-742` output-item finalization | `OpenAIResponsesAdapter._stream` and `_apply_terminal_output_items` | `output_item.done` follows Pi for arrival-order slots, argument finalization, and reasoning signatures; consecutive calls with omitted `output_index` are rebound by item id to prevent call loss (rule (b)); terminal `response.output` only backfills an existing reasoning signature, while refusal status is a C-2 deviation. |
| Responses | `:743-744` completed/incomplete | `OpenAIResponsesAdapter._stream` → completed/incomplete branch | Same terminal response ownership; canonical stop reasons and usage, with cached read/write tokens excluded from `input_tokens`. |
| Responses | terminal `response.output` shape | `OpenAIResponsesAdapter._stream` → terminal response validation | Same successful output-array contract; a present non-array output is a terminal malformed error, preventing a false empty success (rule (b), false information prevention). |
| Responses | `:745-757` provider error/failed | `OpenAIResponsesAdapter._stream` → failed/error branches | Same classification, with Avibe's streamed retry boundary and partial message. |
| Responses | `:759-777` missing terminal/unfinished call | `OpenAIResponsesAdapter._stream` → `StreamAssembler.unfinished_tool_calls` | Same terminal requirement; Avibe emits canonical `ProviderError`. |
| Responses | unknown event fallthrough | `dispatch_wire_event` and `_KNOWN_RESPONSE_EVENTS` | Same: ignored for forward compatibility. |
| Chat | `openai-completions.ts:553-565` chunk metadata and usage | `OpenAIChatAdapter._stream` → chunk metadata/usage | Same relevant metadata/usage behavior, including cache subtraction from `input_tokens`. |
| Chat | `:567-585` choice finish reason | `OpenAIChatAdapter._stream` and `_normalize_stop` | Known mapping is the same, including `end -> stop`; unknown values become canonical `Done(stop_reason="error")` instead of Pi's provider error, as required by C-2 stop reasons. |
| Chat | `:586-633` text and legacy reasoning fields | `OpenAIChatAdapter._stream` → content/reasoning branches | Same first non-empty reasoning field and visible deltas. |
| Chat | `:635-661` tool-call state machine | `OpenAIChatAdapter._stream` and `StreamAssembler.tool_key` | Same index/id correlation; an id arriving before the name is retained and reused. |
| Chat | `:665-675` reasoning-details accumulation | `OpenAIChatAdapter._stream` → `merge_thinking_details` | Same consecutive text/summary merge and opaque-entry preservation. |
| Chat | `:680-701` finish blocks and terminal checks | `OpenAIChatAdapter._stream` → finish/EOF checks | Same terminal requirement; malformed final arguments are a C-2 `invalid_request` deviation from Pi's permissive parser. |
| Chat | top-level provider error | `OpenAIChatAdapter._stream` → top-level error branch | Same classification, with canonical partial usage and streamed retry boundary. |
| Chat | aborted/error cleanup | `openai-completions.ts:702-724` | Same assembled partial policy through `StreamAssembler`. |
| Anthropic | `anthropic-messages.ts:665-690` message start | `AnthropicAdapter._stream` → `message_start` | Same early usage capture; cache-write total is retained. |
| Anthropic | `:691-738` block start | `AnthropicAdapter._stream` → `content_block_start` | Same supported text/thinking/redacted/tool blocks and initial content; non-empty initial `tool_use.input` is retained when no JSON delta follows (rule (b), preventing tool-argument loss); opaque payloads follow C-1 origin rules. |
| Anthropic | `:739-784` content deltas | `AnthropicAdapter._stream` → `content_block_delta` | Same supported delta accumulation and signature append; malformed known fields are a C-2 terminal `invalid_request` deviation. |
| Anthropic | `:785-816` block stop | `AnthropicAdapter._stream` → `content_block_stop` | Same block finalization. |
| Anthropic | `:817-860` message delta and usage merge | `AnthropicAdapter._stream` → `message_delta` | Same non-null usage merge and stop normalization; explicit null fields do not erase earlier counts; malformed usage is terminal `invalid_request`, and unknown stop values become canonical `error` under C-2 stop-reason normalization. |
| Anthropic | stream end after `message_start` | `AnthropicAdapter._stream` → post-loop finalization | Same as Pi: even a captured `message_delta.stop_reason` requires `message_stop`; EOF after `message_start` is incomplete and retains early usage. A stream with no `message_start` may complete from a captured stop reason, matching Pi's handler state. |
| Anthropic | `:863-899` abort/error terminal path | `AnthropicAdapter._stream` → error/cleanup branches | Same assembled partial behavior; cancellation is the C-2 non-retryable `aborted` kind. |
| Anthropic | unknown event/content block | `dispatch_wire_event` and `content_block_start` | Unknown named events and unknown content blocks follow Pi's ignore policy; a valid data-only SSE frame is accepted as a rule (b) data-loss prevention deviation. |
| Google | `google-generative-ai.ts:130-173` text/thought parts | `GoogleAdapter._stream` → candidate parts | Same thought marker; thought signatures are retained on the originating thinking block, or on an empty canonical thinking block when a signed non-thinking text part has no thinking block (canonical schema has no text signature field). A later text signature cannot replace an existing signed thinking block. |
| Google | `:175-220` function-call parts | `GoogleAdapter._stream` → function-call parts | Same call emission; ID-less parallel parts remain distinct as required by C-2 canonical history. |
| Google | `:224-251` finish reason and usage | `GoogleAdapter._stream` → finish/usage branches | Usage subtracts cached-content tokens from `input_tokens`; Avibe retains canonical `safety` for safety finishes under C-2 while Pi maps the safety family to `error`; unknown/failure reasons become canonical `Done(stop_reason="error")` instead of Pi's provider error. |
| All | known event field shape | each adapter's dispatch branch and the wire-shape tables above | Present wrong-type fields are terminal `invalid_request`; optional fields may be absent or null; unknown event types remain ignored like Pi. |
| Google | `:254-283` final block/abort checks | `GoogleAdapter._stream` → final/cleanup branches | Same terminal requirement and assembled partial policy. |
| Google | top-level error/prompt feedback | `GoogleAdapter._stream` → error/prompt feedback | Same provider error classification with retryable transient status codes; prompt blocking is canonical `safety` under C-2 although Pi has no equivalent prompt-feedback branch. |
| Google | unknown fields/parts | `dispatch_wire_event` and candidate-part branches | Same permissive ignore behavior for fields Pi does not consume. |

### Branch closure by protocol

The rows below enumerate the state-machine branches in Pi `7fbbd5f`; the
recorded fixture names point to the table-driven tests that exercise the
canonical projection. “Ignore” is intentional Pi parity, not an unhandled
case.

| Protocol | Pi branch | Avibe outcome | Recorded proof |
| --- | --- | --- | --- |
| Responses | `response.created`, `response.in_progress`, `response.queued` | `created`/`in_progress` record state and validate an optional response object; `queued` is a no-op; unknown type is ignored | `test_responses_known_lifecycle_events_are_accounted_for`, `responses_unknown_event_is_ignored` |
| Responses | `response.output_item.added` reasoning/message/function_call | create arrival-order slot; malformed item/name/arguments is terminal `invalid_request` | `responses_output_slots_arrive_in_order` |
| Responses | missing `output_index` on output-item and argument events | non-tool items retain Pi's shared fallback; consecutive function calls allocate distinct internal fallback slots and rebind by `item_id` | `test_responses_missing_output_index_uses_one_shared_fallback_slot`, `test_responses_consecutive_missing_output_indexes_rebind_tool_slots` |
| Responses | `response.reasoning_summary_text.delta`, `response.reasoning_text.delta` | thinking delta for an existing slot; unknown slot ignored | `responses_output_slots_arrive_in_order` |
| Responses | `response.reasoning_summary_part.added` | ignore | `test_responses_known_lifecycle_events_are_accounted_for` |
| Responses | `response.reasoning_summary_part.done` | append `"\n\n"` to the thinking block, matching Pi; a present non-integer `output_index` is terminal malformed metadata | `test_responses_summary_done_adds_separator`, `test_responses_summary_done_replaces_streaming_separator_after_late_delta`, `test_responses_summary_done_rejects_non_integer_output_index` |
| Responses | `response.output_text.delta`, `response.refusal.delta` | text delta; refusal sets canonical `refusal` stop only for an existing message slot, while orphan refusal deltas are ignored | `responses_terminal_refusal_is_contract_deviation`, `responses_orphan_refusal_delta_is_ignored` |
| Responses | `response.function_call_arguments.delta`, `.done` | accumulate and emit only the unseen suffix; orphan slots are ignored like Pi; malformed arguments for a known slot are terminal | `test_responses_argument_done_emits_only_the_unseen_suffix`, `test_responses_orphan_argument_events_are_ignored_like_pi` |
| Responses | `response.custom_tool_call_input.delta`, `.done`; `custom_tool_call` output items | ignored because Avibe has no canonical custom-tool block; function-call events remain the only tool-call wire form | `test_responses_custom_tool_events_are_ignored_without_custom_tool_blocks` |
| Responses | `response.output_item.done` reasoning/message/function_call | backfill reasoning signatures, replace the final message snapshot, create a missing function-call slot, emit final argument suffixes, close visible and empty slots before later deltas, or finish tool calls; malformed item is terminal | `test_responses_message_output_item_done_backfills_text`, `test_responses_output_item_done_can_create_a_function_call_slot`, `test_responses_output_item_done_closes_slot_before_late_delta`, `test_responses_empty_slots_close_before_late_deltas` |
| Responses | `response.done` alias | normalized to `response.completed` | `test_responses_done_alias_follows_terminal_response_state` |
| Responses | `response.content_part.added/done`, `response.output_text.done`, `response.reasoning_text.done`, `response.reasoning_summary_text.done` | state/no-op | `test_responses_known_lifecycle_events_are_accounted_for` |
| Responses | `response.completed`, `response.incomplete` | usage and terminal stop mapping; content filtering becomes `safety`, unsupported reasons are terminal errors; terminal message snapshots are not adopted, while refusal status is retained | `test_responses_incomplete_reasons_have_explicit_outcomes`, `test_responses_does_not_adopt_terminal_message_snapshot`, `test_responses_terminal_refusal_output_sets_refusal_stop_reason` |
| Responses | `response.failed`, top-level `error` | classified provider error with partial and usage | `test_responses_top_level_error_frame_is_classified`, `_WIRE_EVENT_CASES` Responses rows |
| Responses | missing terminal or unfinished tool call; final-only function calls in terminal `response.output` are ignored like Pi | terminal incomplete/invalid error with partial, or completed streamed tool call | `test_eof_without_protocol_terminal_is_not_success`, `test_responses_rejects_unfinished_tool_call_on_completed_response`, `test_responses_ignores_final_only_function_call_like_pi` |
| Chat | chunk metadata, `usage`, and `choices[0].usage` fallback | retain usage fields; the choice-level fallback is used only when the chunk-level usage is absent | `test_chat_uses_choice_usage_fallback`, `chat_reasoning_details_merge` |
| Chat | `choices[0].finish_reason` | canonical stop mapping, including compatibility value `end -> stop`; unknown value becomes canonical `stop_reason="error"` | `test_chat_finish_reason_end_maps_to_stop`, `test_chat_unknown_finish_reason_is_canonical_error`, `_WIRE_EVENT_CASES` Chat rows |
| Chat | `delta.content` | text delta; wrong content/refusal field types are terminal malformed metadata | `chat_error_after_text_keeps_partial`, `test_chat_known_text_fields_reject_wrong_types` |
| Chat | legacy reasoning fields | first non-empty field becomes thinking text/signature marker | `test_chat_reasoning_field_is_replayed_as_a_marker` |
| Chat | `delta.tool_calls` | index/id/name correlation; late id binds to a stable canonical id; an absent index falls back to the native id alias | `chat_late_tool_id_uses_stable_canonical_id`, `test_chat_tool_call_without_integer_index_uses_native_id_alias` |
| Chat | legacy `delta.function_call` | same single-tool state machine | `_WIRE_EVENT_CASES` legacy function-call row |
| Chat | `delta.reasoning_details` | consecutive text/summary entries merge; opaque entries remain discrete | `chat_reasoning_details_merge` |
| Chat | `delta.refusal` | preserve refusal text and canonical `refusal` stop reason; this is an Avibe C-2 deviation from Pi's text-only projection | `test_chat_refusal_stop_is_not_overwritten_by_later_stop_reason` |
| Chat | top-level `error` | classified provider error with partial after deltas | `chat_error_after_text_keeps_partial` |
| Chat | `[DONE]`, finish reason, missing terminal/close failure | `[DONE]` requires a finish reason; a finish reason may terminate at EOF; otherwise one terminal incomplete/transport error | `test_chat_requires_finish_reason_before_done_marker`, `test_chat_finish_reason_can_terminate_at_eof_without_done_marker`, `test_close_failure_cannot_emit_a_second_terminal_event` |
| Anthropic | `message_start` | capture usage before visible output; malformed message/usage is terminal | `anthropic_message_start_stop_reason_survives_eof` |
| Anthropic | `content_block_start` text/thinking/redacted/tool_use/fallback | create canonical block; initial tool input is retained even without a later delta; unknown block type and pre-output fallback are ignored; a mid-output fallback is terminal like Pi | `test_anthropic_nonempty_initial_tool_input_is_preserved_without_deltas`, `test_anthropic_ignores_unknown_content_block_types_like_pi`, `test_anthropic_pre_output_fallback_is_ignored_like_pi`, `test_anthropic_mid_output_fallback_is_terminal_like_pi` |
| Anthropic | content deltas text/thinking/signature/input JSON | corresponding delta/state; wrong field types are terminal malformed metadata; wrong-kind and unknown delta variants are ignored | `test_anthropic_malformed_delta_is_terminal_invalid_request`, `test_anthropic_known_delta_fields_reject_wrong_types`, `test_anthropic_wrong_kind_delta_is_ignored_like_pi`, `test_anthropic_ignores_unknown_delta_variants_like_pi` |
| Anthropic | known event shape validation | malformed indexes, envelopes, or usage objects are terminal `invalid_request` | `test_wire_event_matrix_has_one_explicit_outcome` malformed Anthropic tuples plus the focused malformed-event tests |
| Anthropic | `content_block_stop` | block end only for non-empty visible output; late deltas for a closed block are ignored | `test_anthropic_streams_thinking_tool_arguments_and_usage`, `test_anthropic_empty_block_end_does_not_count_as_streamed_output` |
| Anthropic | `message_delta` | merge non-null usage and normalize stop reason; explicit `null` fields preserve earlier `message_start` counts; malformed usage is terminal | `test_anthropic_streams_thinking_tool_arguments_and_usage`, `test_anthropic_null_delta_usage_does_not_erase_message_start_usage`, `test_anthropic_malformed_message_delta_usage_is_terminal_invalid_request` |
| Anthropic | `message_stop` or EOF | `message_stop` completes only with a captured stop reason; after `message_start`, EOF without `message_stop` is incomplete even when a stop reason was captured; a no-`message_start` stream with a stop reason follows Pi's completion path | `test_anthropic_message_stop_without_reason_is_terminal_error`, `test_anthropic_message_start_requires_message_stop_even_with_stop_reason`, `test_anthropic_stop_reason_allows_eof_without_message_stop_like_pi` |
| Anthropic | `error`/transport close | classified error with assembled partial and early usage; an empty placeholder block does not make the error non-retryable | `_WIRE_EVENT_CASES` Anthropic rows, `test_close_failure_cannot_emit_a_second_terminal_event` |
| Anthropic | unknown top-level event | named unknown events are ignored before JSON parsing, matching Pi; valid data-only JSON frames are accepted under the documented rule (b) deviation, while malformed data-only frames are ignored like Pi | `test_anthropic_ignores_unknown_top_level_events_like_pi`, `test_anthropic_ignores_unknown_sse_event_before_parsing_like_pi`, `test_anthropic_malformed_data_only_frame_is_ignored_like_pi` |
| Gemini | candidate text/thought parts | text or thinking delta; a text-part `thoughtSignature` backfills the latest thinking block, including an empty-text final part, or creates an empty signed thinking block when no thinking block exists; wrong signature types are terminal malformed metadata | `test_gemini_text_part_signature_is_kept_on_thinking_block`, `test_gemini_empty_text_part_keeps_thought_signature`, `test_gemini_text_signature_without_thinking_block_is_preserved`, `test_gemini_signed_thought_part_is_replayed`, `test_gemini_thought_signature_rejects_wrong_type` |
| Gemini | function-call parts | tool start/delta with `thoughtSignature` on `ToolCallBlock.signature`; object and JSON-string arguments are parsed; parallel and repeated-id calls remain distinct | `test_gemini_function_call_signature_is_kept_on_tool_call`, `test_gemini_string_function_arguments_are_replayed`, `test_gemini_keeps_parallel_idless_calls_distinct`, `test_gemini_repeated_explicit_ids_remain_distinct_calls` |
| Gemini | finish reason | canonical stop/length/safety/error mapping; safety remains a C-2 `safety` result even though Pi maps the safety family to `error`; later usage frames remain readable | `test_gemini_finish_reasons_preserve_safety_and_tool_errors`, `test_gemini_keeps_usage_after_finish_reason_frame` |
| Gemini | `usageMetadata` | input, candidate, cached, and thought token accounting | `test_gemini_carries_tool_thought_signature_and_usage` |
| Gemini | prompt feedback and top-level error | prompt blocking becomes canonical `safety` under C-2; top-level errors are classified provider errors | `_WIRE_EVENT_CASES` Gemini rows |
| Gemini | malformed candidate/part/function metadata | terminal `invalid_request`; unknown fields remain ignored | `test_wire_event_matrix_has_one_explicit_outcome` malformed Gemini tuples plus the focused malformed-function test |
| Gemini | EOF/close failure | terminal incomplete/transport error with partial | `test_eof_without_protocol_terminal_is_not_success` |

## Recorded Pi stream differential

Pi was driven at revision `7fbbd5f` in an isolated temporary checkout with
credential-free stub transports. The runner fed the same recorded wire
sequences used by `tests/agent_core/ai/test_pi_stream_differential.py` to
Pi's `processResponsesStream`, `openai-completions.stream`, and
`anthropic-messages.stream`, then projected both outputs to:
`outcome`, canonical stop reason, canonical blocks, usage, error kind, and
partial blocks. The run covered Responses, Chat, and Anthropic; Google was
not included because this required differential run was explicitly limited to
the three adapters named by the escalation.

| Fixture | Pi projection | Avibe projection | Difference |
| --- | --- | --- | --- |
| `responses_output_slots_arrive_in_order` | reasoning `plan` with signed item, then text `answer`; `stop`; usage `2/3/reasoning=1` | same | none |
| `responses_unknown_event_is_ignored` | empty successful response; `stop`; Pi's initialized usage shape | empty successful response; `stop`; usage remains unknown when the provider omits it | canonical optional usage does not invent provider counts |
| `responses_missing_output_index_shares_pi_slot` | one undefined output slot correlates the function-call argument delta; provider-native composite id; Pi's initialized usage shape | one fallback slot correlates the same delta; stable canonical id plus native id; optional usage remains absent | C-2 canonical id and optional usage |
| `responses_terminal_message_snapshot_is_not_adopted` | terminal message output does not replace the empty slot created by `output_item.added`; `stop`; Pi's initialized usage shape | same; optional usage remains absent | canonical optional usage does not invent provider counts |
| `chat_reasoning_details_merge` | one thinking block with signature `reasoning.text=ab`; `stop`; the usage parser fills missing reasoning details with `0` | same block and stop; canonical usage keeps unknown reasoning details as `None` | canonical optional usage fields |
| `chat_error_after_text_keeps_partial` | server error with text partial and Pi's zero-initialized usage | server error with text partial and absent usage | canonical optional usage does not invent provider counts |
| `anthropic_message_start_stop_reason_survives_eof` | error partial with input usage `7`, empty content | same canonical partial | none |
| `anthropic_unknown_event_is_ignored` | empty successful response with input usage `4` | same | none |
| `responses_terminal_refusal_is_contract_deviation` | completed refusal projected as an empty text block with `stop` and zero-initialized usage | refusal text is retained with `refusal` and optional usage remains absent | C-2 canonical refusal stop reason; refusal is never folded into `stop` |
| `chat_late_tool_id_uses_stable_canonical_id` | final provider id with Pi's initialized usage shape | stable id from `ToolCallStart` is retained, provider id is `native_id`, and optional usage remains absent | C-2 provider-event id stability |
| `responses_consecutive_missing_output_indexes_rebind` | Pi's undefined slot is removed at each `output_item.done`, so both final calls survive with composite ids | item ids bind each call to a distinct internal fallback slot, preserving both streamed argument paths with canonical id/native id split | rule (b) data-loss prevention plus canonical ids |
| `responses_empty_slots_ignore_late_deltas` | empty text and reasoning slots close on `output_item.done`; later deltas are ignored | same, with optional usage remaining absent | canonical optional usage |
| `chat_finish_reason_end_maps_to_stop` | compatibility `finish_reason=end` maps to `stop`; no usage frame leaves Pi's initialized usage shape | same stop mapping; usage remains unknown | canonical optional usage |
| `anthropic_null_delta_usage_preserves_message_start` | `message_start.input_tokens=7` survives null fields in `message_delta` | same | none |
| `chat_malformed_tool_arguments` | Pi's permissive parser completes `read` with `{}` and `tool_use` | terminal `invalid_request` naming malformed arguments; partial has no tool block | C-2 canonical malformed-argument error |
| `chat_eof_after_text_is_partial_abort` | missing finish reason becomes an error with text partial and Pi's initialized usage shape | same error/partial with optional usage absent | canonical optional usage |
| `anthropic_empty_block_end_then_error` | empty text block is ended before the overloaded error; Pi retains the empty block in partial and does not apply the empty-slot retry boundary | empty placeholder does not count as streamed output, so retry remains allowed and partial is omitted | C-2 retry boundary |
| `anthropic_malformed_delta` | Pi's null delta raises a generic stream error with the empty text block in partial | known malformed metadata is terminal `invalid_request`; empty placeholder is omitted from partial | C-2 malformed-event classification and retry boundary |

The isolated Pi run was successful for all 18 fixtures across the three handlers. The only setup
limitation was the absent upstream `node_modules`; the runner used local
credential-free stubs and did not alter the Pi source or Avibe dependencies.

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
| `content_block_start` (`text`, `thinking`, `redacted_thinking`, `tool_use`) | delta/state | Creates the canonical block, preserves initial text/thinking/input, and `tool_use` emits `ToolCallStart`. Empty tool names are terminal `invalid_request`. |
| `content_block_delta.text_delta` | delta | `TextDelta`. |
| `content_block_delta.thinking_delta` | delta | `ThinkingDelta`. |
| `content_block_delta.signature_delta` | state | Appends to the thinking signature. |
| `content_block_delta.input_json_delta` | delta | `ToolCallDelta`; the complete JSON is parsed at `Done`. |
| `content_block_stop` | delta | `BlockEnd`. |
| `message_delta.stop_reason` | delta/state | Normalized stop reason; usage fields are merged. |
| `message_delta.usage` with a non-object value | terminal `ProviderError` | `invalid_request`; malformed usage is never silently discarded. |
| `message_stop` or clean EOF | Done or terminal `ProviderError` | `message_stop` yields `Done` when `message_delta.stop_reason` was present; after `message_start`, clean EOF without `message_stop` yields an incomplete error even when a stop reason was captured. A no-`message_start` stream with a stop reason follows Pi's clean-EOF completion path. |
| `ping` | state | Ignored keepalive. |
| SSE data-only frame with a known JSON event type | delta/state | Accepted because SSE `event` is optional; this is the documented rule (b) data-loss prevention deviation from Pi's event-name filter. |
| `error` before deltas | retryable or terminal `ProviderError` | Provider error type selects the kind; `message_start` usage is retained in `partial` when present. |
| `error` after deltas | terminal `ProviderError` | Partial content and usage are retained; `retryable=false`. |
| unknown `content_block` type | state | Ignored like Pi; later deltas for an unknown block index are ignored. |
| malformed JSON, block, delta, or redacted payload | terminal `ProviderError` | `invalid_request` for malformed provider metadata, otherwise `unknown`; never a successful `Done`. |
| EOF without `message_stop` or response close failure | terminal `ProviderError` | After `message_start`, incomplete/transport failure preserves partial content and early usage regardless of a captured stop reason; close failures retain the same partial. |

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
| `usage` | state | `Usage` from prompt/completion tokens, cached prompt tokens, and reasoning completion details; cached read/write tokens are excluded from `input_tokens`. |
| top-level `error` frame before deltas | retryable or terminal `ProviderError` | Classified from error type/code. |
| top-level `error` frame after deltas | terminal `ProviderError` | Partial content and usage are retained; `retryable=false`. |
| `data: [DONE]` | Done | Completes the response only after a `finish_reason`; a finish reason may also complete at clean EOF. |
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
| `response.refusal.delta` | delta/state | Refusal text delta and canonical `stop_reason="refusal"` only when its message slot exists; orphan deltas are ignored like Pi. |
| `response.reasoning_summary_text.delta`, `response.reasoning_text.delta` | delta | `ThinkingDelta`. |
| `response.reasoning_summary_part.added` | state | Recognized no-op lifecycle marker; future unknown event types remain ignored. |
| `response.reasoning_summary_part.done` | delta | Adds the Pi-compatible `"\n\n"` separator to the current reasoning block. |
| `response.output_item.added` (`reasoning`) | state | Captures reasoning item id and encrypted content; does not mark output streamed by itself. |
| `response.output_item.added` (`function_call`) | delta/state | `ToolCallStart`; missing `output_index` uses a distinct internal fallback per consecutive call and later `item_id` events rebind to it; a later error is non-retryable under C-2. |
| `response.function_call_arguments.delta` | delta | `ToolCallDelta`. |
| `response.function_call_arguments.done` | delta/state | Emits any suffix not already seen and replaces the final argument buffer. |
| `response.output_item.done` | delta/state | Completes reasoning signatures, replaces the final message snapshot, emits unseen text/thinking and argument suffixes, and emits one `BlockEnd`; later deltas for the closed slot are ignored. |
| `response.content_part.added/done`, `response.output_text.done`, reasoning `*.done` | state | Recognized no-op completion markers. |
| `response.completed.response.usage` | Done | `Usage`; cached read/write tokens are excluded from `input_tokens`. Terminal status is normalized. Function calls and ordinary message/reasoning items listed only in terminal `response.output` are not adopted; existing reasoning signatures are backfilled and refusal status is detected. |
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
| Failed/cancelled | terminal `ProviderError` (classified `unknown` when no provider error code is present) |

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
| `candidates[0].finishReason` | Done | Normalized stop reason; parsing continues so later `usageMetadata` is retained. |
| `usageMetadata` | state | `Usage` from prompt, candidate, cached-content, and thought token counts; cached-content tokens are excluded from `input_tokens`. |
| `promptFeedback.blockReason` | Done | `safety` when no candidate is returned. |
| top-level `error` before deltas | retryable or terminal `ProviderError` | Error status/code classification. |
| top-level `error` after deltas | terminal `ProviderError` | Partial content and usage are retained; `retryable=false`. |
| malformed JSON/parts, EOF, or close failure | terminal `ProviderError` | Invalid metadata is `invalid_request`; incomplete/transport failures retain partial state. |

Gemini stop reasons:

| Wire value | Canonical |
| --- | --- |
| `STOP` or absent | `stop` |
| `MAX_TOKENS` | `length` |
| `LENGTH` | `length` |
| `SAFETY`, `IMAGE_SAFETY`, `BLOCKLIST`, `PROHIBITED_CONTENT`, `RECITATION`, `SPII`, `IMAGE_PROHIBITED_CONTENT` | `safety` |
| `MALFORMED_FUNCTION_CALL`, `UNEXPECTED_TOOL_CALL`, `TOO_MANY_TOOL_CALLS`, `OTHER_ERROR`, `FINISH_REASON_UNSPECIFIED`, `IMAGE_RECITATION`, `IMAGE_OTHER`, `LANGUAGE`, `NO_IMAGE`, `OTHER`, or any unknown non-normal value | `error` |
| A normal `STOP` response containing function calls | `tool_use` |
