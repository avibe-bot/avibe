"""S1b owner coverage: persistent settings/patches and the dormant Tools read.

The pre-service hosted-tools tests cannot exercise catalog-dependent defaults,
clamping, role snapshots or Source-keyed patches. These tests use the real V2
store/mutation owner; no provider, credential or live runtime is contacted.
"""

from __future__ import annotations

import asyncio
import copy
import json
import runpy
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft7Validator, ValidationError

from config import paths
from config.v2_config import ModelHubConfig, ModelHubSourceStateConfig, V2Config, hosted_tool_setting
from core.handlers.model_hub import hosted_tools
from core.handlers.model_hub.service import ModelHubError, V2ModelHubConfigStore
from core.services.settings import default_config
from tests.test_model_hub_api import _api_response_registry, _service
from tests.test_model_hub_hosted_tools_config import SOURCE
from vibe import api
from vibe.model_hub_runtime import api_key_vendors

TAVILY = "src_tavily01"
EXA = "src_exa00001"
DEEPSEEK = "src_deepseek1"
MODEL = SOURCE["id"]
GONE = "src_gone0001"
CONTRACT = "model-hub/hosted-tools-status.schema.json"


def _source(source_id, vendor, protocol="search"):
    return {**SOURCE, "id": source_id, "vendor": vendor, "display_name": vendor,
            "protocol": protocol, "models": [], "credential_ref": f"cred_{source_id}"}


def _config(web=None):
    return ModelHubConfig.from_payload({
        "sources": [SOURCE, _source(TAVILY, "tavily"), _source(EXA, "exa"),
                    _source(DEEPSEEK, "deepseek", "openai_chat")],
        "hosted_tools": {"web_search": web or {}},
    })


def _disk_service(tmp_path, config):
    path = paths.get_config_path()
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["model_hub"] = config.to_payload()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    service, _, adapter = _service(tmp_path)
    service.store = V2ModelHubConfigStore()
    return service, path, adapter


def _web(service):
    status = service.hosted_tools_status()["hosted_tools"]
    Draft7Validator({"$ref": CONTRACT}, registry=_api_response_registry()).validate(status)
    return next(tool for tool in status["tools"] if tool["name"] == "web_search")


def _catalog_change(monkeypatch, vendor_id, **changes):
    original = api_key_vendors.api_key_vendor_entry
    vendor = original(vendor_id)
    service = copy.deepcopy(vendor.search)
    service.update(changes)
    monkeypatch.setattr(api_key_vendors, "api_key_vendor_entry", lambda key: replace(vendor, search=service)
                        if key == vendor_id else original(key))


def test_defaults_follow_catalog_without_freezing_overrides(monkeypatch):
    """Default counts and opt-in evolve with catalog facts, not saved effective settings."""
    config = _config()
    setting = hosted_tool_setting(config, "web_search")
    assert setting.prefer_native is True
    assert setting.services == {} and setting.executor_order_kinds is None
    for source in config.sources[1:]:
        assert hosted_tools.search_service_settings(setting, source) == {"results": 5, "content": False, "enabled": True}
    _catalog_change(monkeypatch, "deepseek", auto_enroll=False, limits={"results_max": 3})
    assert hosted_tools.search_service_settings(setting, config.sources[-1]) == {
        "results": 3, "content": False, "enabled": False,
    }
    assert setting.to_payload() == {"enabled": True}
    # A subscription or relay cannot inherit an official API's search permission.
    assert hosted_tools.search_service_settings(setting, replace(config.sources[-1], kind="subscription")) is None
    assert hosted_tools.search_service_settings(setting, replace(config.sources[-1], base_url="https://relay.invalid/v1")) is None


