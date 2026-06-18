# Dynamic Channel Routing Design

> Historical design note: this document describes the pre-Agent-catalog backend
> routing model. Current Avibe routing is Agent-based: scopes select
> `routing.agent_name`, new sessions inherit the default Vibe Agent when no
> scope Agent is set, and `scope_settings.agent_backend` / `routing.agent_backend`
> are retained only as legacy compatibility fields. Existing Agent Sessions keep
> their own `agent_sessions.agent_backend` snapshot.

## Overview

Replace static file-based routing with dynamic per-channel routing configuration that users can change via Slack menus.

## Requirements

1. **Backend selection**: Users can switch between `claude`, `codex`, `opencode` per channel
2. **OpenCode-specific options**: 
   - Agent selection (build, plan, etc.) - from `/agent` API
   - Model selection - from `/config/providers` API
3. **Claude/Codex**: No model selection (use their defaults)
4. **Fallback**: channel routing overrides are persisted in `~/.vibe_remote/state/settings.json`
5. **Entry points**:
   - Slack: `/start` button "Switch Agent" + `/settings` modal

## Data Structure

### UserSettings (in `~/.vibe_remote/state/settings.json`)

```python
@dataclass
class ChannelRouting:
    agent_backend: Optional[str] = None  # "claude" | "codex" | "opencode" | None (use default)
    opencode_agent: Optional[str] = None  # "build" | "plan" | ... | None (use OpenCode default)
    opencode_model: Optional[str] = None  # "provider/model" | None (use OpenCode default)

@dataclass
class UserSettings:
    show_message_types: List[str] = ...  # empty list means hide all
    custom_cwd: Optional[str] = None
     channel_routing: Optional[ChannelRouting] = None

```

### JSON Representation

```json
{
  "C0A6U2GH6P5": {
    "show_message_types": ["assistant"],
    "custom_cwd": "/path/to/project",
    "channel_routing": {
      "agent_backend": "opencode",
      "opencode_agent": "build",
      "opencode_model": "anthropic/claude-opus-4-5"
    }
  }
}
```

## Routing Resolution Priority

```
1. channel_routing.agent_backend (from `~/.vibe_remote/state/settings.json`)
   ↓ if null
2. AgentRouter platform default
   ↓ if not found
3. AgentService.default_agent ("claude")
```

## API Design

### Controller

```python
class Controller:
    def resolve_agent_for_context(self, context: MessageContext) -> str:
        """Unified agent resolution with dynamic override support."""
        settings_key = self._get_settings_key(context)
        
        # Check dynamic override first
        override = self.settings_manager.get_channel_routing(settings_key)
        if override and override.agent_backend:
            # Verify the agent is registered
            if override.agent_backend in self.agent_service.agents:
                return override.agent_backend
        
        # Fall back to static routing
        return self.agent_router.resolve(self.config.platform, settings_key)
    
    def get_opencode_overrides(self, context: MessageContext) -> Tuple[Optional[str], Optional[str]]:
        """Get OpenCode agent and model overrides for this channel."""
        settings_key = self._get_settings_key(context)
        routing = self.settings_manager.get_channel_routing(settings_key)
        if routing:
            return routing.opencode_agent, routing.opencode_model
        return None, None
```

### SettingsManager

```python
class SettingsManager:
    def get_channel_routing(self, settings_key: str) -> Optional[ChannelRouting]:
        """Get channel routing override."""
        settings = self.get_user_settings(settings_key)
        return settings.channel_routing
    
    def set_channel_routing(self, settings_key: str, routing: ChannelRouting):
        """Set channel routing override."""
        settings = self.get_user_settings(settings_key)
        settings.channel_routing = routing
        self.update_user_settings(settings_key, settings)
```

### OpenCodeServerManager

```python
class OpenCodeServerManager:
    async def get_available_agents(self, directory: str) -> List[Dict]:
        """Fetch available agents from OpenCode server."""
        # GET /agent with x-opencode-directory header
        
    async def get_available_models(self, directory: str) -> Dict:
        """Fetch available models from OpenCode server."""
        # GET /config/providers with x-opencode-directory header
        # Returns: { providers: [...], default: {...} }
    
    async def get_default_config(self, directory: str) -> Dict:
        """Fetch current default config from OpenCode server."""
        # GET /config with x-opencode-directory header
```

