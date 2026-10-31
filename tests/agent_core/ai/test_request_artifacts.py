"""Failed model request artifacts (``core/agent_core/ai/request_artifacts.py``).

Inside a capture scope every model request ``drive_sse_stream`` sends is written before it goes out, as the exact
``httpx.Request.content``. A semantically successful one is removed; every other ending is kept with typed facts
and the raw response read. Outside a scope nothing is written.
"""

from __future__ import annotations

import asyncio
import json
import os
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
from core import private_fs
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
        events = await stream()
    # A Stop or a closed stream does not wait for its artifact's write; it lands once the worker gets to it.
    await _drain_worker()
    return sent, events


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
    for path in (store.root, directory, *(directory / name for name in (REQUEST_FILE, RESPONSE_FILE, META_FILE))):
        assert private_fs.is_private(path, os.lstat(path).st_mode), path


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


async def test_a_request_whose_consumer_stopped_reading_is_unknown_to_every_reader(store, tmp_path):
    """No outcome is known, and never a success: the entry is marked ``unknown`` on disk, so another process (the
    CLI) sees the same state while the owner still runs, not ``pending``."""

    body = _response(_text("partial"), stop_reason="end_turn", input_tokens=1, output_tokens=1)
    sent, _ = await _run(
        _anthropic_hub_request(),
        lambda _: httpx.Response(200, headers=_served(), text=body),
        store,
        gateway=True,
        consume=lambda event: not isinstance(event, (Done, ProviderError)),
    )

    (meta,) = _kept(store)
    on_disk = json.loads((store.path(meta["id"]) / META_FILE).read_text())
    assert on_disk["state"] == "unknown" and on_disk["outcome"]["kind"] == "unknown"
    assert (store.path(meta["id"]) / REQUEST_FILE).read_bytes() == sent[0].content


async def test_a_request_still_streaming_is_pending_to_every_reader(store):
    """The other side of the same rule: while the stream is open the entry is active and reads ``pending``."""

    body = _response(_text("partial"), stop_reason="end_turn", input_tokens=1, output_tokens=1)
    seen: list[tuple[str, str, bool]] = []

    def look(event: Any) -> bool:
        if not seen:
            (meta,) = _kept(store)
            on_disk = json.loads((store.path(meta["id"]) / META_FILE).read_text())
            # This process reads its active set; another process reads the disk and whether the owner lives.
            seen.append((meta["state"], on_disk["state"], request_artifacts._owner_alive(on_disk["owner"])))
        return False

    await _run(
        _anthropic_hub_request(), lambda _: httpx.Response(200, headers=_served(), text=body), store, gateway=True,
        consume=look,
    )

    assert seen == [("pending", "pending", True)]
    assert _kept(store) == []


def _blocked_begin(monkeypatch) -> tuple[threading.Event, threading.Event]:
    entered, release = threading.Event(), threading.Event()
    begin = RequestArtifactStore.begin

    def slow(self, meta, body):
        entered.set()
        release.wait(5)
        return begin(self, meta, body)

    monkeypatch.setattr(RequestArtifactStore, "begin", slow)
    return entered, release


async def _drain_worker() -> None:
    await asyncio.get_running_loop().run_in_executor(request_artifacts._worker(), lambda: None)


@pytest.mark.parametrize("landed_first", [False, True], ids=["cancelled-before-write-lands", "write-lands-first"])
async def test_a_task_cancelled_while_its_pending_write_runs_sends_nothing_and_settles_it(store, monkeypatch, landed_first):
    """Whichever comes second, the write landing or the cancellation, settles the attempt: never sent, never active,
    and ``cancelled`` / ``not_sent`` for every reader."""

    loop = asyncio.get_running_loop()
    entered, release = threading.Event(), threading.Event()
    tasks: list[asyncio.Task] = []
    begin = RequestArtifactStore.begin

    def slow(self, meta, body):
        entered.set()
        release.wait(5)
        artifact_id = begin(self, meta, body)
        if landed_first:
            # Queued on the loop before the write's own result: the task sees the cancellation after it landed.
            loop.call_soon_threadsafe(tasks[0].cancel)
        return artifact_id

    monkeypatch.setattr(RequestArtifactStore, "begin", slow)
    sent: list[httpx.Request] = []
    tasks.append(asyncio.create_task(
        _run(_direct_request("anthropic"), lambda wire: sent.append(wire) or httpx.Response(400), store)
    ))
    await asyncio.to_thread(entered.wait, 5)
    if not landed_first:
        tasks[0].cancel()
        await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    await _drain_worker()

    assert sent == []
    (meta,) = _kept(store)
    assert meta["state"] == "cancelled" and meta["outcome"]["kind"] == "not_sent"
    assert meta["id"] not in request_artifacts._ACTIVE


