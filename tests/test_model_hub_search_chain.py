"""MH-SEARCH-SERVICES-001: producer contracts at the chain/custody boundary.

Existing hosted executor tests cover model-only calls, not adapter search calls,
service-only retries or one-shot trials. These tables exercise real resolution,
settlement, C-1 serialization and metering; only the credential adapter is faked.
"""

import asyncio
import copy
import json
import threading
from dataclasses import replace
from datetime import timedelta

import pytest

from config.v2_config import (
    KEYLESS_EXA_SERVICE_ID,
    KEYLESS_EXA_SOURCE_ID,
    ModelHubConfig, ModelHubSearchServiceSettings, ModelHubSourceStateConfig,
    model_hub_keyless_exa_source,
)
from core.handlers.model_hub import hosted_tools, search_chain
from core.handlers.model_hub.adapter import SearchCredentialCheck, SearchResponse
from core.handlers.model_hub.capability_evidence import EvidenceFeature
from tests.test_model_hub_api import _service
from tests.test_model_hub_hosted_executor import _hosted_result
from tests.test_model_hub_l3 import NOW
from tests.test_model_hub_search_settings import (
    DEEPSEEK, EXA, MODEL, TAVILY, _config, _web,
)

IDS = {"deepseek": DEEPSEEK, "tavily": TAVILY, "exa": EXA}
ROW = {"url": "https://example.test/a", "title": "Title", "summary": "Model excerpt",
       "snippet": "UI excerpt", "content": "Tavily excerpt", "highlights": ["First", "Second"],
       "date": "2026-10-21", "published_date": "today", "publishedDate": "yesterday"}


def owner(tmp_path, vendors=("exa",), *, web=None, model=False, include_keyless=False):
    service, store, adapter = _service(tmp_path)
    config = _config(web)
    selected = [IDS[vendor] for vendor in vendors]
    config.sources = [next(s for s in config.sources if s.id == sid) for sid in selected] + (
        [next(s for s in config.sources if s.id == MODEL)] if model else []
    )
    setting = config.hosted_tools["web_search"]
    if not include_keyless:
        services = dict(setting.services)
        services.setdefault(KEYLESS_EXA_SOURCE_ID, ModelHubSearchServiceSettings(enabled=False))
        setting = replace(setting, services=services)
    if not include_keyless and setting.executor_order is None and not (web and web.get("service_order")):
        automatic_without_keyless = list(selected)
        if model:
            automatic_without_keyless.append(MODEL)
        config.hosted_tools["web_search"] = replace(
            setting,
            executor_order=tuple(automatic_without_keyless),
            executor_order_kinds={sid: "model" if sid == MODEL else "service" for sid in automatic_without_keyless},
        )
    else:
        config.hosted_tools["web_search"] = setting
    store.config = config
    service.now = lambda: NOW
    return service


def wire(status=200, body=None, *, error="", headers=None):
    return SearchResponse(status, headers or {}, json.dumps(body if body is not None else {"results": [ROW]}).encode(),
                          error, response_received_at=NOW)


def answers(monkeypatch, service, *responses, sent=True):
    calls = []
    pending = iter(responses)

    async def search(source_id, vendor, service_id, credential_ref, request, *, on_sent):
        calls.append((source_id, vendor, service_id, credential_ref, request))
        if sent:
            await on_sent()
        value = next(pending)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(service.adapter, "search", search, raising=False)
    return calls


async def run(service, query="query", turn=None):
    return await service.run_hosted_tool(
        "web_search", {"arguments": {"query": query}}, turn=turn or service.freeze_turn_hosted_tools(service.store.load()),
    )


def summary(service):
    return service.usage_summary(days=7)


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor,expected_request,snippet,cost", [
    ("deepseek", {"query": "query", "count": 5}, "Model excerpt", .00075),
    ("tavily", {"query": "query", "max_results": 5, "search_depth": "basic", "include_answer": False}, "Tavily excerpt", .008),
    ("exa", {"query": "query", "numResults": 5, "type": "auto", "contents": {"highlights": True}}, "First\nSecond", .007),
])
async def test_vendor_request_mapping_meter_and_evidence(tmp_path, monkeypatch, vendor, expected_request, snippet, cost):
    """MH-SEARCH-SERVICES-001: one catalog request produces bounded C-1 facts, usage and service evidence."""
    service = owner(tmp_path, (vendor,))
    calls = answers(monkeypatch, service, wire())
    result = await run(service)
    assert calls == [(IDS[vendor], vendor, f"{vendor}/search", f"cred_{IDS[vendor]}", expected_request)]
    loaded = _hosted_result("web_search", result)
    assert loaded.record.status == "completed"
    assert loaded.record.sources[0].snippet == snippet
    assert result["execution"]["executor_origin"] == {"provider": vendor, "api": "search_api", "model": f"{vendor}/search"}
    assert result["record"]["attempts"] == [{"source_id": IDS[vendor], "label": vendor, "kind": "service",
                                             "outcome": "completed", "cost": {"basis": "list", "usd": cost}}]
    usage = summary(service)
    assert usage["services"][0]["requests"] == usage["services"][0]["billable_requests"] == 1
    assert usage["totals"]["requests"] == 0
    assert usage["sources"] == []
    assert _web(service)["status"]["latest_ok"]["mode"] == "service"


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor,status,tag,error,expected,health,cooling", [
    ("exa", 401, None, "", "service_key_invalid", "credential_revoked", False),
    ("deepseek", 402, None, "", "service_balance", "balance_exhausted", False),
    ("tavily", 432, None, "", "service_limit", "spend_limit_reached", False),
    ("tavily", 433, None, "", "service_limit", "spend_limit_reached", False),
    ("exa", 402, "API_KEY_BUDGET_EXCEEDED", "", "service_limit", "spend_limit_reached", False),
    ("exa", 402, "TEAM_BUDGET_EXCEEDED", "", "service_limit", "spend_limit_reached", False),
    ("exa", 402, "NEW_TAG", "", "service_balance", "balance_exhausted", False),
    ("exa", 403, "PROHIBITED_CONTENT", "", "service_refused", None, False),
    ("exa", 403, "CONTENT_FILTER_ERROR", "", "service_refused", None, False),
    ("exa", 403, "FEATURE_DISABLED", "", "service_unavailable", None, True),
    ("exa", 403, "NEW_TAG", "", "service_unavailable", None, True),
    ("deepseek", 403, None, "", "service_unavailable", None, True),
    ("tavily", 422, None, "", "service_refused", None, False),
    ("exa", 400, None, "", "service_refused", None, False),
    ("exa", 408, None, "", "service_unavailable", None, True),
    ("exa", 429, None, "", "service_unavailable", None, True),
    ("exa", 500, None, "", "service_unavailable", None, True),
    ("exa", 503, None, "", "service_unavailable", None, True),
    ("exa", 302, None, "redirect", "service_unavailable", None, True),
    ("exa", 404, None, "", "service_unavailable", None, True),
    ("exa", 405, None, "", "service_unavailable", None, True),
    ("exa", 410, None, "", "service_unavailable", None, True),
    ("exa", None, None, "connection", "service_unavailable", None, True),
])
async def test_outcome_table_writes_only_classified_health(tmp_path, monkeypatch, vendor, status, tag, error, expected, health, cooling):
    service = owner(tmp_path, (vendor,))
    before = service.store.load().sources[0].state
    answers(monkeypatch, service, wire(status, {"tag": tag, "error": "query detail"}, error=error))
    result = await run(service)
    assert result["record"]["error_code"] == expected
    assert result["record"]["attempts"][0]["error_code"] == expected
    source = service.store.load().sources[0]
    if health:
        assert source.state == ModelHubSourceStateConfig(status="needs_action", detail_key=f"models.source.needs_action.{health}")
    else:
        assert source.state == before
    assert (service._service_cooldowns.until(source, f"{vendor}/search", NOW) is not None) == cooling
    assert not service.hosted_evidence().unsupported(source, f"{vendor}/search", EvidenceFeature.hosted("web_search", "call"))
    if status is not None:
        assert result["execution"]["cost"] == {"basis": "list", "usd": 0}
    if expected == "service_refused":
        assert "query detail" in result["record"]["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor,status,body,error,sent,usd,billable,uncertain", [
    ("deepseek", 200, {"results": []}, "", True, 0, 0, 0),
    ("exa", 200, {"results": []}, "", True, .007, 1, 0),
    ("tavily", 200, {"results": []}, "", True, .008, 1, 0),
    ("deepseek", 200, {}, "", True, None, 0, 1),
    ("deepseek", 200, {"results": [{"url": "file:///bad"}]}, "", True, .00075, 1, 0),
    ("exa", 200, {}, "body_too_large", True, .007, 1, 0),
    ("tavily", 200, {}, "timeout", True, .008, 1, 0),
    ("deepseek", 200, {}, "timeout", True, None, 0, 1),
    ("deepseek", None, {}, "timeout", True, 0, 0, 0),
    ("exa", None, {}, "timeout", True, None, 0, 1),
    ("deepseek", None, {}, "connection", True, None, 0, 1),
    ("exa", None, {}, "connection", True, None, 0, 1),
    ("exa", 401, {}, "timeout", True, 0, 0, 0),
    ("tavily", 402, {}, "connection", True, 0, 0, 0),
    ("deepseek", None, {}, "connection", False, 0, 0, 0),
    ("exa", None, {}, "timeout", False, 0, 0, 0),
])
async def test_billing_is_a_function_of_sent_status_and_original_results(tmp_path, monkeypatch, vendor, status, body, error, sent, usd, billable, uncertain):
    service = owner(tmp_path, (vendor,))
    answers(monkeypatch, service, wire(status, body, error=error), sent=sent)
    result = await run(service)
    attempt = result["record"]["attempts"][0]
    if usd is None:
        assert "cost" not in attempt and "cost" not in result["execution"]
    else:
        assert attempt["cost"] == {"basis": "list", "usd": usd}
    rows = summary(service)["services"]
    if sent:
        assert (rows[0]["requests"], rows[0]["billable_requests"], rows[0]["uncertain_requests"]) == (1, billable, uncertain)
    else:
        assert rows == [] and attempt["outcome"] == "failed"
    _hosted_result("web_search", result)


