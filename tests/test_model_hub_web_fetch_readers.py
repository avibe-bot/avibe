"""WF-1/WF-3: saved fetch consent, active reads and independent ledger attribution.

The existing search cases cannot catch erased fetch consent or merged Exa tool
rows. These tests exercise the settings/read and ledger owners, not a producer.
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft7Validator

import pytest

from config.v2_config import ModelHubConfig
from core.handlers.model_hub import hosted_tools
from core.handlers.model_hub.usage import BoundedUsageLedger, ServiceCatalogEntry, ServiceUsageCall
from tests.test_model_hub_search_settings import _config, _disk_service, TAVILY, EXA, DEEPSEEK

LOCAL, FREE_EXA, JINA = "src_localfetch", "src_exakeyless", "src_jinakeyless"
NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
FETCH_SETTINGS = {
    "enabled": True,
    "executor_order": [JINA, TAVILY, LOCAL, FREE_EXA],
    "executor_order_kinds": {JINA: "service", TAVILY: "service", LOCAL: "local", FREE_EXA: "service"},
    "services": {LOCAL: {"enabled": False}, FREE_EXA: {"enabled": False}, JINA: {"enabled": False}},
}


def fetch_config(settings=None):
    raw = _config({"services": {TAVILY: {"enabled": False}}, "service_order": [EXA, TAVILY]}).to_payload()
    raw["hosted_tools"]["web_fetch"] = FETCH_SETTINGS if settings is None else settings
    return ModelHubConfig.from_payload(raw)


@pytest.mark.asyncio
async def test_fetch_consent_survives_v2_reload_and_unrelated_save(tmp_path):
    """Fetch activation must not erase persisted built-in opt-outs on unrelated saves."""
    config = fetch_config()
    assert config.to_payload()["hosted_tools"]["web_fetch"] == FETCH_SETTINGS
    service, path, _ = _disk_service(tmp_path, config)
    await service.set_hosted_tool("x_search", {"enabled": False})
    saved = json.loads(path.read_text())["model_hub"]
    assert saved["hosted_tools"]["web_fetch"] == FETCH_SETTINGS
    assert {s["id"] for s in saved["sources"]}.isdisjoint({LOCAL, FREE_EXA, JINA})
    assert ModelHubConfig.from_payload(saved).to_payload()["hosted_tools"]["web_fetch"] == FETCH_SETTINGS


@pytest.mark.parametrize("patch", [{"services": {JINA: {"enabled": True}}}, {"services": {JINA: {"enabled": None}}}, {"executor_order": None}])
def test_fetch_put_merges_only_present_fields(patch):
    config = fetch_config()
    result = hosted_tools.apply_hosted_tool_put(config, "web_fetch", patch).to_payload()
    assert result["services"][LOCAL] == {"enabled": False}
    assert result["services"][FREE_EXA] == {"enabled": False}
    if "executor_order" in patch:
        assert "executor_order" not in result and "executor_order_kinds" not in result
    elif patch["services"][JINA]["enabled"] is None:
        assert JINA not in result["services"]
    else:
        assert result["services"][JINA] == {"enabled": True}


@pytest.mark.parametrize("damaged", [{"services": {JINA: {"enabled": "false"}}}, {"executor_order": 42}])
def test_fetch_damage_disables_only_fetch(damaged):
    config = fetch_config(damaged)
    assert config.hosted_tools["web_fetch"].enabled is False
    assert config.hosted_tools["web_search"].enabled is True


def test_fetch_damaged_roles_preserve_consent():
    config = fetch_config({**FETCH_SETTINGS, "executor_order_kinds": {LOCAL: "model"}})
    assert config.hosted_tools["web_fetch"].executor_order_kinds is None
    assert config.to_payload()["hosted_tools"]["web_fetch"]["services"] == FETCH_SETTINGS["services"]


@pytest.mark.parametrize("window", ["7d", "24h"])
def test_future_fetch_ledger_rows_do_not_merge_shared_exa_identity(tmp_path, window):
    """WF-1: search/fetch share cooldown identity, never usage attribution, even after a search write."""
    path = tmp_path / "usage.json"
    def row(tool, requests):
        counters = {"requests": requests, "billable_requests": 0, "uncertain_requests": 0, "reported_requests": 0, "reported_usd": 0.0}
        return {"day": "2026-10-24", "source_id": FREE_EXA, "service_id": "exa/mcp", **({"tool": tool} if tool else {}), **counters,
                "last_metered_at": NOW.isoformat(), "hours": [{"key": "2026-10-24T12:00:00Z", **counters, "last_metered_at": NOW.isoformat()}], "hourly_history_complete": True}
    path.write_text(json.dumps([row(None, 2), row("web_fetch", 3)]))
    ledger = BoundedUsageLedger(path, now=lambda: NOW)
    catalog = [ServiceCatalogEntry("exa/mcp", "Exa (free)", 0, "2026-10-22")]
    report = ledger.report(window=window, now=NOW, service_catalog=catalog)
    assert {(r.get("tool", "web_search"), r["requests"]) for r in report["services"]} == {("web_search", 2), ("web_fetch", 3)}
    ledger.record_many([ServiceUsageCall(FREE_EXA, "exa/mcp", NOW)])
    reopened = BoundedUsageLedger(path, now=lambda: NOW).report(window=window, now=NOW, service_catalog=catalog)
    assert {(r.get("tool", "web_search"), r["requests"]) for r in reopened["services"]} == {("web_search", 3), ("web_fetch", 3)}


def test_hop_reader_declares_function_only_fetch_by_default():
    """WF-3: a supplied C-6 entry registers the canonical function without flag patches."""
    from types import SimpleNamespace
    from modules.agents.model_hub import ModelHubLaunch
    from modules.agents.vibey.models import selection_from_hop
    from modules.agents.vibey.tools import hosted_tools as turn_tools
    hop = ModelHubLaunch(backend="vibey", channel="hub", requested_model="m", target_model="m", runtime_model="m",
        source_id="src_test0001", source_identity="sha256:fixture", gateway_base_url="http://hub.invalid/vibey",
        gateway_token="fixture", protocol="openai_chat", provider="openai").to_hop_resolution()
    hop["hosted_tools"] = {"web_fetch": {"native": "unsupported", "replay": "unsupported"}}
    selection = selection_from_hop(hop)
    assert selection.endpoint.hosted_tools["web_fetch"].mode == "proxy"
    assert [tool.spec.name for tool in turn_tools(SimpleNamespace(preflight=selection, current=selection))] == ["web_fetch"]


def test_active_read_candidates_and_turn_preserve_saved_fetch_consent(tmp_path):
    """The visible row and frozen candidates agree without rewriting saved choices."""
    config = fetch_config()
    service, path, _ = _disk_service(tmp_path, config)
    before = path.read_bytes()
    fetch = next(t for t in service.hosted_tools_status()["hosted_tools"]["tools"] if t["name"] == "web_fetch")
    turn = service.freeze_turn_hosted_tools(config)
    assert "web_fetch" in turn.tools
    assert [slot.source_id for slot in turn.executors["web_fetch"]] == fetch["effective_order"]
    assert {slot.source_id: slot.enabled for slot in turn.executors["web_fetch"]} == {
        LOCAL: False, FREE_EXA: False, JINA: False, EXA: True, TAVILY: True, DEEPSEEK: True,
    }
    assert path.read_bytes() == before


@pytest.mark.parametrize("settings,expected", [
    ({}, [LOCAL, EXA, TAVILY, DEEPSEEK, FREE_EXA, JINA]),
    ({"executor_order": [JINA, TAVILY]}, [LOCAL, EXA, TAVILY, DEEPSEEK, FREE_EXA, JINA]),
    ({"executor_order": [JINA, TAVILY], "executor_order_kinds": {JINA: "service", TAVILY: "service"}}, [LOCAL, EXA, DEEPSEEK, JINA, TAVILY, FREE_EXA]),
    ({"executor_order": [LOCAL, JINA], "executor_order_kinds": {LOCAL: "service", JINA: "service"}}, [LOCAL, EXA, TAVILY, DEEPSEEK, JINA, FREE_EXA]),
    ({"services": {LOCAL: {"enabled": False}, EXA: {"enabled": False}}}, [TAVILY, DEEPSEEK, FREE_EXA, JINA, LOCAL, EXA]),
])
def test_active_fetch_read_owns_independent_order_without_writing(tmp_path, settings, expected):
    """Search's disabled Tavily/order cannot decide fetch consent or deliberate placement."""
    from tests.test_model_hub_api import _api_response_registry
    service, path, _ = _disk_service(tmp_path, fetch_config(settings))
    before = path.read_bytes()
    payload = service.hosted_tools_status()["hosted_tools"]
    Draft7Validator({"$ref": "model-hub/hosted-tools-status.schema.json"}, registry=_api_response_registry()).validate(payload)
    fetch = next(t for t in payload["tools"] if t["name"] == "web_fetch")
    assert fetch["effective_order"] == expected
    assert [c["source_id"] for c in fetch["executor_candidates"]] == expected
    assert len(expected) == len(set(expected))
    assert all(c["settings"].keys() == {"enabled"} for c in fetch["executor_candidates"])
    assert path.read_bytes() == before