## Slack UI

### /start Button Layout (Updated)

```
Row 1: [📁 Current Dir] [📂 Change Work Dir]
Row 2: [🔄 Clear All Session] [⚙️ Settings]
Row 3: [🤖 Switch Agent]  # NEW
Row 4: [ℹ️ How it Works]
```

### Routing Modal (New)

```
┌─────────────────────────────────────────┐
│  🤖 Agent & Model Settings              │
├─────────────────────────────────────────┤
│  Current: OpenCode (build)              │
│                                         │
│  ┌─────────────────────────────────┐   │
│  │ Backend                          │   │
│  │ [▼ OpenCode                    ] │   │
│  └─────────────────────────────────┘   │
│                                         │
│  ── OpenCode Options ──────────────    │
│                                         │
│  ┌─────────────────────────────────┐   │
│  │ Agent                            │   │
│  │ [▼ build (default)             ] │   │
│  └─────────────────────────────────┘   │
│                                         │
│  ┌─────────────────────────────────┐   │
│  │ Model                            │   │
│  │ [▼ (Default) anthropic/claude..] │   │
│  └─────────────────────────────────┘   │
│                                         │
│  💡 Leave as default to use OpenCode's │
│     configured settings.                │
│                                         │
│  [Cancel]                    [Save]     │
└─────────────────────────────────────────┘
```

### Modal Blocks Structure

```python
view = {
    "type": "modal",
    "callback_id": "routing_modal",
    "title": {"type": "plain_text", "text": "Agent Settings"},
    "submit": {"type": "plain_text", "text": "Save"},
    "close": {"type": "plain_text", "text": "Cancel"},
    "private_metadata": channel_id,
    "blocks": [
        # Header with current status
        {"type": "section", "text": {"type": "mrkdwn", "text": "Current: ..."}},
        {"type": "divider"},
        
        # Backend select
        {
            "type": "input",
            "block_id": "backend_block",
            "element": {
                "type": "static_select",
                "action_id": "backend_select",
                "options": [
                    {"text": {"type": "plain_text", "text": "Claude Code"}, "value": "claude"},
                    {"text": {"type": "plain_text", "text": "Codex"}, "value": "codex"},
                    {"text": {"type": "plain_text", "text": "OpenCode"}, "value": "opencode"},
                ],
                "initial_option": {...}
            },
            "label": {"type": "plain_text", "text": "Backend"}
        },
        
        # OpenCode Agent select (conditional - shown via update)
        {
            "type": "input",
            "block_id": "opencode_agent_block",
            "optional": True,
            "element": {
                "type": "static_select",
                "action_id": "opencode_agent_select",
                "options": [...],  # From /agent API
            },
            "label": {"type": "plain_text", "text": "OpenCode Agent"}
        },
        
        # OpenCode Model select (conditional)
        {
            "type": "input", 
            "block_id": "opencode_model_block",
            "optional": True,
            "element": {
                "type": "static_select",  # or external_select if too many
                "action_id": "opencode_model_select",
                "options": [...],  # From /config/providers API
            },
            "label": {"type": "plain_text", "text": "Model"}
        },
        
        # Tip
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "💡 ..."}]}
    ]
}
```

## OpenCode Agent Message Flow

```
1. User sends message in Slack channel
2. MessageHandler.handle_user_message()
   → controller.resolve_agent_for_context(context) → "opencode"
   → controller.get_opencode_overrides(context) → ("build", "anthropic/claude-opus-4-5")
3. AgentRequest created with overrides attached
4. OpenCodeAgent.handle_message(request)
   → Uses request.opencode_agent, request.opencode_model instead of config defaults
5. OpenCodeServerManager.send_message(..., agent=override_agent, model=override_model)
```

## Implementation Steps

### Phase 1: Data & Routing
1. [x] Add `ChannelRouting` dataclass to `settings_manager.py`
4. [x] Add `resolve_agent_for_context()` to Controller
5. [x] Update all callers of `agent_router.resolve()` to use new method

| `modules/settings_manager.py` | Add `ChannelRouting`, routing methods |
| `core/controller.py` | Add `resolve_agent_for_context()`, `get_opencode_overrides()` |

- [x] Clearing routing falls back to default backend

- [ ] Restart preserves routing settings
