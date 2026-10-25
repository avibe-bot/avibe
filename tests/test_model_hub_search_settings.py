"""Search settings owner coverage: persistent settings/patches and the active Tools read.

The pre-service hosted-tools tests cannot exercise catalog-dependent defaults,
clamping, role snapshots or Source-keyed patches. These tests use the real V2
store/mutation owner; no provider, credential or live runtime is contacted.
Scenarios: MH-HOSTED-002 (compatibility), MH-HOSTED-004 (Tools read/patch).
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
from config.v2_config import (
    KEYLESS_EXA_SERVICE_ID,
    KEYLESS_EXA_SOURCE_ID,
    ModelHubConfig, ModelHubSourceStateConfig, V2Config, hosted_tool_setting,
    model_hub_keyless_exa_source, model_hub_search_service,
)
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
FIXTURES = Path(__file__).parent / "fixtures" / "model_hub"


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


def test_keyless_catalog_is_built_in_and_credential_free():
    """KEX-1: the catalog reader exposes the stable free Exa service without a key."""
    entry = api_key_vendors.api_key_vendor_entry("exa-mcp")
    assert entry is not None and entry.built_in is True
    assert entry.search is not None
    assert entry.search["service_id"] == KEYLESS_EXA_SERVICE_ID
    assert entry.search["auth"] == "none"
    assert model_hub_search_service(model_hub_keyless_exa_source()) == entry.search


@pytest.mark.parametrize("fixture_name", ["released_search_v20.json", "keyless_exa_v21.json"])
def test_released_and_keyless_search_settings_read_without_a_migration_write(fixture_name):
    """KEX-1: both the shipped v20 shape and the future additive v21 shape stay round-trippable."""
    raw = json.loads((FIXTURES / fixture_name).read_text(encoding="utf-8"))
    config = _config(raw)
    setting = config.hosted_tools["web_search"]
    assert setting.to_payload() == raw
    assert setting.executor_order == tuple(raw["executor_order"])
    if KEYLESS_EXA_SOURCE_ID in raw["services"]:
        assert setting.services[KEYLESS_EXA_SOURCE_ID].to_payload() == {"results": 9, "enabled": False}


def test_keyless_candidate_and_catalog_are_active_by_default(tmp_path):
    """KEX-3: the default read includes the built-in candidate and catalog row."""
    config = _config()
    assert hosted_tools.KEYLESS_EXA_ACTIVE is True
    service, _, _ = _disk_service(tmp_path, config)
    visible = _web(service)
    assert any(row["source_id"] == KEYLESS_EXA_SOURCE_ID and row["model_id"] == KEYLESS_EXA_SERVICE_ID
               for row in visible["executor_candidates"])
    vendor = next(row for row in visible["service_vendors"] if row["id"] == "exa-mcp")
    assert vendor["facts"] == {"tool_only": True, "auto_enroll": False, "free_check": False,
                                "built_in": True, "requires_setup": False}
    assert vendor["source_ids"] == [KEYLESS_EXA_SOURCE_ID]


@pytest.mark.asyncio
async def test_keyless_override_role_and_cooldown_survive_an_unrelated_config_write(tmp_path):
    """KEX-3: synthetic state is preserved by a later real V2 save while active."""
    raw = json.loads((FIXTURES / "keyless_exa_v21.json").read_text(encoding="utf-8"))
    service, path, _ = _disk_service(tmp_path, _config(raw))
    source = model_hub_keyless_exa_source()
    generation = service._service_cooldowns.advance(source, KEYLESS_EXA_SERVICE_ID)
    await service.set_hosted_tool("x_search", {"enabled": False})
    saved = json.loads(path.read_text(encoding="utf-8"))["model_hub"]["hosted_tools"]["web_search"]
    assert saved["executor_order"] == [KEYLESS_EXA_SOURCE_ID, TAVILY]
    assert saved["executor_order_kinds"][KEYLESS_EXA_SOURCE_ID] == "service"
    assert saved["services"][KEYLESS_EXA_SOURCE_ID] == {"results": 9, "enabled": False}
    assert service._service_cooldowns.current(source, KEYLESS_EXA_SERVICE_ID, generation)


def test_defaults_follow_catalog_without_freezing_overrides(monkeypatch):
    """Default counts and opt-in evolve with catalog facts, not saved effective settings."""
    config = _config()
    setting = hosted_tool_setting(config, "web_search")
    assert setting.prefer_native is True
    assert setting.services == {} and setting.executor_order_kinds is None
    for source in config.sources[1:]:
        assert hosted_tools.search_service_settings(setting, source) == {"results": 5, "enabled": True}
    _catalog_change(monkeypatch, "deepseek", auto_enroll=False, limits={"results_max": 3})
    assert hosted_tools.search_service_settings(setting, config.sources[-1]) == {
        "results": 3, "enabled": False,
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
        "services": {TAVILY: {"results": 7, "enabled": False}, DEEPSEEK: {"enabled": False}},
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
    # Active search placement keeps the built-in service first, then deliberate model/service entries and disabled tails.
    assert _web(service)["effective_order"] == [KEYLESS_EXA_SOURCE_ID, MODEL, EXA, TAVILY, DEEPSEEK]
    await service.set_hosted_tool("web_search", {"executor_order": None})
    reset = V2ModelHubConfigStore().load().hosted_tools["web_search"].to_payload()
    assert "executor_order" not in reset and "executor_order_kinds" not in reset
    assert reset["service_order"] == [TAVILY, EXA]


def test_released_order_keeps_missing_kinds_absent_and_ignores_unnamed_entries():
    """MH-HOSTED-002: persistence does not invent roles; S2 owns interpreting absent roles as model."""
    setting = _config({"executor_order": [DEEPSEEK, MODEL]}).hosted_tools["web_search"]
    assert setting.executor_order == (DEEPSEEK, MODEL)
    assert setting.executor_order_kinds is None and "executor_order_kinds" not in setting.to_payload()
    setting = replace(setting, executor_order_kinds={DEEPSEEK: "service", EXA: "service"})
    assert setting.to_payload()["executor_order_kinds"] == {DEEPSEEK: "service"}
    loaded = _config({"executor_order": [MODEL], "executor_order_kinds": {EXA: []}}).hosted_tools["web_search"]
    assert loaded.enabled and loaded.executor_order == (MODEL,) and loaded.executor_order_kinds == {}


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


def test_retargeted_source_keeps_its_opt_out_until_search_capability_returns(tmp_path):
    """MH-HOSTED-004: the read hides ineligible overrides, but a real V2 save must keep consent."""
    config = _config({"services": {DEEPSEEK: {"enabled": False}}})
    config.sources[-1].base_url = "https://relay.invalid/v1"
    service, _, _ = _disk_service(tmp_path, config)
    loaded = service.store.load()
    assert hosted_tools.search_service_settings(loaded.hosted_tools["web_search"], loaded.sources[-1]) is None
    assert loaded.hosted_tools["web_search"].services[DEEPSEEK].enabled is False
    assert _web(service)["services"] == {}
    loaded.sources[-1].base_url = None
    service.store.save(loaded)
    restored = V2ModelHubConfigStore().load()
    assert hosted_tools.search_service_settings(restored.hosted_tools["web_search"], restored.sources[-1])["enabled"] is False
    assert _web(service)["services"] == {DEEPSEEK: {"enabled": False}}


@pytest.mark.asyncio
async def test_catalog_downgrade_clamps_on_load_without_rewriting_until_next_save(tmp_path, monkeypatch):
    """A catalog maximum reduction cannot disable the tool or silently edit disk on read."""
    service, path, _ = _disk_service(tmp_path, _config({"services": {TAVILY: {"results": 20}}}))
    original = path.read_bytes()
    _catalog_change(monkeypatch, "tavily", limits={"results_max": 2})
    loaded = V2Config.load().model_hub.hosted_tools["web_search"]
    assert loaded.enabled
    assert loaded.services[TAVILY].to_payload() == {"results": 2}
    assert _web(service)["services"][TAVILY] == {"results": 2}
    assert path.read_bytes() == original
    await service.set_hosted_tool("web_search", {"prefer_native": False})
    written = json.loads(path.read_text())["model_hub"]["hosted_tools"]["web_search"]
    assert written["services"][TAVILY] == {"results": 2}
    assert V2ModelHubConfigStore().load().hosted_tools["web_search"].to_payload() == written


@pytest.mark.asyncio
async def test_put_merges_absent_sources_fields_and_null_resets(tmp_path):
    """MH-HOSTED-004: partial clients cannot echo defaults over other clients' saved choices."""
    service, path, _ = _disk_service(tmp_path, _config({"services": {
        TAVILY: {"results": 8, "enabled": False}, EXA: {"results": 10},
    }}))
    await service.set_hosted_tool("web_search", {"services": {TAVILY: {"results": 9}}})
    assert _web(service)["services"] == {
        TAVILY: {"results": 9, "enabled": False}, EXA: {"results": 10},
    }
    await service.set_hosted_tool("web_search", {"services": {TAVILY: {"enabled": None}, EXA: None}})
    assert _web(service)["services"] == {TAVILY: {"results": 9}}
    await service.set_hosted_tool("web_search", {"services": {TAVILY: {"results": None}}})
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
            {"services": {TAVILY: {"results": 11}}}, {"services": {TAVILY: {"enabled": False}}},
            {"services": {EXA: {"enabled": False}}}, {"prefer_native": False},
        )]
        await asyncio.sleep(0)
        assert all(not task.done() for task in writes)
    await asyncio.gather(*writes)
    restarted = V2ModelHubConfigStore().load().hosted_tools["web_search"]
    assert restarted.to_payload() == {"enabled": True, "prefer_native": False, "services": {
        TAVILY: {"results": 11, "enabled": False}, EXA: {"enabled": False},
    }}