@pytest.mark.asyncio
async def test_mixed_order_and_role_snapshot_survive_real_v2_restart(tmp_path):
    """An explicit model-before-service placement and role map are saved, not recomputed on load."""
    service, path, _ = _disk_service(tmp_path, _config({"service_order": [TAVILY, EXA]}))
    order = [MODEL, TAVILY, GONE, DEEPSEEK, EXA]
    await service.set_hosted_tool("web_search", {
        "prefer_native": False, "executor_order": order,
        "services": {TAVILY: {"results": 7, "content": True}, DEEPSEEK: {"enabled": False}},
    })
    saved = json.loads(path.read_text())["model_hub"]["hosted_tools"]["web_search"]
    assert saved["executor_order"] == order
    assert saved["executor_order_kinds"] == {MODEL: "model", TAVILY: "service", DEEPSEEK: "service", EXA: "service"}
    # Disabled DeepSeek has no existing model executor; it remains a disabled service row (S2).
    assert GONE not in saved["executor_order_kinds"]
    restarted = V2ModelHubConfigStore().load()
    assert restarted.hosted_tools["web_search"].to_payload() == saved
    assert restarted.hosted_tools["web_search"].service_order == (TAVILY, EXA)
    assert not restarted.hosted_tools["web_search"].prefer_native
    assert _web(service)["executor_order"] == order
    # Dormancy keeps actual model execution unchanged, despite the saved future role map/order.
    assert _web(service)["effective_order"] == [MODEL]
    await service.set_hosted_tool("web_search", {"executor_order": None})
    reset = V2ModelHubConfigStore().load().hosted_tools["web_search"].to_payload()
    assert "executor_order" not in reset and "executor_order_kinds" not in reset
    assert reset["service_order"] == [TAVILY, EXA]


def test_released_order_and_missing_kind_read_as_model():
    setting = _config({"executor_order": [DEEPSEEK, MODEL]}).hosted_tools["web_search"]
    assert [setting.saved_executor_kind(key) for key in setting.executor_order] == ["model", "model"]
    assert "executor_order_kinds" not in setting.to_payload()
    setting = replace(setting, executor_order_kinds={DEEPSEEK: "service", EXA: "service"})
    assert setting.saved_executor_kind(DEEPSEEK) == "service"
    assert setting.saved_executor_kind(MODEL) == "model"
    assert setting.saved_executor_kind(EXA) == "model"
    assert setting.to_payload()["executor_order_kinds"] == {DEEPSEEK: "service"}
    loaded = _config({"executor_order": [MODEL], "executor_order_kinds": {EXA: []}}).hosted_tools["web_search"]
    assert loaded.enabled and loaded.saved_executor_kind(MODEL) == "model"


@pytest.mark.asyncio
async def test_order_save_uses_the_resulting_service_opt_in_and_preserves_model_role(tmp_path, monkeypatch):
    """A future model vendor's search opt-out keeps its existing executor; a combined save snapshots the new role."""
    catalog_service = copy.deepcopy(api_key_vendors.api_key_vendor_entry("deepseek").search)
    catalog_service["auto_enroll"] = False
    original = api_key_vendors.api_key_vendor_entry
    vendor = original("xai")
    monkeypatch.setattr(api_key_vendors, "api_key_vendor_entry", lambda key: replace(vendor, search=catalog_service)
                        if key == "xai" else original(key))
    service, _, _ = _disk_service(tmp_path, _config())
    await service.set_hosted_tool("web_search", {"executor_order": [MODEL]})
    assert service.store.load().hosted_tools["web_search"].executor_order_kinds == {MODEL: "model"}
    await service.set_hosted_tool("web_search", {"executor_order": [MODEL], "services": {MODEL: {"enabled": True}}})
    assert V2ModelHubConfigStore().load().hosted_tools["web_search"].executor_order_kinds == {MODEL: "service"}
    await service.set_hosted_tool("web_search", {"executor_order": [MODEL], "services": {MODEL: {"enabled": None}}})
    assert V2ModelHubConfigStore().load().hosted_tools["web_search"].executor_order_kinds == {MODEL: "model"}


