# Backend Configuration

### Backend and local helper endpoints

- `GET /cli/detect?binary=<name-or-path>`
  - detects a CLI binary path
- `POST /agent/<name>/install`
  - `name` must be `opencode`, `claude`, or `codex`
- `POST /opencode/options`
  - payload: `{"cwd": "/path/to/repo"}`
  - returns OpenCode model, agent, and reasoning option data for that cwd
- `POST /opencode/setup-permission`
  - intentionally writes OpenCode native config to set `permission` to `allow`
- `GET /claude/agents?cwd=/path/to/repo`
- `GET /codex/agents?cwd=/path/to/repo`
- `GET /claude/models`
- `GET /codex/models`
- `POST /browse`
  - payload: `{"path": "~", "show_hidden": false}`

## Agent Backend Capability Matrix

Current Avibe Agent capabilities are:

| Backend | Select through Vibe Agent | Subagent | Model | Reasoning |
| --- | --- | --- | --- | --- |
| OpenCode | yes | yes | yes | yes |
| Claude | yes | yes | yes | yes |
| Codex | yes | yes | yes | yes |

Behavior notes:

- OpenCode subagents are selected through `routing.opencode_agent` or through prefix routing such as `reviewer: ...`.
- Claude subagents are selected through `routing.claude_agent` or prefix routing.
- Codex subagents are selected through `routing.codex_agent` or prefix routing.
- Claude reasoning is selected through `routing.claude_reasoning_effort`; common values are `low`, `medium`, and `high`, and some models also allow `max`.
- If a Claude reasoning value is invalid for the chosen model, the API normalizes or drops that override and falls back to the backend default.

## Subagent and Prefix Routing

If the user asks for subagents, remember:

- OpenCode, Claude, and Codex support prefix-triggered subagent selection like `planner: draft a migration plan`
- when a subagent definition provides its own default model or reasoning setting, that subagent-level value overrides the channel default
- Claude subagents are discovered from markdown files under:
  - `~/.claude/agents/`
  - project `.claude/agents/`
- Codex custom agents are discovered from TOML files under:
  - `~/.codex/agents/`
  - project `.codex/agents/`
- OpenCode subagent and model defaults come from the OpenCode runtime/config rather than only from Avibe's own config

## Host Backend Guidance

When the request belongs to the host backend, do not force it into Avibe config.

### OpenCode

Use OpenCode-native config when the user wants to change:

- personal default model
- global reasoning behavior
- provider and API keys
- MCP servers
- skills, plugins, tools, or project-local OpenCode behavior

Important locations:

- `~/.config/opencode/opencode.json`: global OpenCode config
- project `opencode.json`: project-level OpenCode config file
- `.opencode/`: project-local OpenCode config directory
- `~/.config/opencode/agents/`: global OpenCode agents
- `.opencode/agents/`: project-local OpenCode agents
- `~/.config/opencode/skills/`: global OpenCode skills
- `.opencode/skills/`: project-local OpenCode skills

Relevant docs:

- config: `https://opencode.ai/docs/config/`
- skills: `https://opencode.ai/docs/skills`
- plugins: `https://opencode.ai/docs/plugins/`
- MCP servers: `https://opencode.ai/docs/mcp-servers/`

Inside Avibe, a scope can select an OpenCode-backed Vibe Agent and then set subagent, model, and reasoning effort overrides. Use `POST /opencode/setup-permission` only for the specific permission helper.

### Claude Code

Use Claude-native config when the user wants to change:

- Claude subagent definitions
- Claude skills
- CLAUDE instructions and project rules

Important locations:

- `~/.claude/agents/`: global Claude subagents
- `.claude/agents/`: project subagents
- `~/.claude/skills/`: global Claude skills
- `.claude/skills/`: project skills

Relevant docs:

- subagents: `https://docs.anthropic.com/en/docs/claude-code/sub-agents`

Inside Avibe, a scope can select a Claude-backed Vibe Agent and then set model, subagent, and reasoning effort overrides.

### Codex

Use Codex-native config when the user wants to change:

- personal default model
- global reasoning defaults
- MCP servers, approvals, or sandbox policy
- Codex CLI profiles and behavior outside Avibe

Important locations:

- `~/.codex/config.toml`: global Codex config
- `.codex/config.toml`: project-local Codex config
- `~/.codex/agents/`: global Codex custom agents
- `.codex/agents/`: project-local Codex custom agents

Relevant docs:

- config basics: `https://developers.openai.com/codex/config-basic/`
- config reference: `https://developers.openai.com/codex/config-reference/`
- CLI overview: `https://developers.openai.com/codex/cli`
- subagents: `https://developers.openai.com/codex/subagents`

Inside Avibe, a scope can select a Codex-backed Vibe Agent and then set subagent, model, and reasoning effort overrides.
