// Generated from modules/agents/catalog.py; do not edit.
// Regenerate with: uv run python -m scripts.generate_agent_backend_catalog
export const BACKEND_CATALOG = [
  {
    "id": "avibe",
    "display_name": "Vibey",
    "config_key": "avibe",
    "builtin_agent_name": "vibey",
    "default_cli": null,
    "default_enabled": true,
    "latest_probe": null,
    "capabilities": {
      "supports_cli": false,
      "supports_native_sessions": false,
      "supports_runtime_refresh": false,
      "supports_web_oauth": false,
      "supports_install": false
    },
    "builtin": true,
    "description_key": "settings.backends.avibeDescription",
    "settings_route": "/settings/backends/avibe"
  },
  {
    "id": "opencode",
    "display_name": "OpenCode",
    "config_key": "opencode",
    "builtin_agent_name": "opencode",
    "default_cli": "opencode",
    "default_enabled": true,
    "latest_probe": [
      "github",
      "sst/opencode"
    ],
    "capabilities": {
      "supports_cli": true,
      "supports_native_sessions": true,
      "supports_runtime_refresh": true,
      "supports_web_oauth": true,
      "supports_install": true
    },
    "builtin": false,
    "description_key": "settings.backends.opencodeDescription",
    "settings_route": "/settings/backends/opencode"
  },
  {
    "id": "claude",
    "display_name": "Claude Code",
    "config_key": "claude",
    "builtin_agent_name": "claude",
    "default_cli": "claude",
    "default_enabled": true,
    "latest_probe": [
      "npm",
      "@anthropic-ai/claude-code"
    ],
    "capabilities": {
      "supports_cli": true,
      "supports_native_sessions": true,
      "supports_runtime_refresh": true,
      "supports_web_oauth": true,
      "supports_install": true
    },
    "builtin": false,
    "description_key": "settings.backends.claudeDescription",
    "settings_route": "/settings/backends/claude"
  },
  {
    "id": "codex",
    "display_name": "Codex",
    "config_key": "codex",
    "builtin_agent_name": "codex",
    "default_cli": "codex",
    "default_enabled": false,
    "latest_probe": [
      "npm",
      "@openai/codex"
    ],
    "capabilities": {
      "supports_cli": true,
      "supports_native_sessions": true,
      "supports_runtime_refresh": true,
      "supports_web_oauth": true,
      "supports_install": true
    },
    "builtin": false,
    "description_key": "settings.backends.codexDescription",
    "settings_route": "/settings/backends/codex"
  }
] as const;