def test_fetch_catalog_capability_and_catalog_off_preserve_identity(monkeypatch):
    """Only declared official APIs qualify; a catalog kill switch removes candidates, not saved consent."""
    from vibe.model_hub_runtime import fetch_catalog
    from core.handlers.model_hub.fetch_readers import fetch_candidates
    config = fetch_config({"services": {JINA: {"enabled": True}}})
    source = config.sources[-1]
    assert fetch_catalog.fetch_service(source)["service_id"] == "deepseek/fetch"
    for invalid in (replace(source, kind="subscription"), replace(source, base_url="https://relay.invalid/v1"), config.sources[0]):
        assert fetch_catalog.fetch_service(invalid) is None
    rows = deepcopy(fetch_catalog.fetch_service_catalog())
    next(r for r in rows if r.get("source_id") == JINA)["available"] = False
    monkeypatch.setattr(fetch_catalog, "fetch_service_catalog", lambda: rows)
    assert JINA not in [c.source.id for c in fetch_candidates(config)]
    assert config.to_payload()["hosted_tools"]["web_fetch"]["services"][JINA] == {"enabled": True}
    assert fetch_catalog.builtin_tool_identity(JINA) is not None
    schema = Path(__file__).parents[1] / "vibe/data/web_fetch_services.schema.json"
    Draft7Validator(json.loads(schema.read_text())).validate(list(rows))


