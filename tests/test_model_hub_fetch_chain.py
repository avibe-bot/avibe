"""WF-2G fetch producer contracts: no-send settlement, local wiring and free accounting."""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from config.v2_config import ModelHubFetchServiceSettings, model_hub_builtin_source
from core.handlers.model_hub import fetch_chain as fetch_chain_module
from core.handlers.model_hub import hosted_tools
from core.handlers.model_hub.adapter import SearchResponse
from core.handlers.model_hub.fetch_services import map_fetch_response
from tests.test_model_hub_search_chain import EXA, IDS, owner
from tests.test_model_hub_web_fetch_readers import FREE_EXA, JINA, LOCAL, fetch_config
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.fetch_catalog import fetch_service_catalog
from core.handlers.model_hub.service import ModelHubError


@pytest.fixture(autouse=True)
def _reset_fetch_flag(monkeypatch):
    monkeypatch.setattr(hosted_tools, "WEB_FETCH_ACTIVE", False)
    yield


@pytest.mark.asyncio
async def test_jina_builtin_fetch_is_admitted_with_reader_headers():
    adapter = object.__new__(CLIProxyEngineAdapter)
    captured = {}

    async def search_http(**kwargs):
        captured.update(kwargs)
        return SearchResponse(200, {}, b'{"data":{"content":"body"}}')

    adapter._search_http = search_http
    response = await adapter.fetch(
        "src_jinakeyless", "jina-reader", "jina/reader", None,
        {"url": "https://example.com/"},
    )
    assert response.http_status == 200
    assert captured["extra_headers"]["X-Return-Format"] == "markdown"
    assert captured["extra_headers"]["User-Agent"].startswith("Avibe/")


    jina = next(item for item in fetch_service_catalog() if item["vendor"] == "jina-reader")
    mapped = map_fetch_response(
        jina,
        SearchResponse(200, {}, json.dumps({"data": {"content": "body", "title": "t" * 400}}).encode()),
        requested_url="https://example.com/", sent=True, say=lambda key, **_: key,
    )
    assert mapped.record["title"] == "t" * 300

    exa = next(item for item in fetch_service_catalog() if item["vendor"] == "exa-mcp")
    limited = map_fetch_response(
        exa,
        SearchResponse(200, {}, json.dumps({"result": {"_meta": {"ai.exa/rateLimited": True}, "content": []}}).encode()),
        requested_url="https://example.com/", sent=True, say=lambda key, **_: key,
    )
    assert limited.record["error_code"] == "service_rate_limited"
    assert limited.cooling and not limited.target_failure


def test_exa_mcp_markdown_text_block_preserves_page_text_and_leading_metadata():
    exa = next(item for item in fetch_service_catalog() if item["vendor"] == "exa-mcp")
    text = "# Example page\nURL: https://example.com/page\nPublished: 2026-10-25\nAuthor: Example\n\n# Body\nURL: https://example.com/in-page\n"
    mapped = map_fetch_response(
        exa,
        SearchResponse(200, {}, json.dumps({"result": {"content": [{"type": "text", "text": text}]}}).encode()),
        requested_url="https://example.com/", sent=True, say=lambda key, **_: key,
    )
    assert mapped.record["status"] == "completed"
    assert mapped.record["title"] == "Example page"
    assert mapped.record["final_url"] == "https://example.com/page"
    assert mapped.record["content"] == "# Body\nURL: https://example.com/in-page\n"


