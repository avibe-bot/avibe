from __future__ import annotations

import json

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
    assert json.loads(path.read_text(encoding="utf-8"))["oaf_consent01"]["experimental_consent"] is True


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
