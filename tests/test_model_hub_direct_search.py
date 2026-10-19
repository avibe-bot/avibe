"""DeepSeek's standalone Search/Fetch APIs as direct hosted-tool executors."""

from __future__ import annotations

import json
import time

import pytest

from config.v2_config import ModelHubModelConfig, ModelHubSourceConfig, ModelHubSourceStateConfig
from core.handlers.model_hub import service as service_module
from core.handlers.model_hub.hosted_executor import ExecutorCall
from core.agent_core.tools.hosted import hosted_tool_spec
from core.handlers.model_hub.hosted_tools import direct_search_candidate
from core.handlers.model_hub.service import ModelHubService

SOURCE = ModelHubSourceConfig(
    id="src_ds000001",
    kind="api_key",
    vendor="deepseek",
    display_name="DeepSeek",
    protocol="openai_chat",
    supply_channel="hub",
    billing="metered",
    state=ModelHubSourceStateConfig(status="standby"),
    models=[ModelHubModelConfig(id="deepseek-v4-pro", provenance="discovered")],
    credential_ref="cred_deepseek",
    base_url="https://api.deepseek.com",
)


def test_web_fetch_has_one_canonical_schema() -> None:
    spec = hosted_tool_spec("web_fetch")
    assert spec.input_schema["required"] == ["url"]


class _Process:
    def __init__(self, stdout: bytes = b"{}", *, returncode: int = 0, stderr: bytes = b""):
        self.stdout, self.stderr = stdout, stderr
        self.returncode = returncode
        self.killed = False

    async def communicate(self):
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


@pytest.fixture
def service():
    return object.__new__(ModelHubService)


async def _attempt(monkeypatch, service, tool, arguments, stdout, *, returncode=0, stderr=b""):
    calls = []
    recorded = []

    async def record(*args):
        recorded.append(args)

    service.record_hosted_success = record

    async def spawn(*command, **kwargs):
        calls.append(command)
        return _Process(stdout, returncode=returncode, stderr=stderr)

    monkeypatch.setattr(service_module.asyncio, "create_subprocess_exec", spawn)
    candidate = direct_search_candidate(SOURCE, tool)
    assert candidate is not None
    attempt = await service._direct_search_attempt(
        ExecutorCall(tool, arguments), candidate, deadline=time.monotonic() + 10, say=lambda key, **params: key,
    )
    return attempt, calls, recorded


async def test_search_runs_the_standalone_api_with_vault(monkeypatch, service) -> None:
    attempt, calls, recorded = await _attempt(
        monkeypatch, service, "web_search", {"query": "q"},
        json.dumps({"results": [{"title": "T", "url": "https://e", "snippet": "S"}]}).encode(),
    )

    assert attempt.record["status"] == "completed"
    assert attempt.record["sources"] == [{"url": "https://e", "title": "T", "snippet": "S"}]
    assert attempt.direct is True
    assert attempt.execution()["mode"] == "direct"
    assert attempt.execution()["executor_origin"] == {
        "provider": "deepseek", "api": "openai_chat", "model": "DeepSeek Search API",
    }
    assert attempt.cost is not None and attempt.cost.usd == 0.00075
    command = calls[0]
    assert "--auth" in command and "DEEPSEEK_API_KEY" in command
    assert "https://api.deepseek.com/v1/tools/search" in command
    assert recorded == [(SOURCE, "deepseek", "web_search", "direct")]


async def test_an_empty_search_is_completed_but_free(monkeypatch, service) -> None:
    attempt, _, _ = await _attempt(
        monkeypatch, service, "web_search", {"query": "q"}, b'{"results":[]}',
    )
    assert attempt.record["status"] == "completed"
    assert attempt.record["sources"] == []
    assert attempt.cost is None
    assert attempt.billable is False
    assert attempt.moves_on is False


async def test_fetch_returns_the_page_as_the_source(monkeypatch, service) -> None:
    attempt, calls, _ = await _attempt(
        monkeypatch, service, "web_fetch", {"url": "https://example.com/a"},
        json.dumps({"title": "T", "content": "body"}).encode(),
    )

    assert attempt.record["status"] == "completed"
    assert attempt.record["sources"] == [{"url": "https://example.com/a", "title": "T", "snippet": "body"}]
    assert "https://api.deepseek.com/v1/tools/fetch" in calls[0]


async def test_an_empty_fetch_is_a_failure(monkeypatch, service) -> None:
    attempt, _, _ = await _attempt(
        monkeypatch, service, "web_fetch", {"url": "https://example.com/a"},
        json.dumps({"title": "T", "content": ""}).encode(),
    )
    assert attempt.record["status"] == "failed"


async def test_a_vault_failure_is_the_record_error(monkeypatch, service) -> None:
    stderr = b'{\n  "ok": false,\n  "error": "secret not found: DEEPSEEK_API_KEY"\n}\n'
    attempt, _, recorded = await _attempt(
        monkeypatch, service, "web_search", {"query": "q"}, b"{}", returncode=1, stderr=stderr,
    )
    assert attempt.record["status"] == "failed"
    assert "secret not found" in attempt.record["error"]
    assert attempt.record["error"] != "}"
    assert recorded == []
    assert attempt.cost is None
    assert attempt.billable is False
    assert attempt.moves_on is True


async def test_a_base_url_ending_in_v1_does_not_double_the_prefix(monkeypatch, service) -> None:
    from dataclasses import replace

    global SOURCE
    original = SOURCE
    SOURCE = replace(SOURCE, base_url="https://api.deepseek.com/v1")
    try:
        _, calls, _ = await _attempt(
            monkeypatch, service, "web_search", {"query": "q"}, json.dumps({"results": []}).encode(),
        )
    finally:
        SOURCE = original
    assert "https://api.deepseek.com/v1/tools/search" in calls[0]
    assert "/v1/v1/" not in " ".join(calls[0])