@pytest.mark.parametrize("vendor", ["exa-mcp", "jina-reader"])
@pytest.mark.parametrize("body", [b"<html>broken provider</html>", b"[1, 2, 3]"])
def test_http_200_malformed_provider_envelopes_are_service_failures(body, vendor):
    catalog = next(item for item in fetch_service_catalog() if item["vendor"] == vendor)
    mapped = map_fetch_response(
        catalog, SearchResponse(200, {}, body), requested_url="https://example.com/", sent=True,
        say=lambda key, **_: key,
    )
    assert mapped.record["error_code"] == "service_unavailable"
    assert mapped.cooling and not mapped.target_failure
    assert mapped.cost.usd == 0 and not mapped.uncertain and not mapped.billable


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor", ["exa", "tavily", "deepseek"])
async def test_configured_fetch_is_dormant_at_chain_and_adapter_boundaries(tmp_path, vendor):
    """The activation flag still blocks the new producer while the adapter is ready for hermetic tests."""
    sid = IDS[vendor]
    service, config = _service(
        tmp_path, vendor=vendor,
        enabled={LOCAL: False, FREE_EXA: False, JINA: False, sid: True}, order=(sid,),
    )
    calls = []

    async def unexpected(*args, **kwargs):
        calls.append(args)
        raise AssertionError("configured fetch must remain unavailable")

    service.adapter.fetch = unexpected
    turn = service.freeze_turn_hosted_tools(config)
    hosted_tools.WEB_FETCH_ACTIVE = False
    with pytest.raises(ModelHubError) as raised:
        await service.run_hosted_tool(
            "web_fetch", {"arguments": {"url": "https://example.com/"}},
            turn=turn,
        )
    assert raised.value.code == "tool_not_enabled"
    adapter = object.__new__(CLIProxyEngineAdapter)
    captured = {}

    async def transport(**kwargs):
        captured.update(kwargs)
        return SearchResponse(200, {}, b"{}")

    adapter._search_http = transport
    catalog = next(item for item in fetch_service_catalog() if item["vendor"] == vendor)
    request = dict(catalog["request"]["fixed"])
    request[catalog["request"]["url_field"]] = ["https://example.com/"] if catalog["request"]["url_array"] else "https://example.com/"
    response = await adapter.fetch(sid, vendor, catalog["service_id"], f"cred_{sid}", request)
    assert response.http_status == 200
    assert captured["endpoint"] == catalog["endpoint"]
    assert captured["payload"] == request
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("sid,status,body,expected,delay", [
    (FREE_EXA, 429, {}, "service_rate_limited", 600),
    (FREE_EXA, 500, {}, "service_unavailable", 30),
    (FREE_EXA, 200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "Internal error"}}, "service_unavailable", 30),
    (FREE_EXA, 200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "Unauthorized"}}, "service_unavailable", 30),
    (FREE_EXA, 200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "Free MCP rate limit exceeded"}}, "service_rate_limited", 30),
    (FREE_EXA, 200, {"jsonrpc": "2.0", "id": 1, "error": {"code": "CONTENT_FILTER_ERROR"}}, "service_refused", None),
    (FREE_EXA, 200, {"result": {}}, "service_unavailable", 30),
    (FREE_EXA, 200, {"result": []}, "service_unavailable", 30),
    (FREE_EXA, 200, {"result": {"structuredContent": {}}}, "service_unavailable", 30),
    (FREE_EXA, 200, {"result": {"structuredContent": {"content": []}}}, "service_unavailable", 30),
    (FREE_EXA, 200, {"result": {"structuredContent": {"content": ""}}}, "fetch_target_unreadable", None),
    (FREE_EXA, 200, {"result": {"_meta": {"ai.exa/rateLimited": True}, "content": []}}, "service_rate_limited", 30),
    (FREE_EXA, 200, {"result": {"isError": True, "content": [{"type": "text", "text": '{"error":{"code":"CONTENT_FILTER_ERROR"}}'}]}}, "service_refused", None),
    (FREE_EXA, 200, {"result": {"isError": True, "structuredContent": {"error": {"code": "PROHIBITED_CONTENT"}}}}, "service_refused", None),
    (FREE_EXA, 200, {"result": {"isError": True, "structuredContent": {"error": {"code": "CRAWL_UNKNOWN_ERROR"}}}}, "fetch_target_unreadable", None),
    (FREE_EXA, 200, {"result": {"isError": True, "structuredContent": {"error": {"code": "NEW_ERROR"}}}}, "service_unavailable", 30),
    (JINA, 429, {}, "service_rate_limited", 600),
    (JINA, 500, {}, "service_unavailable", 60),
    (JINA, 401, {}, "service_refused", None),
    (JINA, 403, {}, "service_refused", None),
])
async def test_keyless_response_settles_real_shared_cooldown_and_free_usage(tmp_path, sid, status, body, expected, delay):
    """Mapper-only checks missed the lost admission generation and generic MCP refusal branch."""
    service, config = _service(
        tmp_path, enabled={LOCAL: False, EXA: False, FREE_EXA: sid == FREE_EXA, JINA: sid == JINA}, order=(sid,),
    )
    sent = []

    async def fetch(*args, on_sent):
        await on_sent()
        sent.append(True)
        return SearchResponse(status, {"Retry-After": "600"} if status == 429 else {}, json.dumps(body).encode())

    service.adapter.fetch = fetch
    turn = service.freeze_turn_hosted_tools(config)
    result = await service.run_hosted_tool("web_fetch", {"arguments": {"url": "https://example.com/"}}, turn=turn)
    assert result["record"]["error_code"] == expected
    assert result["record"]["attempts"][0]["cost"] == {"basis": "list", "usd": 0}
    source = model_hub_builtin_source(sid)
    service_id = "exa/mcp" if sid == FREE_EXA else "jina/reader"
    until = service._service_cooldowns.until(source, service_id, service.now())
    assert until == (None if delay is None else service.now() + timedelta(seconds=delay))
    assert not service._latest_source_attempt_generation  # built-ins have no credential health
    if delay is not None:
        again = await service.run_hosted_tool("web_fetch", {"arguments": {"url": "https://example.com/"}}, turn=turn)
        assert again["record"]["attempts"][0]["skipped"] == "cooling"
        assert sent == [True]
    row = next(row for row in service.usage_summary(days=7)["services"] if row["tool"] == "web_fetch")
    assert row["requests"] == 1
    assert row["billable_requests"] == row["uncertain_requests"] == row["reported_requests"] == 0


