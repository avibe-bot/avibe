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
| Request URL | `join_endpoint_url` parses the endpoint with `urllib.parse`, joins the protocol path before any existing query, preserves the base query, and appends protocol query fields only when that key is not already present. A complete protocol URL is reused by operation suffix. Adapters do not concatenate endpoint strings. |
| System prompt | Sent from `ModelRequest.system`; it is never added to the persisted message history. |
| Images | `ImageBlock.media_token` is resolved by `MediaLoader` immediately before the request. Unknown `supports_images` is false, so the cross-provider transform replaces each image with `[image: <name or mime>]`. |
| Tool ids | A safe stored id is preserved when accepted by the target. Otherwise the per-request map uses `call_` plus the first 24 hex characters of SHA-256. The same map rewrites tool results. |
| Missing tool results | The transform inserts `[tool call interrupted; no result recorded]` with `is_error=true`. |
| Origin | Direct requests use the endpoint origin. Gateway requests parse only `x-avibe-served-hop`, whose JSON object must contain exactly `provider`, `api`, and `model`. A missing or invalid report makes the response unverified: thinking and tool-call signatures are nulled, and redacted thinking is dropped. |
| Cancellation | `CancelToken` ends the HTTP stream and yields one non-retryable `ProviderError(kind="aborted")`. Opening, stream reads, error-body reads, media loads, and served-hop resolution all use the shared cancellation-aware await owner. |
| Network timeouts | The shared lifecycle driver bounds connect/open at 10 seconds, time-to-first-byte at 30 seconds, and idle time between chunks at 30 seconds. Each timeout is a classified `ProviderError(kind="network")`, retryable only when no model output was emitted; a provider that never sends response headers cannot hang a turn. A bounded 10-second cancellation join detaches a non-cooperative transport task instead of extending the timeout indefinitely. |
| HTTP response status | Only 2xx responses enter an SSE translator. Redirects (3xx) are terminal `invalid_request` errors and are never followed implicitly; 4xx/5xx bodies use the shared status-aware classifier. |
| SSE memory and scan bounds | `SSEParser` caps an unfinished line at 8 MiB of decoded characters and an unfinished event's combined `data:` fields at 32 MiB of decoded characters. It scans each buffer with a cursor and compacts once per feed, so many short fields remain linear rather than repeatedly copying the suffix. Exceeding either bound is one terminal `invalid_request` provider error, with any assembled partial preserved. |
| Cumulative assembled-output bound | `StreamAssembler` caps retained cumulative text, thinking, signatures, and tool-argument assembly at 32 MiB across the whole stream. A delta that would exceed the shared budget raises one terminal `invalid_request` error from `drive_sse_stream`, preserving the assembled partial; the limit applies across all three adapters rather than per SSE frame. |
| Error-body read failure | If a known HTTP error body raises or times out while being read, the shared driver classifies from the already-known status and headers, preserving `Retry-After`; the failure remains one terminal provider error with any partial. |
| Response cleanup | The shared driver closes every response after committing its one terminal event and bounds `response.aclose()` at 10 seconds. A stalled close is cleanup-only and cannot replace or duplicate the terminal outcome. |
| Retry boundary | `ProviderError.retryable` is false once any model content delta or tool-call start/delta was emitted. A Responses `ToolCallStart` therefore makes a later provider failure non-retryable. |
| Partial and abort | The shared `partial_message` policy is used by all three adapters. Usage captured before the first visible delta is retained, and errors/abort carry an assembled `AssistantMessage` whenever visible content or usage exists; empty placeholder slots alone do not count as streamed output, while non-empty terminal snapshots do. Consumer/task cancellation is preserved even if response cleanup fails. |
| Retry admission | `RetryPolicy.delay` in `core/agent_core/agent/models.py` applies only the bounded retry count/time budget and obeys `ProviderError.retryable`; it does not reject a usage-only partial or infer a second streamed-output boundary. A retry-admitted usage-only partial is committed as its own non-final model-response row, preserving per-attempt usage without adding prompt attempts together in one response. `test_retry_obeys_provider_flag_and_allows_usage_only_partial` and `test_retry_preserves_usage_only_partial_in_successful_response` are the loop proofs. |
| Tool-call stop normalization | `StreamAssembler.finalize` changes only `stop` with one or more tool calls to canonical `tool_use`, following Pi. The loop admits execution only for `stop_reason="tool_use"`; calls attached to `length`, `safety`, `refusal`, or `error` are settled as `is_error` results explaining the stop reason. |