def test_retargeted_source_keeps_its_opt_out_until_search_capability_returns():
    """Ignoring a currently ineligible service must not erase consent on an unrelated config save."""
    config = _config({"services": {DEEPSEEK: {"enabled": False}}})
    config.sources[-1].base_url = "https://relay.invalid/v1"
    loaded = ModelHubConfig.from_payload(config.to_payload())
    assert hosted_tools.search_service_settings(loaded.hosted_tools["web_search"], loaded.sources[-1]) is None
    assert loaded.hosted_tools["web_search"].services[DEEPSEEK].enabled is False
    loaded.sources[-1].base_url = None
    restored = ModelHubConfig.from_payload(loaded.to_payload())
    assert hosted_tools.search_service_settings(restored.hosted_tools["web_search"], restored.sources[-1])["enabled"] is False


@pytest.mark.asyncio
async def test_catalog_downgrade_clamps_on_load_without_rewriting_until_next_save(tmp_path, monkeypatch):
    """A catalog maximum/content removal cannot disable the tool or silently edit disk on read."""
    service, path, _ = _disk_service(tmp_path, _config({"services": {TAVILY: {"results": 20, "content": True}}}))
    original = path.read_bytes()
    result_mapping = dict(api_key_vendors.api_key_vendor_entry("tavily").search["result"])
    result_mapping.pop("content")
    _catalog_change(monkeypatch, "tavily", limits={"results_max": 2}, result=result_mapping)
    loaded = V2Config.load().model_hub.hosted_tools["web_search"]
    assert loaded.enabled
    assert loaded.services[TAVILY].to_payload() == {"results": 2, "content": False}
    assert _web(service)["services"][TAVILY] == {"results": 2, "content": False}
    assert path.read_bytes() == original
    await service.set_hosted_tool("web_search", {"prefer_native": False})
    written = json.loads(path.read_text())["model_hub"]["hosted_tools"]["web_search"]
    assert written["services"][TAVILY] == {"results": 2, "content": False}
    assert V2ModelHubConfigStore().load().hosted_tools["web_search"].to_payload() == written


@pytest.mark.asyncio
async def test_put_merges_absent_sources_fields_and_null_resets(tmp_path):
    """Partial clients cannot echo defaults over other clients' saved choices."""
    service, path, _ = _disk_service(tmp_path, _config({"services": {
        TAVILY: {"results": 8, "content": True, "enabled": False}, EXA: {"content": True},
    }}))
    await service.set_hosted_tool("web_search", {"services": {TAVILY: {"results": 9}}})
    assert _web(service)["services"] == {
        TAVILY: {"results": 9, "content": True, "enabled": False}, EXA: {"content": True},
    }
    await service.set_hosted_tool("web_search", {"services": {TAVILY: {"content": None}, EXA: None}})
    assert _web(service)["services"] == {TAVILY: {"results": 9, "enabled": False}}
    await service.set_hosted_tool("web_search", {"services": {TAVILY: {"results": None, "enabled": None}}})
    assert _web(service)["services"] == {}
    before = path.read_bytes()
    await service.set_hosted_tool("web_search", {"services": {}})
    assert path.read_bytes() == before
    assert V2ModelHubConfigStore().load().hosted_tools["web_search"].services == {}


@pytest.mark.asyncio
async def test_concurrent_different_fields_and_sources_are_not_lost(tmp_path):
    """The controller mutation owner loads the latest config after acquiring its lock."""
    service, _, _ = _disk_service(tmp_path, _config())
    async with service._mutation_lock:
        writes = [asyncio.create_task(service.set_hosted_tool("web_search", patch)) for patch in (
            {"services": {TAVILY: {"results": 11}}}, {"services": {TAVILY: {"content": True}}},
            {"services": {EXA: {"enabled": False}}}, {"prefer_native": False},
        )]
        await asyncio.sleep(0)
        assert all(not task.done() for task in writes)
    await asyncio.gather(*writes)
    restarted = V2ModelHubConfigStore().load().hosted_tools["web_search"]
    assert restarted.to_payload() == {"enabled": True, "prefer_native": False, "services": {
        TAVILY: {"results": 11, "content": True}, EXA: {"enabled": False},
    }}