async def test_a_turn_stopped_while_its_pending_write_runs_sends_nothing(store, monkeypatch):
    """A Stop (the CancelToken) that arrives during the pending write is seen before the send: no provider work."""

    entered, release = _blocked_begin(monkeypatch)
    cancel = CancelToken()
    sent: list[httpx.Request] = []

    async def stop() -> None:
        await asyncio.to_thread(entered.wait, 5)
        cancel.cancel("stop")
        release.set()

    stopping = asyncio.create_task(stop())
    _, events = await _run(
        _direct_request("anthropic"), lambda wire: sent.append(wire) or httpx.Response(400), store, cancel=cancel
    )
    await stopping

    assert sent == [] and isinstance(events[-1], ProviderError) and events[-1].kind == "aborted"
    (meta,) = _kept(store)
    assert meta["state"] == "cancelled" and meta["outcome"]["kind"] == "not_sent"
    assert meta["id"] not in request_artifacts._ACTIVE


async def test_a_final_write_queued_behind_other_work_survives_its_tasks_cancellation(store):
    """Cancelling the task that awaits an ending does not cancel the queued write: it still runs, and the attempt
    is out of ``_ACTIVE`` from the moment it ended."""

    gate = threading.Event()
    blocker: list[Any] = []

    def answer(_: httpx.Request) -> httpx.Response:
        # Occupy the single worker so the ending's write has to queue behind it.
        blocker.append(request_artifacts._worker().submit(gate.wait, 5))
        return httpx.Response(400, json=REFUSAL)

    task = asyncio.create_task(_run(_direct_request("anthropic"), answer, store))
    while not blocker or request_artifacts._worker()._work_queue.qsize() == 0:
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (pending,) = _kept(store)
    assert pending["id"] not in request_artifacts._ACTIVE
    gate.set()
    await _drain_worker()

    (meta,) = _kept(store)
    assert meta["state"] == "failed" and meta["outcome"]["http_status"] == 400


class _PrefixThenReset(httpx.AsyncByteStream):
    def __init__(self, prefix: bytes) -> None:
        self.prefix = prefix

    async def __aiter__(self):
        yield self.prefix
        raise httpx.ReadError("connection reset")


async def test_an_error_body_is_kept_as_far_as_classification_read_it_and_no_further(store):
    """Capture only sees the reads the transport makes anyway: past the 64 KiB the error is classified by, nothing
    more is read, so a body that would fail later changes neither the error nor its timing, and the artifact says
    the body was not read to its end."""

    message = "prompt is too long: 250000 tokens > 200000 maximum"
    prefix = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": message}}).encode()
    prefix += b" " * (64 * 1024)

    def answer(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, headers={"content-type": "application/json"}, stream=_PrefixThenReset(prefix))

    _, plain = await _run(_direct_request("anthropic"), answer, None)
    _, captured = await _run(_direct_request("anthropic"), answer, store)

    assert isinstance(plain[-1], ProviderError)
    assert (captured[-1].kind, captured[-1].message, captured[-1].status) == (
        plain[-1].kind, plain[-1].message, plain[-1].status,
    )
    (meta,) = _kept(store)
    assert meta["outcome"]["kind"] == "http_error" and meta["outcome"]["exception_type"] is None
    assert not meta["response"]["read_to_end"] and not meta["response"]["truncated"]
    assert (store.path(meta["id"]) / RESPONSE_FILE).read_bytes() == prefix


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
    # Written as found on disk: not this process's active attempt, whatever its recorded state and owner.
    request_artifacts._ACTIVE.discard(artifact_id)
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
    artifact_id = _write(store, created_at=_meta_now()["created_at"])
    destination = tmp_path / "export"

    target = store.export(artifact_id, destination)

    assert target == destination / artifact_id
    assert (target / REQUEST_FILE).read_bytes() == b"x" * 10
    for path in (target, *target.iterdir()):
        assert private_fs.is_private(path, os.lstat(path).st_mode), path
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


