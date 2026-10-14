"""V2 ``model_hub.hosted_tools`` (``vibey-hosted-tools.md`` §3.3, §12 item 8).

Hosted tools are an optional feature inside the Model Hub section. Released
configs never carry them, and a broken entry must turn off only that tool:
the rest of ``model_hub`` (Sources, Agents) has to load untouched, never through
the whole-section recovery reset.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

import pytest

from config.v2_config import ModelHubConfig, ModelHubHostedToolConfig, ModelHubRouteHopConfig, V2Config
from core.services.settings import default_config
from vibe import api

FIXTURES = Path(__file__).parent / "fixtures" / "model_hub"
SOURCE = {
    "id": "src_xai00001",
    "kind": "api_key",
    "vendor": "xai",
    "display_name": "xAI 密钥",
    "protocol": "openai_responses",
    "supply_channel": "hub",
    "billing": "metered",
    "state": {"status": "standby", "retry_at": None, "detail_key": None},
    "models": [{"id": "grok-4.6", "display_name": "Grok 4.6", "origin": "discovered", "reasoning_efforts": [], "reasoning_efforts_source": None}],
    "created_at": "2026-10-13T00:00:00Z",
    "credential_ref": "cred_src_xai00001",
}


def _section(hosted_tools) -> dict:
    section = ModelHubConfig.from_payload({"sources": [SOURCE]}).to_payload()
    if hosted_tools is not ...:
        section["hosted_tools"] = hosted_tools
    return section


def test_released_configs_load_with_every_tool_off_and_stay_without_the_section():
    section = json.loads((FIXTURES / "released_pre_vibey_consumer.json").read_text(encoding="utf-8"))
    config = ModelHubConfig.from_payload(section, repairing=True)
    assert config.hosted_tools == {}
    assert "hosted_tools" not in config.to_payload()


def test_hosted_tool_toggles_round_trip_in_canonical_order():
    tools = {
        "x_search": {"enabled": False, "executor": None},
        "web_search": {"enabled": True, "executor": {"source_id": "src_xai00001", "model_id": "grok-4.6"}},
    }
    config = ModelHubConfig.from_payload(_section(tools))
    assert config.hosted_tools == {
        "web_search": ModelHubHostedToolConfig(True, ModelHubRouteHopConfig("src_xai00001", "grok-4.6")),
        "x_search": ModelHubHostedToolConfig(False, None),
    }
    assert list(config.to_payload()["hosted_tools"]) == ["web_search", "x_search"]
    assert ModelHubConfig.from_payload(config.to_payload()).to_payload() == config.to_payload()


GOOD = {"enabled": True, "executor": None}
# (case, raw section, tools that survive as (enabled, executor), whether a warning is logged)
DEGRADED = [
    ("not-an-object", ["web_search"], {}, True),
    ("unknown-tool", {"code_interpreter": GOOD, "web_search": GOOD}, {"web_search": (True, None)}, True),
    ("missing-executor", {"web_search": {"enabled": True}, "x_search": GOOD}, {"x_search": (True, None)}, True),
    ("extra-field", {"web_search": {**GOOD, "replay": True}}, {}, True),
    ("non-boolean-enabled", {"web_search": {"enabled": "yes", "executor": None}}, {}, True),
    ("bad-executor-shape", {"web_search": {"enabled": True, "executor": {"source_id": "xai"}}}, {}, True),
    (
        "executor-source-gone",
        {"web_search": {"enabled": True, "executor": {"source_id": "src_gone0001", "model_id": "grok-4.6"}}},
        {"web_search": (True, None)},
        False,
    ),
    (
        "executor-model-gone",
        {"x_search": {"enabled": True, "executor": {"source_id": "src_xai00001", "model_id": "grok-3"}}},
        {"x_search": (True, None)},
        False,
    ),
]


@pytest.mark.parametrize("raw,expected,warns", [case[1:] for case in DEGRADED], ids=[case[0] for case in DEGRADED])
def test_a_broken_hosted_tool_entry_turns_off_only_itself(raw, expected, warns, caplog):
    """MH-HOSTED-002: a broken entry turns off only its tool; Sources and Agents still load."""
    with caplog.at_level(logging.WARNING, logger="config.v2_config"):
        config = ModelHubConfig.from_payload(_section(raw))
    assert [source.id for source in config.sources] == ["src_xai00001"]
    assert {tool: (entry.enabled, entry.executor) for tool, entry in config.hosted_tools.items()} == expected
    assert bool(caplog.records) == warns


def test_disk_load_with_a_broken_hosted_tools_section_keeps_model_hub(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["model_hub"] = _section({"web_search": {"enabled": 1, "executor": None}, "x_search": GOOD})
    config_path = tmp_path / "config.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert not loaded.load_warnings
    assert [source.id for source in loaded.model_hub.sources] == ["src_xai00001"]
    assert set(loaded.model_hub.hosted_tools) == {"x_search"}
    assert config_path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("config.json.bak-recovery-*"))


def test_hosted_tools_never_hold_credential_material():
    section = _section({"web_search": {"enabled": True, "executor": {"source_id": "src_xai00001", "model_id": "sk-live-abc"}}})
    config = ModelHubConfig.from_payload(copy.deepcopy(section))
    assert "web_search" not in config.hosted_tools


def _validate_status(status):
    from jsonschema import Draft7Validator

    schema = json.loads(
        (Path(__file__).parents[1] / "docs/plans/model-hub-contracts/hosted-tools-status.schema.json").read_text()
    )
    Draft7Validator(schema).validate(status)


def _api_service(tmp_path):
    from config.v2_config import ModelHubSourceConfig
    from tests.test_model_hub_api import _service

    service, store, _adapter = _service(tmp_path)
    store.config = ModelHubConfig.from_payload(
        {
            "sources": [
                SOURCE,
                {**SOURCE, "id": "src_gem00001", "vendor": "custom", "protocol": "google", "credential_ref": "cred_g",
                 "models": [{**SOURCE["models"][0], "id": "gemini-3-pro"}]},
            ]
        }
    )
    assert all(isinstance(source, ModelHubSourceConfig) for source in store.config.sources)
    return service, store


@pytest.mark.asyncio
async def test_the_tools_api_reads_and_writes_one_tool_at_a_time(tmp_path):
    """MH-HOSTED-004: the Tools tab writes one toggle; nothing else in Model Hub changes."""
    from core.handlers.model_hub.service import ModelHubError

    service, store = _api_service(tmp_path)
    before = store.config.to_payload()

    status = service.hosted_tools_status()["hosted_tools"]
    assert [tool["name"] for tool in status["tools"]] == ["web_search", "x_search", "image_generation"]
    assert not any(tool["enabled"] for tool in status["tools"])
    x_search = next(tool for tool in status["tools"] if tool["name"] == "x_search")
    assert [(c["source_id"], c["model_id"], c["call"]["status"]) for c in x_search["candidates"]] == [
        ("src_xai00001", "grok-4.6", "unknown")
    ]
    # The cost hint: the overlay's dated list price and the Source's billing, never a computed zero.
    assert x_search["bound"] is None
    assert x_search["candidates"][0]["billing"] == "metered"
    assert x_search["candidates"][0]["price"] == {
        "unit": "post",
        "usd_per_1k": 5,
        "extra": [{"unit": "profile", "usd_per_1k": 10}],
        "as_of": "2026-10-13",
        "url": "https://docs.x.ai/developers/pricing",
    }
    _validate_status(status)
    assert status["reverify"] == {"engine_upgraded": 0, "aged": 0}

    executor = {"source_id": "src_xai00001", "model_id": "grok-4.6"}
    written = await service.set_hosted_tool("x_search", {"enabled": True, "executor": executor})
    after = store.config.to_payload()
    assert after.pop("hosted_tools") == {"x_search": {"enabled": True, "executor": executor}}
    assert after == before
    assert next(tool for tool in written["hosted_tools"]["tools"] if tool["name"] == "x_search")["executor"] == executor

    for tool, body, code, status_code in [
        ("code_interpreter", {"enabled": True, "executor": None}, "invalid_parameter", 400),
        ("web_search", {"enabled": True}, "invalid_parameter", 400),
        ("web_search", {"enabled": True, "executor": {"source_id": "src_gone0001", "model_id": "x"}}, "source_not_found", 404),
        ("web_search", {"enabled": True, "executor": {"source_id": "src_gem00001", "model_id": "gemini-3-pro"}}, "invalid_parameter", 400),
        ("web_search", {"enabled": True, "executor": {"source_id": "src_xai00001", "model_id": "grok-3"}}, "invalid_parameter", 400),
        ("image_generation", {"enabled": True, "executor": executor}, "invalid_parameter", 400),
    ]:
        with pytest.raises(ModelHubError) as refused:
            await service.set_hosted_tool(tool, body)
        assert (refused.value.code, refused.value.status) == (code, status_code), (tool, body)
    assert store.config.to_payload()["hosted_tools"] == {"x_search": {"enabled": True, "executor": executor}}
