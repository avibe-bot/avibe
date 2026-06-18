# Vibe Agent Catalog

## Background

Vibe Remote used to treat "agent" mostly as backend-specific routing data.
The old scope backend route field is now deprecated and ignored.

That was useful while each backend had a different subagent model, but it is not
the right product abstraction. Scope is becoming the durable project/workspace
unit in Vibe Remote. Each scope should select one Vibe-owned Agent, and that
Agent should define how work is handled.

This plan introduces a Vibe Agent catalog as a first-class data model and CLI
surface.

## Goals

- Add a Vibe-owned Agent data structure.
- Let scopes select an Agent by name.
- Move backend, model, effort, description, and system prompt into Agent
  definitions.
- Add `vibe agent` commands for CRUD and import.
- Support importing existing global agents from Claude Code, Codex, and
  OpenCode.
- Stop treating backend-native subagents as scope routing fields.
- Keep the run definition/session design simple: new sessions resolve their
  runtime target from the scope's configured Vibe Agent.

## Scope

This plan covers the Agent catalog, scope-to-Agent resolution, and import
workflow. Backend/model/effort are owned by Agent definitions; task, watch, and
run commands select Agents by name.

Imported agents become Vibe-owned Agent definitions. They keep source metadata
for traceability, but they are not live-linked to the original backend files in
V1.

## Core Data Model

### Agent

Minimum persisted shape:

```text
agents
  id
  name
  description
  backend
  model
  reasoning_effort
  system_prompt
  source
  source_ref
  metadata_json
  created_at
  updated_at
```

Field semantics:

| Field | Meaning |
| --- | --- |
| `id` | Stable internal id. Can be UUID/short id; not the user-facing selector. |
| `name` | Unique Vibe Agent name used by scopes and CLI. |
| `description` | Human-readable purpose and selection guidance. |
| `backend` | `opencode`, `claude`, or `codex`. |
| `model` | Backend model override for this Agent; nullable means backend default. |
| `reasoning_effort` | Backend effort/reasoning override; nullable means backend default. |
| `system_prompt` | Vibe-owned system prompt appended or injected into backend runtime. |
| `source` | `manual`, `imported_claude`, `imported_codex`, `imported_opencode`, etc. |
| `source_ref` | Original source path/name/id for imported agents. |
| `metadata_json` | Extension data such as tools, import details, or backend-specific hints. |
| `created_at` | Agent creation timestamp. |
| `updated_at` | Latest Agent update timestamp. |

Name rules:

- Names are case-insensitive for lookup.
- Store a normalized name for uniqueness.
- Recommended display names may preserve user casing, but the CLI selector
  should be stable and shell-friendly.
- Names should not encode backend. Backend is a field, not part of identity.
- Names are globally unique. Scope-private Agents are not a Vibe-layer concept;
  backend-specific private/subagent behavior remains owned by the backend.

### Scope Routing

Scope settings should move toward:

```text
scope_settings
  scope_id
  agent_name
  workdir
  enabled
  require_mention
  display settings...
```

Scope routing is now represented by one `agent_name` reference plus optional
model, reasoning, and subagent overrides.

Resolution rules:

1. If a command explicitly passes `--agent <name>`, load that Vibe Agent.
2. Otherwise, resolve the incoming or delivery `scope_id`.
3. Load `scope_settings.agent_name`.
4. Load the named Vibe Agent.
5. Use Agent backend/model/effort/system prompt for the turn.
6. Use scope workdir unless a future Agent model explicitly owns cwd.

If a scope has no selected Agent and no command-level `--agent` was provided,
fall back to a system default Agent. The system default should be explicit in
config/state, not inferred from scattered backend defaults forever.

### System Default Agent

Fresh installs and migrations should have an explicit system default Agent:

```text
default_agent_name -> agents.name
```

The first version should create an Agent row named `default` and point
`default_agent_name` to it. Its backend/model/effort can be generated once from
the currently enabled global backend defaults. Runtime should not keep
dynamically synthesizing Agents from legacy backend-specific Scope fields.

Because Agent `name` and `backend` are immutable, changing the system default
backend later should create another Agent and move `default_agent_name` to that
Agent. This keeps default behavior as first-class Agent configuration instead
of hidden fallback logic.

### Agent Sessions

`agent_sessions` should store the resolved Vibe Agent identity:

```text
agent_sessions
  agent_name
  agent_id
  agent_backend
  model
  reasoning_effort
  ...
```

`agent_backend`, `model`, and `reasoning_effort` remain useful snapshots for
history/debugging even when the source of truth is the Agent definition.

