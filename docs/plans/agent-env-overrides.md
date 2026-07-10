# Per-Agent Env Overrides (`metadata.env`)

## Background

Claude backend authentication is a single global choice today: `agents.claude.auth_mode`
(`oauth` | `api_key`) plus `~/.claude/settings.json` decide the credentials for every
Claude subprocess (`vibe/claude_config.py::build_claude_subprocess_env`). Two Agents on
the `claude` backend therefore always share one identity. Users who want a subscription
(OAuth) instance and an API-key instance side by side — e.g. burst work on API billing
while daily work stays on the subscription — cannot express that, even though the
Claude CLI itself supports it (an inherited `ANTHROPIC_API_KEY` suppresses the OAuth
credential store per process).

## Goal

Multiple Agents on one backend with **isolated authentication** and **shared
everything else** (backend binary, `~/.claude` settings, skills, permission config).
No new database schema, no new global config surface.

## Solution

Reuse the existing free-form Agent `metadata` JSON: an `env` object is treated as
per-Agent env overrides for the backend subprocess.

- `core/vibe_agents.py`
  - `AGENT_ENV_METADATA_KEY = "env"`
  - `vibe_agent_name_from_platform_payload(payload)` — resolves the acting Agent from
    `resolved_vibe_agent` (IM routing) or `agent_session_target.agent_name`
    (agent run / tasks / watches).
  - `resolve_agent_env_overrides(agent, base_env=...)` — pure resolver: string
    coercion, `None`/blank-key skipping, `"${NAME}"` indirection from the daemon env
    (unset references are skipped with a warning, never injected as empty).
- `core/handlers/session_handler.py`
  - `_vibe_agent_env_overrides(context)` — payload → store lookup → resolver.
  - `_create_claude_session` merges the overrides on top of
    `build_claude_subprocess_env(...)`, before caller-context identity env and the
    owner marker (identity keys stay authoritative). Key names are logged, values are
    not.
  - Both cached-session reuse paths compare the client's recorded `_vibe_agent_env`
    and recreate the session when overrides changed, mirroring the existing
    `_vibe_caller_env` invalidation contract.

Ordering: `build_claude_subprocess_env` → Agent overrides → caller-context env →
`AVIBE_CLAUDE_PROCESS_OWNER_ENV` / `IS_SANDBOX`.

## Constraints and Notes

- Claude Code applies the `settings.json` `env` block on top of process env at launch;
  per-Agent `ANTHROPIC_API_KEY` therefore requires global claude auth to stay in OAuth
  mode (which keeps that block credential-free). Documented in `docs/COMMANDS.md`.
- Scope: wired for the `claude` backend. The resolver lives in `core/` and is
  backend-agnostic so `codex` (`CODEX_HOME`) / `opencode` can adopt the same metadata
  contract in a follow-up.
- Vault integration (e.g. `${vault:NAME}` value forms) is a possible follow-up; `${NAME}`
  daemon-env indirection covers the secret-hygiene need without new coupling.

## Todo

- [x] resolver + payload helpers in `core/vibe_agents.py`
- [x] claude spawn merge + cached-session invalidation in `core/handlers/session_handler.py`
- [x] unit tests `tests/test_agent_env_overrides.py` (17 cases)
- [x] docs: `docs/COMMANDS.md` + `docs/COMMANDS_ZH.md`
- [ ] follow-up: codex/opencode wiring
- [ ] follow-up: mask `metadata.env` values in `vibe agent show` output
