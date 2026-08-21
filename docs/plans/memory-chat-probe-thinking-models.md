# Memory chat probes for reasoning models

Status: approved by owner. This document is the implementation contract for
one small PR. Update it only if scope changes materially.

## Background

Gemini's OpenAI-compat endpoint
(`https://generativelanguage.googleapis.com/v1beta/openai`) with current-gen
thinking models (`gemini-3.5` / `3.6` / `3.7-flash`, and
`gemini-3-flash-preview`, EverOS's official multimodal default) answers Avibe's
chat probe (`max_tokens=8`, `temperature=0`) with a 2xx body that has no
`content` key at all:

```json
{
  "choices": [
    {
      "finish_reason": "length",
      "index": 0,
      "message": {"role": "assistant"}
    }
  ],
  "usage": {"completion_tokens": 0}
}
```

Probe latency jitters 0.85–4.78s and occasionally exceeds 5s.

Two Avibe-side failures follow:

1. `_chat_probe_response_issue` in `core/memory/everos.py` rejected a missing
   `content` key as `provider_response_missing_content`, even though a
   present-but-empty `content` is already accepted when `role == "assistant"`
   and `finish_reason` is terminal (`stop`, `length`, `content_filter`,
   `tool_calls`, `function_call`).
2. `_PREFLIGHT_TIMEOUT_SECONDS = 5.0` produced intermittent
   `provider_request_timed_out` on save.

EverOS itself has no such probe. This is Avibe's admission layer only.
Preflight and periodic health share the same chat-response validator for both
`llm` and `multimodal` endpoints.

## Goal

Admit reasoning-model OpenAI-compat chat probes that omit `content` when the
rest of the existing empty-content contract holds, and give save-path preflight
enough wall-clock budget for observed Gemini jitter.

## Changes

1. **Validator tolerance.** In `_chat_probe_response_issue`, a missing
   `content` key is treated exactly like `content: null` and falls through to
   the existing empty-content branch: `role` must be `"assistant"` and
   `finish_reason` must be a terminal string. All other rejections stay
   unchanged (missing `choices` / `message`, invalid types, non-terminal or
   missing `finish_reason`).
2. **Preflight budget.** `_PREFLIGHT_TIMEOUT_SECONDS` is `30.0` (owner
   decision). It still bounds both `asyncio.wait_for` per endpoint and
   `httpx.Timeout(..., connect=2.0)`. `vibe/internal_client.py`
   `memory_preflight` uses `timeout=None`, so the IPC hop does not cap this.
   Worst-case save-path preflight is 4 endpoints × 30s.
3. **Periodic health budget is unchanged.**
   `PROCESSING_PROBE_REQUEST_TIMEOUT_SECONDS` stays `8.0`.

## Non-goals

- Do not raise `_CHAT_PROBE_MAX_TOKENS` (stays 8).
- Do not add `reasoning_effort` or any provider-specific request params to
  probe payloads. Probes stay lowest-common-denominator OpenAI-compat.
- No config schema change, no UI change, no i18n change, no sidecar / EverOS
  change.
- Do not touch `enable_llm_rerank`, agentic recall, or rebuild / recovery
  behavior.

## Known notes

- **30s preflight is an owner decision**, not a measured minimum. Observed
  Gemini jitter only needed slightly more than 5s; 30s is the granted
  headroom, not a claim that probes take that long.
- **Preflight (30s) / health (8s) asymmetry.** A provider that only answers
  after more than 8s will pass save preflight and may still flap periodic
  health. That split is intentional in this PR: health remains a short
  liveness check.
- **Admission risk.** A provider that responds but never produces text now
  passes admission when `role` is `assistant` and `finish_reason` is terminal.
  Real generation failures surface later in the Processing Record, not at
  save-path preflight.

## Diagnostic-code shift

A payload that used to fail as `provider_response_missing_content` because the
`content` key was absent now continues into the empty-content branch. When
`role == "assistant"` and `finish_reason` is also absent, the reported code is
now `provider_response_missing_finish_reason`.

## Tests

- Validator unit fixtures for the Gemini thinking shape, the missing-content
  diagnostic shift, a non-assistant role, and the existing OpenAI
  `content` / `""` / `null` shapes.
- Preflight contract tests: stubbed `llm` and `multimodal` endpoints returning
  the thinking shape pass `EverOSPort.preflight()`.
- Health: `processing_healthy()` judges the same stub healthy.
- The preflight constant is pinned at `30.0`; the health probe budget stays
  `8.0`.