@pytest.mark.asyncio
@pytest.mark.parametrize("reported,expected", [(0, 0), (.012, .012), (.7, .7), (.701, None), (-1, None), (True, None), ("0.01", None), (float("nan"), None), (float("inf"), None), (10**100, None)])
async def test_reported_cost_is_bounded_estimated_and_metered(tmp_path, monkeypatch, reported, expected):
    service = owner(tmp_path)
    answers(monkeypatch, service, wire(body={"results": [ROW], "costDollars": {"total": reported}}))
    result = await run(service)
    assert result["execution"]["cost"] == (
        {"usd": expected, "basis": "reported", "estimate": True} if expected is not None else {"usd": .007, "basis": "list"}
    )
    assert summary(service)["services"][0]["reported_requests"] == int(expected is not None)


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [
    ({"results": []}, "completed"), ({}, "failed"), ({"results": None}, "failed"),
    ({"results": [{"url": "ftp://example.test"}, {"url": "https://x/" + "a" * 2048}]}, "failed"),
    ({"results": [None, 1, {"url": "https://[bad"}]}, "failed"),
])
async def test_empty_results_complete_but_nonempty_unmappable_results_fail_over(tmp_path, monkeypatch, body, status):
    service = owner(tmp_path, ("exa", "tavily"))
    calls = answers(monkeypatch, service, wire(body=body), wire())
    result = await run(service)
    assert result["record"]["status"] == "completed"
    assert len(calls) == (1 if status == "completed" else 2)
    if status == "failed":
        assert result["record"]["attempts"][0]["error_code"] == "service_unreadable"
        assert result["execution"]["failed_attempts"] == [{"source_id": EXA, "billing": "metered", "cost": {"basis": "list", "usd": .007}}]


@pytest.mark.asyncio
async def test_mapping_bounds_and_decode_are_off_loop(tmp_path, monkeypatch):
    from core.handlers.model_hub import search_chain

    service = owner(tmp_path, ("deepseek",), web={"services": {DEEPSEEK: {"results": 2}}})
    row = {"url": "https://x/" + "u" * 2038, "title": "t" * 900, "summary": "s" * 3000, "date": "d" * 100}
    rows = [{"url": "data:bad"}, row, {**row, "url": "https://second.test"}, ROW]
    answers(monkeypatch, service, wire(body={"results": rows}))
    mapped = search_chain.map_response
    threads = []

    def recording(*args, **kwargs):
        threads.append(threading.get_ident())
        return mapped(*args, **kwargs)

    monkeypatch.setattr(search_chain, "map_response", recording)
    result = await run(service)
    assert len(result["record"]["sources"]) == 2
    first = result["record"]["sources"][0]
    assert [len(first[key]) for key in ("url", "title", "snippet", "page_age")] == [2048, 300, 1000, 64]
    assert threads and threading.get_ident() not in threads


@pytest.mark.parametrize("saved,kinds,expected", [
    (None, None, [EXA, TAVILY, DEEPSEEK, MODEL, KEYLESS_EXA_SOURCE_ID]),
    ([MODEL], None, [EXA, TAVILY, DEEPSEEK, MODEL, KEYLESS_EXA_SOURCE_ID]),
    ([MODEL, DEEPSEEK], None, [EXA, TAVILY, DEEPSEEK, MODEL, KEYLESS_EXA_SOURCE_ID]),
    ([MODEL, DEEPSEEK], {MODEL: "model", DEEPSEEK: "service"}, [EXA, TAVILY, MODEL, DEEPSEEK, KEYLESS_EXA_SOURCE_ID]),
    ([DEEPSEEK], {DEEPSEEK: "service"}, [DEEPSEEK, EXA, TAVILY, MODEL, KEYLESS_EXA_SOURCE_ID]),
    ([EXA, MODEL], {EXA: "model", MODEL: "model"}, [EXA, TAVILY, DEEPSEEK, MODEL, KEYLESS_EXA_SOURCE_ID]),
])
def test_order_placement_is_read_only_and_survives_reload(tmp_path, saved, kinds, expected):
    web = {"service_order": [EXA, TAVILY]}
    if saved is not None:
        web["executor_order"] = saved
    if kinds is not None:
        web["executor_order_kinds"] = kinds
    service = owner(tmp_path, ("tavily", "deepseek", "exa"), web=web, model=True)
    before = copy.deepcopy(service.store.load().to_payload())
    assert _web(service)["effective_order"] == expected
    assert [slot.source_id for slot in service.freeze_turn_hosted_tools(service.store.load()).executors["web_search"]] == expected
    service.store.config = ModelHubConfig.from_payload(before)
    assert _web(service)["effective_order"] == expected
    assert service.store.load().to_payload() == before