@pytest.mark.asyncio
async def test_stale_keyless_fetch_cannot_overwrite_a_newer_shared_generation(tmp_path):
    """A search retry/reset can advance the same identity while a fetch is in flight."""
    service, config = _service(
        tmp_path, enabled={LOCAL: False, EXA: False, FREE_EXA: True, JINA: False}, order=(FREE_EXA,),
    )
    source = model_hub_builtin_source(FREE_EXA)

    async def fetch(*args, on_sent):
        await on_sent()
        assert service._service_cooldowns.generation(source, "exa/mcp") is not None
        service._service_cooldowns.advance(source, "exa/mcp", clear=True)
        return SearchResponse(429, {}, b"{}")

    service.adapter.fetch = fetch
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}}, turn=service.freeze_turn_hosted_tools(config),
    )
    assert result["record"]["error_code"] == "service_rate_limited"
    assert service._service_cooldowns.until(source, "exa/mcp", service.now()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("release_lock", [False, True])
async def test_keyless_attempt_deadline_includes_lock_wait_and_allows_jina_fallback(tmp_path, monkeypatch, release_lock):
    """The original chain consumed 180s in the lock; resetting 30s after admission is also wrong."""
    monkeypatch.setattr(fetch_chain_module, "ATTEMPT_SECONDS", 0.3, raising=False)
    service, config = _service(
        tmp_path, enabled={LOCAL: False, EXA: False, FREE_EXA: True, JINA: True}, order=(FREE_EXA, JINA),
    )
    await service._keyless_exa_attempt_lock.acquire()
    sent = []

    async def fetch(source_id, *args, on_sent):
        await on_sent()
        sent.append(source_id)
        if source_id == FREE_EXA:
            await asyncio.sleep(0.22)
            body = {"result": {"structuredContent": {"content": "late Exa"}}}
        else:
            body = {"data": {"content": "Jina fallback"}}
        return SearchResponse(200, {}, json.dumps(body).encode())

    service.adapter.fetch = fetch
    release_handle = asyncio.get_running_loop().call_later(0.12, service._keyless_exa_attempt_lock.release) if release_lock else None
    try:
        result = await asyncio.wait_for(service.run_hosted_tool(
            "web_fetch", {"arguments": {"url": "https://example.com/"}}, turn=service.freeze_turn_hosted_tools(config),
        ), timeout=2)
    finally:
        if release_handle:
            release_handle.cancel()
        if service._keyless_exa_attempt_lock.locked():
            service._keyless_exa_attempt_lock.release()
    assert result["record"]["content"] == "Jina fallback"
    first = result["record"]["attempts"][0]
    assert first["outcome"] == ("failed" if release_lock else "skipped")
    if not release_lock:
        assert first["skipped"] == "service_busy"
    assert sent == ([FREE_EXA, JINA] if release_lock else [JINA])
    rows = {row["source_id"]: row for row in service.usage_summary(days=7)["services"] if row["tool"] == "web_fetch"}
    assert rows[JINA]["requests"] == 1
    assert (FREE_EXA in rows) == release_lock


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["fetch_url_blocked", "fetch_dns_unverified"])
async def test_fallback_regate_failure_is_terminal_without_forwarding(tmp_path, code):
    """A sent local failure may not bypass the original-URL gate on service fallback."""
    from core.web_fetch_worker.policy import FetchFailure

    service, config = _service(
        tmp_path, enabled={LOCAL: True, EXA: False, FREE_EXA: True, JINA: True}, order=(LOCAL, FREE_EXA, JINA),
    )
    checks, submissions = [], []
    url = "https://example.com/"

    async def check(value, *, deadline):
        checks.append(value)
        if len(checks) > 1:
            raise FetchFailure(code, terminal=True)

    async def local(value, *, deadline, on_sent):
        on_sent()
        return {"sent": True, "error_code": "fetch_timeout", "terminal": False}

    async def fetch(*args, on_sent):
        await on_sent()
        submissions.append(args)
        raise AssertionError("blocked fallback must not forward the URL")

    service._local_fetch_engine.check = check
    service._local_fetch_engine.fetch = local
    service.adapter.fetch = fetch
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": url}}, turn=service.freeze_turn_hosted_tools(config),
    )
    assert result["record"]["error_code"] == code
    assert checks == [url, url] and submissions == []
    assert [(fact["source_id"], fact["outcome"]) for fact in result["record"]["attempts"]] == [(LOCAL, "failed"), (FREE_EXA, "skipped")]
    rows = service.usage_summary(days=7)["services"]
    assert len(rows) == 1 and rows[0]["source_id"] == LOCAL and rows[0]["requests"] == 1