@pytest.mark.asyncio
@pytest.mark.parametrize("sid,service_id", [(FREE_EXA, "exa/mcp"), (JINA, "jina/reader")])
async def test_synthetic_fetch_cooldown_survives_save_and_retry_is_shared(tmp_path, sid, service_id):
    """Retry uses the same synthetic identity/generation as other tools; no request is made."""
    from config.v2_config import model_hub_builtin_source
    from core.handlers.model_hub.search_services import ServiceCooldowns, ServiceResult
    from core.handlers.model_hub.adapter import SearchResponse
    service, _, _ = _disk_service(tmp_path, fetch_config({}))
    service.now = lambda: NOW
    service._service_cooldowns = ServiceCooldowns()
    source = model_hub_builtin_source(sid)
    generation = service._service_cooldowns.advance(source, service_id)
    service._service_cooldowns.settle(source, service_id, generation,
        ServiceResult({"status": "failed"}, None, False, False, False, None, True),
        SearchResponse(429, {"Retry-After": "20"}, b""), NOW)
    await service.set_hosted_tool("x_search", {"enabled": False})
    assert service._service_cooldowns.current(source, service_id, generation)
    fetch = next(t for t in service.hosted_tools_status()["hosted_tools"]["tools"] if t["name"] == "web_fetch")
    assert next(c for c in fetch["executor_candidates"] if c["source_id"] == sid)["cooldown_until"] is not None
    from core.handlers.model_hub.rpc import dispatch_model_hub_rpc
    await dispatch_model_hub_rpc(service, "clear_service_cooldown", {"source_id": sid, "tool": "web_fetch"})
    assert service._service_cooldowns.until(source, service_id, NOW) is None
    assert not service._service_cooldowns.current(source, service_id, generation)