Existing sessions should use the latest Agent definition on future turns.
Because `name` and `backend` are immutable, live updates can change prompt,
model, effort, description, and metadata without changing the backend family of
an established session.

## CLI Design

### Command Family

```bash
vibe agent list
vibe agent show <name>
vibe agent create ...
vibe agent update <name> ...
vibe agent remove <name>
vibe agent import ...
```

The command family manages Vibe Agent definitions. Direct/manual execution lives
under `vibe agent run` and is covered by `agent-run-harness.md`.

### `vibe agent list`

Purpose: list Vibe Agents available for scope routing and session creation.

Suggested flags:

```bash
vibe agent list
vibe agent list --backend codex
vibe agent list --json
```

Output fields:

- name;
- description preview;
- backend;
- model;
- reasoning effort;
- source;
- updated time.

### `vibe agent show`

Purpose: inspect one full Agent definition.

```bash
vibe agent show release-reviewer
```

Output should include full system prompt unless a future `--redact-prompt` flag
is needed. System prompts are user-authored configuration, not secrets by
default.

### `vibe agent create`

Purpose: create a Vibe Agent manually.

Candidate shape:

```bash
vibe agent create release-reviewer \
  --backend codex \
  --description "Reviews release diffs and deployment risk." \
  --model gpt-5.4 \
  --effort high \
  --system-prompt-file agents/release-reviewer.md
```

Notes:

- `--backend` is required.
- `--description` is recommended.
- `--model` and `--effort` are optional.
- System prompt can come from `--system-prompt` or `--system-prompt-file`.
- If no prompt is provided, the Agent is still valid but uses only backend
  defaults and Vibe Remote's standard prompt injection.

### `vibe agent update`

Purpose: modify an Agent definition without changing the selected scopes.

Candidate shape:

```bash
vibe agent update release-reviewer --model gpt-5.5
vibe agent update release-reviewer --effort xhigh
vibe agent update release-reviewer --description "..."
vibe agent update release-reviewer --system-prompt-file agents/release-reviewer.md
```

Update rules:

- `name` cannot be changed.
- `backend` cannot be changed.
- `description`, `model`, `reasoning_effort`, `system_prompt`, and metadata can
  be changed.
- Existing sessions resolve the latest Agent definition immediately, so edits
  affect future turns in already-created sessions.

### `vibe agent remove`

Purpose: remove a Vibe Agent definition.

Rules:

- Refuse removal if any scope references the Agent unless `--force` is passed.
- With `--force`, affected scopes should be moved to the default Agent or left
  unset with a warning. The better first implementation is to refuse and require
  explicit reassignment.

### `vibe agent import`

Purpose: import existing backend-native global agents into the Vibe Agent
catalog.

Candidate commands:

```bash
vibe agent import --from claude
vibe agent import --from codex
vibe agent import --from opencode
vibe agent import --from claude --name reviewer
vibe agent import --from codex --all
vibe agent import --file reviewer.md --backend codex
```

Import rules:

- Import should read global agent definitions from the backend's normal global
  agent location for `--from`.
- V1 imports global agents only. Project-local agents are out of scope.
- `--file <path>` imports one explicit file in the common markdown-with-header
  format. Because that file is backend-neutral at the path level, `--backend`
  is required with `--file`.
- Imported Vibe Agent names should be unique. On conflict, skip the import and
  report the skipped name clearly.
- Imported definitions become Vibe-owned copies.
- Source metadata should record backend, original name, and original path/id.
- Backend-specific tool permissions should be stored in `metadata_json` only.
  V1 does not enforce them.

Backend mapping:

| Source | Expected fields |
| --- | --- |
| Claude Code global agent | name, description, system prompt, optional model/tools metadata |
| Codex global agent | name, description, developer instructions/system prompt, optional model/effort |
| OpenCode global agent | name, description, prompt/instructions, optional model metadata |

The exact parsing should reuse existing discovery/parsing helpers where
available, but the persisted output should be backend-neutral.

## UI And Scope Settings

The UI and IM settings flows should stop exposing separate backend-native
subagent dropdowns as the main routing model.

New shape:

- Agent selector: one Vibe Agent dropdown.
- Agent detail preview: description, backend, model, effort.
- Workdir remains a scope setting.
- Backend credential/config availability remains global backend configuration.

This makes scope configuration easier to explain:

```text
Scope = project/workspace.
Agent = how this scope should think and act.
Session = one ongoing conversation/run under that scope and Agent.
```

## Runtime Behavior

### Human Messages

For a human turn:

1. Resolve scope.
2. Resolve configured Vibe Agent.
3. Build `AgentRequest` with:
   - `agent_name` or `vibe_agent_name`;
   - backend;
   - model;
   - reasoning effort;
   - system prompt.