@pytest.mark.parametrize("text", [
    '{"content":"Article body","other":"Important details","title":"Document title","url":"https://other.example/"}',
    '{"content":"","other":"Still a nonempty JSON document"}',
    '{"error":{"code":"CONTENT_FILTER_ERROR","message":"Free MCP rate limit exceeded"}}',
    '[{"error":"Internal error"}]',
    '123',
    'A page discussing rate limits and CONTENT_FILTER_ERROR is still page content.',
])
def test_successful_mcp_text_documents_are_not_reinterpreted_as_error_envelopes(text):
    """Successful text blocks are documents, even when a JSON page resembles an RPC error."""
    catalog = next(item for item in fetch_service_catalog() if item["vendor"] == "exa-mcp")
    result = map_fetch_response(
        catalog, SearchResponse(200, {}, json.dumps({"result": {"content": [{"type": "text", "text": text}]}}).encode()),
        requested_url="https://example.com/", sent=True, say=lambda key, **_: key,
    )
    assert result.record["status"] == "completed"
    assert result.record["content"] == text
    assert result.record["title"] is None and result.record["final_url"] is None
    assert not result.cooling and not result.target_failure


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow", [False, True])
async def test_extractor_overflow_survives_short_normalized_content_into_record_and_frame(tmp_path, overflow):
    """Extractor pre-capping can lose the tail even when later sanitization makes the result tiny."""
    from core.agent_core.web_fetch import BUDGET_MARKER, fetch_record_from_dict, frame_fetch_record
    from core.web_fetch_worker.extract import extract_page

    service, config = _service(
        tmp_path, enabled={LOCAL: True, EXA: False, FREE_EXA: False, JINA: False}, order=(LOCAL,),
    )
    source = b"Retained paragraph.\n\n<script>" + b"x" * (200000 if overflow else 20) + b"</script>\n\nFinal paragraph."

    async def fetch(url, *, deadline, on_sent):
        on_sent()
        page = await asyncio.to_thread(extract_page, source, "text/markdown", url)
        assert page.content_overflow is overflow
        assert len(page.content.encode()) < 1000
        return {**page.payload(), "sent": True, "source_size_bytes": len(source), "final_url": url}

    service._local_fetch_engine.fetch = fetch
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}}, turn=service.freeze_turn_hosted_tools(config),
    )
    record = fetch_record_from_dict(result["record"])
    assert record.status == "completed" and record.truncated is overflow
    assert record.truncation_reason == ("budget" if overflow else None)
    assert (BUDGET_MARKER in frame_fetch_record(record)) is overflow
    assert ("Final paragraph." in record.content) is (not overflow)
    assert "content_overflow" not in result["record"]

@pytest.mark.asyncio
@pytest.mark.parametrize("vendor,body,expected_request,expected_cost", [
    ("tavily", {"results": [{"url": "https://example.com/", "raw_content": "Tavily body", "title": "T"}], "failed_results": []},
     {"urls": ["https://example.com/"], "extract_depth": "basic", "format": "markdown"}, 0.0016),
    ("exa", {"results": [{"id": "https://example.com/", "url": "https://final.example/", "text": "Exa body", "title": "E"}],
             "statuses": [{"id": "https://example.com/", "status": "success"}], "costDollars": {"total": 0.02}},
     {"ids": ["https://example.com/"], "text": {"maxCharacters": 48000}}, 0.02),
    ("deepseek", {"title": "D", "content": "DeepSeek body"}, {"url": "https://example.com/"}, 0.00075),
])
async def test_configured_fetch_maps_request_content_cost_and_source_success(
    tmp_path, vendor, body, expected_request, expected_cost,
):
    sid = IDS[vendor]
    service, config = _service(
        tmp_path, vendor=vendor,
        enabled={LOCAL: False, FREE_EXA: False, JINA: False, sid: True}, order=(sid,),
    )
    calls = []

    async def fetch(source_id, actual_vendor, service_id, credential_ref, request, *, on_sent):
        calls.append((source_id, actual_vendor, service_id, credential_ref, request))
        await on_sent()
        return SearchResponse(200, {}, json.dumps(body).encode())

    service.adapter.fetch = fetch
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    )
    assert result["record"]["status"] == "completed"
    assert result["record"]["content"] in {"Tavily body", "Exa body", "DeepSeek body"}
    assert result["execution"]["cost"]["usd"] == expected_cost
    catalog = next(item for item in fetch_service_catalog() if item["vendor"] == vendor)
    assert calls == [(sid, vendor, catalog["service_id"], f"cred_{sid}", expected_request)]
    row = next(row for row in service.usage_summary(days=7)["services"] if row["tool"] == "web_fetch")
    assert row["requests"] == row["billable_requests"] == 1
    assert service.store.load().sources[0].state.status == "standby"


@pytest.mark.parametrize("vendor,body", [
    ("tavily", {"results": [], "failed_results": [{"url": "https://example.com/", "error": "blocked"}]}),
    ("exa", {"results": [{"id": "https://example.com/", "text": ""}], "statuses": [{"id": "https://example.com/", "status": "error"}]}),
])
def test_configured_target_failures_do_not_cool_or_mark_source(tmp_path, vendor, body):
    catalog = next(item for item in fetch_service_catalog() if item["vendor"] == vendor)
    result = map_fetch_response(
        catalog, SearchResponse(200, {}, json.dumps(body).encode()),
        requested_url="https://example.com/", sent=True, say=lambda key, **_: key,
    )
    assert result.record["error_code"] == "fetch_target_unreadable"
    assert result.target_failure and not result.cooling and not result.uncertain
    assert result.cost is not None and result.cost.usd == 0 and not result.billable


def test_tavily_nonmatching_failure_is_malformed_service_evidence():
    catalog = next(item for item in fetch_service_catalog() if item["vendor"] == "tavily")
    body = {"results": [], "failed_results": [{"url": "https://other.example/", "error": "blocked"}]}
    result = map_fetch_response(
        catalog, SearchResponse(200, {}, json.dumps(body).encode()),
        requested_url="https://example.com/", sent=True, say=lambda key, **_: key,
    )
    assert result.record["error_code"] == "service_unreadable"
    assert result.uncertain and result.cooling and result.cost is None


