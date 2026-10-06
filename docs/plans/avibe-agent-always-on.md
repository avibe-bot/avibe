# Avibe Agent is the built-in backend

## Owner decision (2026-10-06)

The Avibe Agent is Avibe's own agent and takes part in the platform's core operation. It is listed first in every
backend list, it is always enabled, and it cannot be turned off.

## Change contract

- **One descriptor fact.** `AgentBackendDescriptor.builtin` marks the built-in backend (`avibe`).
  `BUILTIN_AGENT_BACKENDS` and `is_builtin_backend()` derive from it; the generated UI catalog projects it as
  `builtin`. No consumer special-cases the id.
- **Order.** `AGENT_BACKEND_REGISTRY` lists `avibe` first, and registry order is display order:
  Settings → Backends, the Models page routes (`MODEL_HUB_BACKENDS` is now the registry order, whose ordering had no
  processing meaning), the Agent create picker and Agents page groups (`agentOrder`), and IM routing pickers.
- **Not the default.** `DEFAULT_AGENT_BACKEND` stays `opencode`. Every fallback that picks a backend or Agent nobody
  chose ranks the built-in backend last (`_implicit_default_rank` in `core/vibe_agents.py`, the IM routing modal's
  fallback selection), so it is chosen only when nothing else is enabled.
- **Always on, by construction.** `agents.avibe` no longer exists: `AvibeAgentConfig`, its compat projection and every
  branch reading it are removed. A config written by an earlier build loads unchanged otherwise and drops the key on
  its next save. The controller registers the adapter at startup and never unregisters it; runtime renewal is a no-op
  for it.
- **Built-in Agent.** `ensure_builtin_default_agents` always includes the built-in backend and re-enables its Agent, so
  the row exists and is enabled when controller startup's sync returns (Model Hub seeds the Avibe supply onto it
  next). `VibeAgentStore.update` refuses to disable it (`agent_always_enabled`) and keeps its built-in markers; its
  model, effort, prompt and description stay editable.
- **Refusals.** `POST /api/config` with `agents.avibe` returns 400; `PATCH /api/agents/avibe` with `enabled: false`
  returns 400 `agent_always_enabled`; the CLI reports the same code.
- **UI.** The built-in backend shows a "Built-in" badge where other backends have their enable switch, in the list and
  on its page. The built-in Avibe Agent's enable switch is locked on.
- **Model gateway off.** Stopping the Model Hub runtime stays allowed. The backend stays enabled; its row says
  "Gateway off", its page explains that it has no model to run on and links to Models, and a Turn refuses with
  `errors.modelGatewayOff`, which names the gateway and Models, from the shared model gate or the adapter's preflight.

## Known by design

- The setup wizard lists only native CLIs, which need detection, install or sign-in. The Avibe Agent needs none, so
  the wizard does not show it; this lane adds no wizard card.
- The Agents page detail panel and the setup flow mirror `is_builtin_default_agent` from Agent metadata, as
  `setupTargets.isBuiltinAgent` already does.
- Making the Avibe Agent the default for new chats is a separate owner decision.
