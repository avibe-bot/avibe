from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NativeSessionProviderSpec:
    agent_name: str
    module_path: str
    class_name: str


DEFAULT_PROVIDER_SPECS: tuple[NativeSessionProviderSpec, ...] = (
    NativeSessionProviderSpec(
        agent_name="opencode",
        module_path="modules.agents.native_sessions.opencode",
        class_name="OpenCodeNativeSessionProvider",
    ),
    NativeSessionProviderSpec(
        agent_name="claude",
        module_path="modules.agents.native_sessions.claude",
        class_name="ClaudeNativeSessionProvider",
    ),
    NativeSessionProviderSpec(
        agent_name="codex",
        module_path="modules.agents.native_sessions.codex",
        class_name="CodexNativeSessionProvider",
    ),
    NativeSessionProviderSpec(
        agent_name="antigravity",
        module_path="modules.agents.native_sessions.antigravity",
        class_name="AntigravityNativeSessionProvider",
    ),
)
