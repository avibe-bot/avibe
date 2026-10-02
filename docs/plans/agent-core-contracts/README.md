# Agent core contracts

Status: **draft**, 2026-10-02. Shapes and semantics shared by the lanes in
[`../avibe-agent-core.md`](../avibe-agent-core.md) §7. A contract is frozen on `master` before the lanes that depend
on it fork; after that, a change needs orchestrator approval and lands here first.

| ID | Contract | Producer → consumer | Files | Frozen before |
| --- | --- | --- | --- | --- |
| C-1 | Canonical message model and cross-provider rules | `ai` → `agent`, `harness`, adapter | [`message.schema.json`](message.schema.json), [`cross-provider.md`](cross-provider.md) | P1 |
| C-2 | Provider adapter interface and stream events | `ai` → `agent` | [`provider.md`](provider.md), [`provider-event.schema.json`](provider-event.schema.json) | P1 |
| C-3 | Loop control: hooks, directives, steer, follow-up, fork | `agent` → adapter, `harness`, tests | [`loop-control.md`](loop-control.md) | P1 |
| C-4 | Agent event stream and its mapping to Avibe outputs | `agent` → adapter | [`agent-event.schema.json`](agent-event.schema.json), [`loop-control.md`](loop-control.md) §6 | P2 |
| C-5 | Transcript rows, `context_seq`, projection, fork | adapter ↔ `harness` | [`transcript.md`](transcript.md), [`transcript-rows.schema.json`](transcript-rows.schema.json) | P2 |
| C-6 | Model Hub consumer extension | Model Hub → adapter | [`model-hub-consumer.md`](model-hub-consumer.md), [`hop-resolution.schema.json`](hop-resolution.schema.json) | P1 |
| C-7 | Tools, output governance, job handle | `tools` → `agent`, adapter | [`tools.md`](tools.md), [`job.schema.json`](job.schema.json) | P1 |
| C-8 | Backend registration | catalog → every backend list | [`backend-registration.md`](backend-registration.md) | P2 |
| C-9 | Context management | `harness` → `agent`, adapter | plan §5.2; rows in [`transcript-rows.schema.json`](transcript-rows.schema.json) | P3 |

Conventions:

- JSON Schema draft-07, like `../model-hub-contracts/`. Python implementations use dataclasses; the schemas describe
  the serialized form, which is what crosses a lane boundary or reaches disk.
- Persisted shapes carry `"version": 1`. Readers accept every released version (persisted-shape rule).
- Field names are `snake_case`, except the tool parameters in C-7, which keep Pi's names (`oldText`, `newText`,
  `replaceAll`).
- Protocol names use Model Hub's vocabulary, extended by C-6: `anthropic`, `openai_chat`, `openai_responses`,
  `google`.