def _meta_now(state: str = "pending") -> dict[str, Any]:
    from datetime import datetime, timezone

    return {"state": state, "created_at": datetime.now(timezone.utc).isoformat(),
            "owner": {"pid": os.getpid(), "started": request_artifacts._process_start(os.getpid())}}


@pytest.mark.parametrize("ending", ["failed", "not_sent", "unknown"])
def test_every_kept_ending_applies_retention_and_never_removes_an_active_attempt(tmp_path, ending):
    """With a quota smaller than any attempt, each kept ending removes what it can: older finished attempts and
    itself, but never an attempt this process is still sending."""

    store = RequestArtifactStore(tmp_path / "requests", quota_bytes=1)
    _write(store, created_at=_meta_now()["created_at"])
    active = store.begin(_meta_now(), b"in flight")
    ending_id = store.begin(_meta_now(), b"ending")
    try:
        request_artifacts._release(ending_id)  # as ``RequestAttempt._queue`` does before the write
        if ending == "failed":
            store.fail(ending_id, {"state": "failed", "outcome": {"kind": "http_error"}}, None)
        else:
            state = "cancelled" if ending == "not_sent" else "unknown"
            store.close(ending_id, state=state, kind=ending)

        assert {meta["id"] for meta in store.entries()} == {active}
    finally:
        request_artifacts._release(active)


def test_a_process_with_only_successes_still_expires_old_attempts_at_its_writes(tmp_path, monkeypatch):
    """No timer: retention runs at the store's writes, and at the first one after the maintenance interval a
    successful request's removal also removes what has expired."""

    clock = [1000.0]
    monkeypatch.setattr(request_artifacts, "_monotonic", lambda: clock[0])
    store = RequestArtifactStore(tmp_path / "requests", retention_s=3600)
    expired = _write(store, created_at="2026-01-01T00:00:00+00:00")
    first = store.begin(_meta_now(), b"ok")
    store.succeed(first)
    # Within the interval since the last run (the first write's): kept for now.
    assert expired in {meta["id"] for meta in store.entries()}

    clock[0] += request_artifacts.MAINTENANCE_INTERVAL_S
    second = store.begin(_meta_now(), b"ok")
    store.succeed(second)

    assert store.entries() == []


async def test_waiting_for_the_artifact_write_never_counts_as_provider_silence(tmp_path, monkeypatch):
    """The silence policy is decided before the artifact's final write is awaited: an error just inside the silence
    bound keeps its recovery permissions even when the write takes longer than what is left of the bound."""

    from core.agent_core.ai import _common

    clock = [5000.0]

    class Clock:
        def __getattr__(self, name: str) -> Any:
            import time

            return getattr(time, name)

        @staticmethod
        def monotonic() -> float:
            return clock[0]

    monkeypatch.setattr(_common, "time", Clock())
    fail = RequestArtifactStore.fail

    def slow_fail(self, *args, **kwargs):
        clock[0] += 10  # the worker's write, past the rest of the bound
        return fail(self, *args, **kwargs)

    monkeypatch.setattr(RequestArtifactStore, "fail", slow_fail)

    def answer(_: httpx.Request) -> httpx.Response:
        clock[0] += _common.PROVIDER_SILENCE_TIMEOUT_S - 1
        return httpx.Response(503, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})

    _, plain = await _run(_direct_request("anthropic"), answer, None)
    _, captured = await _run(_direct_request("anthropic"), answer, RequestArtifactStore(tmp_path / "requests"))

    assert plain[-1].retryable or plain[-1].continuable
    assert (captured[-1].retryable, captured[-1].continuable) == (plain[-1].retryable, plain[-1].continuable)


