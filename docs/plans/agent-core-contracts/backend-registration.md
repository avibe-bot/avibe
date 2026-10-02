# C-8 Backend registration

Adding `avibe` touches every place that lists backends. Today those lists are literals scattered through the tree,
and they mean two different things. This contract makes the difference explicit.

## 1. Two sets

| Set | Members | Meaning |
| --- | --- | --- |
| **Agent backends** | `claude`, `codex`, `opencode`, `avibe` | anything an Avibe Agent can run on: catalog, routing, settings, Model Hub supply and routes, turn provenance, Agent Run targets, IM agent pickers |
| **Native CLI backends** | `claude`, `codex`, `opencode` | backends that are an external CLI: CLI path discovery, CLI auth and OAuth setup, native session listing and resume, CLI process restart, Model Hub launch overlays for native CLIs |

`avibe` has no CLI, CLI auth, native session store, or native fork. It must never be added to a native-CLI list, and
it must be added to every agent-backend list.

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

One contract test: every backend list in the tree is exactly one of the two sets from the catalog. A new literal list
fails the test until it imports a set or is classified.
