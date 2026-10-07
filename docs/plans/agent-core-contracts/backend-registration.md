# C-8 Backend registration

Adding `vibey` touches every place that lists backends. Today those lists are literals scattered through the tree,
and they mean two different things. This contract makes the difference explicit.

## 1. Two sets

| Set | Members | Meaning |
| --- | --- | --- |
| **Agent backends** | `claude`, `codex`, `opencode`, `vibey` | anything an Avibe Agent can run on: catalog, routing, settings, Model Hub supply and routes, turn provenance, Agent Run targets, IM agent pickers |
| **Native CLI backends** | `claude`, `codex`, `opencode` | backends that are an external CLI: CLI path discovery, CLI auth and OAuth setup, native session listing and resume, CLI process restart, Model Hub launch overlays for native CLIs |

`vibey` has no CLI, CLI auth, native session store, or native fork. It must never be added to a native-CLI list, and
it must be added to every agent-backend list.

`vibey` is also the one **built-in** backend (`AgentBackendDescriptor.builtin`, owner decision 2026-10-06): part of the
platform, so it is always enabled. It has no `agents.vibey` config section and no enable switch, the controller
registers it at startup and never unregisters it, and its built-in Agent cannot be disabled. Registry order is display
order, and the built-in backend comes first. Being listed first does not make it the default: any fallback that picks
a backend or Agent nobody chose ranks it last ([`avibe-agent-always-on.md`](../avibe-agent-always-on.md)).

## 2. One declaration

`modules/agents/catalog.py` declares both sets. Every other list imports them; literals of backend names are removed.
`core/vibe_agents.py`'s `SUPPORTED_AGENT_BACKENDS` folds into the catalog. Model Hub's JSON contracts keep literal
enums (JSON Schema has no imports); a contract test compares them with the catalog.

## 3. Inventory to classify (`master` at `21fb3c3ab`)

Found by searching for the three names as a list or set; the P2 adapter lane re-runs the search at its base and
classifies every hit.

- Likely agent-backend lists: `config/v2_config.py` (route backend `Literal` and validation),
  `config/v2_settings.py`, `core/controller.py` (backend registration, target backend check),
  `core/message_dispatcher.py`, `core/services/agent_run_target.py`, `modules/agents/model_hub.py`,
  `modules/im/slack.py`, `modules/im/discord.py`, `modules/im/feishu.py` (agent pickers), Model Hub contract enums
  (`agent-supply`, `agent-chain`, `probe-result`, `turn-provenance`, `resolution-event`, `source`).
- Likely native-CLI lists: `vibe/cli_paths.py`, `core/agent_auth_service.py`, `modules/agents/native_sessions/types.py`,
  `core/backend_restart.py`.

## 4. Test

One contract test over declarations that stand for a whole universe: each must equal one of the two catalog sets.
Capability-specific sets that are legitimately different, such as `core/handlers/model_hub/events.py`'s
`EventAgent` (agent backends plus `system`) or the two-vendor credential set in
`core/handlers/model_hub/migration.py`, are listed in the test with their classification (subset or superset of which
set, and why); they are checked for staying inside that relation, not for equality. An unclassified new literal list
fails the test until it imports a set or is classified.
