# Model Hub: accept unprefixed Claude Code models

## Background

Claude Code Model Hub catalog admission currently requires non-built-in Claude
model IDs to begin with `claude-` or `anthropic-`. That rejects explicitly
configured third-party IDs such as xAI's exact `grok-4.7`, even though the
Gateway route already names the upstream source and exact model.

## Goal

Allow canonical, credential-free Claude backend model IDs without a vendor
prefix. Persist and emit the exact ID, while retaining all existing identity,
duplicate/origin, `default` sentinel, route-target, native-vs-Gateway, and
source/model validation rules. Native Anthropic subscription routing must not
become an implicit fallback for an xAI model.

## Solution

- Remove only the Claude-specific prefix-only admission rule and its obsolete
  error path/copy; keep the shared canonical identifier validation.
- Leave route planning and execution exact-match behavior unchanged so a
  `grok-4.7` menu model resolves to an xAI Source's exact `grok-4.7` hop.
- Update focused API/catalog tests for save, catalog projection, resolution, and
  fake-upstream execution. Preserve invalid-ID and `default` rejection and
  existing prefixed-ID round trips.
- Do not change backend wire protocol, Claude Code discovery behavior, source
  auto-detection, or persisted schema unless contract prose contradicts the
  accepted identifier shape. Document any Claude Code `/model` auto-discovery
  filtering as a known-by-design residual if explicit Avibe launch remains
  exact and functional.

## Tests

- Red/green `set_agent_models` save of exact `grok-4.7` and catalog listing.
- Rejection of `default`, malformed/non-canonical, duplicate, and credential-
  bearing IDs; round-trip existing Claude aliases.
- Hub route resolution and mock upstream request asserting xAI receives bare
  `grok-4.7`, with no native Anthropic substitution.
- Relevant Python tests, changed-file Ruff, and UI tests/build only if UI copy
  changes are required.
