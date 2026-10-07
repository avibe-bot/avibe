# Avibe Agent is the built-in backend

## Owner decision (2026-10-06)

The Avibe Agent is Avibe's own agent and takes part in the platform's core operation. It is listed first in every
backend list, it is always enabled, and it cannot be turned off.

## Change contract

- **One descriptor fact.** `AgentBackendDescriptor.builtin` marks the built-in backend (`avibe`).
  `BUILTIN_AGENT_BACKENDS` and `is_builtin_backend()` derive from it; the generated UI catalog projects it as
  `builtin`. No consumer special-cases the id.
- **Order.** `AGENT_BACKEND_REGISTRY` lists `avibe` first, and registry order is the one display order:
  Settings → Backends, the Models page routes (`MODEL_HUB_BACKENDS` is now the registry order, whose ordering had no
  processing meaning), the Agent create picker, the Agents page groups and filter, the Global prompts tabs (the UI's
  `BACKEND_ORDER`, derived from the catalog; the separate `agentOrder` is gone), and IM routing pickers. Native
  backends therefore read OpenCode, Claude Code, Codex everywhere a backend list is shown.
- **Not the default.** `DEFAULT_AGENT_BACKEND` stays `opencode`. `implicit_default_rank` (`modules/agents/catalog.py`) is the
  one rule for a backend or Agent nobody chose: the built-in backend comes last, so it is chosen only when nothing else
  is enabled. Every implicit pick goes through it: the built-in sync's default, the effective-default fallbacks
  (`resolve_effective_default_agent`, `_effective_default_agent`), the per-principal fallback
  (`resolve_usable_default_agent`), the archived default's replacement after its same-backend preference, and the
  IM routing modal's fallback selection.
- **Always on, by construction.** `agents.avibe` no longer exists: `AvibeAgentConfig`, its compat projection and every
  branch reading it are removed. A config written by an earlier build loads unchanged otherwise and drops the key on
  its next save. The controller registers the adapter at startup and never unregisters it; runtime renewal is a no-op
  for it.
- **Built-in Agent.** `get_builtin_default_agent_for_backend` is the one owner of built-in identity: the row the store
  created for the backend (`source == "builtin"`, which no caller can write), under any name. A marked row wins, then
  the oldest; the legacy instance default (`default`) counts only if marked. The built-in markers (`builtin`,
  `builtin_default`, `lock_delete`, `backend_enabled`) are a projection only the store writes: `create` and `update`
  drop them from caller metadata and keep the row's own. `ensure_builtin_default_agents` always includes the built-in
  backend, restores the markers of whatever row the lookup names and re-enables it, so the row exists and is enabled
  when controller startup's sync returns (Model Hub seeds the Avibe supply onto it next). Without such a row it is
  created at the catalog's `builtin_agent_name` (`vibey`), or at the next free name (`vibey-2`) when a user's Agent
  holds `vibey`; a row an earlier build created as `avibe` stays the built-in. `VibeAgentStore.update` refuses to
  disable it (`agent_always_enabled`); its model, effort, prompt and description stay editable.
- **Backend choices route to the built-in.** A routing picker that chooses a backend saves that backend's built-in
  Agent's name (`VibeAgentStore.routing_name_for_backend`), never the backend id; a saved backend id that names no
  Agent resolves to the backend's built-in Agent.
- **Refusals.** `POST /api/config` with `agents.avibe` returns 400 (`errors.builtinBackendConfig`, in the configured
  language); `PATCH /api/agents/<built-in name>` (`vibey` on a fresh install) with `enabled: false` returns 400
  `agent_always_enabled`; the CLI reports the same code.
- **UI.** The built-in backend shows a "Built-in" badge where other backends have their enable switch, in the list and
  on its page. The built-in Agent's enable switch is locked on. Users meet the backend as Vibey (the catalog's
  `display_name`), drawn with the mascot mark (`BACKEND_BRAND_MARKS.avibe`).
- **Model Hub cannot supply.** `hub_supply_block(backend, hub_config)` (`config/v2_config.py`) is the one owner of why
  the Model Hub leaves a backend no model to run on now. The shared model gate and the adapter's preflight render its
  copy (`hub_supply_refusal`), `get_backend_connection` reports such a backend unready, and `GET /api/config` carries
  it as `agent_supply_blocks`, which the Backends pages show. The backend stays listed and enabled in both cases.
  - Gateway off (Models → Start/Stop): its row says "Gateway off", its page links to Models, and a Turn refuses with
    `errors.modelGatewayOff`.
  - Model Hub disabled on the instance (`VIBE_MODEL_HUB_ENABLED=0`): a backend without its own CLI gets every model
    through the Hub, so its row says "Model Hub disabled", its page says so without a Models link, and a Turn refuses
    with `errors.modelHubDisabled`.

## Known by design

- The setup wizard lists only native CLIs here. Its Avibe Agent card (built-in, model picker, providers destination)
  is a follow-up owner decision, delivered separately as `feat/avibe-agent-setup-wizard`.
- The setup wizard keeps its own native setup sequence (Claude Code, Codex, OpenCode: `nativeOrder`). It is an
  ordered onboarding flow, not a backend list: its intro geometry is indexed and its first ready backend decides the
  default Agent after setup, which this lane leaves unchanged.
- The Agents page detail panel mirrors `is_builtin_default_agent` from the Agent's `source` and markers;
  `setupTargets.isBuiltinAgent` reads the markers, which callers can no longer write.
- Making the Avibe Agent the default for new chats is a separate owner decision.
