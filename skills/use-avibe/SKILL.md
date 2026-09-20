---
name: use-avibe
slug: use-avibe
description: Safely inspect and modify local Avibe configuration, routing, runtime settings, watches, scheduled tasks, Avibe Cloud remote access, and operational state. Also use it to report an Avibe bug, send product feedback, or file a feature request from the current conversation.
version: 0.8.0
---

# Use Avibe

Use this skill when the user asks you to configure, repair, explain, or operate a local Avibe installation.

Typical requests include:

- enable a Slack, Discord, Telegram, Lark/Feishu, or WeChat scope
- route one channel or DM user to OpenCode, Claude, or Codex
- set a working directory for a channel or DM user
- choose a backend model, subagent, or reasoning level
- show or hide intermediate message types
- configure an outbound proxy (`proxy_url`) for an IM platform that cannot reach its API directly
- pair, start, stop, or inspect Avibe Cloud remote Web UI access
- create, update, inspect, pause, resume, or remove a managed background watch with `vibe watch`
- create, inspect, run, pause, resume, or remove a scheduled task with `vibe task`
- run a one-shot Agent job with `vibe agent run`, including async background runs
- inspect or cancel concrete Agent Run records with `vibe runs`
- check or apply Avibe updates (`vibe check-update`, `vibe upgrade`)
- inspect logs, run doctor, check service status, or explain where Avibe stores state
- decide whether a requested change belongs in Avibe config or in the host backend's own config
- report an Avibe bug, send product feedback, or file a feature request from the current conversation ("report this to Avibe", "submit a bug", "request a feature")

Follow this skill as an operations playbook for agents, not as end-user marketing copy.

## Core Rules

1. Prefer the Web UI API for Avibe configuration changes. Do not hand-edit config files for routine work.
2. Read current API state before mutating. Merge the user's requested change into the current payload.
3. Preserve unrelated scopes, platforms, users, and secrets.
4. Treat secrets as opaque. Do not print, invent, rotate, or overwrite tokens unless the user explicitly provides replacements.
5. Use the smallest viable API call and verify by reading back the API response.
6. For `POST /settings`, preserve every existing channel for that platform; the endpoint replaces the platform's channel map.
7. For `POST /api/users`, merge each edited user with its current user payload first; missing user fields are not a patch.
8. Make every persistent-state change through the Web UI API or the `vibe` CLI. Avibe's internal storage is opaque — do not read, query, or hand-edit it.
9. `POST /config` persists the new payload but does not restart running platform adapters by itself. When the change is platform credentials, `proxy_url`, or other transport-level settings, plan an explicit restart afterwards; prefer the delayed CLI form (`vibe restart --delay-seconds 60`) when triggering it from inside an active conversation. The only credential save that restarts on its own is the WeChat QR-login completion through `POST /wechat/qr_login/poll`.
10. Do not restart the service by default. Use `POST /doctor`, `GET /status`, and read-back checks first.
11. Only start, stop, restart, or reload Avibe when the user explicitly asks or when a change cannot take effect otherwise; explain why before doing it.
12. If an agent must restart Avibe from an active conversation, use `vibe restart --delay-seconds 60` so the current session can receive the reply before the restart lands.
13. Tell the user whether the change is global or scope-specific.

## API First Workflow

Use this order when changing Avibe configuration:

1. Determine the Web UI base URL.
   - Default is `http://127.0.0.1:5123`.
   - If the user has a custom UI host or port (from `ui.setup_host` / `ui.setup_port`), use that exact origin.
   - When Avibe Cloud remote access is active, the public origin (e.g. `https://<slug>.avibe.bot`) also speaks the same API and requires OIDC session cookies — prefer the local origin from the host running Avibe.
   - Check liveness with `GET /health` or `GET /status`.
2. Decide whether the request belongs in:
   - `POST /config` for global defaults, platform credentials, runtime config, agent defaults, UI config, remote-access provider settings, update policy, or global display toggles
   - `POST /settings` for channel-level routing, working directory, visibility, enablement, and mention policy
   - `/api/users` and `/api/bind-codes` for DM user binding and user-scope settings
   - `/remote-access/*` for Avibe Cloud pairing and tunnel control
   - host backend config instead of Avibe when the request is OpenCode, Claude Code, or Codex native behavior
