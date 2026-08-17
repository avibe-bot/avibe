# OpenCode Active-Turn Timeout Becomes Opt-In

## Background

`docs/plans/opencode-active-turn-timeout.md` (#1197, fixing #1190) shipped a
90-minute wall-clock cap on accepted OpenCode turns. The incident's root cause
was OpenCode's ai-sdk runtime retrying exhausted-provider errors forever
without surfacing a terminal message, which left Avibe's error-driven
settlement nothing to observe.

Upstream has since fixed that class directly: OpenCode >= 1.18.x disables
ai-sdk internal retries (`maxRetries: 0`), runs its own bounded retry policy
(`RETRY_MAX_RETRIES = 5`, capped backoff), and writes the exhausted error onto
the assistant message, which Avibe's existing error path settles as a failed
terminal result. Verified against the locally installed 1.18.18 binary and the
dev-branch source (`packages/opencode/src/session/retry.ts`; fix commits
#40707, #41939).

The wall-clock cap therefore kills legitimate long turns while no longer
guarding a live failure mode. It also disagrees with the other backends, which
settle on surfaced errors rather than turn duration.

## Goal

- Default-disable the cap: `agents.opencode.active_turn_timeout_seconds`
  defaults to `0`, meaning no wall-clock bound.
- Keep the mechanism as an explicit operator opt-in; a positive value behaves
  exactly as before.
- Neutralize the legacy default echo at load: configs saved while the cap
  shipped carry `5400` because a settings save materializes the whole section,
  so exactly that value migrates to `0`. Any other persisted value is an
  explicit choice and survives.
- Non-positive, missing, or non-finite config values read as disabled; they
  must not silently fall back to a cap.

## Solution

- `config/v2_config.py`: `DEFAULT_OPENCODE_ACTIVE_TURN_TIMEOUT_SECONDS = 0`,
  `LEGACY_DEFAULT_OPENCODE_ACTIVE_TURN_TIMEOUT_SECONDS = 90 * 60`, and
  `_migrate_opencode_active_turn_timeout_on_load` wired into the load
  migration chain (in-memory only; idempotent).
- `modules/agents/opencode/poll_loop.py`: disabled timeout yields an infinite
  deadline; `wait_for` receives `None` for the infinite budget. Error-driven
  settlement, user stop, and shutdown cancellation are unchanged.
- UI: the Settings → Messaging field accepts `0` (minutes) and documents the
  disabled semantics; hint copy updated in en/zh.

## Scope

HFR-432 keeps its scenario ID and semantics for the opted-in lifecycle. The
default-disabled deadline has its own unit coverage beside the poll loop
(disabled-semantics seeding test plus a no-deadline prompt-poll test).
