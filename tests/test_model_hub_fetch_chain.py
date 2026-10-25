"""WF-2G fetch producer contracts: no-send settlement, local wiring and free accounting."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from config.v2_config import ModelHubFetchServiceSettings
from core.handlers.model_hub import hosted_tools
from core.handlers.model_hub.adapter import SearchResponse
from core.handlers.model_hub.fetch_services import map_fetch_response
from tests.test_model_hub_search_chain import EXA, owner
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


def _service(tmp_path: Path, *, enabled: dict[str, bool], order=(LOCAL, EXA)):
    service = owner(tmp_path, ("exa",), include_keyless=False)
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
        tmp_path,
        enabled={LOCAL: False, EXA: False, FREE_EXA: False, JINA: False},
        order=(LOCAL, EXA),
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
async def test_keyed_target_failure_is_sent_but_does_not_cool_shared_service(tmp_path):
    service, config = _service(
        tmp_path,
        enabled={LOCAL: False, EXA: True, FREE_EXA: False, JINA: False},
        order=(EXA,),
    )

    async def fetch(source_id, vendor, service_id, credential_ref, request, *, on_sent):
        await on_sent()
        return SearchResponse(
            200,
            {},
            json.dumps({"results": [], "statuses": [{"status": "failed"}]}).encode(),
        )

    service.adapter.fetch = fetch
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    )
    assert result["record"]["error_code"] == "fetch_target_unreadable"
    source = next(source for source in config.sources if source.id == EXA)
    assert service._service_cooldowns.until(source, "exa/contents", service.now()) is None
    row = next(row for row in service.usage_summary(days=7)["services"] if row["tool"] == "web_fetch")
    assert row["requests"] == 1 and row["billable_requests"] == 0


@pytest.mark.asyncio
async def test_remote_submission_rechecks_original_url_gate(tmp_path):
    service, config = _service(
        tmp_path,
        enabled={LOCAL: False, EXA: True, FREE_EXA: False, JINA: False},
        order=(EXA,),
    )

    async def fetch(source_id, vendor, service_id, credential_ref, request, *, on_sent):
        await on_sent()
        body = {"results": [{"text": "body", "title": "T", "url": request["ids"][0]}]}
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
        enabled={LOCAL: False, EXA: True, FREE_EXA: False, JINA: False},
        order=(EXA,),
    )

    async def fetch(*args, **kwargs):
        return SearchResponse(None, {}, None, "invalid_endpoint")

    service.adapter.fetch = fetch
    result = await service.run_hosted_tool(
        "web_fetch", {"arguments": {"url": "https://example.com/"}},
        turn=service.freeze_turn_hosted_tools(config),
    )
    assert result["record"]["status"] == "failed"
    assert result["record"]["error_code"] == "fetch_sources_blocked"
    assert result["record"]["attempts"][0]["outcome"] == "skipped"
    assert service.usage_summary(days=7)["services"] == []
    source = next(source for source in config.sources if source.id == EXA)
    assert service._service_cooldowns.until(source, "exa/contents", service.now()) is None


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
        enabled={LOCAL: False, EXA: True, FREE_EXA: False, JINA: False},
        order=(EXA,),
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
    source = next(source for source in config.sources if source.id == EXA)
    assert service._service_cooldowns.until(source, "exa/contents", service.now()) is None