3. Fetch the current state from the matching GET endpoint.
4. Merge the requested change in memory.
5. Send the mutating request through the Web UI API with CSRF protection.
6. Read back the changed resource and verify the effective payload.
7. Run `POST /doctor` only when the change affects runtime health, platform credentials, or backend availability.
8. Report the changed scope or global keys and whether a restart was avoided or still required.

## Task References

Read only the reference needed for the current task, before composing its calls.
Paths below are relative to this Skill directory; scripts remain under
`scripts/`, not under `references/`.

| Task | Reference |
| --- | --- |
| Send API requests with CSRF, cookies, JSON payloads, or the bundled helper | [API client](references/api-client.md) |
| Change global defaults, platform credentials, proxies, or discover IM channels | [Global configuration and platforms](references/global-config.md) |
| Change channel/DM routing, models, cwd, visibility, mention policy, or bind users | [Scopes and routing](references/scopes-and-routing.md) |
| Pair, inspect, repair, or control the Avibe Cloud tunnel | [Remote access](references/remote-access.md) |
| Run or delegate Agent work, schedule tasks, manage watches or Session queues | [Agent Harness](references/harness.md) |
| Choose backend/subagent settings or change native OpenCode, Claude, or Codex config | [Backends](references/backends.md) |
| Inspect runtime paths, logs, or health; restart or upgrade the service | [Operations and troubleshooting](references/operations.md) |
| Turn a problem or a wish in this conversation into a bug report, feedback, or feature request | [Feedback and feature requests](references/feedback.md) |

For multi-step API work, use the bundled `scripts/vibe_api.py` helper.
The API client reference contains its exact invocations and fetch/merge examples.

## Safety Boundaries

Always follow these constraints:

- never delete unrelated platform scopes
- never blank out tokens or secrets as part of an unrelated config task
- never claim a backend feature exists if current Avibe behavior does not support it
- never read, query, or hand-edit Avibe's internal state storage — go through the Web UI API or `vibe` CLI
- never expose bind codes, pairing keys, tunnel tokens, instance secrets, or session secrets unless the user explicitly asks
- never paste a credentialed `proxy_url` (`user:pass@host`) back into chat — mask the credentials portion when echoing the value
- always say when a requested change actually belongs in OpenCode, Claude Code, or Codex config instead of Avibe
- never publish anything derived from this conversation until that exact content, destination, and posting identity are authorized — a destination is a host, a repository, and an acting account, never whatever the environment happens to supply; sanitize first, and keep the authorization to one concrete yes rather than a prompt per step; ordinary reads and generic public searches are already covered by the task
- never splice a generated or user-supplied value into a shell command string; pass it as its own argument

## Escalation

Some requests are not local maintenance at all. Switch to the feedback workflow in [Feedback and feature requests](references/feedback.md) when:

- the behavior looks like a real bug rather than a local misconfiguration
- the user is asking for a feature Avibe does not support yet
- backend integration behavior appears inconsistent with the documented configuration surface
- the user simply asks to report something or request a change, with or without prior diagnosis

That reference owns the whole path: gathering facts from this conversation, drafting a short English report, sanitizing it, getting approval, and submitting it to `https://github.com/avibe-bot/avibe` through an already authorized channel. Do not stop at "open an issue yourself", and do not treat diagnosis, a restart, or a repair as a precondition for filing.

When the user has approved the sanitized content and public consent, use the
official route in the feedback reference when commissioning has marked it
available. Its packaged `scripts/feedback_intake.py` helper is distributed
inside this existing Skill by the normal wheel `skills/` inclusion and
built-in snapshot publication path. A development checkout or a deployed
receiver alone does not update a client's installed Skill. The helper is bound
to the operator's HTTPS Show share and `cs-agent-bot` posting identity; it
creates one UUIDv4, persists the exact payload in its local outbox, and reads
the separate public receipt before claiming an Issue URL. `resume <request_id>`
only reads receipts; explicit delivery recovery requires saved POST429 evidence
and a fresh minimal404.

If the user wants to contribute back with code, suggest a pull request in that repository.

## Response Pattern

When you complete an Avibe maintenance task, report back with:

1. which API endpoint changed the state
2. whether the change is global or scope-specific
3. which keys changed
4. the read-back or doctor evidence
5. whether a restart was avoided, deferred, or still required and why

A feedback or feature-request task reports something different: the verified issue URL, or the finished draft plus an explicit statement that it was not submitted.
