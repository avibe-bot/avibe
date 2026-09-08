# Codex Native User-Input Policy

## Ownership

Codex owns native tool registration. Avibe owns which native interaction
surfaces it exposes in the app-server processes it launches. Model capability
metadata and host interaction policy are separate concerns.

Avibe passes `-c tools.experimental_request_user_input.enabled=false` through
the existing final app-server configuration overrides, in both Direct and
Gateway mode. Earlier backend extra arguments cannot override this policy.
No user configuration, model catalog, prompt, route, or model selection is
rewritten. Existing unsupported synchronous request handling remains a
compatibility fallback, not the mechanism for hiding the tool.

This change does not restart an existing app-server or install a different
Codex binary. It applies when Avibe next launches an app-server normally.

## Asynchronous Gap

In Codex 0.153.2 the setting above removes `request_user_input` from the
model-visible tool list, but does **not** remove `request_user_input_async`.
The latter is registered independently when the model catalog advertises
`request_user_input_async` or its legacy name `send_user_message_async`.
The removed `features.send_async_message` flag does not disable it.
`features.default_mode_request_user_input` controls the synchronous tool's
available modes, not asynchronous registration.

The preferred completion is an upstream tool-registration opt-out, consumed
as a process argument by Avibe. The implementation must not replace the
complete model catalog to disguise a host policy as model capability.
The upstream request is
[openai/codex#43821](https://github.com/openai/codex/issues/43821).
Until an applicable upstream version supports this, asynchronous suppression
remains unresolved; this change is not a complete fix for missing async question
UI.

Upstream currently accepts issue reports and analysis, not external code PRs.
Related reports
[#43753](https://github.com/openai/codex/issues/43753),
[#43803](https://github.com/openai/codex/issues/43803), and
[#43057](https://github.com/openai/codex/issues/43057)
describe missing or disappearing asynchronous question interfaces.
Those reports do not establish the same root cause or a maintainer commitment
to an opt-out.

## Evidence and Boundaries

- Eight credential-free loopback Responses captures using the actual 0.153.2
  binary covered Default and Plan modes, with no input override, synchronous
  disable, the removed async flag, and both flags plus the Default-mode flag.
  Every capture kept async input exposed. Only synchronous disable removed
  synchronous input. The catalog bytes were identical across all cases.
- The captures preserved the remaining tool names and
  `parallel_tool_calls=true`. No real model calls or model-directed tool
  executions occurred. This does not prove background job completion.
- Transport unit coverage requires the explicit policy value and verifies
  host overrides follow conflicting backend arguments.
- The opt-in native prompt contract also checks the model-visible synchronous
  tool is absent, ordinary execution tools remain, and prompt refresh,
  process restart, legacy migration, and remote compaction still work.
  Its fixture is eligible for async input without requiring that upstream
  limitation to persist forever.
- Native contracts require `CODEX_PROMPT_CONTRACT_BINARY` to select a real
  binary. Ordinary CI runs unit coverage and skips this opt-in contract.
- No global async shutdown, prompt prohibition, model-capability filtering,
  gateway-only workaround, custom Codex fork, deployment, or user-state reset.
