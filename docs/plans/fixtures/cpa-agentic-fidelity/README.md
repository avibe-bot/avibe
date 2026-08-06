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
values must be injected by Vault:

```sh
vibe vault run --env ANTHROPIC_API_KEY,OPENAI_API_KEY -- \
  env CPA_BASE_URL=http://127.0.0.1:15220 \
      CPA_GATEWAY_TOKEN=runtime-token \
      CPA_ANTHROPIC_MODEL=claude-model \
      CPA_OPENAI_RESPONSES_MODEL=openai-responses-model \
      CPA_OPENAI_CHAT_MODEL=openai-chat-model \
  python3 docs/plans/fixtures/cpa-agentic-fidelity/probe.py
```

The example model ids and token are placeholders. The probe exits before any
request when either vendor variable is absent. In this M0 run both Vault
entries were absent, so no CPA process or upstream request was started.

The output is a compact JSON report containing only case names, HTTP status,
event-shape counts, and transport/termination failures. It deliberately does
not print semantic response fields; compare those locally against the matrix
in the survey and retain only a redacted result. The seven cases
include single and parallel tool calls, tool-result round trips, and streaming
requests in both directions of both conversion pairs.