@pytest.mark.parametrize("vendor,status,body,error,expected_code,expected_usd,expected_billable,expected_uncertain", [
    ("tavily", 503, {}, "", "service_unavailable", 0, False, False),
    ("exa", 429, {}, "", "service_rate_limited", 0, False, False),
    ("deepseek", 401, {}, "", "service_key_invalid", 0, False, False),
    ("deepseek", 404, {}, "", "fetch_target_unreadable", 0, False, False),
    ("deepseek", None, {}, "timeout", "service_unavailable", 0, False, False),
    ("deepseek", None, {}, "connection", "service_unavailable", None, False, True),
    ("exa", None, {}, "timeout", "service_unavailable", None, False, True),
    ("deepseek", 200, {"title": "missing content"}, "", "service_unreadable", None, False, True),
    ("tavily", 200, {"results": [], "failed_results": [{"url": "https://example.com/"}]}, "",
     "fetch_target_unreadable", 0, False, False),
])
def test_configured_billing_distinguishes_known_zero_from_unknown_sent_cost(
    vendor, status, body, error, expected_code, expected_usd, expected_billable, expected_uncertain,
):
    catalog = next(item for item in fetch_service_catalog() if item["vendor"] == vendor)
    result = map_fetch_response(
        catalog, SearchResponse(status, {}, json.dumps(body).encode() if status is not None else None, error),
        requested_url="https://example.com/", sent=True, say=lambda key, **_: key,
    )
    assert result.record["error_code"] == expected_code
    assert result.billable is expected_billable and result.uncertain is expected_uncertain
    assert (None if result.cost is None else result.cost.usd) == expected_usd


def test_configured_url_matching_rejects_malformed_provider_urls_without_mapper_failure():
    catalog = next(item for item in fetch_service_catalog() if item["vendor"] == "exa")
    body = {
        "results": [{"id": "https://example.com:bad/", "text": "body"}],
        "statuses": [{"id": "https://example.com:bad/", "status": "success"}],
    }
    result = map_fetch_response(
        catalog, SearchResponse(200, {}, json.dumps(body).encode()),
        requested_url="https://example.com/", sent=True, say=lambda key, **_: key,
    )
    assert result.record["error_code"] == "fetch_target_unreadable"
    assert result.target_failure


def test_deepseek_200_unrecoverable_body_is_uncertain_and_cooling():
    catalog = next(item for item in fetch_service_catalog() if item["vendor"] == "deepseek")
    result = map_fetch_response(
        catalog, SearchResponse(200, {}, b'{"title":"missing content"}'),
        requested_url="https://example.com/", sent=True, say=lambda key, **_: key,
    )
    assert result.record["error_code"] == "service_unreadable"
    assert result.uncertain and result.cooling and result.cost is None


def test_exa_reported_cost_outside_catalog_ceiling_falls_back_to_list_estimate():
    catalog = next(item for item in fetch_service_catalog() if item["vendor"] == "exa")
    body = {"results": [{"id": "https://example.com/", "text": "body"}],
            "statuses": [{"id": "https://example.com/", "status": "success"}],
            "costDollars": {"total": 0.1001}}
    result = map_fetch_response(
        catalog, SearchResponse(200, {}, json.dumps(body).encode()),
        requested_url="https://example.com/", sent=True, say=lambda key, **_: key,
    )
    assert result.cost == result.cost.__class__(basis="list", usd=0.001)
    assert result.reported_usd is None


@pytest.mark.asyncio
async def test_stale_configured_fetch_health_generation_cannot_overwrite_newer_source(tmp_path):
    sid = IDS["deepseek"]
    service, config = _service(
        tmp_path, vendor="deepseek",
        enabled={LOCAL: False, FREE_EXA: False, JINA: False, sid: True}, order=(sid,),
    )

    async def fetch(*args, on_sent):
        await on_sent()
        service._reserve_settlement_generation(sid, source=service.store.load().sources[0])
        return SearchResponse(401, {}, b"{}")

    service.adapter.fetch = fetch
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    )
    assert result["record"]["error_code"] == "service_key_invalid"
    assert service.store.load().sources[0].state.status == "standby"


@pytest.mark.asyncio
async def test_paid_sent_cancellation_is_uncertain_and_does_not_cool(tmp_path):
    service, config = _service(
        tmp_path, vendor="deepseek",
        enabled={LOCAL: False, FREE_EXA: False, JINA: False, IDS["deepseek"]: True},
        order=(IDS["deepseek"],),
    )
    started = asyncio.Event()

    async def fetch(*args, on_sent):
        await on_sent()
        started.set()
        await asyncio.Event().wait()

    service.adapter.fetch = fetch
    task = asyncio.create_task(service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    ))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = next(row for row in service.usage_summary(days=7)["services"] if row["tool"] == "web_fetch")
    assert row["requests"] == 1 and row["billable_requests"] == 0 and row["uncertain_requests"] == 1
    assert service._service_cooldowns.until(service.store.load().sources[0], "deepseek/fetch", service.now()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor,status,detail", [
    ("deepseek", 401, "credential_revoked"),
    ("tavily", 432, "spend_limit_reached"),
])
async def test_configured_blocker_response_settles_source_health(tmp_path, vendor, status, detail):
    sid = IDS[vendor]
    service, config = _service(
        tmp_path, vendor=vendor,
        enabled={LOCAL: False, FREE_EXA: False, JINA: False, sid: True}, order=(sid,),
    )

    async def fetch(*args, on_sent):
        await on_sent()
        return SearchResponse(status, {}, b"{}")

    service.adapter.fetch = fetch
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    )
    assert result["record"]["error_code"] == (
        "service_key_invalid" if detail == "credential_revoked" else "service_limit"
    )
    source = service.store.load().sources[0]
    assert source.state.status == "needs_action"
    assert source.state.detail_key == f"models.source.needs_action.{detail}"