@pytest.mark.parametrize("tool,patch", [
    ("web_search", {"prefer_native": None}), ("web_search", {"prefer_native": 1}),
    ("web_search", {"services": None}), ("web_search", {"service_order": []}),
    ("web_search", {"executor_order_kinds": {TAVILY: "model"}}),
    ("web_search", {"services": {TAVILY: {"results": 21}}}),
    ("web_search", {"services": {TAVILY: {"results": True}}}),
    ("web_search", {"services": {TAVILY: {"results": 0}}}),
    ("web_search", {"services": {TAVILY: {"enabled": 1}}}),
    ("web_search", {"services": {TAVILY: {"content": True}}}),
    ("web_search", {"services": {TAVILY: {"content": False}}}),
    ("web_search", {"services": {TAVILY: {"content": None}}}),
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


@pytest.mark.parametrize("content", [True, False, None, "obsolete"])
@pytest.mark.asyncio
async def test_development_content_override_is_ignored_without_losing_saved_choices(tmp_path, caplog, content):
    """MH-HOSTED-002: pre-withdrawal dev settings must not break load or erase opt-outs.

    Existing released-shape tests predate services.content. Read this fixture through V2,
    not through the current writer, which must no longer be able to produce the field.
    """
    service, path, _ = _disk_service(tmp_path, _config())
    fixture = Path(__file__).parent / "fixtures/model_hub/development_search_content.json"
    saved = json.loads(fixture.read_text(encoding="utf-8"))
    saved["services"][TAVILY]["content"] = content
    payload = json.loads(path.read_text())
    payload["model_hub"]["hosted_tools"]["web_search"] = saved
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    expected = {**saved, "services": {TAVILY: {"results": 7, "enabled": False}}}
    loaded = V2ModelHubConfigStore().load().hosted_tools["web_search"]
    assert loaded.to_payload() == expected
    assert _web(service)["services"] == expected["services"]
    assert "content" in caplog.text and "ignores unknown fields" in caplog.text
    assert path.read_bytes() == before
    await service.set_hosted_tool("x_search", {"enabled": False})
    assert json.loads(path.read_text())["model_hub"]["hosted_tools"]["web_search"] == expected
    assert V2ModelHubConfigStore().load().hosted_tools["web_search"].to_payload() == expected


def test_service_order_read_skips_removed_sources_without_rewriting_saved_order(tmp_path):
    """MH-HOSTED-004: a retained stale id is never presented as an automatic search Source."""
    order = [EXA, GONE, TAVILY]
    service, path, _ = _disk_service(tmp_path, _config({"service_order": order}))
    before = path.read_bytes()
    assert _web(service)["service_order"] == [EXA, TAVILY]
    assert V2ModelHubConfigStore().load().hosted_tools["web_search"].service_order == tuple(order)
    assert path.read_bytes() == before


@pytest.mark.parametrize("stale_role", ["service", []])
@pytest.mark.asyncio
async def test_role_metadata_drops_removed_sources_on_load_and_real_deletion(tmp_path, stale_role):
    """MH-HOSTED-002: removed roles are ignored before validation; normal deletion saves the same invariant."""
    order = [GONE, TAVILY, DEEPSEEK, MODEL]
    live_roles = {TAVILY: "service", DEEPSEEK: "service", MODEL: "model"}
    config = _config({"executor_order": order, "executor_order_kinds": live_roles,
                      "service_order": [TAVILY, EXA], "services": {DEEPSEEK: {"enabled": False}}})
    config.sources[-1].base_url = "https://relay.invalid/v1"  # Retargeting is not deletion: keep its saved role.
    service, path, _ = _disk_service(tmp_path, config)
    raw = json.loads(path.read_text())
    raw["model_hub"]["hosted_tools"]["web_search"]["executor_order_kinds"][GONE] = stale_role
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = path.read_bytes()
    loaded = V2ModelHubConfigStore().load().hosted_tools["web_search"]
    assert loaded.enabled and loaded.executor_order_kinds == live_roles
    assert loaded.executor_order == tuple(order)
    assert path.read_bytes() == before
    await service.delete_source(TAVILY)
    written = json.loads(path.read_text())["model_hub"]["hosted_tools"]["web_search"]
    assert written["executor_order"] == order
    assert written["executor_order_kinds"] == {DEEPSEEK: "service", MODEL: "model"}
    assert written["services"] == {DEEPSEEK: {"enabled": False}}
    assert written["service_order"] == [EXA]
    assert V2ModelHubConfigStore().load().hosted_tools["web_search"].to_payload() == written


def test_removed_source_settings_are_ignored_before_validation():
    config = _config({"services": {GONE: "obsolete", TAVILY: {"results": 6}}})
    assert config.hosted_tools["web_search"].enabled
    assert config.hosted_tools["web_search"].to_payload()["services"] == {TAVILY: {"results": 6}}


@pytest.mark.parametrize("field,value", [("prefer_native", None), ("services", None),
                                        ("services", {TAVILY: {"enabled": None}})])
def test_damaged_optional_settings_disable_only_web_search(field, value):
    config = _config({"executor_order": [TAVILY], field: value})
    assert not config.hosted_tools["web_search"].enabled
    assert len(config.sources) == 4
    assert hosted_tool_setting(config, "x_search").enabled


@pytest.mark.parametrize("damaged", [None, [], {DEEPSEEK: []}, {DEEPSEEK: "future"}])
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.asyncio
async def test_damaged_role_map_keeps_opt_out_across_load_write_and_reenable(tmp_path, caplog, damaged, enabled):
    """MH-HOSTED-002: damaged derived metadata must not erase consent on the next real V2 write."""
    expected = {"enabled": enabled, "prefer_native": False, "executor_order": [DEEPSEEK, MODEL],
                "service_order": [TAVILY], "services": {DEEPSEEK: {"enabled": False}}}
    service, path, _ = _disk_service(tmp_path, _config(expected))
    payload = json.loads(path.read_text())
    payload["model_hub"]["hosted_tools"]["web_search"]["executor_order_kinds"] = damaged
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    loaded = V2ModelHubConfigStore().load().hosted_tools["web_search"]
    assert loaded.to_payload() == expected
    assert "executor_order_kinds" in caplog.text and "ignoring damaged" in caplog.text
    assert path.read_bytes() == before
    await service.set_hosted_tool("x_search", {"enabled": False})
    assert V2ModelHubConfigStore().load().hosted_tools["web_search"].to_payload() == expected
    await service.set_hosted_tool("web_search", {"enabled": True})
    restarted = V2ModelHubConfigStore().load()
    setting = restarted.hosted_tools["web_search"]
    assert setting.to_payload() == {**expected, "enabled": True}
    assert hosted_tools.search_service_settings(setting, restarted.sources[-1])["enabled"] is False


@pytest.mark.asyncio
async def test_active_service_read_validates_without_paid_calls(tmp_path):
    """The shipped read projects service health/settings and vendor membership without a search."""
    service, _, _ = _disk_service(tmp_path, _config({"services": {DEEPSEEK: {"enabled": False}}}))
    visible = _web(service)
    assert visible["prefer_native"] is True
    assert {row["kind"] for row in visible["executor_candidates"]} == {"service", "model"}
    config = service.store.load()
    config.sources[-1].billing = "monthly"  # Model billing does not make the separate search API included.
    config.sources[2].state = ModelHubSourceStateConfig(
        status="needs_action", detail_key="models.source.needs_action.balance_exhausted")
    service.store.save(config)
    service._search_reenable_trials.add(EXA)
    visible = _web(service)
    exa = next(row for row in visible["executor_candidates"] if row["source_id"] == EXA)
    assert exa == {
        "kind": "service", "source_id": EXA, "model_id": "exa/search", "billing": "metered", "relay": False,
        "price": {"unit": "search", "usd_per_1k": 7, "extra": [],
                  "as_of": "2026-10-19", "url": "https://exa.ai/blog/exa-3"},
        "settings": {"results": 5, "enabled": True},
        "state": {"status": "needs_action", "retry_at": None, "detail_key": "models.source.needs_action.balance_exhausted"},
        "trial_pending": True, "cooldown_until": None,
    }
    deepseek_candidate = next(row for row in visible["executor_candidates"] if row["source_id"] == DEEPSEEK)
    assert deepseek_candidate["billing"] == "metered" and not deepseek_candidate["settings"]["enabled"]
    assert service.store.load().sources[-1].billing == "monthly"
    deepseek = next(vendor for vendor in visible["service_vendors"] if vendor["id"] == "deepseek")
    assert deepseek["source_ids"] == [DEEPSEEK] and deepseek["facts"]["auto_enroll"] is True
    assert deepseek["free_tier"] is None
    for vendor in visible["service_vendors"]:
        assert set(vendor) == {"id", "name", "price", "free_tier", "facts", "results_max", "source_ids"}
        assert vendor["price"]["extra"] == []
    validator = Draft7Validator({"$ref": CONTRACT + "#/definitions/ServiceExecutor"}, registry=_api_response_registry())
    for changed in (
        {"kind": "model"}, {"billing": "monthly"}, {"settings": {"results": 5}}, {"trial_pending": None},
        {"settings": {"results": 5, "enabled": True, "content": False}},
        {"price": {**exa["price"], "extra": [{"unit": "page", "usd_per_1k": 1}]}},
        {"price": {**exa["price"], "extra": [{"unit": "search", "usd_per_1k": 1, "when": "content"}]}},
    ):
        with pytest.raises(ValidationError):
            validator.validate({**exa, **changed})


def test_http_tools_put_and_read_preserve_patch_and_active_contract(tmp_path, monkeypatch):
    """MH-HOSTED-004: the public route accepts patches and returns the active service read."""
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
        json={"prefer_native": False, "services": {TAVILY: {"results": 7, "enabled": False}}},
    )
    assert response.status_code == 200
    written = response.get_json()
    response = client.get("/api/models/hosted-tools", base_url=origin)
    assert response.status_code == 200 and response.get_json() == written
    Draft7Validator({"$ref": "model-hub/api-response.schema.json#/definitions/HostedToolsResponse"},
                    registry=_api_response_registry()).validate(written)
    web = written["hosted_tools"]["tools"][0]
    assert web["prefer_native"] is False and web["services"] == {TAVILY: {"results": 7, "enabled": False}}
    assert {vendor["id"] for vendor in web["service_vendors"]} == {"deepseek", "tavily", "exa", "exa-mcp"}
    assert [(row["source_id"], row["kind"]) for row in web["executor_candidates"]] == [
        (EXA, "service"), (DEEPSEEK, "service"), (KEYLESS_EXA_SOURCE_ID, "service"), (MODEL, "model"), (TAVILY, "service"),
    ]
    assert web["executor_candidates"][-1]["settings"] == {"results": 7, "enabled": False}


def test_rc12_loader_preserves_tool_switch_and_orders_but_drops_service_overrides(caplog):
    """MH-HOSTED-002: rc12 keeps its known choices, but a downgrade write cannot retain new service opt-outs."""
    released = runpy.run_path(str(Path(__file__).parent / "fixtures/model_hub/released_rc12_hosted_tools.py"))
    section = _config({"enabled": False, "prefer_native": False, "executor_order": [MODEL, DEEPSEEK],
                       "executor_order_kinds": {MODEL: "model", DEEPSEEK: "service"},
                       "services": {DEEPSEEK: {"enabled": False}}, "service_order": [TAVILY]}).to_payload()
    loaded = released["_hosted_tools_from_payload"](section)
    written = loaded["web_search"].to_payload()
    assert written == {"enabled": False, "executor_order": [MODEL, DEEPSEEK], "service_order": [TAVILY]}
    assert all(field in caplog.text for field in ("prefer_native", "services", "executor_order_kinds"))
    assert "service_order" not in caplog.text
    section["hosted_tools"]["web_search"] = written
    upgraded = ModelHubConfig.from_payload(section)
    setting = upgraded.hosted_tools["web_search"]
    assert not setting.enabled and setting.services == {}
    assert hosted_tools.search_service_settings(setting, upgraded.sources[-1])["enabled"] is True
