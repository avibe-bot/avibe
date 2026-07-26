"""Contract tests for POSIX UDS and Windows loopback Controller IPC."""

from __future__ import annotations

import asyncio
import errno
import os
import socket
from contextlib import suppress
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from core import control_ipc, internal_server, session_turns
from modules.im import MessageContext
from vibe import internal_client


def _descriptor(
    *,
    port: int = 45678,
    instance_id: str = "a" * 32,
    bearer_token: str = "A" * 43,
) -> control_ipc.ControlIpcDescriptor:
    return control_ipc.ControlIpcDescriptor(
        schema_version=1,
        transport="tcp",
        host="127.0.0.1",
        port=port,
        instance_id=instance_id,
        bearer_token=bearer_token,
    )


def _controller_double() -> MagicMock:
    controller = MagicMock()
    controller._t = lambda key, **_kwargs: key
    return controller


async def _wait_until_ready(
    task: asyncio.Task,
    descriptor_path: Path,
) -> control_ipc.ControlIpcDescriptor:
    for _ in range(300):
        if task.done():
            await task
        if descriptor_path.exists() and await internal_client.health():
            return control_ipc.load_descriptor(descriptor_path)
        await asyncio.sleep(0.01)
    raise AssertionError("control IPC server did not become ready")


async def _stop_server(task: asyncio.Task) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def test_endpoint_selection_keeps_posix_uds_and_uses_windows_descriptor(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.delenv("VIBE_INTERNAL_DISPATCH_SOCKET", raising=False)

    posix = control_ipc.resolve_client_endpoint(platform_name="posix")
    assert posix.transport == "unix"
    assert posix.socket_path == (tmp_path / "state" / "dispatch.sock").resolve()
    assert posix.descriptor is None

    descriptor_path = control_ipc.default_descriptor_path()
    expected = _descriptor()
    control_ipc.write_descriptor_atomic(descriptor_path, expected)
    windows = control_ipc.resolve_client_endpoint(platform_name="nt")
    assert windows.transport == "tcp"
    assert windows.socket_path is None
    assert windows.descriptor == expected
    assert windows.base_url == "http://127.0.0.1:45678"
    assert windows.headers == {"Authorization": f"Bearer {expected.bearer_token}"}


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"schema_version": 2}, "schema"),
        ({"transport": "unix"}, "transport"),
        ({"host": "0.0.0.0"}, "loopback"),
        ({"host": "localhost"}, "loopback"),
        ({"port": 0}, "port"),
        ({"port": True}, "port"),
        ({"instance_id": "short"}, "instance"),
        ({"bearer_token": "guessable"}, "credential"),
        ({"extra": "field"}, "fields"),
    ],
)
def test_descriptor_validation_rejects_malformed_or_unsupported_data(update, message):
    payload = _descriptor().to_dict()
    payload.update(update)
    with pytest.raises(control_ipc.ControlIpcDescriptorError, match=message):
        control_ipc.validate_descriptor(payload)


def test_descriptor_reader_rejects_malformed_json_without_echoing_contents(tmp_path):
    target = tmp_path / "control-ipc.json"
    marker = "must-not-appear-in-error"
    target.write_text(f'{{"bearer_token":"{marker}"', encoding="utf-8")

    with pytest.raises(control_ipc.ControlIpcDescriptorError) as exc:
        control_ipc.load_descriptor(target)

    assert marker not in str(exc.value)


def test_descriptor_atomic_replace_preserves_previous_endpoint_on_failure(monkeypatch, tmp_path):
    target = tmp_path / "runtime" / "control-ipc.json"
    first = _descriptor(instance_id="1" * 32, bearer_token="B" * 43)
    second = _descriptor(instance_id="2" * 32, bearer_token="C" * 43)
    control_ipc.write_descriptor_atomic(target, first)

    def _fail_replace(_source, _target):
        raise PermissionError("replace blocked")

    monkeypatch.setattr(control_ipc.os, "replace", _fail_replace)
    with pytest.raises(PermissionError, match="replace blocked"):
        control_ipc.write_descriptor_atomic(target, second)

    assert control_ipc.load_descriptor(target) == first
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))
    if os.name != "nt":
        assert target.stat().st_mode & 0o077 == 0


def test_shutdown_cleanup_does_not_remove_successor_descriptor(tmp_path):
    target = tmp_path / "runtime" / "control-ipc.json"
    first = control_ipc.WindowsLoopbackHost(
        target,
        instance_id="1" * 32,
        bearer_token="B" * 43,
    )
    successor = control_ipc.WindowsLoopbackHost(
        target,
        instance_id="2" * 32,
        bearer_token="C" * 43,
    )
    first_bound = first.bind()
    successor_bound = successor.bind()
    try:
        assert not target.exists()
        first.publish(first_bound)
        successor.publish(successor_bound)

        first.cleanup(first_bound)
        assert control_ipc.load_descriptor(target) == successor_bound.descriptor
    finally:
        first_bound.listener.close()
        successor.cleanup(successor_bound)

    assert not target.exists()


