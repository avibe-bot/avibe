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
makes two sequential exchanges: the first response supplies the observed
tool-call ids and complete continuation, and the second request returns tool
results using those exact ids. The eight cases cover single and parallel calls,
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
latest gate-complete rerun exercised all eight cases after the current-head
parser fixes: all eight completed both turns with HTTP 200 and no fallback was
used. Messages-to-Responses single failed both parse gates while its parallel
case passed every gate. Both Responses-to-Messages first responses lacked the
requested reasoning signal; the single final failed exact tuples and the
parallel final repeated tools with no final text, system markers, outputs, or
exact tuples. Both Messages-to-Chat cases failed the first and final parse gates
with thinking present; the single final also failed its exact tuple gate. Both
Chat-to-Messages cases lacked reasoning and failed final system scope; the
single final also failed its exact tuple gate.

## S4 matrix mapping

The eight S4 capability rows are closed to the following named probe cases and
assertions. A row marked not verified is an explicit residual, not an implicit
success claim.

| S4 capability row | Probe case or assertion |
| --- | --- |
| Single tool call | `_request` strict JSON plus `_validate_first`: `expected_tool_count`, `tool_names`, `tool_arguments`, `tool_ids_unique`, and protocol `stop_reason` |
| Parallel tools | `CaseSpec.expected_tools`, `_user_prompt(True)`, and the same `_validate_first` tool invariants |
| Multi-turn loop | `_run_case` observed `first_turn.tool_calls` plus `_validate_second`: `no_followup_tool_calls`, `tool_outputs`, and `_tool_output_pair_present` exact call-ID/result tuple association |
| Streaming text | `_request` strict UTF-8/JSON and `text/event-stream` checks, `_parse_anthropic_stream`/`_parse_responses_stream` `stream_text_delta_count` and snapshot comparison, `_parse_chat_stream` assistant-role preservation, `_stream_order_ok`, and `stream_complete` |
| Streaming tool fragments | `_parse_anthropic_stream`, `_parse_responses_stream`, `_parse_chat_stream`, and `_stream_order_ok` opening/terminal envelopes, error events, block/delta compatibility, item/choice/index/ID continuity, argument fragments, and monotonic lifecycle checks |
| System prompt | `_system_prompt` plus `_validate_second`: `system_marker` and `system_scope` |
| Thinking/reasoning | `_anthropic_payload`, `_responses_payload`, `_chat_payload`, `_parse_anthropic_document` payload/signature validation, `_parse_responses_stream` reasoning-delta reconstruction, `_reasoning_item_has_signal`, `_chat_reasoning_usage_present` integer-token gate, and `_validate_first`: `reasoning_present` plus `reasoning_not_visible` |
| Context length/truncation | `probe.CONTEXT_LENGTH_NOT_VERIFIED` residual; no low-cost context-limit run was included in M0 |

The live evidence in the survey used the owner-provided compatible relay. Direct
official vendor APIs were not measured. A transient `no available accounts`
503 is retried with short bounded backoff; after the configured Claude fallback
also fails, the affected direction is reported as blocked rather than as a
semantic no-go.
