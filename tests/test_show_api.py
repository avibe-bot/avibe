"""The opt-in boundary is exact, bounded and carries no browser authority."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.requests import Request

from config import paths
from core import show_api, show_runtime
from core.show_pages import ShowPageStore, ensure_show_page_dir
from core.show_runtime import ShowRuntimeContext, ShowRuntimeProtocolEnvelope
from storage.importer import ensure_sqlite_state
from tests.ui_server_test_helpers import _save_config
from vibe import ui_server


def create_ingress(tmp_path):
    _save_config(tmp_path)
    ensure_sqlite_state()
    store = ShowPageStore()
    try:
        page = store.update_visibility("sesingress", "public")
    finally:
        store.close()
    workspace = ensure_show_page_dir(page.session_id)
    (workspace / "api").mkdir(exist_ok=True)
    (workspace / "api/receive.ts").write_text("export function POST() { return Response.json({ok:true}) }\n")
    manifest = {
        "schema_version": 1,
        "server_to_server": [{
            "path": "api/receive", "method": "POST", "auth": "handler", "max_body_bytes": 1024,
            "forward_headers": ["x-hub-signature-256", "x-github-event", "x-github-delivery"],
        }],
    }
    (workspace / show_api.MANIFEST_NAME).write_text(json.dumps(manifest))
    return SimpleNamespace(page=page, workspace=workspace, manifest=manifest, path=f"/p/{page.share_id}/api/receive")


@pytest.fixture
def ingress(tmp_path):
    return create_ingress(tmp_path)


def resolve(ingress, **kwargs):
    return show_api.resolve_server_api(kwargs.get("path", ingress.path).encode(), kwargs.get("method", "POST"), kwargs.get("query", b""))


def test_registration_tracks_current_lifecycle_and_file(ingress):
    registration = resolve(ingress)
    assert registration.module == ingress.workspace / "api/receive.ts"
    store = ShowPageStore()
    try:
        for visibility in ("private", "offline", "public"):
            store.update_visibility(ingress.page.session_id, visibility)
            assert bool(resolve(ingress)) == (visibility == "public")
        store.rotate_share(ingress.page.session_id)
        assert resolve(ingress) is None
    finally:
        store.close()


@pytest.mark.parametrize("tail", ["api/receive/", "api//receive", "api/%72eceive", "api%2freceive", "api/%252freceive", "api/../api/receive", "api/receive.ts", "api/[name]", "api/Receive", "api/other"])
def test_only_canonical_route_acquires_admission(ingress, tail):
    assert resolve(ingress, path=f"/p/{ingress.page.share_id}/{tail}") is None


def test_only_declared_method_and_public_surface_acquire_admission(ingress):
    for method in ("GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"):
        assert resolve(ingress, method=method) is None
    assert resolve(ingress, query=b"target=api/receive") is None
    assert resolve(ingress, path="/show/sesingress/api/receive") is None


@pytest.mark.parametrize("change", [
    {"method": "GET"}, {"auth": "hmac"}, {"path": "api/*"}, {"max_body_bytes": True},
    {"max_body_bytes": show_api.MAX_BODY_BYTES + 1}, {"max_body_bytes": 0},
    {"forward_headers": ["Authorization"]}, {"forward_headers": ["X-Avibe-Show-Context"]},
    {"forward_headers": ["Cookie"]}, {"forward_headers": ["X-Api-Key"]},
    {"forward_headers": ["x-vibe-show-event-token"]}, {"forward_headers": ["X-Forwarded-Host"]},
    {"forward_headers": ["Connection"]}, {"forward_headers": ["x-event", "X-Event"]},
    {"forward_headers": ["x-" + "a" * 65]}, {"forward_headers": ["x-event"] * 17},
    {"extra": True},
])
def test_invalid_optional_declaration_disables_only_admission(ingress, change):
    ingress.manifest["server_to_server"][0].update(change)
    (ingress.workspace / show_api.MANIFEST_NAME).write_text(json.dumps(ingress.manifest))
    assert resolve(ingress) is None
    store = ShowPageStore()
    try:
        assert store.get(ingress.page.session_id).visibility == "public"
    finally:
        store.close()


@pytest.mark.parametrize("contents", ["{", "null", '{"schema_version":1,"schema_version":1,"server_to_server":[]}', " " * (show_api.MAX_MANIFEST_BYTES + 1)])
def test_manifest_read_is_bounded_and_unambiguous(ingress, contents):
    (ingress.workspace / show_api.MANIFEST_NAME).write_text(contents)
    assert resolve(ingress) is None


def test_all_declarations_must_be_valid(ingress):
    ingress.manifest["server_to_server"] *= show_api.MAX_ROUTES + 1
    (ingress.workspace / show_api.MANIFEST_NAME).write_text(json.dumps(ingress.manifest))
    assert resolve(ingress) is None


def test_fallback_and_external_symlink_cannot_acquire_admission(ingress, tmp_path):
    module = ingress.workspace / "api/receive.ts"
    module.rename(ingress.workspace / "api/index.ts")
    assert resolve(ingress) is None
    outside = tmp_path / "outside.ts"
    outside.write_text("export function POST() {}")
    module.symlink_to(outside)
    assert resolve(ingress) is None
    module.unlink()
    module.write_text("export function POST() {}")
    manifest = ingress.workspace / show_api.MANIFEST_NAME
    external_manifest = tmp_path / "outside.json"
    manifest.rename(external_manifest)
    manifest.symlink_to(external_manifest)
    assert resolve(ingress) is None


async def post(ingress, *, headers=None, content=b'{ "message": "hello" }', path=None, method="POST", host="alex.avibe.bot"):
    transport = httpx.ASGITransport(app=ui_server.app, client=("203.0.113.10", 3456))
    async with httpx.AsyncClient(transport=transport, base_url=f"https://{host}") as client:
        return await client.request(method, path or ingress.path, headers=headers, content=content)


def fake_manager(monkeypatch, response=None):
    manager = SimpleNamespace(request=AsyncMock(return_value=response or httpx.Response(202, content=b"private handler data")))
    monkeypatch.setattr(show_runtime, "get_show_runtime_manager", lambda: manager)
    return manager


async def test_public_ingress_preserves_bytes_and_shared_identity(ingress, monkeypatch):
    manager = fake_manager(monkeypatch)
    raw = '{  "中文": "签名 ☃", "spacing": [ 1,  2 ] }\n'.encode()
    headers = {
        "Content-Type": "application/json", "X-Hub-Signature-256": "sha256=fixture",
        "X-Github-Event": "push", "X-Github-Delivery": "fixture-id",
        "Cookie": "owner=fixture", "Authorization": "Bearer fixture", "Origin": "https://evil.test",
        "Referer": "https://evil.test", "X-Avibe-Show-Context": "private", "X-Avibe-Show-Protocol": "999",
        "X-Vibe-Show-Base": "/show/owner/", "X-Vibe-Show-Target": "/private",
        "X-Vibe-Show-Event-Token": "fixture", "X-Unregistered": "value",
    }
    response = await post(ingress, headers=headers, content=raw)
    assert response.status_code == 202
    assert response.json() == {"ok": True}
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    args, kwargs = manager.request.call_args
    assert args == ("POST", "/sessions/sesingress/app/api/receive")
    assert kwargs["body"] == raw
    assert kwargs["envelope"].context is ShowRuntimeContext.SHARED
    assert kwargs["headers"] == {
        "content-type": "application/json", "x-hub-signature-256": "sha256=fixture",
        "x-github-event": "push", "x-github-delivery": "fixture-id",
    }
    assert kwargs["start_if_needed"] is False
    assert kwargs["max_response_bytes"] == show_api.MAX_RESPONSE_BYTES


@pytest.mark.parametrize("headers,expected", [
    ({"content-length": "9999"}, 413), ({"content-length": "-1"}, 400),
    ({"content-encoding": "gzip"}, 415),
    ([("x-hub-signature-256", "a"), ("X-Hub-Signature-256", "b")], 400),
    ([("authorization", "a"), ("Authorization", "b")], 400),
    ({"x-github-event": "a" * 2049}, 400),
    ({"x-unregistered": "a" * 17000}, 431),
])
async def test_header_bounds_precede_body_read(ingress, monkeypatch, headers, expected):
    manager = fake_manager(monkeypatch)
    async def unreadable():
        raise AssertionError("body must not be read")
        yield b""
    response = await post(ingress, headers=headers, content=unreadable())
    assert response.status_code == expected
    manager.request.assert_not_called()


async def test_streamed_count_enforced_independently_of_length(ingress, monkeypatch):
    manager = fake_manager(monkeypatch)
    async def chunks():
        yield b"x" * 700
        yield b"x" * 700
        raise AssertionError("must stop reading at bound")
    for headers in ({}, {"content-length": "1"}):
        response = await post(ingress, headers=headers, content=chunks())
        assert response.status_code in {400, 413}  # HTTPX adds TE to a streamed body.
    manager.request.assert_not_called()
    registration = resolve(ingress)
    messages = iter([{"type": "http.request", "body": b"x" * 1025, "more_body": False}])
    async def receive():
        return next(messages)
    req = Request({"type": "http", "headers": [(b"content-length", b"1")]}, receive)
    assert show_api.server_api_headers(req, registration) == {}
    with pytest.raises(show_api.ServerAPIRequestError) as exc:
        await show_api.read_server_api_body(req, registration)
    assert exc.value.status_code == 413


async def test_body_and_whole_request_deadlines(ingress, monkeypatch):
    manager = fake_manager(monkeypatch)
    monkeypatch.setattr(show_api, "BODY_TIMEOUT_SECONDS", 0.02)
    async def stalled():
        await asyncio.sleep(1)
        yield b"x"
    assert (await post(ingress, content=stalled())).status_code == 408
    manager.request.assert_not_called()
    monkeypatch.setattr(show_api, "TOTAL_TIMEOUT_SECONDS", 0.02)
    manager.request.side_effect = lambda *a, **k: None
    async def slow(*args, **kwargs):
        await asyncio.sleep(1)
    manager.request.side_effect = slow
    assert (await post(ingress)).status_code == 504


async def test_revocation_during_upload_prevents_dispatch(ingress, monkeypatch):
    manager = fake_manager(monkeypatch)
    async def revoked():
        (ingress.workspace / show_api.MANIFEST_NAME).unlink()
        yield b"{}"
    assert (await post(ingress, content=revoked())).status_code == 404
    manager.request.assert_not_called()


async def test_unopted_browser_and_host_protections_remain(ingress, monkeypatch):
    manager = fake_manager(monkeypatch)
    for path in [ingress.path + "/", ingress.path + "?target=x", ingress.path.replace("receive", "other"), "/api/config"]:
        response = await post(ingress, path=path)
        assert response.status_code in {401, 403}
    assert (await post(ingress, host="other.test")).status_code == 503
    manager.request.assert_not_called()
    (ingress.workspace / show_api.MANIFEST_NAME).unlink()
    assert (await post(ingress)).status_code == 403
    # The existing browser route still validates JSON and then forwards normally.
    assert (await post(ingress, headers={"Origin": "https://alex.avibe.bot", "Content-Type": "application/json"}, content=b"{")).status_code == 400
    assert (await post(ingress, headers={"Origin": "https://alex.avibe.bot"})).status_code == 202
    assert "start_if_needed" not in manager.request.call_args.kwargs


async def test_private_metadata_file_is_not_public(ingress):
    response = await post(ingress, method="GET", content=b"", path=f"/p/{ingress.page.share_id}/.show-api.json")
    assert response.status_code == 404
    assert "server_to_server" not in response.text


async def test_no_start_is_request_scoped_and_does_not_probe_or_write(ingress, tmp_path, monkeypatch):
    manager = show_runtime.ShowRuntimeManager(runtime_dir=tmp_path / "never-created", auto_install=True)
    monkeypatch.setattr(show_runtime, "get_show_runtime_manager", lambda: manager)
    ensure = AsyncMock(side_effect=AssertionError("anonymous admission cannot start/install"))
    negotiate = AsyncMock(side_effect=AssertionError("unavailable runtime must not probe"))
    monkeypatch.setattr(manager, "ensure", ensure)
    monkeypatch.setattr(manager, "_negotiate_context_key_capability", negotiate)
    response = await post(ingress)
    assert response.status_code == 503
    ensure.assert_not_called()
    negotiate.assert_not_called()
    assert not manager.runtime_dir.exists()
    # A subsequent authorized/default caller still invokes the normal lifecycle.
    with pytest.raises(AssertionError, match="cannot start"):
        await manager.request("GET", "/sessions/sesingress/app/", envelope=ShowRuntimeProtocolEnvelope(ShowRuntimeContext.PRIVATE))
    ensure.assert_awaited_once_with(automatic=True)
    assert paths.get_vibe_remote_dir().is_relative_to(tmp_path)
    assert Path.home().is_relative_to(tmp_path)


class RecordingStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("headers,chunks,expected_reads", [
    ({}, [b"123", b"456", b"never"], 2), ({"content-length": "6"}, [b"never"], 0),
    ({"content-encoding": "gzip"}, [b"compressed-bomb"], 0),
])
async def test_response_limit_closes_stream_without_invalidating_runtime(tmp_path, monkeypatch, headers, chunks, expected_reads):
    manager = show_runtime.ShowRuntimeManager(runtime_dir=tmp_path)
    manager._base_url = "http://127.0.0.1:55555"
    monkeypatch.setattr(manager, "_negotiate_context_key_capability", AsyncMock())
    stream = RecordingStream(chunks)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, stream=stream))
    original_client = httpx.AsyncClient
    monkeypatch.setattr(show_runtime.httpx, "AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs))
    with pytest.raises(show_runtime.ShowRuntimeResponseLimitError):
        await manager.request("POST", "/sessions/sesingress/app/api/receive", envelope=ShowRuntimeProtocolEnvelope(ShowRuntimeContext.SHARED), start_if_needed=False, max_response_bytes=5)
    assert stream.closed and stream.reads == expected_reads
    assert manager._base_url == "http://127.0.0.1:55555"


async def test_bounded_response_materializes_consistent_headers(tmp_path, monkeypatch):
    manager = show_runtime.ShowRuntimeManager(runtime_dir=tmp_path)
    manager._base_url = "http://127.0.0.1:55555"
    monkeypatch.setattr(manager, "_negotiate_context_key_capability", AsyncMock())
    stream = RecordingStream([b"hello"])
    transport = httpx.MockTransport(lambda request: httpx.Response(503, headers={"transfer-encoding": "chunked"}, stream=stream))
    original_client = httpx.AsyncClient
    monkeypatch.setattr(show_runtime.httpx, "AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs))
    response = await manager.request("POST", "/sessions/sesingress/app/api/receive", envelope=ShowRuntimeProtocolEnvelope(ShowRuntimeContext.SHARED), start_if_needed=False, max_response_bytes=5)
    assert response.content == b"hello"
    assert response.headers["content-length"] == "5"
    assert "transfer-encoding" not in response.headers
    assert stream.closed and manager._base_url


async def test_handler_auth_sees_even_non_json_original_bytes(ingress, monkeypatch):
    manager = fake_manager(monkeypatch, httpx.Response(401, content=b"fixture secret/error"))
    response = await post(ingress, content=b"{ malformed \xff", headers={"Content-Type": "application/json"})
    assert response.status_code == 401
    assert manager.request.call_args.kwargs["body"] == b"{ malformed \xff"
    assert response.json() == {"error": "show_server_api_rejected"}
    assert "fixture" not in response.text


async def test_hop_by_hop_metadata_is_not_forwarded(ingress, monkeypatch):
    manager = fake_manager(monkeypatch)
    response = await post(ingress, headers={"Connection": "X-Github-Event", "X-Github-Event": "push"})
    assert response.status_code == 400
    manager.request.assert_not_called()


async def test_total_deadline_covers_runtime_capability_lock(ingress, monkeypatch, tmp_path):
    manager = show_runtime.ShowRuntimeManager(runtime_dir=tmp_path / "runtime")
    manager._base_url = "http://127.0.0.1:55555"
    monkeypatch.setattr(show_runtime, "get_show_runtime_manager", lambda: manager)
    monkeypatch.setattr(show_api, "TOTAL_TIMEOUT_SECONDS", 0.03)
    transport = AsyncMock(side_effect=AssertionError("lock has not admitted transport"))
    monkeypatch.setattr(manager, "_request_runtime_transport", transport)
    async with manager._capability_lock:
        response = await post(ingress)
    assert response.status_code == 504
    assert response.headers["cache-control"] == "no-store"
    assert manager._base_url == "http://127.0.0.1:55555"
    transport.assert_not_called()


@pytest.mark.parametrize("status", [202, 401, 500, 302])
async def test_handler_responses_are_secret_free_and_never_redirect(ingress, monkeypatch, status):
    fake_manager(monkeypatch, httpx.Response(status, content=b"secret payload", headers={"location": "https://login.test", "set-cookie": "secret=value"}))
    response = await post(ingress)
    assert response.status_code == (502 if status == 302 else status)
    assert response.json() == ({"ok": True} if status == 202 else {"error": "show_server_api_rejected"})
    assert "location" not in response.headers and "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("failure_site", ["init", "lookup", "close"])
async def test_admission_datastore_failures_never_escape_public_boundary(ingress, monkeypatch, failure_site):
    manager = fake_manager(monkeypatch)
    closed = []

    class FailedStore:
        def __init__(self, *, read_only):
            assert read_only
            if failure_site == "init":
                raise OSError("fixture private constructor database path and credential")

        def get_by_share_id(self, share_id):
            if failure_site == "lookup":
                raise OSError("fixture private lookup database path and credential")
            return ingress.page

        def close(self):
            closed.append(True)
            if failure_site == "close":
                raise OSError("fixture private close database path and credential")

    monkeypatch.setattr(show_api, "ShowPageStore", FailedStore)
    response = await post(ingress)
    assert response.status_code == 503
    assert response.json() == {"error": "show_server_api_rejected"}
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert len(closed) == (0 if failure_site == "init" else 1)
    manager.request.assert_not_called()


@pytest.mark.parametrize("failure_site", ["resolver", "earlier_hook"])
async def test_total_deadline_interrupts_initial_admission(ingress, monkeypatch, failure_site):
    import threading
    import time

    manager = fake_manager(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original = show_api.resolve_server_api

    def stalled(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(2), "test failed to release storage worker"
            return original(*args, **kwargs) if failure_site == "resolver" else None
        finally:
            finished.set()

    if failure_site == "resolver":
        monkeypatch.setattr(show_api, "resolve_server_api", stalled)
    else:
        monkeypatch.setattr(ui_server.app, "_before_request_handlers", [stalled, *ui_server.app._before_request_handlers])
    monkeypatch.setattr(show_api, "TOTAL_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    try:
        response = await post(ingress)
        assert entered.is_set()
        assert response.status_code == 504
        assert response.headers["cache-control"] == "no-store"
        assert "set-cookie" not in response.headers
        assert time.monotonic() - started < 0.75
        manager.request.assert_not_called()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
    await asyncio.sleep(0)
    manager.request.assert_not_called()


async def test_earlier_admission_error_has_generic_receipt(ingress, monkeypatch):
    def failed_hook():
        raise RuntimeError("fixture private diagnostic")
    monkeypatch.setattr(ui_server.app, "_before_request_handlers", [failed_hook, *ui_server.app._before_request_handlers])
    response = await post(ingress)
    assert response.status_code == 503
    assert response.json() == {"error": "show_server_api_rejected"}
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


async def test_unregistered_browser_keeps_its_existing_handler_deadline(ingress, monkeypatch):
    (ingress.workspace / show_api.MANIFEST_NAME).unlink()
    monkeypatch.setattr(show_api, "TOTAL_TIMEOUT_SECONDS", 0.05)
    manager = fake_manager(monkeypatch)
    async def browser_handler(*args, **kwargs):
        await asyncio.sleep(0.1)
        return httpx.Response(200, content=b"browser result")
    manager.request.side_effect = browser_handler
    response = await post(ingress, headers={"Origin": "https://alex.avibe.bot"})
    assert response.status_code == 200 and response.content == b"browser result"


@pytest.mark.parametrize("status", [503, 504])
def test_admission_receipt_does_not_require_dispatch_context(status):
    response = ui_server._show_server_api_result(status)
    assert response.status_code == status
    assert json.loads(response.body) == {"error": "show_server_api_rejected"}
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


def test_read_only_store_reuses_wal_rows_without_migration_lock(ingress, monkeypatch, hold_migration_lock_elsewhere):
    from core import show_pages
    from storage import migrations
    from storage.lock import migration_lock_path_for

    def forbidden(*args, **kwargs):
        raise AssertionError("read-only admission cannot initialize or migrate")

    monkeypatch.setattr(show_pages, "ensure_sqlite_state", forbidden)
    monkeypatch.setattr(migrations, "run_migrations", forbidden)
    with hold_migration_lock_elsewhere(migration_lock_path_for(paths.get_sqlite_state_path())):
        assert resolve(ingress).session_id == ingress.page.session_id
        # Explicit paths must also skip the separate run_migrations branch.
        store = ShowPageStore(paths.get_sqlite_state_path(), read_only=True)
        try:
            assert store.get_by_share_id(ingress.page.share_id) == ingress.page
            with store.engine.connect() as connection:
                assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
                assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
                assert store.engine.hide_parameters
                with pytest.raises(Exception, match="readonly"):
                    connection.exec_driver_sql("CREATE TABLE forbidden_write (id integer)")
        finally:
            store.close()


@pytest.mark.parametrize("kind", ["missing", "missing-parent", "directory", "schema", "corrupt"])
async def test_unavailable_read_only_store_returns_controlled_receipt(ingress, monkeypatch, tmp_path, kind):
    import sqlite3

    db_path = tmp_path / "absent-parent" / "state.sqlite" if kind == "missing-parent" else tmp_path / "isolated.sqlite"
    if kind == "directory":
        db_path.mkdir()
    elif kind == "schema":
        with sqlite3.connect(db_path) as db:
            db.execute("CREATE TABLE unrelated (id integer)")
    elif kind == "corrupt":
        db_path.write_bytes(b"fixture invalid database")
    real_store = ShowPageStore
    monkeypatch.setattr(show_api, "ShowPageStore", lambda *, read_only: real_store(db_path, read_only=read_only))
    manager = fake_manager(monkeypatch)
    response = await post(ingress)
    assert response.status_code == 503
    assert response.json() == {"error": "show_server_api_rejected"}
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    manager.request.assert_not_called()
    if kind in {"missing", "missing-parent"}:
        assert not db_path.exists()
    if kind == "missing-parent":
        assert not db_path.parent.exists()


def test_read_only_factory_never_recreates_deleted_database(tmp_path):
    import sqlite3
    from storage.db import create_sqlite_engine

    db_path = tmp_path / "deleted.sqlite"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE marker (id integer)")
    engine = create_sqlite_engine(db_path, read_only=True)
    db_path.unlink()  # after factory construction, before actual SQLite connect
    try:
        with pytest.raises(Exception, match="unable to open"):
            with engine.connect():
                pytest.fail("read-only open recreated deleted state")
        assert not db_path.exists()
    finally:
        engine.dispose()


def test_read_only_uri_preserves_encoded_path_and_journal_mode(tmp_path):
    import sqlite3
    from storage.db import create_sqlite_engine

    db_path = tmp_path / "中文 space ? mode=rw#%.sqlite"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE marker (value text)")
        db.execute("INSERT INTO marker VALUES ('exact path')")
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    engine = create_sqlite_engine(db_path, read_only=True)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT value FROM marker").scalar() == "exact path"
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "delete"
    finally:
        engine.dispose()
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_default_store_still_initializes_and_writes(tmp_path):
    db_path = tmp_path / "ordinary" / "state.sqlite"
    assert not db_path.exists()
    store = ShowPageStore(db_path)
    try:
        page = store.ensure("sesdefault")
        assert store.get(page.session_id) == page
        assert db_path.is_file()
    finally:
        store.close()