@pytest.mark.parametrize("tool,patch", [
    ("web_search", {"prefer_native": None}), ("web_search", {"prefer_native": 1}),
    ("web_search", {"services": None}), ("web_search", {"service_order": []}),
    ("web_search", {"executor_order_kinds": {TAVILY: "model"}}),
    ("web_search", {"services": {TAVILY: {"results": 21}}}),
    ("web_search", {"services": {TAVILY: {"results": True}}}),
    ("web_search", {"services": {TAVILY: {"results": 0}}}),
    ("web_search", {"services": {TAVILY: {"content": 1}}}),
    ("web_search", {"services": {TAVILY: {"unknown": True}}}),
    ("web_search", {"services": {GONE: {"results": 5}}}),
    ("web_search", {"services": {MODEL: {"enabled": True}}}),
    ("x_search", {"prefer_native": False}), ("image_generate", {"services": {}}),
])
@pytest.mark.asyncio
async def test_invalid_patches_are_atomic(tmp_path, tool, patch):
    service, path, _ = _disk_service(tmp_path, _config())
    before = path.read_bytes()
    with pytest.raises(ModelHubError) as error:
        await service.set_hosted_tool(tool, {"enabled": False, **patch})
    assert (error.value.status, error.value.code) == (400, "invalid_parameter")
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_content_write_requires_catalog_page_text(tmp_path, monkeypatch):
    service, path, _ = _disk_service(tmp_path, _config())
    result = dict(api_key_vendors.api_key_vendor_entry("tavily").search["result"])
    result.pop("content")
    _catalog_change(monkeypatch, "tavily", result=result)
    before = path.read_bytes()
    with pytest.raises(ModelHubError):
        await service.set_hosted_tool("web_search", {"services": {TAVILY: {"content": True}}})
    assert path.read_bytes() == before
    await service.set_hosted_tool("web_search", {"services": {TAVILY: {"content": False}}})
    assert _web(service)["services"][TAVILY] == {"content": False}


def test_removed_source_settings_are_ignored_before_validation():
    config = _config({"services": {GONE: "obsolete", TAVILY: {"results": 6}}})
    assert config.hosted_tools["web_search"].enabled
    assert config.hosted_tools["web_search"].to_payload()["services"] == {TAVILY: {"results": 6}}


@pytest.mark.parametrize("field,value", [("prefer_native", None), ("services", None),
                                        ("services", {TAVILY: {"content": None}}),
                                        ("executor_order_kinds", {TAVILY: []})])
def test_damaged_optional_settings_disable_only_web_search(field, value):
    config = _config({"executor_order": [TAVILY], field: value})
    assert not config.hosted_tools["web_search"].enabled
    assert len(config.sources) == 4
    assert hosted_tool_setting(config, "x_search").enabled


