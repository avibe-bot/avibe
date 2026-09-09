# Model Hub native terminal compatibility

Verification date: September 9, 2026.

This records the isolated native audit supporting the transport mapping in
[bounded quiet recovery](model-hub-retry-experience.md#native-terminal-compatibility-decision).
It is not evidence of an integrated controller, managed-engine, browser, or
Incus acceptance pass.

## Isolation and procedure

The audit ran installed Codex 0.153.2, Claude Code 2.1.263, and OpenCode 1.18.18
against an ephemeral Node HTTP mock bound to `127.0.0.1`. A macOS Seatbelt
sandbox denied network access except the mock's exact port, writes outside the
temporary audit root except `/dev/null`, reads from the real agent/Avibe homes
and Keychains, and securityd access. An outside-root write was observed to fail
while a scratch-root write succeeded.

Each native child received a fresh environment whitelist and separate temporary
home, Codex home, Claude config directory, XDG config/cache/data/state directories,
and temporary directory. Authentication values were explicitly non-secret mock
fixtures. No production credentials, paid inference, running service operations,
or user configuration changes were involved.

The mock recorded timestamps, method, path, model, streaming mode, and initial
system input. It returned a synthetic model catalog for model discovery and the
following JSON for inference, with `Content-Type: application/json` and
`Cache-Control: no-store`:

```json
{
  "type": "error",
  "error": {
    "type": "model_hub_recovery_exhausted",
    "code": "model_hub_recovery_exhausted",
    "message": "Automatic recovery has ended. Try again or choose another model."
  }
}
```

The final cases used neither `Retry-After` nor `x-should-retry`. Delayed cases
withheld all headers for 121,000 ms before returning that terminal response.
OpenCode title generation received an immediate response and was counted
separately from the main call using the distinct title-generator system input.

Only Responses and Anthropic Messages were exercised by the native audit.
Chat Completions is covered by adapter/gateway loopback tests, not by this native
binary evidence.

## Native settings exercised

Codex used `exec --json --ephemeral --skip-git-repo-check`, a synthetic local
model catalog, and an isolated custom provider with `wire_api="responses"`,
`requires_openai_auth=false`, `supports_websockets=false`, and its base URL and
fixture token directed solely at the mock. The significant retry overrides
were:

```toml
model_providers.audit.request_max_retries = 0
features.unbounded_connection_retries = false
```

`stream_max_retries` remained unchanged.

Claude used `-p --output-format stream-json --verbose`, the fixture model,
`--setting-sources '' --no-session-persistence --tools ''`,
`--strict-mcp-config --mcp-config '{"mcpServers":{}}'`, and a mock
`ANTHROPIC_BASE_URL`/fixture auth token. Auto-update, telemetry, error reporting,
and nonessential traffic were disabled. `CLAUDE_CODE_MAX_RETRIES`, API timeouts,
and non-streaming fallback were not overridden for this audit. The main cases
set the retry watchdog to zero; a separate 424 case enabled it. Therefore these
results prove the terminal response itself is sufficient for that Claude
version, independently of the implementation's additional Hub-only retry limit.

OpenCode used `run --format json`, one enabled synthetic provider/model,
`permission={"*":"deny"}`, and disabled auto-update and model fetching. Provider
options contained only the mock URL and a non-secret token fixture. The
Responses cases used `@ai-sdk/openai`; the Anthropic cases used
`@ai-sdk/anthropic`. No global native retry behavior was changed.

## Observed results

These counts are main inference requests, not model discovery or title calls.

| Native binary / protocol | HTTP 424 | HTTP 422 | HTTP 400 |
| --- | ---: | ---: | ---: |
| Codex 0.153.2 / Responses | 6 | 6 | 1 |
| Claude Code 2.1.263 / Messages | 1 | 1 | 1 |
| OpenCode 1.18.18 / Responses | 1 | 1 | 1 |

Codex 424/422 reported five reconnects before failing. Codex 400 immediately
reported `turn.failed`.

| Additional case | Main requests | Title requests | Process elapsed |
| --- | ---: | ---: | ---: |
| Codex 400 after 121-second header delay | 1 | 0 | 121,089 ms |
| Claude 424 after 121-second header delay | 1 | 0 | 121,652 ms |
| OpenCode Responses 424 after 121-second header delay | 1 | 1 | 123,522 ms |
| OpenCode Anthropic 424, immediate | 1 | 1 | 2,259 ms |
| OpenCode Anthropic 400, immediate | 1 | 1 | 1,683 ms |
| Claude 424 with retry watchdog enabled | 1 | 0 | 585 ms |

All listed cases exited with code 1, without a harness timeout or terminating
signal. Immediate-matrix elapsed times were not retained and are not inferred.

OpenCode preserved `APIError`, integer `data.statusCode=424`,
`data.isRetryable=false`, and a JSON-string `data.responseBody` containing the
closed envelope. Claude preserved the status and neutral message but classified
the custom type as `error: unknown` and `terminal_reason: api_error`.

## Explanation and evidence limits

Version-pinned source inspection identified the relevant native ownership:

- Codex `rust-v0.153.2`: `codex-rs/codex-api/src/api_bridge.rs` maps HTTP 400
  to `InvalidRequest`, while 424/422 become `UnexpectedStatus`.
  `codex-rs/protocol/src/error.rs` defines retryability.
  `codex-rs/core/src/session/turn.rs` and `responses_retry.rs` apply the outer
  stream retry budget independently of provider HTTP retries.
- Codex `codex-rs/model-provider-info/src/lib.rs` separates request and stream
  retry settings; the audited default stream budget was five.
- OpenCode `v1.18.18`: `packages/opencode/src/session/retry.ts` checks status,
  native retryability, and message/body patterns. The closed neutral envelope
  did not match its retry patterns. `session/message-v2.ts` preserves the
  structured native error used by Avibe's narrowly scoped `continue` bypass.
- Claude's rolling environment-variable documentation is not a public,
  version-pinned classifier. Its classification evidence here is the actual
  installed 2.1.263 binary.

The audit's temporary harness and model catalog were deleted during cleanup.
This document preserves the procedure, significant options, and observed
results, not an immediately runnable retained harness. Reproduction requires
recreating the isolated catalog and OS sandbox.

The integrated implementation still needs its own exact-head tests and the
declared Incus/managed-engine/IM/browser acceptance. Native compatibility must
be rechecked when supported native versions change. A lost terminal response
does not create a cross-retry request identity, so this evidence is not an
exactly-once delivery guarantee.