@pytest.mark.asyncio
async def test_turn_freezes_preference_candidates_results_and_disabled_tail(tmp_path, monkeypatch):
    service = owner(tmp_path, ("exa", "deepseek"), model=True,
                    web={"prefer_native": False, "services": {EXA: {"results": 2}, DEEPSEEK: {"enabled": False}}})
    turn = service.freeze_turn_hosted_tools(service.store.load())
    assert [slot.source_id for slot in turn.executors["web_search"]] == [EXA, MODEL, DEEPSEEK, KEYLESS_EXA_SOURCE_ID]
    model = service.store.load().sources[-1]
    facts = service.hosted_tools_for_hop(model, "grok-4.6", turn)["web_search"]
    assert facts == {"native": "unsupported", "replay": "unsupported"}
    await service.set_hosted_tool("web_search", {"prefer_native": True, "services": {EXA: {"results": 9}, DEEPSEEK: {"enabled": True}}})
    calls = answers(monkeypatch, service, wire())
    await run(service, turn=turn)
    assert calls[0][-1]["numResults"] == 2
    assert service.hosted_tools_for_hop(model, "grok-4.6", turn)["web_search"] == facts
    fresh = service.freeze_turn_hosted_tools(service.store.load())
    assert fresh.prefer_native is True and fresh.executors["web_search"][0].results == 9
    assert service.hosted_tools_for_hop(model, "grok-4.6", fresh)["web_search"]["native"] == "assumed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mix,code,skips", [
    ("disabled", "search_sources_blocked", ["disabled", "disabled", "disabled"]),
    ("blocked", "search_sources_blocked", ["blocked", "disabled", "disabled"]),
    ("cooling", "service_unavailable", ["cooling", "disabled", "disabled"]),
    ("unavailable", "service_unavailable", ["unavailable", "disabled", "disabled"]),
    ("long", "search_query_too_long", ["query_too_long", "disabled", "disabled"]),
    ("blocked_long", "search_sources_blocked", ["blocked", "disabled", "disabled"]),
    ("cooling_long", "service_unavailable", ["cooling", "disabled", "disabled"]),
])
async def test_skip_precedence_terminal_reason_and_attempts_without_execution(tmp_path, mix, code, skips):
    service = owner(tmp_path, ("deepseek", "exa"), web={"services": {EXA: {"enabled": False}, DEEPSEEK: {"enabled": mix != "disabled"}}})
    source = service.store.load().sources[0]
    if mix.startswith("blocked"):
        source.state = ModelHubSourceStateConfig(status="needs_action", detail_key="models.source.needs_action.credential_revoked")
    elif mix.startswith("cooling"):
        source.state = ModelHubSourceStateConfig(status="cooldown", detail_key="models.source.cooldown.rate_limited", retry_at=(NOW + timedelta(seconds=70)).isoformat())
    turn = service.freeze_turn_hosted_tools(service.store.load())
    if mix == "unavailable":
        source.credential_generation = "vp_" + "2" * 32
    result = await run(service, "x" * 129, turn)
    assert result["record"]["error_code"] == code
    assert [attempt["skipped"] for attempt in result["record"]["attempts"]] == skips
    assert "execution" not in result
    assert all("cost" not in attempt for attempt in result["record"]["attempts"])
    assert summary(service)["services"] == []
    if mix.startswith("blocked"):
        assert result["record"]["attempts"][0]["needs_action"].endswith("credential_revoked")
    if mix == "long":
        assert "128" in result["record"]["error"]
    _hosted_result("web_search", result)


@pytest.mark.asyncio
async def test_keyless_producer_writes_request_only_zero_cost_usage(tmp_path, monkeypatch):
    """KEX-2: an admitted keyless call records one request, zero billable requests and zero cost."""
    service = owner(
        tmp_path,
        ("exa",),
        web={
            "enabled": True,
            "executor_order": [KEYLESS_EXA_SOURCE_ID],
            "executor_order_kinds": {KEYLESS_EXA_SOURCE_ID: "service"},
            "services": {KEYLESS_EXA_SOURCE_ID: {"enabled": True, "results": 3}},
        },
    )
    body = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps({
        "results": [{"url": "https://example.test/keyless", "title": "Keyless", "highlights": ["excerpt"]}],
    })}]}}
    calls = answers(monkeypatch, service, wire(body=body))
    result = await run(service)
    assert calls == [(KEYLESS_EXA_SOURCE_ID, "exa-mcp", KEYLESS_EXA_SERVICE_ID, None, {
        "type": "auto", "query": "query", "numResults": 3,
        "enableHighlights": True, "highlightsMaxCharacters": 1000, "textMaxCharacters": 1,
    })]
    assert result["record"]["attempts"] == [{
        "source_id": KEYLESS_EXA_SOURCE_ID, "label": "Exa (free)", "kind": "service",
        "outcome": "completed", "cost": {"basis": "list", "usd": 0},
    }]
    usage = summary(service)
    assert usage["services"][0]["requests"] == 1
    assert usage["services"][0]["billable_requests"] == 0


@pytest.mark.asyncio
async def test_keyless_rate_limit_settles_service_cooldown_without_persisted_source(tmp_path, monkeypatch):
    """KEX-2: synthetic keyless identity still owns the in-memory service cooldown."""
    service = owner(
        tmp_path,
        (),
        web={
            "enabled": True,
            "executor_order": [KEYLESS_EXA_SOURCE_ID],
            "executor_order_kinds": {KEYLESS_EXA_SOURCE_ID: "service"},
            "services": {KEYLESS_EXA_SOURCE_ID: {"enabled": True, "results": 3}},
        },
    )
    calls = answers(monkeypatch, service, wire(429, {"error": {"message": "slow down"}}, headers={"Retry-After": "60"}))
    first = await run(service)
    second = await run(service)
    assert len(calls) == 1
    assert first["record"]["attempts"][0]["error_code"] == "service_rate_limited"
    assert second["record"]["attempts"] == [{
        "source_id": KEYLESS_EXA_SOURCE_ID, "label": "Exa (free)", "kind": "service",
        "outcome": "skipped", "skipped": "cooling",
        "retry_at": (NOW + timedelta(seconds=60)).isoformat().replace("+00:00", "Z"),
    }]
    assert _web(service)["executor_candidates"][0]["cooldown_until"] == (NOW + timedelta(seconds=60)).isoformat().replace("+00:00", "Z")