## Shared stream assembler and dispatch ledger

`StreamAssembler` in `core/agent_core/ai/_common.py` is the only owner of
stream-boundary state, and `drive_sse_stream` is the only lifecycle owner.
Adapter modules translate wire fields from the driver's shared SSE iterator into
assembler calls; they do not open/close responses, classify retryability,
construct partials, mark terminal state, or parse final tool arguments.

| Concern | Single owner | Adapter call sites |
| --- | --- | --- |
| Content accumulation and visible-output flag | `StreamAssembler.text_delta`, `thinking_delta`, `tool_start`, `tool_arguments` | `anthropic.py` content-block branches; `openai_chat.py` delta branches; `openai_responses.py` output/reasoning/tool branches |
| Stable tool id and native id map | `StreamAssembler.tool_start`, `tool_state` | The three adapters' tool-call translation branches only |
| Final JSON argument validation | `StreamAssembler.finalize` → `parsed_arguments` | The three adapters call `finalize` once after their protocol terminator; malformed arguments are a C-2 terminal `invalid_request` deviation from Pi's permissive partial parser |
| Usage and early usage retention | `set_usage` | Anthropic `message_start`/`message_delta`; Chat usage chunks; Responses terminal response |
| Partial on every error and abort | `StreamAssembler.partial`, `error`, `exception`, `aborted`, `incomplete` | `drive_sse_stream` owns HTTP, transport-read, error-body, cancellation, and translator exceptions; protocol translators only request assembler errors |
| Exactly one terminal event before response cleanup | `drive_sse_stream` → `StreamAssembler.terminal` | The driver commits the translator's candidate or its own transport/incomplete outcome before `response.aclose`; cleanup cannot replace a consumer/task cancellation |
| Endpoint redaction | `sanitize_endpoint_text` in `StreamAssembler.terminal`, `error`, and `exception` | Includes preparation errors, HTTP error bodies, stream errors, network errors, and close failures |
| Unknown wire event policy | `dispatch_wire_event` plus protocol frame validation | Anthropic and Responses use explicit protocol event sets; Chat uses implicit frame kinds and validates its payload shapes directly. Unknown explicit types are ignored like Pi. Anthropic also accepts a valid data-only SSE frame whose JSON `type` is known; this is a rule (b) data-loss prevention deviation because SSE `event` is optional. |
| Cumulative output budget | `StreamAssembler._reserve_output`; `drive_sse_stream` catches `OutputBudgetExceeded` | `text_delta`, `thinking_delta`, thinking-signature updates, and tool-argument assembly reserve through the shared assembler; no adapter owns the limit or converts the overflow into a protocol-specific result |

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

### Provider error classification

All three adapters route named error frames, data-only error frames, and HTTP
error bodies through `StreamAssembler.error` and the shared `ProviderError`
classifier. Each protocol translator supplies the provider-specific envelope
or extracted message/code pair that its wire format makes available.

| Protocol | Auth | Rate limit | Overload/server | Overflow/context |
| --- | --- | --- | --- | --- |
| Anthropic | `authentication_error`, `invalid_api_key`, `permission_error`, `permission` | `rate_limit_error` | `overloaded_error` → `overloaded`; `api_error` → `server` | `invalid_request_error` with context text; `context_length_exceeded` → `overflow` |
| OpenAI Chat | `authentication_error`, `invalid_api_key`, `permission` | `rate_limit_error`, `too_many_requests` | `server_error`, `api_error` | `context_length_exceeded`, `request_too_large` |
| OpenAI Responses | `unauthenticated`, `permission_denied`, `invalid_api_key` | `rate_limit`, `rate_limit_error` | `overloaded`, `server_error`, `service_unavailable` | `context_length_exceeded`, `request_too_large` |
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
| Extracted diagnostic credentials | `errors._redact` | Named and query `password` values are redacted before a provider diagnostic reaches `ProviderError.message`; `test_error_message_redacts_password_named_and_query_values` |
| Media preparation and served-hop resolver failures | adapter preflight plus `drive_sse_stream` | `test_media_loader_failure_is_a_provider_error` and cancellation lifecycle matrix |
| Adapter logs | none: adapters do not log endpoint URLs | source audit of all three adapter modules |

The driver call sites are exactly `AnthropicAdapter._stream`,
`OpenAIChatAdapter._stream`, and `OpenAIResponsesAdapter._stream`. The delayed
transport-read regression exercises all three call sites and proves terminal
delivery precedes response cleanup.

