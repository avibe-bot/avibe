# CPA agentic-fidelity fixture

This directory contains a self-contained probe for the M0 spike in
`model-hub-engine-survey.md`. It sends the same small agentic workload through
an isolated, pinned CPA v7.2.95 instance for these four client/upstream
directions:

- Anthropic Messages client to an OpenAI Responses source;
- OpenAI Responses client to an Anthropic Messages source;
- Anthropic Messages client to an OpenAI Chat Completions source;
- OpenAI Chat Completions client to an Anthropic Messages source.

The probe is standard-library-only and does not print request or response
bodies, call ids, arguments, model prefixes, environment values, or
credentials. `run_relay.py` is the runtime harness: it obtains CPA through
`vibe/model_hub_runtime/`, creates a private temporary source projection, binds
CPA to `127.0.0.1`, and removes the temporary state when the run ends.

## Run

Load the owner-provided regression environment in the shell, then run the
launcher from the repository root:

```sh
set -a
. "$AVIBE_REGRESSION_ENV"
set +a
python3 docs/plans/fixtures/cpa-agentic-fidelity/run_relay.py
```

`AVIBE_REGRESSION_ENV` is a caller-provided path to the local regression
environment. It is never committed. The environment must provide
`ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, and `OPENAI_API_KEY`; the launcher
derives the OpenAI-compatible base as the relay root plus `/v1`. It selects
`claude-haiku-4-5` and `gpt-5.4-mini`, and configures one Claude fallback model
for bounded 503 recovery. The packaged v7.2.95 manifest is selected explicitly,
ambient manifest overrides are ignored, and the installed version is verified
before requests begin. The values are injected into private runtime state and
child-process environments only.

The output is a compact JSON report containing case names, HTTP statuses,
redacted semantic checks, tool names/counts, and stream event counts. Each case
makes a second exchange when the first response contains parsed tool calls; when
it does not, the report records that the follow-up was skipped because the
multi-turn path was not reached. When reached, the first response supplies the
observed tool-call ids and complete continuation, and the second request returns
tool results using those exact ids. The eight cases cover single and parallel calls,
streaming, tool-fragment reconstruction, system scope, stop reasons, and
thinking/reasoning in both directions of both conversion pairs. A semantic
failure exits non-zero; missing credentials or an unsafe endpoint exits with a
blocked status before network access.

Anthropic thinking uses the minimum valid manual budget (`1024`) with
`max_tokens` above that budget. OpenAI Responses uses low reasoning effort and
requests `reasoning.encrypted_content` so stateless follow-ups can carry an
opaque reasoning item.
The probe records whether the requested reasoning signal remains observable. For
Chat, it accepts either explicit `reasoning_content` or a positive standard
`usage.completion_tokens_details.reasoning_tokens` value; it does not infer
equivalence from parameter names alone. Loopback requests disable environment
proxy handlers, and exhausted 503 capacity is reported as blocked for every
target while the alternate Claude model is tried only for Anthropic targets.
Each case reports only a redacted `fallback_used` boolean and primary/fallback
scope; fallback evidence must not be attributed to the primary model. The
latest gate-complete rerun (r36) exercised all eight cases after recognizing
unknown output-marker tuples: all eight completed both turns with HTTP 200 and
no fallback was used. Messages-to-Responses single passed first parsing but
lacked thinking, then had thinking with final parsing, system scope, and exact
tuple failures; the parallel passed every first- and final-turn gate.
Responses-to-Messages single passed both parse gates and its final system/tuple
checks but lacked reasoning in both turns. The parallel also lacked reasoning,
failed both parse/order gates, and failed the final tuple while system scope and
tool outputs passed. Messages-to-Chat single had thinking with a first parse
failure, then lacked final reasoning and failed the exact tuple; the parallel
retained thinking but failed first parsing/order and final parsing while its
final tuple, system, and output checks passed. Chat-to-Messages single passed
both parse gates but lacked reasoning and failed final system scope and the
exact tuple. The parallel also lacked reasoning and failed both parse gates,
final system scope, and the final tuple while retaining stream order and tool
outputs.

## S4 matrix mapping

The eight S4 capability rows are closed to the following named probe cases and
assertions. A row marked not verified is an explicit residual, not an implicit
success claim.

| S4 capability row | Probe case or assertion |
| --- | --- |
| Single tool call | `_request` strict JSON, protocol-specific `_parse_arguments` wire encoding, `_parse_responses_document` nonempty `output_item_id`, plus `_validate_first`: `expected_tool_count`, `tool_names`, `tool_arguments`, `tool_ids_unique`, and protocol `stop_reason` |
| Parallel tools | `CaseSpec.expected_tools`, `_user_prompt(True)`, and the same `_validate_first` tool invariants |
| Multi-turn loop | `_run_case` observed `first_turn.tool_calls` plus `_validate_second`: `no_followup_tool_calls`, `tool_outputs`, and `_tool_output_tuples` marker-independent syntax recognition followed by an exact complete call-ID/result tuple-set comparison |
| Streaming text | `_request` strict UTF-8/JSON and `text/event-stream` checks with one-space SSE field handling, default-event normalization, and immediate Chat `[DONE]` dispatch termination, `_parse_anthropic_stream`/`_parse_responses_stream` delta, done-event, item, and terminal snapshot comparison for both `text` and `output_text` content parts, `RESPONSES_MESSAGE_PART_TYPES` plus content-part-to-item snapshot correlation, per-item `stream_text_snapshot_mismatch`, `_parse_chat_stream` `CHAT_STREAM_EVENT_TYPES`/`chat.completion.chunk` and typed content checks, assistant-role preservation, `_stream_order_ok`, and `stream_complete` |
| Streaming tool fragments | `_parse_anthropic_stream`, `_parse_responses_stream`, `_parse_chat_stream`, and `_stream_order_ok` opening/terminal envelopes, explicit `stream_failure_event`, `ANTHROPIC_STREAM_EVENT_TYPES`/`RESPONSES_STREAM_EVENT_TYPES`/`CHAT_STREAM_EVENT_TYPES` and supported content-block/output-item discriminators, monotonic wire sequence, contiguous block/item/content-part indexes, content-part and block/delta/done snapshot compatibility, response/item/choice/output-index/ID continuity from the opening snapshot, `summary_index` continuity, typed `stream_name_invalid`/`stream_arguments_invalid` fragments and done events, duplicate terminal usage rejection, and monotonic lifecycle checks |
| System prompt | `_system_prompt`, `_token_present`, `_validate_first`: negative `user_scope`, and `_validate_second`: exact-token `system_marker` and `system_scope` |
| Thinking/reasoning | `_anthropic_payload`, `_responses_payload`, `_chat_payload`, `_parse_anthropic_document` payload/signature validation, `_parse_anthropic_stream` empty `thinking`/`signature` opening snapshots plus typed deltas, `_parse_responses_document` `RESPONSES_REASONING_PART_TYPES`/encrypted reasoning type validation, `_parse_responses_stream` reasoning text and encrypted snapshot comparison, `_reasoning_item_has_signal`, `_chat_reasoning_usage_present` terminal integer-token gate, and `_validate_first`/`_validate_second`: `reasoning_present` plus `reasoning_not_visible` |
| Context length/truncation | `probe.CONTEXT_LENGTH_NOT_VERIFIED` residual; no low-cost context-limit run was included in M0 |

The live evidence in the survey used the owner-provided compatible relay. Direct
official vendor APIs were not measured. A transient `no available accounts`
503 is retried with short bounded backoff; after the configured Claude fallback
also fails, the affected direction is reported as blocked rather than as a
semantic no-go.

Malformed relay URL handling, including nonnumeric loopback ports, top-level
Anthropic message ID validation, and mixed primary/fallback second-turn
accounting remain outside the closed eight-row semantic matrix; they are
recorded as known-by-design residuals rather than promoted into probe gates.

The closed matrix requires monotonic Responses wire sequence, not a particular
origin. The measured relay emitted contiguous one-based values. Non-stream
response media-type enforcement is likewise outside the closed semantic matrix;
both are recorded as residuals in the survey rather than promoted into probe
gates. Fixture-owned historical manifest resolution, minimal allowlisting of
the probe child environment, and non-stream inference deadline classification
are also outside the closed matrix. The current launcher verifies v7.2.95 from
the product manifest and inherits the caller environment; these limitations are
explicit residuals rather than semantic success claims.