@pytest.mark.asyncio
async def test_keyless_overlapping_searches_settle_rate_limit_before_next_admission(tmp_path, monkeypatch):
    """KEX-2: a queued keyless turn cannot invalidate the active cooldown generation."""
    service = owner(
        tmp_path,
        (),
        web={
            "enabled": True,
            "executor_order": [KEYLESS_EXA_SOURCE_ID],
            "executor_order_kinds": {KEYLESS_EXA_SOURCE_ID: "service"},
            "services": {KEYLESS_EXA_SOURCE_ID: {"enabled": True, "results": 3}},
        },
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def search(source_id, vendor, service_id, credential_ref, request, *, on_sent):
        calls.append((source_id, service_id))
        await on_sent()
        if len(calls) == 1:
            entered.set()
            await release.wait()
            return wire(429, {"error": {"message": "slow down"}}, headers={"Retry-After": "60"})
        raise AssertionError("queued keyless search reached the adapter")

    monkeypatch.setattr(service.adapter, "search", search, raising=False)
    active = asyncio.create_task(run(service))
    await entered.wait()
    queued = asyncio.create_task(run(service))
    await asyncio.sleep(0)
    release.set()
    first, second = await asyncio.gather(active, queued)
    assert calls == [(KEYLESS_EXA_SOURCE_ID, KEYLESS_EXA_SERVICE_ID)]
    assert first["record"]["attempts"][0]["error_code"] == "service_rate_limited"
    assert second["record"]["attempts"][0]["skipped"] == "cooling"


@pytest.mark.asyncio
async def test_keyless_queued_waiter_times_out_before_admission(tmp_path, monkeypatch):
    """KEX-2: lock wait consumes the attempt budget and never sends a keyless request."""
    service = owner(
        tmp_path,
        (),
        web={
            "enabled": True,
            "executor_order": [KEYLESS_EXA_SOURCE_ID],
            "executor_order_kinds": {KEYLESS_EXA_SOURCE_ID: "service"},
            "services": {KEYLESS_EXA_SOURCE_ID: {"enabled": True, "results": 3}},
        },
    )
    monkeypatch.setattr(search_chain, "ATTEMPT_SECONDS", 0.01)
    await service._keyless_exa_attempt_lock.acquire()
    calls = answers(monkeypatch, service, wire())
    try:
        result = await run(service)
    finally:
        service._keyless_exa_attempt_lock.release()
    assert calls == []
    assert result["record"]["attempts"] == [{
        "source_id": KEYLESS_EXA_SOURCE_ID, "label": "Exa (free)", "kind": "service",
        "outcome": "skipped", "skipped": "unavailable",
    }]


@pytest.mark.asyncio
async def test_keyless_retry_branch_accepts_the_synthetic_source(tmp_path):
    """KEX-3: a built-in cooldown clears without a credential Source or network call."""
    service = owner(tmp_path, ("exa",))
    source = model_hub_keyless_exa_source()
    generation = service._service_cooldowns.advance(source, KEYLESS_EXA_SERVICE_ID)
    assert service._service_cooldowns.current(source, KEYLESS_EXA_SERVICE_ID, generation)
    assert await service.clear_service_cooldown(KEYLESS_EXA_SOURCE_ID, KEYLESS_EXA_SERVICE_ID) == {}
    assert not service._service_cooldowns.current(source, KEYLESS_EXA_SERVICE_ID, generation)


@pytest.mark.asyncio
async def test_service_retry_advice_backoff_and_clear_are_independent_of_model_calls(tmp_path, monkeypatch):
    service = owner(tmp_path, ("deepseek",))
    source = service.store.load().sources[0]
    clock = [NOW]
    service.now = lambda: clock[0]
    answers(monkeypatch, service, wire(503, headers={"Retry-After": "900"}), *(wire(500) for _ in range(4)), wire())
    await run(service)
    assert source.state.status == "standby"
    assert _web(service)["executor_candidates"][0]["cooldown_until"] == (NOW + timedelta(seconds=900)).isoformat().replace("+00:00", "Z")
    service._reserve_settlement_generation(source.id)  # A model call does not touch the service fence.
    assert (await run(service))["record"]["attempts"][0]["skipped"] == "cooling"
    clock[0] += timedelta(seconds=901)
    for delay in (60, 120, 240, 300):
        await run(service)
        until = service._service_cooldowns.until(source, "deepseek/search", clock[0])
        assert until == clock[0] + timedelta(seconds=delay)
        clock[0] = until + timedelta(seconds=1)
    await service.clear_service_cooldown(source.id, "deepseek/search")
    assert _web(service)["executor_candidates"][0]["cooldown_until"] is None
    assert (await run(service))["record"]["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 402, 432])
async def test_authenticating_outcomes_clear_pending_without_changing_identity(tmp_path, monkeypatch, status):
    service = owner(tmp_path, ("tavily",))
    source = service.store.load().sources[0]
    source.verification_pending = source.credential_generation = "vp_" + "1" * 32
    before = hosted_tools.identity_digest(source)
    answers(monkeypatch, service, wire(status))
    await run(service)
    current = service.store.load().sources[0]
    assert current.verification_pending is None
    assert hosted_tools.identity_digest(current) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,pending,detail", [
    (200, {"results": []}, False, None),
    (200, {}, False, None),
    (401, {}, False, "credential_revoked"),
    (402, {}, False, "balance_exhausted"),
    (400, {}, True, "spend_limit_reached"),
    (503, {}, False, "spend_limit_reached"),
])
async def test_trial_is_one_request_and_settles_by_observed_outcome(tmp_path, monkeypatch, status, body, pending, detail):
    service = owner(tmp_path)
    source = service.store.load().sources[0]
    source.state = ModelHubSourceStateConfig(status="needs_action", detail_key="models.source.needs_action.spend_limit_reached")
    await service.reenable_search_source(source.id)
    generation = service._latest_source_attempt_generation[source.id]
    assert (await service.reenable_search_source(source.id))["trial_armed"] is True
    assert service._latest_source_attempt_generation[source.id] == generation
    entered, finish = asyncio.Event(), asyncio.Event()

    async def search(*args, on_sent):
        await on_sent()
        entered.set()
        await finish.wait()
        return wire(status, body)

    monkeypatch.setattr(service.adapter, "search", search, raising=False)
    task = asyncio.create_task(run(service))
    await entered.wait()
    assert _web(service)["executor_candidates"][0]["trial_pending"] is True
    assert (await run(service))["record"]["attempts"][0]["skipped"] == "blocked"
    assert (await service.reenable_search_source(source.id))["trial_armed"] is True
    finish.set()
    await task
    assert service.search_reenable_trial_pending(source.id) == pending
    current = service.store.load().sources[0]
    assert current.state.detail_key == (f"models.source.needs_action.{detail}" if detail else None)
    assert summary(service)["services"][0]["requests"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("sent,status,vendor,expected", [
    (False, None, "exa", None), (True, None, "exa", (0, 1)),
    (True, 401, "exa", (0, 0)), (True, 200, "exa", (1, 0)),
    (True, 200, "deepseek", (0, 1)), (True, 503, "exa", (0, 0)),
])
async def test_stop_has_no_record_but_keeps_received_status_and_metering(tmp_path, monkeypatch, sent, status, vendor, expected):
    service = owner(tmp_path, (vendor,))
    source = service.store.load().sources[0]
    source.verification_pending = source.credential_generation = "vp_" + "4" * 32
    entered = asyncio.Event()

    async def search(*args, on_sent):
        if sent:
            await on_sent()
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            return SearchResponse(status, {}, None, "cancelled", sent, NOW if status else None)

    monkeypatch.setattr(service.adapter, "search", search, raising=False)
    task = asyncio.create_task(run(service))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    current = service.store.load().sources[0]
    assert (service._service_cooldowns.until(current, f"{vendor}/search", NOW) is not None) is (status == 503)
    assert (current.state.status == "needs_action") is (status == 401)
    assert (current.verification_pending is None) is (status == 200)
    rows = summary(service)["services"]
    if expected is None:
        assert rows == []
    else:
        assert (rows[0]["billable_requests"], rows[0]["uncertain_requests"]) == expected
        assert rows[0]["requests"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["new_attempt", "replace", "remove", "retry", "reenable"])
async def test_late_failures_are_fenced_and_report_supersession(tmp_path, monkeypatch, mutation):
    service = owner(tmp_path)
    source = service.store.load().sources[0]
    entered, finish = asyncio.Event(), asyncio.Event()

    async def search(*args, on_sent):
        await on_sent()
        entered.set()
        await finish.wait()
        return wire(401)

    monkeypatch.setattr(service.adapter, "search", search, raising=False)
    task = asyncio.create_task(run(service))
    await entered.wait()
    if mutation == "new_attempt":
        service._reserve_settlement_generation(source.id)
    elif mutation == "retry":
        await service.clear_service_cooldown(source.id)
    elif mutation == "reenable":
        source.state = ModelHubSourceStateConfig(status="needs_action", detail_key="models.source.needs_action.spend_limit_reached")
        await service.reenable_search_source(source.id)
    else:
        config = service._clone_config(service.store.load())
        if mutation == "replace":
            config.sources[0].credential_generation = "vp_" + "2" * 32
        else:
            config.sources = []
        await service._save_config(config)
    result = None
    finish.set()
    result = await task
    assert result["record"]["attempts"][0].get("superseded", False) is (mutation != "retry")
    current = service.store.load().sources
    if current and mutation not in {"retry"}:
        assert current[0].state.detail_key != "models.source.needs_action.credential_revoked"
    assert summary(service)["services"][0]["requests"] == 1


@pytest.mark.asyncio
async def test_free_check_is_joined_without_holding_the_mutation_lock(tmp_path, monkeypatch):
    service = owner(tmp_path, ("tavily",))
    source = service.store.load().sources[0]
    source.state = ModelHubSourceStateConfig(status="needs_action", detail_key="models.source.needs_action.spend_limit_reached")
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def check(*args):
        calls.append(args)
        entered.set()
        await finish.wait()
        return SearchCredentialCheck("pass")

    monkeypatch.setattr(service.adapter, "check_search_credential", check)
    first = asyncio.create_task(service.reenable_search_source(source.id))
    await entered.wait()
    second = asyncio.create_task(service.reenable_search_source(source.id))
    await asyncio.sleep(0)
    assert not service._mutation_lock.locked()
    finish.set()
    a, b = await asyncio.gather(first, second)
    assert a == b and len(calls) == 1
    assert service.store.load().sources[0].state.status == "standby"


@pytest.mark.asyncio
async def test_search_read_and_chain_activate_together(tmp_path, monkeypatch):
    """MH-SEARCH-SERVICES-001: shipped defaults expose the same active services that the frozen turn executes.

    The default-on reader and producer share one predicate; no activation monkeypatch is allowed.
    """
    service = owner(tmp_path, ("deepseek", "exa"), web={"prefer_native": False}, include_keyless=True)
    calls = answers(monkeypatch, service, wire())
    frozen = service.freeze_turn_hosted_tools(service.store.load())
    visible = _web(service)
    assert frozen.prefer_native is False
    assert [(slot.source_id, slot.kind, slot.results, slot.label) for slot in frozen.executors["web_search"]] == [
        (EXA, "service", 5, "exa"),
        (DEEPSEEK, "service", 5, "deepseek"),
        (KEYLESS_EXA_SOURCE_ID, "service", 5, "Exa (free)"),
    ]
    assert [(row["source_id"], row["kind"]) for row in visible["executor_candidates"]] == [
        (EXA, "service"),
        (DEEPSEEK, "service"),
        (KEYLESS_EXA_SOURCE_ID, "service"),
    ]
    assert {vendor["id"] for vendor in visible["service_vendors"]} == {"exa", "tavily", "deepseek", "exa-mcp"}
    answer = await run(service, turn=frozen)
    assert len(calls) == 1 and calls[0][0] == EXA
    assert answer["record"]["status"] == "completed"
    assert answer["record"]["attempts"][0]["source_id"] == EXA
    assert answer["execution"]["executor_origin"]["api"] == "search_api"


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["unreadable", "unknown", "model"])
async def test_mixed_chain_keeps_every_attempts_cost_even_when_the_last_failed_before_send(tmp_path, monkeypatch, first):
    """MH-SEARCH-SERVICES: existing model/reader tests cannot generate a mixed producer chain."""
    from config.v2_config import ModelHubHostedToolConfig
    from tests.test_model_hub_hosted_executor import BILLED, WEB_CALL, _event, _responses
    from tests.test_model_hub_l3 import LiveInvokeHandle, RawOutcomeKind, _outcome
    from tests.test_model_hub_vibey_consumer import _vibey_service

    if first == "model":
        config = _config()
        model = next(s for s in config.sources if s.id == MODEL)
        handle = LiveInvokeHandle(
            _outcome(RawOutcomeKind.HTTP_ERROR, source_id=MODEL, status=200, code="server_error", stream_started=True, usage=BILLED),
            (*_responses(WEB_CALL)[:-1], _event({"type": "response.failed", "response": {
                "status": "failed", "usage": {"input_tokens": 900, "output_tokens": 40, "cost_in_usd_ticks": 1_234_000_000},
            }})),
        )
        service = _vibey_service(tmp_path, [model], handles=[handle])
        service.store.config.sources.append(next(s for s in config.sources if s.id == EXA))
        service.store.config.hosted_tools["web_search"] = ModelHubHostedToolConfig(
            executor_order=(MODEL, EXA), executor_order_kinds={MODEL: "model", EXA: "service"},
        )
        calls = answers(monkeypatch, service, wire(None, {}, error="connection"), sent=False)
        expected = [MODEL, EXA]
    else:
        service = owner(tmp_path, ("exa", "tavily"))
        calls = []

        async def search(source_id, *args, on_sent):
            calls.append(source_id)
            if source_id == EXA:
                await on_sent()
                return wire(200, {}) if first == "unreadable" else wire(None, {}, error="connection")
            return wire(None, {}, error="connection")

        monkeypatch.setattr(service.adapter, "search", search, raising=False)
        expected = [EXA, TAVILY]
    result = await run(service)
    attempts = result["record"]["attempts"]
    executed_candidates = [row for row in attempts if row["source_id"] != KEYLESS_EXA_SOURCE_ID]
    assert [row["source_id"] for row in executed_candidates] == expected
    assert executed_candidates[-1]["cost"] == {"basis": "list", "usd": 0}
    assert len(calls) == 2
    earlier = result["execution"]["failed_attempts"][0]
    assert earlier["source_id"] == expected[0]
    assert earlier.get("cost") == executed_candidates[0].get("cost")
    if first == "unknown":
        assert "cost" not in executed_candidates[0]
    else:
        assert executed_candidates[0]["cost"]["usd"] > 0
    assert result["execution"]["executor_source_id"] == expected[-1]
    _hosted_result("web_search", result)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["replace", "remove", "later_attempt"])
