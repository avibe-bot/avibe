"""Contract tests for POSIX UDS and Windows loopback Controller IPC."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import socket
import subprocess
import threading
from contextlib import suppress
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from config import paths
from core import control_ipc, internal_server, session_turns
from vibe import internal_client, model_hub_client


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
    controller._delivery_recovery_complete = None
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


def test_new_windows_hosts_rotate_instance_and_bearer_credentials(tmp_path):
    first = control_ipc.WindowsLoopbackHost(tmp_path / "control-ipc.json")
    second = control_ipc.WindowsLoopbackHost(tmp_path / "control-ipc.json")

    assert first.instance_id != second.instance_id
    assert first.bearer_token != second.bearer_token


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows ACLs")
def test_windows_control_ipc_artifacts_have_exact_private_security(monkeypatch, tmp_path):
    target = tmp_path / "runtime" / "control-ipc.json"
    descriptor = _descriptor()
    security = control_ipc._windows_security()
    original_replace = control_ipc.os.replace
    temporary_checked = False

    def _validate_temporary_before_replace(source, destination):
        nonlocal temporary_checked
        security.validate_path(Path(source))
        temporary_checked = True
        original_replace(source, destination)

    monkeypatch.setattr(control_ipc.os, "replace", _validate_temporary_before_replace)

    control_ipc.write_descriptor_atomic(target, descriptor)

    assert temporary_checked
    security.validate_path(target.parent)
    security.validate_path(target.with_name(f"{target.name}.lock"))
    security.validate_path(target)
    assert control_ipc.load_descriptor(target) == descriptor
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))


def _restore_windows_inheritance(root: Path) -> None:
    """Undo a protected DACL so pytest can still delete *root* afterwards.

    Teardown only, and deliberately not ``check=True``: a cleanup failure must
    never replace the assertion failure the test exists to report.
    """

    for arguments in (["/inheritance:e"], ["/reset", "/T", "/C", "/Q"]):
        subprocess.run(["icacls", str(root), *arguments], capture_output=True, text=True)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows ACLs")
def test_windows_control_ipc_hardening_keeps_existing_runtime_siblings_readable(monkeypatch, tmp_path):
    """Hardening the descriptor must not orphan files the runtime already owns.

    ``SetNamedSecurityInfoW`` with ``PROTECTED_DACL`` propagates: when the DACL
    it applies carries no inheritable ACE, the inherited ACEs are stripped from
    every child that already exists. Children created purely by inheritance hold
    no explicit ACE of their own, so they are left unreadable to the very
    process that made them. This is what killed the gh-v3.1.1rc9 Windows leg --
    the service lock, both captured stdio logs and the model-hub tree all became
    ``[Errno 13]`` about twenty milliseconds after the Controller started.

    The assertion is deliberately about a file that exists *before* the securing
    call. A file created after it stays readable even with the defect present,
    so testing that direction would pass on broken code.
    """

    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path)
    runtime = paths.get_runtime_dir()
    runtime.mkdir(parents=True)

    lock = runtime / "service.lock"
    lock.write_text("4321\n", encoding="utf-8")
    service_log = runtime / "service_stderr.log"
    service_log.write_text("started\n", encoding="utf-8")
    hub = runtime / "model-hub"
    hub.mkdir()
    (hub / "manifest.json").write_text("{}", encoding="utf-8")

    descriptor = _descriptor()
    try:
        control_ipc.write_descriptor_atomic(paths.get_runtime_control_ipc_endpoint_path(), descriptor)

        # vibe/runtime.py opens the service lock exactly this way on every lease
        # poll, and rc9 died on this call.
        with lock.open("a+", encoding="utf-8") as handle:
            handle.seek(0)
            assert handle.read() == "4321\n"
        # The probe's own diagnostics: losing these is what made rc9 tell us
        # less than rc8 did.
        assert service_log.read_text(encoding="utf-8") == "started\n"
        # rc9 failed here twice, on scandir and again on the chmod that
        # TemporaryDirectory cleanup falls back to.
        assert sorted(entry.name for entry in os.scandir(hub)) == ["manifest.json"]
        assert control_ipc.load_descriptor(paths.get_runtime_control_ipc_endpoint_path()) == descriptor
    finally:
        _restore_windows_inheritance(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows ACLs")
def test_windows_control_ipc_hardening_survives_a_second_service_start(monkeypatch, tmp_path):
    """Cover the create path and the repair path in the order a service hits them.

    The first write reaches ``CreateDirectoryW``; the second reaches
    ``secure_existing_owned_path``. Between them the launcher writes the files a
    running service produces, so a second start must not strand them.
    """

    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path)
    endpoint = paths.get_runtime_control_ipc_endpoint_path()
    first = _descriptor(instance_id="1" * 32, bearer_token="B" * 43)
    successor = _descriptor(instance_id="2" * 32, bearer_token="C" * 43)

    try:
        control_ipc.write_descriptor_atomic(endpoint, first)

        lock = paths.get_runtime_dir() / "service.lock"
        lock.write_text("8765\n", encoding="utf-8")

        control_ipc.write_descriptor_atomic(endpoint, successor)

        with lock.open("a+", encoding="utf-8") as handle:
            handle.seek(0)
            assert handle.read() == "8765\n"
        assert control_ipc.load_descriptor(endpoint) == successor
    finally:
        _restore_windows_inheritance(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows ACLs")
def test_windows_descriptor_reader_rejects_widened_dacl_and_writer_repairs_it(tmp_path):
    target = tmp_path / "runtime" / "control-ipc.json"
    first = _descriptor(instance_id="1" * 32, bearer_token="B" * 43)
    successor = _descriptor(instance_id="2" * 32, bearer_token="C" * 43)
    control_ipc.write_descriptor_atomic(target, first)

    subprocess.run(
        ["icacls", str(target), "/grant", "*S-1-1-0:R"],
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(control_ipc.ControlIpcDescriptorError):
        control_ipc.load_descriptor(target)

    control_ipc.write_descriptor_atomic(target, successor)
    assert control_ipc.load_descriptor(target) == successor
    control_ipc._windows_security().validate_path(target)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows ACLs")
def test_windows_descriptor_reader_rejects_widened_lock_and_writer_repairs_it(tmp_path):
    target = tmp_path / "runtime" / "control-ipc.json"
    descriptor = _descriptor()
    control_ipc.write_descriptor_atomic(target, descriptor)
    lock_path = target.with_name(f"{target.name}.lock")

    subprocess.run(
        ["icacls", str(lock_path), "/grant", "*S-1-1-0:R"],
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(control_ipc.ControlIpcDescriptorError):
        control_ipc.load_descriptor(target)

    control_ipc.write_descriptor_atomic(target, descriptor)
    assert control_ipc.load_descriptor(target) == descriptor
    control_ipc._windows_security().validate_path(lock_path)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows ACLs")
def test_windows_descriptor_reader_rejects_widened_runtime_directory(tmp_path):
    target = tmp_path / "runtime" / "control-ipc.json"
    descriptor = _descriptor()
    control_ipc.write_descriptor_atomic(target, descriptor)

    subprocess.run(
        ["icacls", str(target.parent), "/grant", "*S-1-1-0:R"],
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(control_ipc.ControlIpcDescriptorError):
        control_ipc.load_descriptor(target)

    control_ipc.write_descriptor_atomic(target, descriptor)
    assert control_ipc.load_descriptor(target) == descriptor
    control_ipc._windows_security().validate_path(target.parent)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows ACLs")
def test_windows_descriptor_rejects_non_owner_and_security_api_failure(
    monkeypatch,
    tmp_path,
):
    target = tmp_path / "runtime" / "control-ipc.json"
    descriptor = _descriptor()
    control_ipc.write_descriptor_atomic(target, descriptor)
    security = control_ipc._windows_security()

    fd = os.open(target, os.O_RDONLY)
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(security.advapi32, "EqualSid", lambda *_args: False)
            with pytest.raises(
                control_ipc.ControlIpcSecurityError,
                match="owner or DACL",
            ):
                security.validate_file_descriptor(fd, target)
    finally:
        os.close(fd)

    with monkeypatch.context() as scoped:
        scoped.setattr(security.advapi32, "GetSecurityInfo", lambda *_args: 5)
        with pytest.raises(control_ipc.ControlIpcDescriptorError):
            control_ipc.load_descriptor(target)


def test_descriptor_read_blocks_successor_publication_until_file_is_closed(monkeypatch, tmp_path):
    target = tmp_path / "runtime" / "control-ipc.json"
    first = _descriptor(instance_id="1" * 32, bearer_token="B" * 43)
    successor = _descriptor(instance_id="2" * 32, bearer_token="C" * 43)
    control_ipc.write_descriptor_atomic(target, first)

    reader_open = threading.Event()
    release_reader = threading.Event()
    replace_started = threading.Event()
    original_fdopen = control_ipc.os.fdopen
    original_replace = control_ipc.os.replace

    def _paused_fdopen(fd, mode, *args, **kwargs):
        stream = original_fdopen(fd, mode, *args, **kwargs)
        if mode == "r":
            reader_open.set()
            assert release_reader.wait(timeout=5)
        return stream

    def _observed_replace(source, destination):
        replace_started.set()
        return original_replace(source, destination)

    monkeypatch.setattr(control_ipc.os, "fdopen", _paused_fdopen)
    monkeypatch.setattr(control_ipc.os, "replace", _observed_replace)

    read_result: list[control_ipc.ControlIpcDescriptor] = []
    reader = threading.Thread(target=lambda: read_result.append(control_ipc.load_descriptor(target)))
    publisher = threading.Thread(target=lambda: control_ipc.write_descriptor_atomic(target, successor))
    reader.start()
    assert reader_open.wait(timeout=5)
    publisher.start()
    assert not replace_started.wait(timeout=0.1)

    release_reader.set()
    reader.join(timeout=5)
    publisher.join(timeout=5)

    assert not reader.is_alive()
    assert not publisher.is_alive()
    assert read_result == [first]
    assert replace_started.is_set()
    assert control_ipc.load_descriptor(target) == successor


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


def test_model_hub_client_uses_windows_endpoint_auth_and_instance_validation(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(internal_client, "_platform_name", lambda: "nt")
    descriptor = _descriptor(instance_id="1" * 32, bearer_token="B" * 43)
    control_ipc.write_descriptor_atomic(control_ipc.default_descriptor_path(), descriptor)
    requests: list[tuple[str, str]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append((payload["operation"], request.headers["authorization"]))
        response_instance = "2" * 32 if payload["operation"] == "stale" else descriptor.instance_id
        return httpx.Response(
            200,
            json={"ok": True, "result": payload["operation"]},
            headers={control_ipc.CONTROL_IPC_INSTANCE_HEADER: response_instance},
        )

    transport = httpx.MockTransport(_handler)
    monkeypatch.setattr(internal_client.httpx, "HTTPTransport", lambda **_kwargs: transport)
    monkeypatch.setattr(internal_client.httpx, "AsyncHTTPTransport", lambda **_kwargs: transport)

    assert model_hub_client._rpc_sync("sync") == "sync"
    assert asyncio.run(model_hub_client._rpc("async")) == "async"
    with pytest.raises(model_hub_client.ModelHubError) as exc_info:
        model_hub_client._rpc_sync("stale")

    assert exc_info.value.code == "engine_down"
    assert requests == [
        ("sync", f"Bearer {descriptor.bearer_token}"),
        ("async", f"Bearer {descriptor.bearer_token}"),
        ("stale", f"Bearer {descriptor.bearer_token}"),
    ]


def test_archive_client_uses_windows_endpoint_auth_and_instance_validation(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(internal_client, "_platform_name", lambda: "nt")
    descriptor = _descriptor(instance_id="3" * 32, bearer_token="C" * 43)
    control_ipc.write_descriptor_atomic(control_ipc.default_descriptor_path(), descriptor)
    requests: list[tuple[str, str, dict]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append((request.url.path, request.headers["authorization"], payload))
        response_instance = "4" * 32 if payload["session_id"] == "ses_stale" else descriptor.instance_id
        return httpx.Response(
            200,
            json={"ok": True},
            headers={control_ipc.CONTROL_IPC_INSTANCE_HEADER: response_instance},
        )

    transport = httpx.MockTransport(_handler)
    monkeypatch.setattr(internal_client.httpx, "AsyncHTTPTransport", lambda **_kwargs: transport)

    assert asyncio.run(internal_client.archive_session("ses_ok")) == {
        "status_code": 200,
        "body": {"ok": True},
    }
    with pytest.raises(internal_client.InternalServerUnavailable, match="stale instance"):
        asyncio.run(internal_client.archive_session("ses_stale"))
    assert requests == [
        ("/internal/sessions/archive", f"Bearer {descriptor.bearer_token}", {"session_id": "ses_ok"}),
        ("/internal/sessions/archive", f"Bearer {descriptor.bearer_token}", {"session_id": "ses_stale"}),
    ]


def test_windows_unhandled_500_response_carries_instance_header():
    descriptor = _descriptor(instance_id="1" * 32, bearer_token="B" * 43)
    app = internal_server.create_app(
        _controller_double(),
        instance_id=descriptor.instance_id,
        bearer_token=descriptor.bearer_token,
    )

    @app.get("/internal/test-unhandled-error")
    async def _unhandled_error():
        raise RuntimeError("test failure")

    async def _run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {descriptor.bearer_token}"},
        ) as client:
            return await client.get("/internal/test-unhandled-error")

    response = asyncio.run(_run())

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert response.headers[control_ipc.CONTROL_IPC_INSTANCE_HEADER] == descriptor.instance_id
    internal_client._validate_response(
        response,
        control_ipc.ControlIpcClientEndpoint(
            transport="tcp",
            descriptor=descriptor,
        ),
    )


def test_real_windows_loopback_auth_and_non_ascii_event_sse(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(internal_client, "_platform_name", lambda: "nt")
    monkeypatch.setattr(
        session_turns.SessionTurnManager,
        "recover_persisted_agent_run_queue",
        lambda _self: asyncio.sleep(0, result=[]),
    )
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

            stream = internal_client.stream_events()
            try:
                assert await anext(stream) == ("connected", {})
                next_event = asyncio.create_task(anext(stream))
                published = await internal_client.publish_event(
                    "queue.updated",
                    {"text": "请处理 café 文件：你好，Windows ✓"},
                )
                assert published == {"ok": True}
                assert await asyncio.wait_for(next_event, timeout=2.0) == (
                    "queue.updated",
                    {"text": "请处理 café 文件：你好，Windows ✓"},
                )
            finally:
                await stream.aclose()
        finally:
            await _stop_server(task)
        assert not descriptor_path.exists()

    asyncio.run(_run())


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


class _FakeSid:
    """A SID pointer stand-in that compares by the SDDL text that produced it."""

    def __init__(self, value: str | None = None) -> None:
        self.value = value

    def __bool__(self) -> bool:
        return self.value is not None


class _FakeCtypes:
    @staticmethod
    def byref(cell):
        return cell


class _FakeWinTypes:
    LPVOID = _FakeSid
    BOOL = _FakeSid
    DWORD = _FakeSid


def _windows_security_stub(*, user_sid: str, owner_sid: str):
    """Drive the real owner-acceptance logic without Windows ctypes bindings.

    ``_WindowsSecurity.__init__`` loads ``advapi32``/``kernel32``, so it cannot
    run off Windows. Every method under test reads only instance attributes, so
    a bypassed constructor with fake bindings exercises the real decision.
    """

    security = object.__new__(control_ipc._WindowsSecurity)
    security.ctypes = _FakeCtypes
    security.wintypes = _FakeWinTypes
    security.current_user_sid = user_sid
    security.current_owner_sid = owner_sid
    security.sddl = f"O:{user_sid}D:P(A;;FA;;;{user_sid})(A;;FA;;;SY)"

    def _convert(sddl, _revision, descriptor_ref, _size_ref):
        descriptor_ref.value = sddl
        return 1

    def _descriptor_owner(descriptor, owner_ref, _defaulted_ref):
        owner_ref.value = descriptor.value.split("D:", 1)[0][len("O:") :]
        return 1

    security.advapi32 = MagicMock(
        ConvertStringSecurityDescriptorToSecurityDescriptorW=_convert,
        GetSecurityDescriptorOwner=_descriptor_owner,
        EqualSid=lambda left, right: left.value == right.value,
    )
    security.kernel32 = MagicMock(LocalFree=lambda *_args: None)
    return security


_USER_SID = "S-1-5-21-1111-2222-3333-1001"
_ADMINISTRATORS_SID = "S-1-5-32-544"
_STRANGER_SID = "S-1-5-21-9999-8888-7777-500"


def test_elevated_token_default_owner_is_accepted_as_self():
    """An elevated token stamps BUILTIN\\Administrators on what it creates."""

    security = _windows_security_stub(user_sid=_USER_SID, owner_sid=_ADMINISTRATORS_SID)

    assert security._owner_is_self(_FakeSid(_USER_SID)) is True
    assert security._owner_is_self(_FakeSid(_ADMINISTRATORS_SID)) is True
    assert security._owner_is_self(_FakeSid(_STRANGER_SID)) is False
    assert security._owner_is_self(_FakeSid(None)) is False


def test_administrators_is_not_trusted_when_it_is_not_this_token_owner():
    """Acceptance follows the process token, not a hardcoded well-known SID."""

    security = _windows_security_stub(user_sid=_USER_SID, owner_sid=_USER_SID)

    assert security._self_owner_sddls() == (security.sddl,)
    assert security._owner_is_self(_FakeSid(_USER_SID)) is True
    assert security._owner_is_self(_FakeSid(_ADMINISTRATORS_SID)) is False


def test_relaxed_owner_still_requires_the_protected_private_dacl():
    """Only the owner rule moved; the DACL contract is unchanged."""

    security = _windows_security_stub(user_sid=_USER_SID, owner_sid=_ADMINISTRATORS_SID)
    security._descriptor_dacl = lambda _descriptor: "private-dacl"
    security._acl_signature = lambda acl: (str(acl).encode(),)
    control = control_ipc._WindowsSecurity._SE_DACL_PRESENT | control_ipc._WindowsSecurity._SE_DACL_PROTECTED

    security._descriptor_control = lambda _descriptor: control
    security._validate_security_descriptor(
        _FakeSid(_ADMINISTRATORS_SID), "private-dacl", object(), Path("runtime")
    )

    for broken_control, dacl in (
        (control_ipc._WindowsSecurity._SE_DACL_PRESENT, "private-dacl"),
        (control, "inherited-dacl"),
    ):
        security._descriptor_control = lambda _descriptor, value=broken_control: value
        with pytest.raises(control_ipc.ControlIpcSecurityError, match="owner or DACL"):
            security._validate_security_descriptor(
                _FakeSid(_ADMINISTRATORS_SID), dacl, object(), Path("runtime")
            )