| Shared driver branch | Canonical outcome | Recorded proof |
| --- | --- | --- |
| transport read raises after a visible delta, while response close is observable | one non-retryable `ProviderError(kind="network", partial=...)`, delivered before `response.aclose`; no second terminal | `test_delayed_transport_read_emits_terminal_before_close` across all three adapters |

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
| All | known event field shape | each adapter's dispatch branch and the wire-shape tables above | Present wrong-type fields are terminal `invalid_request`; optional fields may be absent or null; unknown event types remain ignored like Pi. |

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
| Responses | message content parts in `response.output_item.done` and terminal `response.output` | content must be an array when present; every present part must be an object, and `type`, `text`, and `refusal` must be strings when present; malformed content is terminal `invalid_request` | `test_responses_rejects_malformed_message_content_parts` |
| Responses | `response.done` alias | normalized to `response.completed` | `test_responses_done_alias_follows_terminal_response_state` |
| Responses | `response.content_part.added/done`, `response.output_text.done`, `response.reasoning_text.done`, `response.reasoning_summary_text.done` | state/no-op | `test_responses_known_lifecycle_events_are_accounted_for` |
| Responses | `response.completed`, `response.incomplete` | usage and terminal stop mapping; content filtering becomes `safety`, unsupported reasons are terminal errors; terminal message snapshots are not adopted, while refusal status is retained | `test_responses_incomplete_reasons_have_explicit_outcomes`, `test_responses_does_not_adopt_terminal_message_snapshot`, `test_responses_terminal_refusal_output_sets_refusal_stop_reason` |
| Responses | `response.failed`, top-level `error` | classified provider error with partial and usage | `test_responses_top_level_error_frame_is_classified`, `_WIRE_EVENT_CASES` Responses rows |
| Responses | missing terminal or unfinished tool call; final-only function calls in terminal `response.output` are ignored like Pi | terminal incomplete/invalid error with partial, or completed streamed tool call | `test_eof_without_protocol_terminal_is_not_success`, `test_responses_rejects_unfinished_tool_call_on_completed_response`, `test_responses_ignores_final_only_function_call_like_pi` |
| Chat | chunk metadata, `usage`, and `choices[0].usage` fallback | retain usage fields; the choice-level fallback is used only when the chunk-level usage is absent | `test_chat_uses_choice_usage_fallback`, `chat_reasoning_details_merge` |
| Chat | `choices[0].finish_reason` and `native_finish_reason` | canonical stop mapping, including `max_tokens -> length` and compatibility value `end -> stop`; a non-normal native reason overrides `stop`; unknown value becomes canonical `stop_reason="error"` | `test_chat_finish_reason_end_maps_to_stop`, `test_chat_max_tokens_maps_to_length`, `test_chat_native_safety_overrides_stop`, `test_chat_unknown_finish_reason_is_canonical_error`, `_WIRE_EVENT_CASES` Chat rows |
| Chat | `delta.content` | text delta; wrong content/refusal field types are terminal malformed metadata | `chat_error_after_text_keeps_partial`, `test_chat_known_text_fields_reject_wrong_types` |
| Chat | legacy reasoning fields | first non-empty field becomes thinking text/signature marker | `test_chat_reasoning_field_is_replayed_as_a_marker` |
| Chat | `delta.tool_calls` | index/id/name correlation; late id binds to a stable canonical id; an absent index falls back to the native id alias | `chat_late_tool_id_uses_stable_canonical_id`, `test_chat_tool_call_without_integer_index_uses_native_id_alias` |
| Chat | legacy `delta.function_call` | same single-tool state machine | `_WIRE_EVENT_CASES` legacy function-call row |
| Chat | `delta.reasoning_details` | consecutive text/summary entries merge; opaque entries remain discrete | `chat_reasoning_details_merge` |
| Chat | `delta.refusal` | preserve refusal text and canonical `refusal` stop reason; this is an Avibe C-2 deviation from Pi's text-only projection | `test_chat_refusal_stop_is_not_overwritten_by_later_stop_reason` |
| Chat | top-level `error` | classified provider error with partial after deltas | `chat_error_after_text_keeps_partial` |
| Chat | `[DONE]`, finish reason, missing terminal/close failure | `[DONE]` requires a finish reason; a finish reason may terminate at EOF; a prompt-level frame such as `promptFeedback` without candidates still has no finish reason and is incomplete; otherwise one retryable incomplete network error when no output was emitted | `test_chat_requires_finish_reason_before_done_marker`, `test_chat_prompt_feedback_without_candidates_is_incomplete`, `test_chat_finish_reason_can_terminate_at_eof_without_done_marker`, `test_close_failure_cannot_emit_a_second_terminal_event` |
| Anthropic | `message_start` | capture usage before visible output; malformed message/usage is terminal | `anthropic_message_start_stop_reason_survives_eof` |
| Anthropic | `content_block_start` text/thinking/redacted/tool_use/fallback | create canonical block; initial tool input is retained even without a later delta; unknown block type and pre-output fallback are ignored; a mid-output fallback is terminal like Pi | `test_anthropic_nonempty_initial_tool_input_is_preserved_without_deltas`, `test_anthropic_ignores_unknown_content_block_types_like_pi`, `test_anthropic_pre_output_fallback_is_ignored_like_pi`, `test_anthropic_mid_output_fallback_is_terminal_like_pi` |
| Anthropic | content deltas text/thinking/signature/input JSON | corresponding delta/state; wrong field types are terminal malformed metadata; wrong-kind and unknown delta variants are ignored | `test_anthropic_malformed_delta_is_terminal_invalid_request`, `test_anthropic_known_delta_fields_reject_wrong_types`, `test_anthropic_wrong_kind_delta_is_ignored_like_pi`, `test_anthropic_ignores_unknown_delta_variants_like_pi` |
| Anthropic | known event shape validation | malformed indexes, envelopes, or usage objects are terminal `invalid_request` | `test_wire_event_matrix_has_one_explicit_outcome` malformed Anthropic tuples plus the focused malformed-event tests |
| Anthropic | `content_block_stop` | block end only for non-empty visible output; late deltas for a closed block are ignored | `test_anthropic_streams_thinking_tool_arguments_and_usage`, `test_anthropic_empty_block_end_does_not_count_as_streamed_output` |
| Anthropic | `message_delta` | merge non-null usage and normalize stop reason; explicit `null` fields preserve earlier `message_start` counts; malformed usage is terminal | `test_anthropic_streams_thinking_tool_arguments_and_usage`, `test_anthropic_null_delta_usage_does_not_erase_message_start_usage`, `test_anthropic_malformed_message_delta_usage_is_terminal_invalid_request` |
| Anthropic | `message_stop` or EOF | `message_stop` completes only with a captured stop reason; after `message_start`, EOF without `message_stop` is incomplete even when a stop reason was captured; a no-`message_start` stream with a stop reason follows Pi's completion path | `test_anthropic_message_stop_without_reason_is_terminal_error`, `test_anthropic_message_start_requires_message_stop_even_with_stop_reason`, `test_anthropic_stop_reason_allows_eof_without_message_stop_like_pi` |
| Anthropic | `error`/transport close | classified error with assembled partial and early usage; an empty placeholder block does not make the error non-retryable | `_WIRE_EVENT_CASES` Anthropic rows, `test_close_failure_cannot_emit_a_second_terminal_event` |
| Anthropic | unknown top-level event | named unknown events are ignored before JSON parsing, matching Pi; valid data-only JSON frames are accepted under the documented rule (b) deviation, while malformed data-only frames are ignored like Pi | `test_anthropic_ignores_unknown_top_level_events_like_pi`, `test_anthropic_ignores_unknown_sse_event_before_parsing_like_pi`, `test_anthropic_malformed_data_only_frame_is_ignored_like_pi` |