async def test_free_check_cannot_repair_a_replaced_removed_or_newer_settlement(tmp_path, monkeypatch, mutation):
    """The check is asynchronous: joining tests alone cannot expose stale repairs."""
    from core.handlers.model_hub.service import ModelHubError

    service = owner(tmp_path, ("tavily",))
    source = service.store.load().sources[0]
    blocker = ModelHubSourceStateConfig(status="needs_action", detail_key="models.source.needs_action.spend_limit_reached")
    source.state = blocker
    entered, finish = asyncio.Event(), asyncio.Event()

    async def check(*args):
        entered.set()
        await finish.wait()
        return SearchCredentialCheck("pass")

    monkeypatch.setattr(service.adapter, "check_search_credential", check)
    task = asyncio.create_task(service.reenable_search_source(source.id))
    await entered.wait()
    if mutation == "later_attempt":
        service._reserve_settlement_generation(source.id)
    else:
        config = service._clone_config(service.store.load())
        if mutation == "replace":
            config.sources[0].credential_generation = "vp_" + "3" * 32
        else:
            config.sources = []
        await service._save_config(config)
    finish.set()
    if mutation == "remove":
        with pytest.raises(ModelHubError, match="source_not_found"):
            await task
    else:
        await task
        assert service.store.load().sources[0].state == blocker


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_phase", ["before_send", "after_send", "received_200"])
async def test_trial_stop_releases_only_the_reservation_and_restart_fails_closed(tmp_path, monkeypatch, cancel_phase):
    """A stopped trial has different admission/billing facts from an ordinary stopped search."""
    service = owner(tmp_path)
    source = service.store.load().sources[0]
    source.state = ModelHubSourceStateConfig(status="needs_action", detail_key="models.source.needs_action.balance_exhausted")
    await service.reenable_search_source(source.id)
    entered = asyncio.Event()

    async def search(*args, on_sent):
        if cancel_phase != "before_send":
            await on_sent()
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            return SearchResponse(200 if cancel_phase == "received_200" else None, {}, None, "cancelled")

    monkeypatch.setattr(service.adapter, "search", search, raising=False)
    task = asyncio.create_task(run(service))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service._service_cooldowns.until(service.store.load().sources[0], "exa/search", NOW) is None
    assert service.search_reenable_trial_pending(source.id) is (cancel_phase == "before_send")
    assert service.store.load().sources[0].state.status == ("standby" if cancel_phase == "received_200" else "needs_action")
    saved = service.store.load().to_payload()
    fresh = owner(tmp_path / "restart")
    fresh.store.config = ModelHubConfig.from_payload(saved)
    assert not fresh.search_reenable_trial_pending(source.id)
    assert _web(fresh)["executor_candidates"][0]["cooldown_until"] is None
    if cancel_phase != "received_200":
        assert (await run(fresh))["record"]["attempts"][0]["skipped"] == "blocked"


