from __future__ import annotations

import pytest

from modules.agents.catalog import (
    AGENT_BACKENDS,
    NATIVE_CLI_BACKENDS,
    DEFAULT_AGENT_BACKEND,
    agent_backend_catalog_payload,
    default_cli_for_backend,
    default_enabled_for_backend,
    display_name_for_backend,
    is_agent_backend,
    is_builtin_backend,
    latest_probe_for_backend,
    supports_install,
    supports_runtime_refresh,
    supports_web_oauth,
)
from vibe import api


def test_agent_catalog_is_backend_management_source_of_truth() -> None:
    # The built-in backend leads every backend list; the default backend is unchanged.
    assert AGENT_BACKENDS == ("vibey", "opencode", "claude", "codex")
    assert DEFAULT_AGENT_BACKEND == "opencode"

    for backend in NATIVE_CLI_BACKENDS:
        assert is_agent_backend(backend)
        assert supports_runtime_refresh(backend)
        assert supports_web_oauth(backend)
        assert supports_install(backend)
        assert default_cli_for_backend(backend)
        assert latest_probe_for_backend(backend) is not None
        assert not is_builtin_backend(backend)

    assert display_name_for_backend("opencode") == "OpenCode"
    assert display_name_for_backend("claude") == "Claude Code"
    assert default_enabled_for_backend("codex") is False
    assert is_agent_backend("vibey")
    assert display_name_for_backend("vibey") == "Vibey"
    assert is_builtin_backend("vibey")
    assert default_enabled_for_backend("vibey")
    assert default_cli_for_backend("vibey") is None
    assert latest_probe_for_backend("vibey") is None
    assert not supports_runtime_refresh("vibey")
    assert not supports_web_oauth("vibey")
    assert not supports_install("vibey")
    assert not is_agent_backend("unknown")
    assert not supports_runtime_refresh("unknown")
    assert not supports_web_oauth("unknown")
    assert not supports_install("unknown")


def test_agent_backend_catalog_payload_exposes_public_metadata() -> None:
    payload = agent_backend_catalog_payload()

    assert [item["id"] for item in payload] == list(AGENT_BACKENDS)
    assert [item["builtin"] for item in payload] == [True, False, False, False]
    assert payload[1]["settings_route"] == "/settings/backends/opencode"
    assert payload[1]["capabilities"]["supports_runtime_refresh"] is True


def test_avibe_names_the_workbench_platform_and_no_agent_backend() -> None:
    """``avibe`` is the product and the Workbench platform id; the built-in backend is ``vibey``.

    One token meaning both a platform and a backend let one Session row carry platform=avibe and
    backend=avibe, and made the built-in the only backend whose Agent was not named after its id.
    """
    from config.platform_registry import WORKBENCH_PLATFORM_ID, supported_platform_set

    def strings(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from strings(item)
        elif isinstance(value, str):
            yield value

    assert WORKBENCH_PLATFORM_ID == "avibe" and "avibe" in supported_platform_set()
    assert not is_agent_backend("avibe")
    assert not any("avibe" in value for value in strings(agent_backend_catalog_payload()))
    assert [item["id"] for item in agent_backend_catalog_payload() if item["builtin"]] == ["vibey"]


def test_api_exposes_agent_backend_catalog() -> None:
    payload = api.get_agent_backend_catalog()

    assert [item["id"] for item in payload["backends"]] == list(AGENT_BACKENDS)


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_copy_about_the_built_in_names_it_from_the_catalog(lang) -> None:
    """Renaming the built-in is one catalog value: every message that names it takes that value."""
    from modules.agents.vibey.errors import error_text
    from vibe.i18n import t

    name = display_name_for_backend("vibey")
    for kind in ("aborted", "UnsupportedModelRoute", "ProviderProtocolViolation", None):
        text = error_text(kind, lang)
        assert name in text and "{backend}" not in text and "Avibe Agent" not in text
    for key in ("errors.modelHubDisabled", "errors.modelGatewayOff", "errors.builtinBackendConfig"):
        text = t(key, lang, backend=name, section="agents.vibey")
        assert name in text and "{backend}" not in text
    always_on = t("error.agentLifecycle.agent_always_enabled.message", lang, agent="vibey")
    assert "Avibe Agent" not in always_on and "`vibey`" in always_on