class _Local:
    def __init__(self):
        self.checks = 0

    async def check(self, url, *, deadline):
        self.checks += 1

    async def fetch(self, url, *, deadline, on_sent=None):
        if on_sent is not None:
            on_sent()
        return {
            "content": "local page",
            "content_type": "text/markdown",
            "final_url": url,
            "source_size_bytes": 10,
            "sent": True,
        }


def _service(tmp_path: Path, *, enabled: dict[str, bool], order=(LOCAL, EXA), vendor="exa"):
    service = owner(tmp_path, (vendor,) if vendor else (), include_keyless=False)
    raw = fetch_config().hosted_tools["web_fetch"]
    saved = {sid: ModelHubFetchServiceSettings(enabled=value) for sid, value in enabled.items()}
    config = service.store.config
    config.hosted_tools["web_fetch"] = replace(
        raw,
        services=saved,
        executor_order=order,
        executor_order_kinds={sid: ("local" if sid == LOCAL else "service") for sid in order},
    )
    service.store.config = config
    service._local_fetch_engine = _Local()
    hosted_tools.WEB_FETCH_ACTIVE = True
    return service, config


@pytest.mark.asyncio
async def test_flag_off_rejects_fetch_without_running_gate_or_adapter(tmp_path, monkeypatch):
    service, config = _service(tmp_path, enabled={LOCAL: True, EXA: True, FREE_EXA: False, JINA: False})
    turn = service.freeze_turn_hosted_tools(config)
    hosted_tools.WEB_FETCH_ACTIVE = False
    called = []
    monkeypatch.setattr(service.adapter, "fetch", lambda *args, **kwargs: called.append(args), raising=False)
    with pytest.raises(ModelHubError) as raised:
        await service.run_hosted_tool(
            "web_fetch", {"arguments": {"url": "https://example.com/"}},
            turn=turn,
        )
    assert raised.value.code == "tool_not_enabled"
    assert called == []


@pytest.mark.asyncio
async def test_local_sent_attempt_is_recorded_as_exact_zero_fetch_usage(tmp_path):
    service, config = _service(
        tmp_path,
        enabled={LOCAL: True, EXA: False, FREE_EXA: False, JINA: False},
        order=(LOCAL, EXA),
    )
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    )
    assert result["record"]["status"] == "completed"
    assert result["record"]["attempts"][0]["kind"] == "local"
    assert result["execution"]["executor_origin"]["api"] == "local_fetch"
    assert result["execution"]["cost"] == {"basis": "list", "usd": 0}
    row = next(row for row in service.usage_summary(days=7)["services"] if row["tool"] == "web_fetch")
    assert row["requests"] == 1 and row["billable_requests"] == 0 and row["uncertain_requests"] == 0


@pytest.mark.asyncio
async def test_disabled_candidates_are_skipped_and_never_metered(tmp_path):
    service, config = _service(
        tmp_path, vendor=None,
        enabled={LOCAL: False, FREE_EXA: False, JINA: False},
        order=(LOCAL,),
    )
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    )
    assert result["record"]["status"] == "failed"
    assert result["record"]["error_code"] == "fetch_sources_blocked"
    assert all(attempt["outcome"] == "skipped" for attempt in result["record"]["attempts"])
    assert service.usage_summary(days=7)["services"] == []


@pytest.mark.asyncio
async def test_keyless_target_failure_is_sent_but_does_not_cool_shared_service(tmp_path):
    service, config = _service(
        tmp_path,
        enabled={LOCAL: False, EXA: False, FREE_EXA: True, JINA: False},
        order=(FREE_EXA,),
    )

    async def fetch(source_id, vendor, service_id, credential_ref, request, *, on_sent):
        await on_sent()
        return SearchResponse(
            200,
            {},
            json.dumps({"result": {"content": [{"type": "text", "text": "No content found."}]}}).encode(),
        )

    service.adapter.fetch = fetch
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    )
    assert result["record"]["error_code"] == "fetch_target_unreadable"
    source = model_hub_builtin_source(FREE_EXA)
    assert service._service_cooldowns.until(source, "exa/mcp", service.now()) is None
    row = next(row for row in service.usage_summary(days=7)["services"] if row["tool"] == "web_fetch")
    assert row["requests"] == 1 and row["billable_requests"] == 0