@pytest.mark.asyncio
async def test_attempt_deadlines_share_one_chain_budget_and_preserve_timeout_billing(tmp_path, monkeypatch):
    """A per-attempt timeout must not reset the chain deadline or lose a sent request."""
    from core.handlers.model_hub import search_chain

    service = owner(tmp_path, ("deepseek", "exa", "tavily"), web={"service_order": [EXA, TAVILY]})
    monkeypatch.setattr(search_chain, "ATTEMPT_SECONDS", .2)
    monkeypatch.setattr(search_chain, "call_timeout", lambda tool: .3)
    calls = []

    async def search(source_id, *args, on_sent):
        await on_sent()
        calls.append(source_id)
        await asyncio.Future()

    monkeypatch.setattr(service.adapter, "search", search, raising=False)
    result = await asyncio.wait_for(run(service), timeout=1)
    assert calls == [EXA, TAVILY]
    assert len(result["record"]["attempts"]) == 2
    assert all("cost" not in attempt for attempt in result["record"]["attempts"])
    assert sum(row["requests"] for row in summary(service)["services"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [401, 503])
@pytest.mark.parametrize("newer", ["model", "retry"])
async def test_health_and_cooldown_fences_each_protect_only_their_own_state(tmp_path, monkeypatch, failure, newer):
    """A shared credential does not make model settlement the owner of service cooldown."""
    service = owner(tmp_path, ("deepseek",))
    source = service.store.load().sources[0]
    entered, finish = asyncio.Event(), asyncio.Event()

    async def search(*args, on_sent):
        await on_sent()
        entered.set()
        await finish.wait()
        return wire(failure)

    monkeypatch.setattr(service.adapter, "search", search, raising=False)
    task = asyncio.create_task(run(service))
    await entered.wait()
    if newer == "model":
        service._reserve_settlement_generation(source.id)
    else:
        await service.clear_service_cooldown(source.id)
    finish.set()
    result = await task
    current = service.store.load().sources[0]
    assert (current.state.status == "needs_action") == (failure == 401 and newer == "retry")
    assert (service._service_cooldowns.until(current, "deepseek/search", NOW) is not None) == (failure == 503 and newer == "model")
    assert result["record"]["attempts"][0].get("superseded", False) == (
        (failure == 401 and newer == "model") or (failure == 503 and newer == "retry")
    )


@pytest.mark.asyncio
async def test_prefer_native_off_flows_through_the_hop_to_function_declaration_and_synthetic_history(tmp_path):
    """C-6 consumer fixture: facts alone miss a declaration/replay integration regression."""
    from core.agent_core.ai.provider import ModelRequest
    from core.agent_core.tools.hosted import hosted_tool_spec
    from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
    from modules.agents.model_hub import ModelHubRuntimeRouter
    from modules.agents.vibey.models import selection_from_hop
    from tests.agent_core.ai._wire import build_wire_request
    from tests.agent_core.ai.test_hosted_transform import ANSWER, SEARCH, SEARCH_TEXT, _follow_up, _response
    from tests.test_model_hub_vibey_consumer import _vibey_service, _validate

    config = _config({"prefer_native": False})
    service = _vibey_service(tmp_path, [next(s for s in config.sources if s.id == MODEL)])
    service.store.config.hosted_tools = config.hosted_tools
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:services", turn_id="turn-search-services")
        _validate("hop-resolution.schema.json", hop)
        selection = selection_from_hop(hop)
        assert selection.endpoint.hosted_tools["web_search"].mode == "proxy"
        request = ModelRequest(endpoint=selection.endpoint, system="system", messages=(_response(SEARCH, ANSWER), _follow_up()),
                               tools=(hosted_tool_spec("web_search"),), max_tokens=256)
        sent = (await build_wire_request(request, gateway=True)).body
        assert sent["tools"][0]["type"] == "function" and sent["tools"][0]["name"] == "web_search"
        assert not any(item.get("type") == "web_search_call" for item in sent["input"])
        assert next(item["output"] for item in sent["input"] if item.get("type") == "function_call_output") == SEARCH_TEXT
        await service.set_hosted_tool("web_search", {"prefer_native": True})
        again = await router.resolve_hop("menu-alias", process_scope="vibey:services", turn_id="turn-search-services")
        assert again["hosted_tools"]["web_search"] == hop["hosted_tools"]["web_search"]
    finally:
        await gateway.close()


@pytest.mark.parametrize("scenario", ["retarget", "retired", "new_catalog", "opt_in"])
def test_read_time_role_changes_never_duplicate_sources_or_rewrite_config(tmp_path, monkeypatch, scenario):
    """New capabilities and official retargets use the same saved-role placement rule."""
    from dataclasses import replace
    from config.v2_config import ModelHubHostedToolConfig, ModelHubSearchServiceSettings
    from vibe.model_hub_runtime import api_key_vendors

    service = owner(tmp_path, ("deepseek", "exa"), model=True,
                    web={"executor_order": [MODEL, DEEPSEEK], "executor_order_kinds": {MODEL: "model", DEEPSEEK: "model"}})
    config = service.store.load()
    deepseek = next(s for s in config.sources if s.id == DEEPSEEK)
    if scenario == "retarget":
        deepseek.base_url = "https://relay.example.test/v1"
        assert DEEPSEEK not in _web(service)["effective_order"]
        deepseek.base_url = None
    elif scenario == "retired":
        next(s for s in config.sources if s.id == EXA).vendor = "retired-search-vendor"
    else:
        original = api_key_vendors.api_key_vendor_entry
        vendor = original("xai")
        search = copy.deepcopy(original("deepseek").search)
        search.update(service_id="xai/search", auto_enroll=False)
        monkeypatch.setattr(api_key_vendors, "api_key_vendor_entry", lambda key: replace(vendor, search=search) if key == "xai" else original(key))
        if scenario == "opt_in":
            config.hosted_tools["web_search"] = ModelHubHostedToolConfig(
                executor_order=(MODEL,), executor_order_kinds={MODEL: "model"},
                services={MODEL: ModelHubSearchServiceSettings(enabled=True)},
            )
    before = copy.deepcopy(config.to_payload())
    rows = _web(service)["executor_candidates"]
    ids = [row["source_id"] for row in rows]
    assert len(ids) == len(set(ids))
    stable_rows = [row for row in rows if row["source_id"] != KEYLESS_EXA_SOURCE_ID]
    stable_ids = [row["source_id"] for row in stable_rows]
    if scenario == "retired":
        assert stable_ids == [DEEPSEEK, MODEL]
    elif scenario == "opt_in":
        assert stable_ids == [EXA, DEEPSEEK, MODEL] and all(row["kind"] == "service" for row in stable_rows)
    else:
        assert stable_ids == [EXA, DEEPSEEK, MODEL] and stable_rows[-1]["kind"] == "model"
    assert config.to_payload() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["concurrent", "recovered", "rearmed"])