def test_ephemeral_bind_retries_address_in_use_with_fresh_socket(tmp_path):
    attempts: list[object] = []

    class OccupiedSocket:
        def setsockopt(self, *_args):
            return None

        def bind(self, _address):
            raise OSError(errno.EADDRINUSE, "occupied")

        def close(self):
            return None

    def _socket_factory(family, kind):
        attempts.append(object())
        if len(attempts) == 1:
            return OccupiedSocket()
        return socket.socket(family, kind)

    host = control_ipc.WindowsLoopbackHost(
        tmp_path / "control-ipc.json",
        socket_factory=_socket_factory,
    )
    bound = host.bind()
    try:
        assert len(attempts) == 2
        assert bound.descriptor is not None
        assert bound.descriptor.host == "127.0.0.1"
        assert bound.descriptor.port > 0
    finally:
        host.cleanup(bound)


def test_windows_client_rejects_stale_instance_header(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(internal_client, "_platform_name", lambda: "nt")
    descriptor = _descriptor(instance_id="1" * 32)
    control_ipc.write_descriptor_atomic(control_ipc.default_descriptor_path(), descriptor)

    app = internal_server.create_app(
        _controller_double(),
        instance_id="2" * 32,
        bearer_token=descriptor.bearer_token,
    )
    transport = httpx.ASGITransport(app=app)
    monkeypatch.setattr(internal_client.httpx, "AsyncHTTPTransport", lambda **_kwargs: transport)

    with pytest.raises(internal_client.InternalServerUnavailable, match="stale instance"):
        asyncio.run(internal_client.turn_state("ses_stale"))


def test_real_windows_loopback_auth_and_non_ascii_dispatch_sse(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(internal_client, "_platform_name", lambda: "nt")
    monkeypatch.setattr(
        session_turns.SessionTurnManager,
        "recover_persisted_agent_run_queue",
        lambda _self: asyncio.sleep(0, result=[]),
    )
    captured: dict[str, object] = {}
    context = MessageContext(user_id="workbench", channel_id="unicode", platform="avibe")

    async def _build_payload(payload):
        captured["request_text"] = payload["text"]
        return payload["text"], context

    async def _dispatch(_controller, _context, text, *, on_chunk):
        captured["dispatch_text"] = text
        await on_chunk({"kind": "notify", "text": "你好，Windows"})
        await on_chunk({"kind": "result", "text": "完成 ✓"})

    monkeypatch.setattr(internal_server, "_build_dispatch_payload", _build_payload)
    monkeypatch.setattr(internal_server, "dispatch_turn", _dispatch)

    async def _run():
        descriptor_path = control_ipc.default_descriptor_path()
        task = asyncio.create_task(
            internal_server.serve(
                _controller_double(),
                platform_name="nt",
                descriptor_path=descriptor_path,
            )
        )
        try:
            descriptor = await _wait_until_ready(task, descriptor_path)
            async with httpx.AsyncClient(base_url=f"http://{descriptor.host}:{descriptor.port}") as client:
                missing = await client.get("/internal/health")
                incorrect = await client.get(
                    "/internal/health",
                    headers={"Authorization": "Bearer definitely-wrong"},
                )
            assert missing.status_code == 401
            assert incorrect.status_code == 401
            assert descriptor.bearer_token not in missing.text
            assert descriptor.bearer_token not in incorrect.text

            events = [
                event
                async for event in internal_client.stream_dispatch(
                    {"session_id": None, "text": "请处理 café 文件"}
                )
            ]
            assert events == [
                ("turn.start", {"session_id": None}),
                ("turn.chunk", {"kind": "notify", "text": "你好，Windows"}),
                ("turn.chunk", {"kind": "result", "text": "完成 ✓"}),
                ("turn.end", {"session_id": None}),
            ]
        finally:
            await _stop_server(task)
        assert not descriptor_path.exists()

    asyncio.run(_run())
    assert captured == {
        "request_text": "请处理 café 文件",
        "dispatch_text": "请处理 café 文件",
    }


def test_sse_reconnect_loads_successor_descriptor(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(internal_client, "_platform_name", lambda: "nt")
    monkeypatch.setattr(
        session_turns.SessionTurnManager,
        "recover_persisted_agent_run_queue",
        lambda _self: asyncio.sleep(0, result=[]),
    )

    async def _first_event():
        stream = internal_client.stream_events()
        try:
            return await anext(stream)
        finally:
            await stream.aclose()

    async def _run():
        descriptor_path = control_ipc.default_descriptor_path()
        first_task = asyncio.create_task(
            internal_server.serve(
                _controller_double(),
                platform_name="nt",
                descriptor_path=descriptor_path,
            )
        )
        first = await _wait_until_ready(first_task, descriptor_path)
        assert await _first_event() == ("connected", {})

        successor_task = asyncio.create_task(
            internal_server.serve(
                _controller_double(),
                platform_name="nt",
                descriptor_path=descriptor_path,
            )
        )
        try:
            successor = await _wait_until_ready(successor_task, descriptor_path)
            assert successor.instance_id != first.instance_id
            assert successor.bearer_token != first.bearer_token
            assert await _first_event() == ("connected", {})
        finally:
            await _stop_server(successor_task)
            await _stop_server(first_task)

    asyncio.run(_run())