@pytest.mark.asyncio
async def test_remote_submission_rechecks_original_url_gate(tmp_path):
    service, config = _service(
        tmp_path,
        enabled={LOCAL: False, EXA: False, FREE_EXA: True, JINA: False},
        order=(FREE_EXA,),
    )

    async def fetch(source_id, vendor, service_id, credential_ref, request, *, on_sent):
        await on_sent()
        body = {"result": {"structuredContent": {"content": "body", "title": "T", "url": request["urls"][0]}}}
        return SearchResponse(200, {}, json.dumps(body).encode())

    service.adapter.fetch = fetch
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    )
    assert result["record"]["status"] == "completed"
    assert service._local_fetch_engine.checks == 2


@pytest.mark.asyncio
async def test_keyless_mcp_fetch_is_free_and_preserves_record_shape(tmp_path):
    service, config = _service(
        tmp_path,
        enabled={LOCAL: False, EXA: False, FREE_EXA: True, JINA: False},
        order=(FREE_EXA,),
    )

    async def fetch(source_id, vendor, service_id, credential_ref, request, *, on_sent):
        await on_sent()
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "structuredContent": {
                    "content": "keyless body",
                    "title": "Free title",
                    "url": request["urls"][0],
                }
            },
        }
        return SearchResponse(200, {}, json.dumps(body).encode())

    service.adapter.fetch = fetch
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    )
    assert result["record"]["status"] == "completed"
    assert result["record"]["content"] == "keyless body"
    row = next(row for row in service.usage_summary(days=7)["services"] if row["tool"] == "web_fetch")
    assert row["requests"] == 1 and row["billable_requests"] == 0


@pytest.mark.asyncio
async def test_pre_send_service_result_is_skipped_without_usage_or_cooldown(tmp_path):
    service, config = _service(
        tmp_path,
        enabled={LOCAL: False, EXA: False, FREE_EXA: True, JINA: False},
        order=(FREE_EXA,),
    )

    async def fetch(*args, **kwargs):
        return SearchResponse(None, {}, None, "invalid_endpoint")

    service.adapter.fetch = fetch
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    )
    assert result["record"]["status"] == "failed"
    assert result["record"]["error_code"] == "fetch_unavailable"
    assert result["record"]["attempts"][0]["outcome"] == "skipped"
    assert service.usage_summary(days=7)["services"] == []
    assert service.capability_evidence.records() == []
    source = model_hub_builtin_source(FREE_EXA)
    assert service._service_cooldowns.until(source, "exa/mcp", service.now()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor,sid", [("exa", FREE_EXA), ("deepseek", IDS["deepseek"])])
async def test_keyed_pre_send_adapter_rejection_is_skipped_and_unmetered(tmp_path, vendor, sid):
    service, config = _service(
        tmp_path, vendor=vendor,
        enabled={LOCAL: False, EXA: False, FREE_EXA: False, JINA: False, sid: True},
        order=(sid,),
    )

    async def fetch(*args, **kwargs):
        return SearchResponse(None, {}, None, "invalid_endpoint")

    service.adapter.fetch = fetch
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    )
    assert result["record"]["error_code"] == "fetch_unavailable"
    attempt = result["record"]["attempts"][0]
    assert attempt["source_id"] == sid and attempt["kind"] == "service"
    assert attempt["outcome"] == "skipped" and attempt["skipped"] == "unavailable"
    assert service.usage_summary(days=7)["services"] == []


@pytest.mark.asyncio
async def test_local_failure_code_survives_record_cleanup(tmp_path):
    service, config = _service(
        tmp_path,
        enabled={LOCAL: True, EXA: False, FREE_EXA: False, JINA: False},
        order=(LOCAL,),
    )

    class FailedLocal:
        async def check(self, url, *, deadline):
            return url

        async def fetch(self, url, *, deadline, on_sent=None):
            on_sent()
            return {"error_code": "fetch_url_blocked", "terminal": True, "sent": True}

    service._local_fetch_engine = FailedLocal()
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    )
    assert result["record"]["error_code"] == "fetch_url_blocked"
    assert result["record"]["attempts"][0]["error_code"] == "fetch_url_blocked"


@pytest.mark.asyncio
async def test_jina_rolling_minute_budget_is_reserved_before_send(tmp_path):
    service, config = _service(
        tmp_path,
        enabled={LOCAL: False, EXA: False, FREE_EXA: False, JINA: True},
        order=(JINA,),
    )
    service._jina_fetch_sent_at.extend([service.now()] * 20)
    sent = []

    async def fetch(*args, on_sent):
        await on_sent()
        sent.append(True)
        return SearchResponse(200, {}, json.dumps({"data": {"content": "body"}}).encode())

    service.adapter.fetch = fetch
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    )
    assert result["record"]["attempts"][0]["skipped"] == "cooling"
    assert result["record"]["attempts"][0]["retry_at"]
    assert sent == []
    assert service.usage_summary(days=7)["services"] == []


