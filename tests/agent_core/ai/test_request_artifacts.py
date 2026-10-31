"""Failed model request artifacts (``core/agent_core/ai/request_artifacts.py``).

Inside a capture scope every model request ``drive_sse_stream`` sends is written before it goes out, as the exact
``httpx.Request.content``. A semantically successful one is removed; every other ending is kept with typed facts
and the raw response read. Outside a scope nothing is written.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import threading
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from core.agent_core.ai import request_artifacts
from core.agent_core.ai.provider import Done, ModelEndpoint, ModelRequest, ProviderError
from core.agent_core.ai.registry import create_adapter
from core.agent_core.ai.request_artifacts import (
    META_FILE,
    REQUEST_FILE,
    RESPONSE_FILE,
    STREAM_HEAD_BYTES,
    STREAM_TAIL_BYTES,
    RequestArtifactStore,
    capture_model_requests,
)
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import TextBlock, UserMessage
from tests.agent_core.ai.test_hosted_pause_turn import _request as _anthropic_hub_request
from tests.agent_core.ai.test_hosted_pause_turn import _response, _served, _text
from tests.fake_pid_helpers import fake_pid

SECRET = "sk-test-credential-never-stored"
PROMPT = "private prompt text"
ENDPOINTS = {
    "anthropic": ("https://api.anthropic.invalid/v1", "claude-opus-5-5", "anthropic"),
    "openai_responses": ("https://api.openai.invalid/v1", "gpt-5.6", "openai"),
    "openai_chat": ("https://chat.invalid/v1", "chat-model", "openai"),
    "google": ("https://gemini.invalid/v1beta?key=" + SECRET, "gemini-4-argon", "google"),
}
REFUSAL = {"type": "error", "error": {"type": "invalid_request_error", "message": f"refused: {PROMPT}"}}


def _direct_request(protocol: str) -> ModelRequest:
    base_url, model, provider = ENDPOINTS[protocol]
    return ModelRequest(
        endpoint=ModelEndpoint(protocol, base_url, model, SECRET, provider=provider, source_id="src_direct0001"),  # type: ignore[arg-type]
        system="system",
        messages=(UserMessage((TextBlock(PROMPT),)),),
        tools=(),
        max_tokens=64,
        cache="none",
    )


async def _run(
    request: ModelRequest,
    answer: Callable[[httpx.Request], httpx.Response],
    store: RequestArtifactStore | None,
    *,
    gateway: bool = False,
    cancel: CancelToken | None = None,
    consume: Callable[[Any], Any] | None = None,
) -> tuple[list[httpx.Request], list[Any]]:
    sent: list[httpx.Request] = []

    def transport(wire: httpx.Request) -> httpx.Response:
        sent.append(wire)
        return answer(wire)

    async def stream() -> list[Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            adapter = create_adapter(request.endpoint.protocol, client=client, gateway=gateway)
            events = []
            async with aclosing(adapter.stream(request, cancel or CancelToken())) as stream:
                async for event in stream:
                    events.append(event)
                    if consume is not None and consume(event):
                        break
            return events

    if store is None:
        return sent, await stream()
    with capture_model_requests(store, session_id="ses_artifacts", turn_id="trn_artifacts", version="9.9.9"):
        return sent, await stream()


def _kept(store: RequestArtifactStore) -> list[dict[str, Any]]:
    return store.entries()


@pytest.fixture
def store(tmp_path: Path) -> RequestArtifactStore:
    return RequestArtifactStore(tmp_path / "state" / "model-requests")


@pytest.mark.parametrize("protocol", sorted(ENDPOINTS))
@pytest.mark.parametrize("status", [400, 503])
async def test_a_refused_request_keeps_the_exact_bytes_sent_with_typed_facts_only(store, protocol, status):
    """Every adapter goes through the shared driver: the kept body is the transport's own bytes, and the metadata
    names the hop without its credential, the URL's query, or the provider's message."""

    sent, events = await _run(_direct_request(protocol), lambda _: httpx.Response(status, json=REFUSAL), store)

    assert isinstance(events[-1], ProviderError)
    (meta,) = _kept(store)
    directory = store.path(meta["id"])
    assert (directory / REQUEST_FILE).read_bytes() == sent[0].content
    assert json.loads((directory / RESPONSE_FILE).read_bytes()) == REFUSAL
    assert meta["state"] == "failed"
    assert meta["outcome"] == {
        "kind": "http_error", "error_kind": events[-1].kind, "http_status": status, "exception_type": None,
    }
    assert meta["session_id"] == "ses_artifacts" and meta["turn_id"] == "trn_artifacts"
    assert meta["avibe_version"] == "9.9.9"
    assert meta["request"]["protocol"] == protocol and meta["request"]["model"] == ENDPOINTS[protocol][1]
    assert meta["request"]["body_bytes"] == len(sent[0].content)
    assert meta["response"]["read_to_end"] and not meta["response"]["truncated"]
    text = (directory / META_FILE).read_text()
    assert SECRET not in text and PROMPT not in text and "?" not in meta["request"]["url"]
    assert stat.S_IMODE(os.stat(store.root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    for name in (REQUEST_FILE, RESPONSE_FILE, META_FILE):
        assert stat.S_IMODE(os.stat(directory / name).st_mode) == 0o600


async def test_a_request_that_never_got_a_response_is_kept_as_a_network_error(store):
    def answer(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    sent, events = await _run(_direct_request("anthropic"), answer, store)

    assert isinstance(events[-1], ProviderError)
    (meta,) = _kept(store)
    assert (store.path(meta["id"]) / REQUEST_FILE).read_bytes() == sent[0].content
    assert not (store.path(meta["id"]) / RESPONSE_FILE).exists()
    assert meta["outcome"]["kind"] == "network_error" and meta["outcome"]["exception_type"] == "ConnectError"


async def test_a_stream_that_ends_in_an_error_after_http_200_is_kept_with_its_end(store):
    """HTTP 200 alone is not success: a stream whose terminal is an error keeps the request and the stream's head and
    tail, so the error event at its end survives a long response."""

    filler = "x" * (STREAM_HEAD_BYTES + STREAM_TAIL_BYTES)
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": filler}},
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    ]
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)

    sent, result = await _run(
        _anthropic_hub_request(), lambda _: httpx.Response(200, headers=_served(), text=body), store, gateway=True
    )

    assert isinstance(result[-1], ProviderError)
    (meta,) = _kept(store)
    kept = (store.path(meta["id"]) / RESPONSE_FILE).read_bytes()
    assert meta["outcome"]["kind"] == "stream_error" and meta["outcome"]["http_status"] == 200
    assert meta["response"]["truncated"] and meta["response"]["truncation"] == "middle"
    assert meta["response"]["omitted_bytes"] == len(body.encode()) - len(kept)
    assert kept.startswith(b'data: {"type": "message_start"')
    assert kept.endswith(body.encode()[-200:]) and b"overloaded_error" in kept
    assert meta["response"]["headers"]["x-avibe-served-hop"]
    assert (store.path(meta["id"]) / REQUEST_FILE).read_bytes() == sent[0].content


async def test_an_error_body_is_kept_whole_up_to_its_limit_and_the_cut_is_stated(store, monkeypatch):
    monkeypatch.setattr(request_artifacts, "ERROR_BODY_LIMIT", 1024)
    body = b"e" * 5000

    await _run(_direct_request("openai_chat"), lambda _: httpx.Response(500, content=body), store)

    (meta,) = _kept(store)
    kept = (store.path(meta["id"]) / RESPONSE_FILE).read_bytes()
    assert kept == body[:1024]
    assert meta["response"]["truncated"] and meta["response"]["truncation"] == "end"
    assert meta["response"]["read_bytes"] == 5000 and meta["response"]["omitted_bytes"] == 5000 - 1024


async def test_a_successful_request_and_a_paused_sub_request_are_not_kept(store):
    """A ``Done`` removes its request; so does an Anthropic ``pause_turn`` sub-request that ended normally, while a
    continuation that then fails is kept on its own."""

    def answer(number: int) -> httpx.Response:
        if number == 1:
            body = _response(_text("part 1"), stop_reason="pause_turn", input_tokens=10, output_tokens=1)
        elif number == 2:
            body = _response(_text("done"), stop_reason="end_turn", input_tokens=10, output_tokens=1)
        else:
            return httpx.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "boom"}})
        return httpx.Response(200, headers=_served(), text=body)

    calls = iter(range(1, 10))
    sent, events = await _run(_anthropic_hub_request(), lambda _: answer(next(calls)), store, gateway=True)

    assert len(sent) == 2 and isinstance(events[-1], Done)
    assert _kept(store) == []

    calls = iter((1, 3))
    sent, events = await _run(_anthropic_hub_request(), lambda _: answer(next(calls)), store, gateway=True)

    assert len(sent) == 2 and isinstance(events[-1], ProviderError)
    (meta,) = _kept(store)
    assert (store.path(meta["id"]) / REQUEST_FILE).read_bytes() == sent[1].content


async def test_a_cancelled_request_is_kept_as_cancelled(store):
    cancel = CancelToken()

    def answer(_: httpx.Request) -> httpx.Response:
        cancel.cancel("stop")
        return httpx.Response(200, headers=_served(), text=_response(_text("x"), stop_reason="end_turn", input_tokens=1, output_tokens=1))

    _, events = await _run(_anthropic_hub_request(), answer, store, gateway=True, cancel=cancel)

    assert isinstance(events[-1], ProviderError) and events[-1].kind == "aborted"
    (meta,) = _kept(store)
    assert meta["state"] == "cancelled" and meta["outcome"]["kind"] == "cancelled"


async def test_a_request_whose_consumer_stopped_reading_stays_unknown_never_a_success(store):
    """No outcome is known: the pending entry written before the send stays, reads ``unknown``, and nothing else is
    scheduled for it."""

    body = _response(_text("partial"), stop_reason="end_turn", input_tokens=1, output_tokens=1)
    sent, _ = await _run(
        _anthropic_hub_request(),
        lambda _: httpx.Response(200, headers=_served(), text=body),
        store,
        gateway=True,
        consume=lambda event: not isinstance(event, (Done, ProviderError)),
    )

    (meta,) = _kept(store)
    assert meta["state"] == "unknown" and "outcome" not in meta and "finished_at" not in meta
    assert (store.path(meta["id"]) / REQUEST_FILE).read_bytes() == sent[0].content


async def test_nothing_is_written_outside_a_capture_scope(tmp_path):
    await _run(_direct_request("anthropic"), lambda _: httpx.Response(400, json=REFUSAL), None)

    assert not any(tmp_path.rglob("request.bin"))


async def test_the_store_works_off_the_event_loop_and_its_failure_changes_no_outcome(tmp_path, monkeypatch, caplog):
    """Artifact work runs on its own thread; a store that cannot be written leaves the request's events exactly as
    they are without capture, and logs no payload."""

    threads: list[str] = []
    begin = RequestArtifactStore.begin

    def record(self, meta, body):
        threads.append(threading.current_thread().name)
        return begin(self, meta, body)

    monkeypatch.setattr(RequestArtifactStore, "begin", record)
    working = RequestArtifactStore(tmp_path / "ok")
    await _run(_direct_request("anthropic"), lambda _: httpx.Response(400, json=REFUSAL), working)
    assert threads and all(name.startswith("avibe-request-artifacts") for name in threads)
    assert threading.current_thread().name not in threads

    (tmp_path / "blocked").write_text("not a directory")
    broken = RequestArtifactStore(tmp_path / "blocked")
    body = _response(_text("ok"), stop_reason="end_turn", input_tokens=1, output_tokens=1)
    answer = lambda _: httpx.Response(200, headers=_served(), text=body)  # noqa: E731
    _, plain = await _run(_anthropic_hub_request(), answer, None, gateway=True)
    with caplog.at_level("WARNING", logger="core.agent_core.ai.request_artifacts"):
        _, captured = await _run(_anthropic_hub_request(), answer, broken, gateway=True)

    assert [type(event) for event in captured] == [type(event) for event in plain]
    assert captured[-1].message.content == plain[-1].message.content
    assert any("record a pending request" in record.getMessage() for record in caplog.records)
    assert all(PROMPT not in record.getMessage() for record in caplog.records)


def _write(store: RequestArtifactStore, *, created_at: str, size: int = 10, owner: dict | None = None,
           state: str = "failed") -> str:
    meta = {"state": state, "created_at": created_at, "owner": owner or {"pid": 0, "started": None}}
    artifact_id = store.begin(meta, b"x" * size)
    store.abandon(artifact_id)
    return artifact_id


@pytest.fixture
def other_process():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield process.pid
    finally:
        process.kill()
        process.wait()


def test_retention_drops_expired_and_oldest_attempts_but_never_an_active_one(tmp_path, other_process):
    store = RequestArtifactStore(tmp_path / "requests", retention_s=3600)
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    expired = _write(store, created_at=(now - timedelta(hours=2)).isoformat())
    old = _write(store, created_at=(now - timedelta(minutes=30)).isoformat(), size=900)
    new = _write(store, created_at=(now - timedelta(minutes=10)).isoformat(), size=900)
    # Pending and owned by another live process: active, never removed, however old or over quota.
    live = {"pid": other_process, "started": request_artifacts._process_start(other_process)}
    active = _write(store, created_at=(now - timedelta(hours=5)).isoformat(), size=900, owner=live, state="pending")
    # Pending but its owner is gone: unknown, never a success, and removable like any finished attempt.
    orphan = _write(
        store, created_at=(now - timedelta(minutes=20)).isoformat(), owner={"pid": fake_pid(), "started": 1.0},
        state="pending",
    )
    # This process's own pending attempt that it no longer sends (``abandon``): unknown as well.
    own = _write(
        store, created_at=(now - timedelta(minutes=5)).isoformat(),
        owner={"pid": os.getpid(), "started": request_artifacts._process_start(os.getpid())}, state="pending",
    )

    entries = {meta["id"]: meta for meta in store.entries()}
    assert entries[active]["state"] == "pending"
    assert entries[orphan]["state"] == "unknown" and entries[own]["state"] == "unknown"
    # Room for exactly the newest finished attempts and the active one.
    store.quota_bytes = sum(entries[key]["size_bytes"] for key in (active, new, own))

    store.prune()

    remaining = {meta["id"] for meta in store.entries()}
    # Past retention it goes; over quota, the oldest finished ones go first; the active one stays though oldest.
    assert remaining == {active, new, own}
    assert expired not in remaining and old not in remaining and orphan not in remaining


def test_an_attempt_is_read_only_from_the_store_and_exported_into_a_new_private_directory(tmp_path):
    store = RequestArtifactStore(tmp_path / "requests")
    artifact_id = _write(store, created_at="2026-10-31T00:00:00+00:00")
    destination = tmp_path / "export"

    target = store.export(artifact_id, destination)

    assert target == destination / artifact_id
    assert (target / REQUEST_FILE).read_bytes() == b"x" * 10
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(target / REQUEST_FILE).st_mode) == 0o600
    with pytest.raises(FileExistsError):
        store.export(artifact_id, destination)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    with pytest.raises(request_artifacts.ArtifactStoreError):
        store.export(artifact_id, link)
    for bad in ("../requests", "mr_x", f"{artifact_id}/../.."):
        with pytest.raises(request_artifacts.ArtifactStoreError):
            store.path(bad)


async def test_concurrent_requests_each_keep_their_own_bytes(store):
    async def one(protocol: str) -> tuple[list[httpx.Request], list[Any]]:
        request = _direct_request(protocol)
        request = replace(request, messages=(UserMessage((TextBlock(f"{PROMPT} {protocol}"),)),))
        return await _run(request, lambda _: httpx.Response(400, json=REFUSAL), store)

    results = await asyncio.gather(*(one(protocol) for protocol in sorted(ENDPOINTS)))

    kept = {store.path(meta["id"]).joinpath(REQUEST_FILE).read_bytes() for meta in _kept(store)}
    assert kept == {sent[0].content for sent, _ in results}
