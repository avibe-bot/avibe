# Harness Caller Context

## Goal

Harness commands invoked from inside an Agent turn should know the Avibe
Session/Run that triggered them without relying on prompt instructions. The
first-class contract is a unified environment variable:

- `AVIBE_SESSION_ID`: Avibe Agent Session that owns the current Agent turn.
- `AVIBE_RUN_ID`: Avibe Agent Run that owns the current Agent turn, when the turn
  came from Harness.
- `AVIBE_CALLER_SOURCE`: coarse source kind for the turn, such as `agent_run`,
  `task`, `watch`, or `callback`.
- `AVIBE_CALLER_BACKEND`: backend that is executing the caller turn.
- `AVIBE_NATIVE_SESSION_ID`: backend-native session/thread id for diagnostics.

Explicit CLI flags still win. Environment caller context is the defaulting and
provenance layer, not an override of user intent.

## Existing Storage

`agent_runs` already has the topology fields needed for the v1:

- `source_kind`
- `source_actor`
- `parent_run_id`
- `callback_session_id`
- `metadata_json`

So v1 should populate these consistently instead of adding a parallel topology
table.

## Backend Transports

- Claude: inject through `ClaudeAgentOptions.env`.
- Codex: inject through thread-scoped `shell_environment_policy.set` on
  `thread/start`, `thread/resume`, and `thread/fork`.
- OpenCode: inject through an Avibe-owned `shell.env` plugin. Until OpenCode has
  first-class hidden per-run metadata, Avibe writes a short-lived file binding
  keyed by OpenCode native session id. The plugin receives the native
  `sessionID` from OpenCode and uses that binding to return
  `AVIBE_SESSION_ID`/`AVIBE_RUN_ID` to the shell subprocess. This avoids
  guessing from Avibe's many-to-one native session mappings.

## CLI Semantics

`vibe agent run --async`:

- explicit `--callback-session-id` wins;
- explicit `--no-callback` means intentional no callback and prints tracking
  guidance;
- otherwise, if caller context has `AVIBE_SESSION_ID`, default callback to that
  session;
- otherwise fail early with an actionable error.

Synchronous `vibe agent run` does not require callback/no-callback flags.