@pytest.mark.asyncio
@pytest.mark.parametrize("sid,service_id,label", [(LOCAL, "local/fetch", "Local"), (FREE_EXA, "exa/mcp", "Exa (free)"), (JINA, "jina/reader", "Jina Reader (free)")])
async def test_synthetic_fetch_evidence_and_usage_survive_without_credential_sources(tmp_path, sid, service_id, label):
    """Synthetic identities join both durable readers without becoming credential Sources."""
    from config.v2_config import model_hub_builtin_source
    from core.handlers.model_hub.capability_evidence import CapabilityEvidenceStore
    service, _, _ = _disk_service(tmp_path, fetch_config())
    service.now = lambda: NOW
    await service.record_hosted_success(model_hub_builtin_source(sid), service_id, "web_fetch", "service")
    await service.set_hosted_tool("x_search", {"enabled": False})
    reopened = CapabilityEvidenceStore(service.capability_evidence.path)
    assert [(r.source_id, r.feature.name) for r in reopened.records()] == [(sid, "web_fetch")]
    path = tmp_path / "future-usage.json"
    path.write_text(json.dumps([{"day": "2026-10-24", "source_id": sid, "service_id": service_id, "tool": "web_fetch",
        "requests": 1, "billable_requests": 0, "uncertain_requests": 0, "reported_requests": 0, "reported_usd": 0.0}]))
    service.usage = BoundedUsageLedger(path, now=lambda: NOW)
    report = service.usage_summary(window="7d")
    [row] = report["services"]
    assert row["label"] == label and row["tool"] == "web_fetch"
    assert row["priced"] is True and row["api_cost_usd"] == 0
    assert sid not in {s.id for s in service.store.load().sources}


@pytest.mark.asyncio
async def test_local_retry_is_not_a_whole_executor_cooldown(tmp_path):
    from core.handlers.model_hub.service import ModelHubError
    service, _, _ = _disk_service(tmp_path, fetch_config())
    with pytest.raises(ModelHubError) as exc:
        await service.clear_service_cooldown(LOCAL, tool="web_fetch")
    assert exc.value.code == "source_not_found"


@pytest.mark.parametrize("body,status", [({}, 200), ({"unexpected": True}, 400)])
def test_fetch_retry_http_uses_its_own_tool_and_existing_member_boundary(tmp_path, monkeypatch, body, status):
    """The public empty-body route reaches Jina's fetch service, not search's unknown service."""
    from tests.test_model_hub_api import app, csrf_headers
    from vibe import ui_server, model_hub_client
    from core.handlers.model_hub.rpc import dispatch_model_hub_rpc
    from vibe.authorization import http_authorization_policy
    service, _, _ = _disk_service(tmp_path, fetch_config())
    calls = []
    async def rpc(operation, payload):
        calls.append((operation, payload))
        return await dispatch_model_hub_rpc(service, operation, payload)
    monkeypatch.setattr(model_hub_client, "_rpc", rpc)
    monkeypatch.setattr(ui_server, "_model_hub_service", model_hub_client.ModelHubRemoteService)
    client = app.test_client()
    route = f"/api/models/hosted-tools/web_fetch/services/{JINA}/retry"
    response = client.post(route, json=body, headers=csrf_headers(client))
    assert response.status_code == status
    assert calls == ([("clear_service_cooldown", {"source_id": JINA, "service_id": None, "tool": "web_fetch"})] if status == 200 else [])
    assert http_authorization_policy("POST", route).minimum_role == "member"
    assert http_authorization_policy("GET", route).minimum_role == "owner"


@pytest.mark.parametrize("tool", [[], {}])
def test_malformed_future_usage_tool_disables_only_that_row(tmp_path, tool):
    """Damaged optional attribution must not crash the complete usage read."""
    path = tmp_path / "usage.json"
    path.write_text(json.dumps([{"day": "2026-10-24", "source_id": FREE_EXA, "service_id": "exa/mcp", "tool": tool, "requests": 1}]))
    assert BoundedUsageLedger(path, now=lambda: NOW).report(window="7d", now=NOW)["services"] == []
