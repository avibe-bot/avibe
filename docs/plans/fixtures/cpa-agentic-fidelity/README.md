# CPA agentic-fidelity fixture

This directory is a self-contained, standard-library-only probe for the M0
spike in `model-hub-engine-survey.md`. It sends the same small agentic workload
families through the four client/upstream combinations covered by the charter:

- Anthropic Messages client to an OpenAI Responses source;
- OpenAI Responses client to an Anthropic Messages source;
- Anthropic Messages client to an OpenAI Chat Completions source;
- OpenAI Chat Completions client to an Anthropic Messages source.

The CPA process and its source configuration are owned by the existing Model
Hub runtime pipeline. The probe only talks to a loopback gateway and never
reads Avibe configuration, CLI login state, or auth files. It does not print
request or response bodies.

## Run

The real spike must be run with both vendor keys supplied by Vault and with a
fresh CPA instance configured with one API-key source per target protocol. The
following names are intentionally only environment variable names; their
values must be injected by Vault or by the isolated runtime launcher:

```sh
vibe vault run --env ANTHROPIC_API_KEY,OPENAI_API_KEY -- \
  env CPA_BASE_URL=http://127.0.0.1:15220 \
      CPA_GATEWAY_TOKEN=runtime-token \
      CPA_ANTHROPIC_QUALIFIED_MODEL=avibe-src123/claude-model \
      CPA_OPENAI_RESPONSES_QUALIFIED_MODEL=avibe-src456/openai-responses-model \
      CPA_OPENAI_CHAT_QUALIFIED_MODEL=avibe-src789/openai-chat-model \
  python3 docs/plans/fixtures/cpa-agentic-fidelity/probe.py
```

The example model ids and token are placeholders. Because managed CPA uses
`force-model-prefix: true`, every model value must be the source-qualified
`<private-prefix>/<model>` assembled by the isolated runtime; bare upstream
model ids are rejected before any request. The probe also exits before any
request when either vendor variable or the gateway token is absent. In this M0
run both Vault entries were absent, so no CPA process or upstream request was
started.

The output is a compact JSON report containing only case names, statuses,
redacted semantic checks, tool names/counts, and stream event counts. It never
prints request/response bodies, tool-call ids, arguments, text, environment
values, or credentials. Each case makes two sequential exchanges: the first
response supplies the observed tool-call ids and continuation, and the second
request returns tool results using those exact ids. The eight cases include
single and parallel calls plus streaming and tool-fragment reconstruction in
both directions of both conversion pairs. A semantic failure exits non-zero;
an absent credential or unsafe endpoint exits with a blocked status before
network access.

Anthropic thinking is enabled with the minimum valid manual budget (1024) and
`max_tokens` greater than that budget. Its first request uses `tool_choice:
auto` so the thinking/tool-use combination remains valid; the continuation
preserves the complete assistant content returned by the first response.
