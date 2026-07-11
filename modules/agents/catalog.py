"""Shared agent backend capability catalog."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Final


@dataclass(frozen=True)
class AgentBackendCapabilities:
    supports_runtime_refresh: bool = True
    supports_web_oauth: bool = True
    supports_install: bool = True


@dataclass(frozen=True)
class AgentBackendDescriptor:
    id: str
    display_name: str
    config_key: str
    default_cli: str
    default_enabled: bool
    latest_probe: tuple[str, str] | None
    capabilities: AgentBackendCapabilities

    @property
    def description_key(self) -> str:
        return f"settings.backends.{self.id}Description"

    @property
    def settings_route(self) -> str:
        return f"/settings/backends/{self.id}"

    def to_public_dict(self) -> dict:
        payload = asdict(self)
        payload["description_key"] = self.description_key
        payload["settings_route"] = self.settings_route
        return payload


AGENT_BACKEND_REGISTRY: Final[dict[str, AgentBackendDescriptor]] = {
    "opencode": AgentBackendDescriptor(
        id="opencode",
        display_name="OpenCode",
        config_key="opencode",
        default_cli="opencode",
        default_enabled=True,
        latest_probe=("github", "sst/opencode"),
        capabilities=AgentBackendCapabilities(),
    ),
    "claude": AgentBackendDescriptor(
        id="claude",
        display_name="Claude Code",
        config_key="claude",
        default_cli="claude",
        default_enabled=True,
        latest_probe=("npm", "@anthropic-ai/claude-code"),
        capabilities=AgentBackendCapabilities(),
    ),
    "codex": AgentBackendDescriptor(
        id="codex",
        display_name="Codex",
        config_key="codex",
        default_cli="codex",
        default_enabled=False,
        latest_probe=("npm", "@openai/codex"),
        capabilities=AgentBackendCapabilities(),
    ),
}

AGENT_BACKENDS: Final[tuple[str, ...]] = tuple(AGENT_BACKEND_REGISTRY)
DEFAULT_AGENT_BACKEND: Final[str] = "opencode"
RUNTIME_REFRESH_BACKENDS: Final[frozenset[str]] = frozenset(
    descriptor.id
    for descriptor in AGENT_BACKEND_REGISTRY.values()
    if descriptor.capabilities.supports_runtime_refresh
)
WEB_OAUTH_BACKENDS: Final[frozenset[str]] = frozenset(
    descriptor.id
    for descriptor in AGENT_BACKEND_REGISTRY.values()
    if descriptor.capabilities.supports_web_oauth
)

_RUNTIME_REFRESH_SUCCESS_MESSAGES: Final[dict[str, str]] = {
    "opencode": "OpenCode restart accepted; active turns will drain before the server refreshes.",
    "claude": "Claude restart accepted; active turns will drain before sessions reconnect.",
    "codex": "Codex restart accepted; active turns will drain before transports refresh.",
}


def agent_backend_descriptors() -> list[AgentBackendDescriptor]:
    """Return backend descriptors in stable UI/routing order."""
    return list(AGENT_BACKEND_REGISTRY.values())


def get_agent_backend_descriptor(name: str) -> AgentBackendDescriptor:
    """Return the descriptor for *name* or raise ``ValueError``."""
    try:
        return AGENT_BACKEND_REGISTRY[name]
    except KeyError as err:
        raise ValueError(f"Unsupported agent backend: {name}") from err


def is_agent_backend(name: str) -> bool:
    """Return whether *name* is a known agent backend."""
    return name in AGENT_BACKEND_REGISTRY


def supported_agent_backend_set() -> set[str]:
    """Return the set of known agent backend ids."""
    return set(AGENT_BACKEND_REGISTRY)


def agent_backend_catalog_payload() -> list[dict]:
    """Return a JSON-safe public backend catalog."""
    return [descriptor.to_public_dict() for descriptor in agent_backend_descriptors()]


def default_cli_for_backend(name: str) -> str:
    """Return the default CLI command for *name*."""
    return get_agent_backend_descriptor(name).default_cli


def default_enabled_for_backend(name: str) -> bool:
    """Return whether *name* is enabled by default."""
    return get_agent_backend_descriptor(name).default_enabled


def display_name_for_backend(name: str) -> str:
    """Return the user-facing display name for *name*."""
    if name in AGENT_BACKEND_REGISTRY:
        return AGENT_BACKEND_REGISTRY[name].display_name
    return name.replace("_", " ").title()


def latest_probe_for_backend(name: str) -> tuple[str, str] | None:
    """Return latest-version probe metadata for *name*, when available."""
    if name not in AGENT_BACKEND_REGISTRY:
        return None
    return AGENT_BACKEND_REGISTRY[name].latest_probe


def supports_runtime_refresh(name: str) -> bool:
    """Return whether *name* supports runtime config refresh."""
    return name in RUNTIME_REFRESH_BACKENDS


def supports_web_oauth(name: str) -> bool:
    """Return whether *name* supports Web Settings OAuth setup."""
    return name in WEB_OAUTH_BACKENDS


def supports_install(name: str) -> bool:
    """Return whether *name* supports managed CLI install/upgrade."""
    if name not in AGENT_BACKEND_REGISTRY:
        return False
    return AGENT_BACKEND_REGISTRY[name].capabilities.supports_install


def runtime_refresh_success_message(name: str) -> str:
    """Return the success message for a refreshed backend runtime."""
    return _RUNTIME_REFRESH_SUCCESS_MESSAGES.get(
        name,
        f"{name} restart accepted; active turns will drain before runtime refresh.",
    )