## Recorded Pi stream differential

Pi was driven at revision `7fbbd5f` in an isolated temporary checkout with
credential-free stub transports. The runner fed the same recorded wire
sequences used by `tests/agent_core/ai/test_pi_stream_differential.py` to
Pi's `processResponsesStream`, `openai-completions.stream`, and
`anthropic-messages.stream`, then projected both outputs to:
`outcome`, canonical stop reason, canonical blocks, usage, error kind, and
partial blocks. The run covered Responses, Chat, and Anthropic. The Google
adapter was not included because the owner deferred it from v1.

| Fixture | Pi projection | Avibe projection | Difference |
| --- | --- | --- | --- |
| `responses_output_slots_arrive_in_order` | reasoning `plan` with signed item, then text `answer`; `stop`; usage `2/3/reasoning=1` | same | none |
| `responses_unknown_event_is_ignored` | empty successful response; `stop`; Pi's initialized usage shape | empty successful response; `stop`; usage remains unknown when the provider omits it | canonical optional usage does not invent provider counts |
| `responses_missing_output_index_shares_pi_slot` | one undefined output slot correlates the function-call argument delta; provider-native composite id; Pi's initialized usage shape | one fallback slot correlates the same delta; stable canonical id plus native id; optional usage remains absent | C-2 canonical id and optional usage |
| `responses_terminal_message_snapshot_is_not_adopted` | terminal message output does not replace the empty slot created by `output_item.added`; `stop`; Pi's initialized usage shape | same; optional usage remains absent | canonical optional usage does not invent provider counts |
| `chat_reasoning_details_merge` | one thinking block with signature `reasoning.text=ab`; `stop`; the usage parser fills missing reasoning details with `0` | same block and stop; canonical usage keeps unknown reasoning details as `None` | canonical optional usage fields |
| `chat_error_after_text_keeps_partial` | server error with text partial and Pi's zero-initialized usage | server error with text partial and absent usage | canonical optional usage does not invent provider counts |
| `anthropic_message_start_stop_reason_survives_eof` | error partial with input usage `7`, empty content | `network` error partial with the same usage; retryable because usage alone is not streamed output | C-2 retry boundary and shared incomplete classification |
| `anthropic_unknown_event_is_ignored` | empty successful response with input usage `4` | same | none |
| `responses_terminal_refusal_is_contract_deviation` | completed refusal projected as an empty text block with `stop` and zero-initialized usage | refusal text is retained with `refusal` and optional usage remains absent | C-2 canonical refusal stop reason; refusal is never folded into `stop` |
| `chat_late_tool_id_uses_stable_canonical_id` | final provider id with Pi's initialized usage shape | stable id from `ToolCallStart` is retained, provider id is `native_id`, and optional usage remains absent | C-2 provider-event id stability |
| `responses_consecutive_missing_output_indexes_rebind` | Pi's undefined slot is removed at each `output_item.done`, so both final calls survive with composite ids | item ids bind each call to a distinct internal fallback slot, preserving both streamed argument paths with canonical id/native id split | rule (b) data-loss prevention plus canonical ids |
| `responses_empty_slots_ignore_late_deltas` | empty text and reasoning slots close on `output_item.done`; later deltas are ignored | same, with optional usage remaining absent | canonical optional usage |
| `chat_finish_reason_end_maps_to_stop` | compatibility `finish_reason=end` maps to `stop`; no usage frame leaves Pi's initialized usage shape | same stop mapping; usage remains unknown | canonical optional usage |
| `anthropic_null_delta_usage_preserves_message_start` | `message_start.input_tokens=7` survives null fields in `message_delta` | same | none |
| `chat_malformed_tool_arguments` | Pi's permissive parser completes `read` with `{}` and `tool_use` | terminal `invalid_request` naming malformed arguments; partial has no tool block | C-2 canonical malformed-argument error |
| `chat_eof_after_text_is_partial_abort` | missing finish reason becomes an error with text partial and Pi's initialized usage shape | `network` error with text partial and `retryable=false`; optional usage remains absent | C-2 retry boundary and canonical optional usage |
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
| Reasoning effort | `reasoning_effort`; supported declared values, including explicit `none`, are forwarded unchanged, while `off`/`disabled` are omitted. |
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
| `choices[0].finish_reason` | state | Stop reason. `max_tokens` becomes `length`; unknown values become terminal `error`, not `stop`. |
| `choices[0].native_finish_reason` | state | A non-normal native reason such as `safety` or `recitation` overrides `finish_reason="stop"`; this prevents tool execution after a safety stop from a converted Gemini response. |
| `usage` | state | `Usage` from prompt/completion tokens, cached prompt tokens, and reasoning completion details; cached read/write tokens are excluded from `input_tokens`. |
| top-level `error` frame before deltas | retryable or terminal `ProviderError` | Classified from error type/code. |
| top-level `error` frame after deltas | terminal `ProviderError` | Partial content and usage are retained; `retryable=false`. |
| `data: [DONE]` | Done or terminal error | Completes the response only after a `finish_reason`; without one it is an incomplete `network` error, retryable only when no model output was emitted. A finish reason may also complete at clean EOF. |
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
| Reasoning effort | `reasoning.effort`, with supported declared values, including explicit `none`, forwarded unchanged, and `summary="auto"`; `off`/`disabled` are omitted. |
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
| `response.incomplete.response.usage` without an error | Done or terminal `ProviderError` | The outer `response.incomplete` event always takes the incomplete path even when nested `response.status` is absent or stale. `max_output_tokens`/length becomes `length`; safety/content filtering becomes `safety`; other, missing, or unknown reasons become terminal `error`. |
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
