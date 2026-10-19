"""DeepSeek's standalone Search/Fetch APIs as direct hosted-tool executors."""

from __future__ import annotations

import asyncio
import datetime
import json
import time

import pytest

from config.v2_config import ModelHubModelConfig, ModelHubSourceConfig, ModelHubSourceStateConfig
from core.handlers.model_hub import service as service_module
from core.handlers.model_hub.hosted_executor import ExecutorCall, ExecutorCallRefused, parse_call
from core.agent_core.messages import message_from_dict
from core.agent_core.tools.hosted import hosted_tool_spec
from core.handlers.model_hub.hosted_tools import direct_search_candidate, executor_vendors
from core.handlers.model_hub.service import ModelHubService
from tests.fake_pid_helpers import fake_pid

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


def test_web_fetch_rejects_a_non_http_url() -> None:
    for url in ("file:///etc/passwd", "https://[", "https://e\nnext", "https://e "):
        with pytest.raises(ExecutorCallRefused):
            parse_call("web_fetch", {"arguments": {"url": url}})


def test_web_fetch_names_deepseek_as_a_vendor() -> None:
    assert "deepseek" in executor_vendors("web_fetch")


def test_web_fetch_has_one_canonical_schema() -> None:
    spec = hosted_tool_spec("web_fetch")
    assert spec.input_schema["required"] == ["url"]


class _Stream:
    def __init__(self, data: bytes = b"", error: BaseException | None = None):
        self.data = data
        self.error = error

    async def read(self, size: int) -> bytes:
        if self.error is not None:
            raise self.error
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk


class _Process:
    def __init__(self, stdout: bytes = b"{}", *, returncode: int = 0, stderr: bytes = b"", pid: int | None = None):
        self.stdout, self.stderr = _Stream(stdout), _Stream(stderr)
        self.returncode = returncode
        self.killed = False
        self.pid = fake_pid() if pid is None else pid

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
    service.now = lambda: datetime.datetime.now(datetime.timezone.utc)

    class _UsageWriter:
        def __init__(self):
            self.calls = []

        def record(self, **kwargs):
            self.calls.append(kwargs)
            return object()

        async def wait_recorded(self, _future):
            return None

    service.usage_writer = _UsageWriter()

    async def spawn(*command, **kwargs):
        calls.append(command)
        return _Process(stdout, returncode=returncode, stderr=stderr)

    monkeypatch.setattr(service_module.asyncio, "create_subprocess_exec", spawn)
    candidate = direct_search_candidate(SOURCE, tool)
    assert candidate is not None
    attempt = await service._direct_search_attempt(
        ExecutorCall(tool, arguments), candidate, deadline=time.monotonic() + 10, say=lambda key, **params: key,
    )
    return attempt, calls, recorded, service.usage_writer.calls