async def test_connect_holds_no_mutation_lock_and_only_the_first_ready_trial_sends(tmp_path, monkeypatch, phase):
    """Connection latency cannot stall other sessions or let a losing contender fence the winner."""
    service = owner(tmp_path)
    source = service.store.load().sources[0]
    source.state = ModelHubSourceStateConfig(status="needs_action", detail_key="models.source.needs_action.balance_exhausted")
    await service.reenable_search_source(source.id)
    connecting = [asyncio.Event(), asyncio.Event()]
    connect = [asyncio.Event(), asyncio.Event()]
    sent, finish = asyncio.Event(), asyncio.Event()
    connections, sends = [], []

    async def search(*args, on_sent):
        index = len(connections)
        connections.append(index)
        connecting[index].set()
        await connect[index].wait()
        await on_sent()
        sends.append(index)
        sent.set()
        await finish.wait()
        return wire(400 if phase == "rearmed" and index == 0 else 401 if phase == "recovered" and index == 1 else 200)

    async def another_session():
        async with service._mutation_lock:
            return True

    monkeypatch.setattr(service.adapter, "search", search, raising=False)
    first = asyncio.create_task(run(service))
    await connecting[0].wait()
    assert await asyncio.wait_for(another_session(), timeout=.2)
    second = asyncio.create_task(run(service))
    await asyncio.wait_for(connecting[1].wait(), timeout=.2)
    connect[0].set()
    await sent.wait()
    if phase != "concurrent":
        finish.set()
        await first
    connect[1].set()
    loser = await asyncio.wait_for(second, timeout=.2)
    if phase == "concurrent":
        assert loser["record"]["attempts"][0]["skipped"] == "blocked"
    else:
        assert loser["record"]["attempts"][0]["outcome"] == ("failed" if phase == "recovered" else "completed")
        assert not loser["record"]["attempts"][0].get("superseded")
    finish.set()
    assert (await first)["record"]["status"] == ("failed" if phase == "rearmed" else "completed")
    assert sends == ([0] if phase == "concurrent" else [0, 1])
    assert summary(service)["services"][0]["requests"] == len(sends)
    assert service.store.load().sources[0].state.status == ("needs_action" if phase == "recovered" else "standby")


@pytest.mark.asyncio
async def test_stop_during_timeout_teardown_never_runs_the_next_paid_candidate(tmp_path, monkeypatch):
    """The finite adapter drain may outlive its timeout; a later Stop still owns the call."""
    from core.handlers.model_hub import search_chain

    service = owner(tmp_path, ("exa", "tavily"))
    monkeypatch.setattr(search_chain, "ATTEMPT_SECONDS", .1)
    draining, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def search(source_id, *args, on_sent):
        await on_sent()
        calls.append(source_id)
        if source_id == TAVILY:
            return wire()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            draining.set()
            await finish.wait()
            return SearchResponse(None, {}, None, "cancelled")

    monkeypatch.setattr(service.adapter, "search", search, raising=False)
    task = asyncio.create_task(run(service))
    await asyncio.wait_for(draining.wait(), timeout=2)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == [EXA]
    assert summary(service)["services"][0]["uncertain_requests"] == 1
    assert service._service_cooldowns.until(service.store.load().sources[0], "exa/search", NOW) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mixed", [False, True])
