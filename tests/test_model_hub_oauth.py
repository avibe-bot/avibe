from __future__ import annotations

import json

from core.handlers.model_hub.native_oauth import _signed_in
from core.handlers.model_hub.oauth import OAuthFlowRegistry


def test_flow_registry_persists_experimental_consent(tmp_path):
    path = tmp_path / "oauth_flows.json"
    registry = OAuthFlowRegistry(path)

    registry.remember(
        "oaf_consent01",
        "hub",
        "src_consent01",
        "anthropic",
        experimental_consent=True,
    )

    binding = OAuthFlowRegistry(path).binding("oaf_consent01")
    assert binding is not None
    assert binding.experimental_consent is True
    assert binding.completed is False
    assert json.loads(path.read_text(encoding="utf-8"))["oaf_consent01"]["experimental_consent"] is True

    OAuthFlowRegistry(path).complete("oaf_consent01")
    completed = OAuthFlowRegistry(path).binding("oaf_consent01")
    assert completed is not None
    assert completed.completed is True


def test_flow_registry_defaults_legacy_bindings_to_no_consent(tmp_path):
    path = tmp_path / "oauth_flows.json"
    path.write_text(
        json.dumps(
            {
                "oaf_legacy01": {
                    "channel": "hub",
                    "source_id": "src_legacy01",
                    "vendor": "anthropic",
                }
            }
        ),
        encoding="utf-8",
    )

    binding = OAuthFlowRegistry(path).binding("oaf_legacy01")
    assert binding is not None
    assert binding.experimental_consent is False
    assert binding.completed is False


def test_flow_registry_returns_latest_pending_reauth_for_source(tmp_path):
    registry = OAuthFlowRegistry(tmp_path / "oauth_flows.json")
    registry.remember(
        "oaf_first001",
        "native_cli",
        "src_native001",
        "anthropic",
        intent="reauth",
    )
    registry.remember(
        "oaf_second01",
        "native_cli",
        "src_native001",
        "anthropic",
        intent="reauth",
    )
    registry.remember(
        "oaf_create01",
        "hub",
        "src_native001",
        "anthropic",
    )
    registry.complete("oaf_second01")

    pending = registry.pending_reauth("src_native001")

    assert pending is not None
    assert pending[0] == "oaf_first001"

    registry.remember(
        "oaf_replaced1",
        "native_cli",
        "src_native001",
        "anthropic",
        intent="reauth",
        replace_flow_id="oaf_first001",
    )

    assert registry.binding("oaf_first001") is None
    replaced = registry.pending_reauth("src_native001")
    assert replaced is not None
    assert replaced[0] == "oaf_replaced1"


def test_native_status_trusts_codex_keyring_success_but_not_active_api_keys():
    assert _signed_in(
        "codex",
        {
            "active_auth_mode": "none",
            "has_chatgpt_tokens": False,
            "auth_mode_uncertain": False,
        },
    )
    assert not _signed_in(
        "codex",
        {
            "active_auth_mode": "api_key",
            "has_chatgpt_tokens": True,
        },
    )


def test_native_status_does_not_override_explicit_claude_api_key_mode():
    assert not _signed_in(
        "claude",
        {
            "active_auth_mode": "api_key",
            "has_oauth_credentials": True,
        },
    )
    assert _signed_in("claude", {"has_oauth_credentials": True})