@pytest.mark.asyncio
async def test_dormant_read_and_future_service_variant_validate_without_paid_calls(tmp_path, monkeypatch):
    """One switch gates both read additions; defined service payloads do not require execution/UI activation."""
    service, _, _ = _disk_service(tmp_path, _config({"services": {DEEPSEEK: {"enabled": False}}}))
    dormant = _web(service)
    assert dormant["prefer_native"] is True
    assert "service_vendors" not in dormant
    assert {row["kind"] for row in dormant["executor_candidates"]} == {"model"}
    # Check the prepared projection only. S2's ordering/chain/cooldown semantics are intentionally not exercised.
    config = service.store.load()
    config.sources[2].state = ModelHubSourceStateConfig(
        status="needs_action", detail_key="models.source.needs_action.balance_exhausted")
    service.store.save(config)
    service._search_reenable_trials.add(EXA)
    monkeypatch.setattr(hosted_tools, "SEARCH_SERVICES_ACTIVE", True)
    visible = _web(service)
    exa = next(row for row in visible["executor_candidates"] if row["source_id"] == EXA)
    assert exa == {
        "kind": "service", "source_id": EXA, "model_id": "exa/search", "billing": "metered", "relay": False,
        "price": {"unit": "search", "usd_per_1k": 7, "extra": [{"unit": "page", "usd_per_1k": 1, "when": "content"}],
                  "as_of": "2026-10-19", "url": "https://exa.ai/blog/exa-3"},
        "settings": {"results": 5, "content": False, "enabled": True},
        "state": {"status": "needs_action", "retry_at": None, "detail_key": "models.source.needs_action.balance_exhausted"},
        "trial_pending": True, "cooldown_until": None,
    }
    deepseek = next(vendor for vendor in visible["service_vendors"] if vendor["id"] == "deepseek")
    assert deepseek["source_ids"] == [DEEPSEEK] and deepseek["facts"]["auto_enroll"] is True
    assert deepseek["free_tier"] is None
    assert next(vendor for vendor in visible["service_vendors"] if vendor["id"] == "exa")["content_changes_price"]
    validator = Draft7Validator({"$ref": CONTRACT + "#/definitions/ServiceExecutor"}, registry=_api_response_registry())
    for changed in ({"kind": "model"}, {"billing": "monthly"}, {"settings": {"results": 5}}, {"trial_pending": None}):
        with pytest.raises(ValidationError):
            validator.validate({**exa, **changed})


def test_http_tools_put_and_read_preserve_patch_and_dormant_contract(tmp_path, monkeypatch):
    """The public route accepts the new fields and returns a valid dormant envelope, not only the pure helper."""
    from tests.test_model_hub_api import _as_ui_client
    from tests.ui_server_test_helpers import csrf_headers, save_config
    from vibe import ui_server

    save_config(tmp_path)
    service, store, _ = _service(tmp_path)
    store.config = _config()
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: _as_ui_client(service))
    client = ui_server.app.test_client()
    origin = "http://127.0.0.1:15131"
    response = client.put(
        "/api/models/hosted-tools/web_search", base_url=origin, headers=csrf_headers(client, origin),
        json={"prefer_native": False, "services": {TAVILY: {"results": 7, "content": True}}},
    )
    assert response.status_code == 200
    written = response.get_json()
    response = client.get("/api/models/hosted-tools", base_url=origin)
    assert response.status_code == 200 and response.get_json() == written
    Draft7Validator({"$ref": "model-hub/api-response.schema.json#/definitions/HostedToolsResponse"},
                    registry=_api_response_registry()).validate(written)
    web = written["hosted_tools"]["tools"][0]
    assert web["prefer_native"] is False and web["services"] == {TAVILY: {"results": 7, "content": True}}
    assert "service_vendors" not in web and all(row["kind"] == "model" for row in web["executor_candidates"])


def test_release_before_loader_ignores_new_fields_but_keeps_opt_out_and_order(caplog):
    """The released loader, not a reimplementation, must safely ignore new optional settings."""
    released = runpy.run_path(str(Path(__file__).parent / "fixtures/model_hub/released_rc11_hosted_tools.py"))
    section = _config({"enabled": False, "prefer_native": False, "executor_order": [MODEL, DEEPSEEK],
                       "executor_order_kinds": {MODEL: "model", DEEPSEEK: "service"},
                       "services": {DEEPSEEK: {"enabled": False}}, "service_order": [TAVILY]}).to_payload()
    loaded = released["_hosted_tools_from_payload"](section)
    assert loaded["web_search"].to_payload() == {"enabled": False, "executor_order": [MODEL, DEEPSEEK]}
    assert all(field in caplog.text for field in ("prefer_native", "services", "service_order", "executor_order_kinds"))
