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
    """The split must not expose an unreviewed paid transport even if the flag is enabled."""
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
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    )
    first = result["record"]["attempts"][0]
    assert first["source_id"] == sid and first["skipped"] == "unavailable"
    assert service.usage_summary(days=7)["services"] == []
    assert service.capability_evidence.records() == []
    adapter = object.__new__(CLIProxyEngineAdapter)
    adapter._search_http = unexpected
    catalog = next(item for item in fetch_service_catalog() if item["vendor"] == vendor)
    response = await adapter.fetch(sid, vendor, catalog["service_id"], f"cred_{sid}", {"url": "https://example.com/"})
    assert response.error == "invalid_endpoint"
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("sid,status,body,expected,delay", [
    (FREE_EXA, 429, {}, "service_rate_limited", 600),
    (FREE_EXA, 500, {}, "service_unavailable", 30),
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
