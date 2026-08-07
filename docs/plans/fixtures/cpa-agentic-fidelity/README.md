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
for bounded 503 recovery. The values are injected into private runtime state
and child-process environments only.

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
`max_tokens` above that budget. OpenAI Responses uses low reasoning effort.
The probe records whether the requested reasoning signal remains observable. For
Chat, it accepts either explicit `reasoning_content` or a positive standard
`usage.completion_tokens_details.reasoning_tokens` value; it does not infer
equivalence from parameter names alone. Loopback requests disable environment
proxy handlers, and exhausted 503 capacity is reported as blocked for every
target while the alternate Claude model is tried only for Anthropic targets.

The live evidence in the survey used the owner-provided compatible relay. Direct
official vendor APIs were not measured. A transient `no available accounts`
503 is retried with short bounded backoff; after the configured Claude fallback
also fails, the affected direction is reported as blocked rather than as a
semantic no-go.