async def test_search_runs_the_standalone_api_with_vault(monkeypatch, service) -> None:
    attempt, calls, recorded, usage = await _attempt(
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
    assert usage and usage[0]["source_id"] == SOURCE.id and usage[0]["model_id"] == "deepseek"


async def test_an_empty_search_is_completed_but_free(monkeypatch, service) -> None:
    attempt, _, _, _ = await _attempt(
        monkeypatch, service, "web_search", {"query": "q"}, b'{"results":[]}',
    )
    assert attempt.record["status"] == "completed"
    assert attempt.record["sources"] == []
    assert attempt.cost is None
    assert attempt.billable is False
    assert attempt.moves_on is False


def _message(attempt, tool: str):
    return message_from_dict({
        "role": "tool_result",
        "tool_call_id": "toolu_1",
        "tool_name": tool,
        "content": [],
        "is_error": attempt.record["status"] != "completed",
        "hosted": {"record": attempt.record, "execution": attempt.execution()},
    })


async def test_direct_records_pass_the_real_reader(monkeypatch, service) -> None:
    search, _, _, _ = await _attempt(
        monkeypatch, service, "web_search", {"query": "q"},
        json.dumps({"results": [{"title": "T", "url": "https://e", "snippet": "S", "date": "2026-10-19"}]}).encode(),
    )
    assert _message(search, "web_search").hosted is not None
    fetch, _, _, _ = await _attempt(
        monkeypatch, service, "web_fetch", {"url": "https://example.com/a"},
        json.dumps({"title": "T", "content": "body"}).encode(),
    )
    assert _message(fetch, "web_fetch").hosted is not None


async def test_fetch_returns_the_page_as_the_source(monkeypatch, service) -> None:
    attempt, calls, _, _ = await _attempt(
        monkeypatch, service, "web_fetch", {"url": "https://example.com/a"},
        json.dumps({"title": "T", "content": "body"}).encode(),
    )

    assert attempt.record["status"] == "completed"
    assert attempt.record["sources"] == [{"url": "https://example.com/a", "title": "T", "snippet": "body"}]
    assert "https://api.deepseek.com/v1/tools/fetch" in calls[0]


async def test_an_empty_fetch_is_a_failure(monkeypatch, service) -> None:
    attempt, _, _, _ = await _attempt(
        monkeypatch, service, "web_fetch", {"url": "https://example.com/a"},
        json.dumps({"title": "T", "content": ""}).encode(),
    )
    assert attempt.record["status"] == "failed"


async def test_a_vault_failure_is_the_record_error(monkeypatch, service) -> None:
    stderr = b'{\n  "ok": false,\n  "error": "secret not found: DEEPSEEK_API_KEY"\n}\n'
    attempt, _, recorded, _ = await _attempt(
        monkeypatch, service, "web_search", {"query": "q"}, b"{}", returncode=1, stderr=stderr,
    )
    assert attempt.record["status"] == "failed"
    assert attempt.record["error_code"] == "provider_unreachable"
    assert "secret not found" not in attempt.record["error"]
    assert recorded == []
    assert attempt.cost is None
    assert attempt.billable is False
    assert attempt.moves_on is True


async def test_malformed_source_fields_are_dropped(monkeypatch, service) -> None:
    attempt, _, _, _ = await _attempt(
        monkeypatch, service, "web_search", {"query": "q"},
        json.dumps({"results": [
            {"url": "", "title": "bad"},
            {"url": "https://e\nnext", "title": "bad"},
            {"url": "https://e", "title": {"bad": 1}, "snippet": "ok", "date": "2026-10-19"},
        ]}).encode(),
    )
    assert attempt.record["sources"] == [
        {"url": "https://e", "snippet": "ok", "page_age": "2026-10-19"},
    ]


class _FailingProcess(_Process):
    def __init__(self, error):
        super().__init__()
        self.stdout = _Stream(error=error)
        self.stderr = _Stream(error=error)


async def test_a_timeout_kills_the_process_tree(monkeypatch, service) -> None:
    process = _FailingProcess(asyncio.TimeoutError())

    async def spawn(*command, **kwargs):
        return process

    monkeypatch.setattr(service_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        service_module, "signal_process_tree",
        lambda process, sig, logger, label: process.kill(),
    )
    candidate = direct_search_candidate(SOURCE, "web_search")
    attempt = await service._direct_search_attempt(
        ExecutorCall("web_search", {"query": "q"}), candidate,
        deadline=time.monotonic() + 10, say=lambda key, **params: key,
    )
    assert process.killed is True
    assert attempt.record["status"] == "failed"
    assert attempt.record["error_code"] == "provider_timed_out"


async def test_cancellation_kills_the_process_tree(monkeypatch, service) -> None:
    process = _FailingProcess(asyncio.CancelledError())

    async def spawn(*command, **kwargs):
        return process

    monkeypatch.setattr(service_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        service_module, "signal_process_tree",
        lambda process, sig, logger, label: process.kill(),
    )
    candidate = direct_search_candidate(SOURCE, "web_search")
    with pytest.raises(asyncio.CancelledError):
        await service._direct_search_attempt(
            ExecutorCall("web_search", {"query": "q"}), candidate,
            deadline=time.monotonic() + 10, say=lambda key, **params: key,
        )
    assert process.killed is True


async def test_a_provider_parameter_error_does_not_fall_through(monkeypatch, service) -> None:
    body = json.dumps({"error": {"message": "'count' must be between 1 and 50", "code": "invalid_request_error"}}).encode()
    attempt, _, _, _ = await _attempt(
        monkeypatch, service, "web_search", {"query": "q"}, body, returncode=1,
    )
    assert attempt.record["error_code"] == "provider_refused"
    assert attempt.moves_on is False


async def test_a_rate_limit_is_transient_and_an_auth_error_is_forever(monkeypatch, service) -> None:
    rate = json.dumps({"error": {"message": "slow down, retry in 401ms", "code": "rate_limit_error"}}).encode()
    attempt, _, _, _ = await _attempt(
        monkeypatch, service, "web_search", {"query": "q"}, rate, returncode=1,
    )
    assert attempt.record["error_code"] == "provider_unavailable"
    assert attempt.moves_on is True

    auth = json.dumps({"error": {"message": "invalid key", "code": "authentication_error"}}).encode()
    attempt, _, _, _ = await _attempt(
        monkeypatch, service, "web_search", {"query": "q"}, auth, returncode=1,
    )
    assert attempt.record["error_code"] == "provider_refused"
    assert attempt.moves_on is False


async def test_cancellation_during_parse_still_meters(monkeypatch, service) -> None:
    import threading

    started = threading.Event()

    def slow_record(*_args):
        started.set()
        time.sleep(0.2)
        return {"status": "completed", "sources": [{"url": "https://e"}]}

    monkeypatch.setattr(service_module, "_direct_record", slow_record)
    monkeypatch.setattr(service_module.asyncio, "create_subprocess_exec", lambda *a, **k: _async_value(_Process()))
    service.record_hosted_success = _noop
    service.now = lambda: datetime.datetime.now(datetime.timezone.utc)

    class _UsageWriter:
        def __init__(self):
            self.calls = []

        def record(self, **kwargs):
            self.calls.append(kwargs)
            return object()

        async def wait_recorded(self, _future):
            return None

    service.usage_writer = _UsageWriter()
    candidate = direct_search_candidate(SOURCE, "web_search")
    task = asyncio.create_task(service._direct_search_attempt(
        ExecutorCall("web_search", {"query": "q"}), candidate,
        deadline=time.monotonic() + 10, say=lambda key, **params: key,
    ))
    while not started.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.5)
    assert service.usage_writer.calls


async def _noop(*_args, **_kwargs):
    return None


async def _async_value(value):
    return value


async def test_an_oversized_direct_response_is_a_failure(monkeypatch, service) -> None:
    attempt, _, _, _ = await _attempt(
        monkeypatch, service, "web_search", {"query": "q"}, b"x" * (4 * 1024 * 1024 + 1),
    )
    assert attempt.record["status"] == "failed"
    assert attempt.record["error_code"] == "provider_too_large"


async def test_a_base_url_ending_in_v1_does_not_double_the_prefix(monkeypatch, service) -> None:
    from dataclasses import replace

    global SOURCE
    original = SOURCE
    SOURCE = replace(SOURCE, base_url="https://api.deepseek.com/v1")
    try:
        _, calls, _, _ = await _attempt(
            monkeypatch, service, "web_search", {"query": "q"}, json.dumps({"results": []}).encode(),
        )
    finally:
        SOURCE = original
    assert "https://api.deepseek.com/v1/tools/search" in calls[0]
    assert "/v1/v1/" not in " ".join(calls[0])
