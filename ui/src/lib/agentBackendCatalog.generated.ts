// Generated from modules/agents/catalog.py; do not edit.
// Regenerate with: uv run python -m scripts.generate_agent_backend_catalog
export const BACKEND_CATALOG = [
  {
    "id": "opencode",
    "display_name": "OpenCode",
    "config_key": "opencode",
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
    "description_key": "settings.backends.opencodeDescription",
    "settings_route": "/settings/backends/opencode"
  },
  {
    "id": "claude",
    "display_name": "Claude Code",
    "config_key": "claude",
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
    "description_key": "settings.backends.claudeDescription",
    "settings_route": "/settings/backends/claude"
  },
  {
    "id": "codex",
    "display_name": "Codex",
    "config_key": "codex",
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
    "description_key": "settings.backends.codexDescription",
    "settings_route": "/settings/backends/codex"
  },
  {
    "id": "avibe",
    "display_name": "Avibe Agent",
    "config_key": "avibe",
    "default_cli": null,
    "default_enabled": false,
    "latest_probe": null,
    "capabilities": {
      "supports_cli": false,
      "supports_native_sessions": false,
      "supports_runtime_refresh": false,
      "supports_web_oauth": false,
      "supports_install": false
    },
    "description_key": "settings.backends.avibeDescription",
    "settings_route": "/settings/backends/avibe"
  }
] as const;