4. Route to the backend selected by the Agent.

Prefix-triggered quick switching should target Vibe Agent names if it is added
later. Routing is based on Vibe Agent resolution, not backend-native subagent
prefix parsing.

### Background New Sessions

For `--create-session --deliver-key <scope-id>`:

1. Resolve `scope_id`.
2. Resolve scope's Vibe Agent.
3. Reserve or create `agent_sessions.id`.
4. Store the resolved Agent identity on the session.
5. Execute the first turn through that Agent.
6. Return the new `session_id` in command output.

For `--create-session-per-run`, repeat that flow for each run.

### Existing Sessions

For `--session-id <id>`:

1. Load the existing session.
2. Use the session's stored Agent identity to load the current Agent definition.
3. Apply the latest Agent prompt/model/effort on future turns.

This gives users a simple mental model: editing a Vibe Agent changes how that
Agent behaves everywhere it is selected, including existing sessions.

### System Prompt Composition

The Agent system prompt replaces backend-native subagent prompt selection at the
Vibe routing layer. Vibe Remote should inject:

```text
agent_system_prompt + vibe_remote_instructions
```

through the same prompt injection point already used for Vibe Remote
instructions. Backend-native default prompts still exist underneath the backend.
Vibe Remote uses the selected Vibe Agent's system prompt as its owned prompt
layer.

## Output Contract

`vibe agent` commands should use the same JSON style as the planned background
commands.

Create output:

```json
{
  "ok": true,
  "agent": {
    "name": "release-reviewer",
    "description": "Reviews release diffs and deployment risk.",
    "backend": "codex",
    "model": "gpt-5.4",
    "reasoning_effort": "high",
    "source": "manual",
    "updated_at": "2026-05-19T15:00:00+00:00"
  },
  "warnings": []
}
```

Import output:

```json
{
  "ok": true,
  "imported": [
    {
      "name": "reviewer",
      "backend": "codex",
      "source": "imported_codex",
      "source_ref": "~/.codex/agents/reviewer.md"
    }
  ],
  "skipped": [],
  "warnings": []
}
```

List output:

```json
{
  "agents": [
    {
      "name": "release-reviewer",
      "description": "Reviews release diffs and deployment risk.",
      "backend": "codex",
      "model": "gpt-5.4",
      "reasoning_effort": "high",
      "source": "manual",
      "updated_at": "2026-05-19T15:00:00+00:00"
    }
  ]
}
```

## Storage Options

### Preferred

Add SQLite tables:

```text
agents
scope_settings.agent_name
agent_sessions.agent_id / agent_name snapshot fields
```

This aligns with the direction that scopes and sessions already live in SQLite.

### Compatibility Window

The existing JSON settings shape can keep its old fields during the transition,
but new code should treat `agent_name` as the source of truth.

No migration is required for old backend-native agent fields per product
decision. Existing users may need to select/import/create Vibe Agents again.

## Relationship To Existing Plans

- `agent-run-harness.md`: background/direct `--create-session` resolves the
  runtime target through the scope's Vibe Agent, then records actual execution
  in `agent_runs`.
- `agent-run-harness.md`: `vibe agent run` is the direct/manual execution
  surface. It consumes Vibe Agent definitions from this catalog instead of
  accepting backend/model/effort overrides.
- Delivery targeting is owned by `agent-run-harness.md`: `--deliver-key`
  becomes `scopes.id`, not a legacy session key.

## Implementation Slices

1. Add Agent storage model and CRUD service.
2. Add `vibe agent list/show/create/update/remove`.
3. Add import discovery for one backend first, then Claude/Codex/OpenCode.
4. Add `scope_settings.agent_name` and scope routing resolution.
5. Update runtime message routing to resolve Vibe Agents.
6. Update UI/IM settings to select Vibe Agents.
7. Update background session creation to use scope Agent resolution.
8. Remove old backend-native agent routing from primary UI/docs.

## Specification Summary

1. Agent names are globally unique.
2. Agent edits affect existing sessions immediately.
3. Agent `name` and `backend` are immutable; other fields are editable.
4. The Agent system prompt is prepended before Vibe Remote instructions at the
   existing prompt injection point.
5. Imported backend-specific tool permissions are stored in `metadata_json` only
   and are not enforced in V1.
6. `vibe agent import --from ...` imports global agents only in V1.
7. Import name conflicts are skipped and reported.
8. `vibe agent import --file <path> --backend <backend>` imports one explicit
   markdown-with-header file.
9. Fresh installs and migrations should create an explicit system default Agent
   and reference it through `default_agent_name`; runtime should not synthesize
   legacy backend default Agents dynamically.