@pytest.mark.asyncio
async def test_remote_mapping_cancellation_waits_for_sent_settlement(tmp_path):
    service, config = _service(
        tmp_path,
        enabled={LOCAL: False, EXA: False, FREE_EXA: True, JINA: False},
        order=(FREE_EXA,),
    )
    mapping_started = threading.Event()
    release_mapping = threading.Event()
    original_mapper = fetch_chain_module.map_fetch_response

    def blocked_mapper(*args, **kwargs):
        mapping_started.set()
        assert release_mapping.wait(10)
        return original_mapper(*args, **kwargs)

    fetch_chain_module.map_fetch_response = blocked_mapper
    try:
        async def fetch(*args, on_sent):
            await on_sent()
            return SearchResponse(200, {}, json.dumps({"result": {"structuredContent": {"content": "body"}}}).encode())

        service.adapter.fetch = fetch
        task = asyncio.create_task(service.run_hosted_tool(
            "web_fetch", {"arguments": {"url": "https://example.com/"}},
            turn=service.freeze_turn_hosted_tools(config),
        ))
        assert await asyncio.to_thread(mapping_started.wait, 10)
        task.cancel()
        release_mapping.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release_mapping.set()
        fetch_chain_module.map_fetch_response = original_mapper
    row = next(row for row in service.usage_summary(days=7)["services"] if row["tool"] == "web_fetch")
    assert row["requests"] == 1


@pytest.mark.asyncio
async def test_sent_local_cancellation_is_metered(tmp_path):
    service, config = _service(
        tmp_path,
        enabled={LOCAL: True, EXA: False, FREE_EXA: False, JINA: False},
        order=(LOCAL,),
    )
    started = asyncio.Event()

    class HangingLocal:
        async def check(self, url, *, deadline):
            return url

        async def fetch(self, url, *, deadline, on_sent=None):
            on_sent()
            started.set()
            await asyncio.Event().wait()

    service._local_fetch_engine = HangingLocal()
    task = asyncio.create_task(service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    ))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = next(row for row in service.usage_summary(days=7)["services"] if row["tool"] == "web_fetch")
    assert row["requests"] == 1 and row["billable_requests"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("local_succeeded", [False, True])
async def test_cancellation_during_local_usage_drains_once_and_cannot_return_or_fallback(tmp_path, monkeypatch, local_succeeded):
    """Stop can arrive after transport completes; draining is not permission to continue the chain."""
    service, config = _service(
        tmp_path, enabled={LOCAL: True, EXA: False, FREE_EXA: True, JINA: True}, order=(LOCAL, FREE_EXA, JINA),
    )
    waiting, release = asyncio.Event(), asyncio.Event()
    original_wait = service.usage_writer.wait_recorded
    remote_calls = []

    async def wait_recorded(write):
        waiting.set()
        await release.wait()
        await original_wait(write)

    async def local(url, *, deadline, on_sent):
        on_sent()
        return ({"sent": True, "content": "Finished local page"} if local_succeeded else
                {"sent": True, "error_code": "fetch_challenge", "terminal": False})

    async def fetch(*args, **kwargs):
        remote_calls.append(args)
        raise AssertionError("Stop must not forward the URL to the next provider")

    monkeypatch.setattr(service.usage_writer, "wait_recorded", wait_recorded)
    service._local_fetch_engine.fetch = local
    service.adapter.fetch = fetch
    task = asyncio.create_task(service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}}, turn=service.freeze_turn_hosted_tools(config),
    ))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert remote_calls == []
    rows = service.usage_summary(days=7)["services"]
    assert len(rows) == 1 and rows[0]["source_id"] == LOCAL and rows[0]["requests"] == 1
    assert rows[0]["billable_requests"] == rows[0]["uncertain_requests"] == 0
    assert service.capability_evidence.records() == []


@pytest.mark.asyncio
async def test_sent_local_redirect_skip_is_a_failed_metered_attempt(tmp_path):
    service, config = _service(
        tmp_path,
        enabled={LOCAL: True, EXA: False, FREE_EXA: False, JINA: False},
        order=(LOCAL,),
    )

    class RedirectCoolingLocal:
        async def check(self, url, *, deadline):
            return url

        async def fetch(self, url, *, deadline, on_sent=None):
            on_sent()
            return {"error_code": "fetch_unavailable", "skipped": "cooling", "sent": True}

    service._local_fetch_engine = RedirectCoolingLocal()
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    )
    assert result["record"]["status"] == "failed"
    assert result["record"]["attempts"][0]["outcome"] == "failed"
    row = next(row for row in service.usage_summary(days=7)["services"] if row["tool"] == "web_fetch")
    assert row["requests"] == 1


@pytest.mark.asyncio
async def test_cancellation_settles_a_sent_fetch_before_rethrowing(tmp_path):
    service, config = _service(
        tmp_path,
        enabled={LOCAL: False, EXA: False, FREE_EXA: True, JINA: False},
        order=(FREE_EXA,),
    )
    started = asyncio.Event()

    async def fetch(source_id, vendor, service_id, credential_ref, request, *, on_sent):
        await on_sent()
        started.set()
        await asyncio.Event().wait()

    service.adapter.fetch = fetch
    task = asyncio.create_task(service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    ))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = next(row for row in service.usage_summary(days=7)["services"] if row["tool"] == "web_fetch")
    assert row["requests"] == 1
    source = model_hub_builtin_source(FREE_EXA)
    assert service._service_cooldowns.until(source, "exa/mcp", service.now()) is None