async def test_model_executor_retains_the_chain_budget_not_the_service_attempt_limit(tmp_path, monkeypatch, mixed):
    """A native model search is not cut short and paid for again just because services are active."""
    from config.v2_config import ModelHubHostedToolConfig, ModelHubSearchServiceSettings
    from core.handlers.model_hub import search_chain
    from tests.test_model_hub_hosted_executor import WEB_ANSWER, WEB_CALL, _responses
    from tests.test_model_hub_l3 import LiveInvokeHandle, RawOutcomeKind, _outcome
    from tests.test_model_hub_vibey_consumer import _vibey_service

    class SlowModel(LiveInvokeHandle):
        @staticmethod
        async def _iterate(chunks):
            await asyncio.sleep(.04)
            for chunk in chunks:
                yield chunk

    config = _config()
    service = _vibey_service(tmp_path, [next(s for s in config.sources if s.id == MODEL)], handles=[
        SlowModel(_outcome(RawOutcomeKind.SUCCESS, source_id=MODEL), _responses(WEB_CALL, WEB_ANSWER)),
    ])
    if mixed:
        service.store.config.sources.append(next(s for s in config.sources if s.id == EXA))
    service.store.config.hosted_tools["web_search"] = ModelHubHostedToolConfig(
        executor_order=(MODEL, EXA) if mixed else (MODEL,),
        executor_order_kinds={MODEL: "model", EXA: "service"} if mixed else {MODEL: "model"},
        services={KEYLESS_EXA_SOURCE_ID: ModelHubSearchServiceSettings(enabled=False)},
    )
    monkeypatch.setattr(search_chain, "ATTEMPT_SECONDS", .01)
    monkeypatch.setattr(search_chain, "call_timeout", lambda tool: 1)
    calls = answers(monkeypatch, service, wire())
    result = await run(service)
    assert not calls and result["record"]["status"] == "completed"
    assert result["execution"]["executor_source_id"] == MODEL
    assert "attempts" in result["record"]


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", ["Query is invalid", {"error": "Query is invalid"}, [{"msg": "Field required", "input": "do not echo me"}]])
async def test_tavily_validation_error_keeps_bounded_vendor_text_but_not_echoed_inputs(tmp_path, monkeypatch, detail):
    service = owner(tmp_path, ("tavily",))
    answers(monkeypatch, service, wire(422, {"detail": detail}))
    result = await run(service)
    assert result["record"]["error_code"] == "service_refused"
    assert ("Field required" if isinstance(detail, list) else "Query is invalid") in result["record"]["error"]
    assert "do not echo me" not in result["record"]["error"]


def test_retry_http_client_rpc_clears_only_the_service_fence_with_plain_success(tmp_path, monkeypatch):
    """The existing route inventory checks registration, not forwarding or controller-owned state."""
    from core.handlers.model_hub.rpc import dispatch_model_hub_rpc
    from tests.test_model_hub_api import app, csrf_headers, save_config
    from vibe import model_hub_client, ui_server

    save_config(tmp_path / "home")
    service = owner(tmp_path)
    source = service.store.load().sources[0]
    source.state = ModelHubSourceStateConfig(status="needs_action", detail_key="models.source.needs_action.balance_exhausted")
    generation = service._service_cooldowns.advance(source, "exa/search")
    calls = []

    async def rpc(operation, payload):
        calls.append((operation, payload))
        return await dispatch_model_hub_rpc(service, operation, payload)

    monkeypatch.setattr(model_hub_client, "_rpc", rpc)
    monkeypatch.setattr(ui_server, "_model_hub_service", model_hub_client.ModelHubRemoteService)
    client = app.test_client()
    response = client.post(f"/api/models/hosted-tools/web_search/services/{EXA}/retry", json={},
                           headers=csrf_headers(client, "http://127.0.0.1:15131"))
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "contract_version": 21}
    assert calls == [("clear_service_cooldown", {"source_id": EXA, "service_id": None})]
    assert not service._service_cooldowns.current(source, "exa/search", generation)
    assert service.store.load().sources[0].state == source.state
    assert summary(service)["services"] == []


@pytest.mark.asyncio
async def test_blocked_model_executor_keeps_its_own_remedy_beside_a_disabled_service(tmp_path):
    """The model role must carry its health remedy, not a tool-only re-enable instruction."""
    service = owner(tmp_path, ("exa",), model=True, web={"services": {EXA: {"enabled": False}}})
    model = next(s for s in service.store.load().sources if s.id == MODEL)
    model.state = ModelHubSourceStateConfig(status="error", detail_key="models.source.error.unclassified")
    result = await run(service)
    assert result["record"]["error_code"] == "search_sources_blocked"
    assert result["record"]["attempts"] == [
        {"source_id": MODEL, "label": model.display_name, "kind": "model", "outcome": "skipped",
         "skipped": "blocked", "needs_action": "models.source.error.unclassified"},
        {"source_id": EXA, "label": "exa", "kind": "service", "outcome": "skipped", "skipped": "disabled"},
        {"source_id": KEYLESS_EXA_SOURCE_ID, "label": "Exa (free)", "kind": "service", "outcome": "skipped", "skipped": "disabled"},
    ]
    assert "execution" not in result
    _hosted_result("web_search", result)


@pytest.mark.asyncio
async def test_trial_reservation_is_released_even_when_health_persistence_fails(tmp_path, monkeypatch):
    """A failed local write keeps the persisted blocker, not an unreleasable in-flight trial."""
    service = owner(tmp_path)
    source = service.store.load().sources[0]
    source.state = ModelHubSourceStateConfig(status="needs_action", detail_key="models.source.needs_action.balance_exhausted")
    await service.reenable_search_source(source.id)
    answers(monkeypatch, service, wire())

    async def disk_unavailable(*args, **kwargs):
        raise OSError("test-owned persistence failure")

    monkeypatch.setattr(service, "_save_runtime_config", disk_unavailable)
    with pytest.raises(OSError, match="test-owned persistence failure"):
        await run(service)
    assert not service.search_reenable_trial_pending(source.id)
    assert service.store.load().sources[0].state.status == "needs_action"
    assert summary(service)["services"][0]["requests"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("trial", [False, True])
async def test_service_admission_and_settlement_never_reload_config_on_the_controller_loop(tmp_path, monkeypatch, trial):
    """A generation helper's hidden store read must not bypass the off-loop snapshots."""
    service = owner(tmp_path)
    source = service.store.load().sources[0]
    if trial:
        source.state = ModelHubSourceStateConfig(status="needs_action", detail_key="models.source.needs_action.balance_exhausted")
    turn = service.freeze_turn_hosted_tools(service.store.load())
    load = service.store.load
    threads = []

    def observed():
        threads.append(threading.get_ident())
        return load()

    monkeypatch.setattr(service.store, "load", observed)
    answers(monkeypatch, service, wire())
    if trial:
        await service.reenable_search_source(source.id)
    assert (await run(service, turn=turn))["record"]["status"] == "completed"
    assert threads and threading.get_ident() not in threads
