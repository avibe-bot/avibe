from __future__ import annotations

from modules.agents.catalog import (
    AGENT_BACKENDS,
    DEFAULT_AGENT_BACKEND,
    agent_backend_catalog_payload,
    default_cli_for_backend,
    default_enabled_for_backend,
    display_name_for_backend,
    is_agent_backend,
    latest_probe_for_backend,
    supports_install,
    supports_runtime_refresh,
    supports_web_oauth,
)
from vibe import api


def test_agent_catalog_is_backend_management_source_of_truth() -> None:
    assert AGENT_BACKENDS == ("opencode", "claude", "codex")
    assert DEFAULT_AGENT_BACKEND == "opencode"

    for backend in AGENT_BACKENDS:
        assert is_agent_backend(backend)
        assert supports_runtime_refresh(backend)
        assert supports_web_oauth(backend)
        assert supports_install(backend)
        assert default_cli_for_backend(backend)
        assert latest_probe_for_backend(backend) is not None

    assert display_name_for_backend("opencode") == "OpenCode"
    assert display_name_for_backend("claude") == "Claude Code"
    assert default_enabled_for_backend("codex") is False
    assert not is_agent_backend("unknown")
    assert not supports_runtime_refresh("unknown")
    assert not supports_web_oauth("unknown")
    assert not supports_install("unknown")


def test_agent_backend_catalog_payload_exposes_public_metadata() -> None:
    payload = agent_backend_catalog_payload()

    assert [item["id"] for item in payload] == list(AGENT_BACKENDS)
    assert payload[0]["settings_route"] == "/settings/backends/opencode"
    assert payload[0]["capabilities"]["supports_runtime_refresh"] is True


def test_api_exposes_agent_backend_catalog() -> None:
    payload = api.get_agent_backend_catalog()

    assert [item["id"] for item in payload["backends"]] == list(AGENT_BACKENDS)