@pytest.mark.parametrize("chunks", [[3 * STREAM_TAIL_BYTES], [STREAM_TAIL_BYTES // 3 + 7] * 7, [10, 3 * STREAM_TAIL_BYTES, 5]],
                         ids=["one-large-chunk", "many-chunks", "large-between-small"])
def test_a_streams_kept_end_stays_within_its_bound_whatever_the_chunking(chunks):
    capture = request_artifacts.ResponseCapture()
    capture.response(200, {})
    stream = bytes(index % 251 for index in range(STREAM_HEAD_BYTES + sum(chunks)))
    capture.feed(stream[:STREAM_HEAD_BYTES])
    offset = STREAM_HEAD_BYTES
    for size in chunks:
        capture.feed(stream[offset:offset + size])
        offset += size
        assert capture._tail_size <= STREAM_TAIL_BYTES and sum(map(len, capture._tail)) == capture._tail_size

    kept = capture.body()
    assert kept == stream[:STREAM_HEAD_BYTES] + stream[-STREAM_TAIL_BYTES:]
    summary = capture.summary()
    assert summary["captured_bytes"] == len(kept)
    assert summary["omitted_bytes"] == len(stream) - len(kept) and summary["truncation"] == "middle"



# -- Stop through the Agent loop's real owner (``RunScope.call``) while the artifact worker is held ------------------


class _Held:
    """The artifact worker held by a gate, as a stalled filesystem would hold it."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.blocker: Any = None

    def hold(self) -> None:
        self.blocker = request_artifacts._worker().submit(self.gate.wait, 30)

    async def release(self) -> None:
        self.gate.set()
        await _drain_worker()


async def _drive_in_run_scope(request: ModelRequest, answer, store, *, cancel: CancelToken, stop, ready):
    """Read the adapter stream the way the Agent loop does, each step owned by ``RunScope.call``; ``stop`` is called
    once ``ready()`` says the driver waits on the held worker."""

    from core.agent_core.agent.lifecycle import RunAborted, RunScope

    scope = RunScope(cancel)
    events: list[Any] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        adapter = create_adapter(request.endpoint.protocol, client=client, gateway=False)
        with capture_model_requests(store, session_id="ses_artifacts", turn_id="trn_artifacts"):
            stream = adapter.stream(request, cancel)
            iterator = stream.__aiter__()

            async def read() -> None:
                while True:
                    try:
                        events.append(await scope.call(iterator.__anext__))
                    except StopAsyncIteration:
                        return

            reading = asyncio.create_task(read())
            while not ready() and not reading.done():
                await asyncio.sleep(0.005)
            assert not reading.done()
            started = asyncio.get_running_loop().time()
            await stop(reading)
            try:
                await asyncio.wait_for(reading, timeout=2)
            except (asyncio.CancelledError, RunAborted):
                pass
            elapsed = asyncio.get_running_loop().time() - started
    return events, elapsed


class _Hanging(httpx.AsyncByteStream):
    """A stream that sends its start and then nothing, as a slow model does."""

    def __init__(self, first: bytes, started: threading.Event) -> None:
        self.first = first
        self.started = started

    async def __aiter__(self):
        yield self.first
        self.started.set()
        await asyncio.Event().wait()


@pytest.mark.parametrize("stop_kind", ["cancel_token", "task_cancel"])
@pytest.mark.parametrize("phase", ["pending_write", "streaming", "final_write"])
async def test_a_stop_ends_at_once_while_the_artifact_worker_is_held(store, monkeypatch, stop_kind, phase):
    """With the worker held (a stalled filesystem), a Stop through the loop's owner ends the read at once, in every
    phase: a held pending write sends nothing; a stream cut mid-way only queues its ``unknown`` write; a held final
    write is left to run. Once the worker is free, every write lands and nothing stays active."""

    held = _Held()
    sent: list[httpx.Request] = []
    entered = threading.Event()
    if phase == "pending_write":
        begin = RequestArtifactStore.begin

        def gated(self, meta, body):
            entered.set()
            held.gate.wait(30)
            return begin(self, meta, body)

        monkeypatch.setattr(RequestArtifactStore, "begin", gated)
        ready = entered.is_set
    else:
        # The failure's write is queued behind the held worker.
        ready = lambda: request_artifacts._worker()._work_queue.qsize() > 0  # noqa: E731

    started = threading.Event()
    if phase == "streaming":
        ready = lambda: started.is_set() and held.blocker is not None  # noqa: E731

    def answer(wire: httpx.Request) -> httpx.Response:
        sent.append(wire)
        held.hold()  # every later write queues behind this
        if phase == "streaming":
            first = b'data: {"type": "message_start", "message": {"usage": {"input_tokens": 1}}}\n\n'
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Hanging(first, started))
        return httpx.Response(400, json=REFUSAL)

    cancel = CancelToken()

    async def stop(reading: asyncio.Task) -> None:
        if stop_kind == "cancel_token":
            cancel.cancel("stop")
        else:
            reading.cancel()

    _, elapsed = await _drive_in_run_scope(
        _direct_request("anthropic"), answer, store, cancel=cancel, stop=stop, ready=ready
    )

    assert elapsed < 1.5
    await held.release()
    (meta,) = _kept(store)
    assert meta["id"] not in request_artifacts._ACTIVE
    if phase == "pending_write":
        assert sent == [] and meta["state"] == "cancelled" and meta["outcome"]["kind"] == "not_sent"
    elif phase == "streaming":
        # Cut by the task (``unknown``), or by the driver's own CancelToken check first (``cancelled``).
        assert len(sent) == 1 and meta["state"] in {"unknown", "cancelled"}
    else:
        assert len(sent) == 1 and meta["state"] == "failed" and meta["outcome"]["http_status"] == 400


async def test_a_consumer_that_closes_the_stream_never_waits_for_the_held_worker(store):
    """A consumer's ``aclose`` queues the ``unknown`` write and returns: nothing in the driver's cleanup waits for
    diagnostics. The write lands once the worker is free."""

    held = _Held()
    body = _response(_text("partial"), stop_reason="end_turn", input_tokens=1, output_tokens=1)

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers=_served(), text=body)
    )) as client:
        adapter = create_adapter("anthropic", client=client, gateway=True)
        with capture_model_requests(store, session_id="ses_artifacts", turn_id="trn_artifacts"):
            stream = adapter.stream(_anthropic_hub_request(), CancelToken())
            await stream.__anext__()
            held.hold()
            await asyncio.wait_for(stream.aclose(), timeout=1)

    await held.release()
    (meta,) = _kept(store)
    assert meta["state"] == "unknown" and meta["id"] not in request_artifacts._ACTIVE


async def test_a_pending_write_that_stalls_past_its_bound_lets_the_request_go_untracked(store, monkeypatch):
    """Diagnostics never hold a request back for long: past ``ARTIFACT_WAIT_S`` the request is sent without
    waiting, its outcome is the same, and the late write lands as ``unknown`` / ``untracked``, never active."""

    monkeypatch.setattr(request_artifacts, "ARTIFACT_WAIT_S", 0.05)
    from core.agent_core.ai import _common

    monkeypatch.setattr(_common, "ARTIFACT_WAIT_S", 0.05)
    gate = threading.Event()
    begin = RequestArtifactStore.begin

    def stalled(self, meta, body):
        gate.wait(30)
        return begin(self, meta, body)

    monkeypatch.setattr(RequestArtifactStore, "begin", stalled)

    def answer(_: httpx.Request) -> httpx.Response:
        # Sent while the write still stalls; it lands only now.
        assert not gate.is_set()
        gate.set()
        return httpx.Response(400, json=REFUSAL)

    sent, events = await _run(_direct_request("anthropic"), answer, store)

    assert len(sent) == 1 and isinstance(events[-1], ProviderError) and events[-1].status == 400
    (meta,) = _kept(store)
    assert meta["state"] == "unknown" and meta["outcome"]["kind"] == "untracked"
    assert meta["id"] not in request_artifacts._ACTIVE
