import ast
import asyncio
import base64
import errno
import gzip
import hashlib
import inspect
import io
import json
import os
import re
import shlex
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tarfile
import textwrap
import threading
import urllib.error
import urllib.parse
import zlib
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import core.dependency_network as dependency_network
import core.show_runtime as show_runtime
import core.show_runtime_failures as show_runtime_failures_module
from aiohttp import ClientConnectionError, WSServerHandshakeError
from starlette.websockets import WebSocketDisconnect

from config import paths
from core import show_pages as show_pages_module
from core.show_pages import (
    SHOW_ACCESS_ENTRY_VALUE_MAX_LENGTH,
    SHOW_ACCESS_VISITOR_GROUP_MAX_COUNT,
    SHOW_PAGE_RUNTIME_RECOVERY_LOADING_DELAY_SECONDS,
    VISIBILITIES,
    ShowPageError,
    ShowPageStore,
    ensure_show_page_dir,
    show_cli_event_token,
    show_event_write_token,
    show_public_event_write_token,
)
from core.show_runtime import (
    SHOW_RUNTIME_BASE_HEADER,
    SHOW_RUNTIME_CLI_FALLBACK_DELAY_SECONDS,
    SHOW_RUNTIME_TARGET_HEADER,
    ShowRuntimeContext,
    ShowRuntimeManager,
    ShowRuntimeProtocolEnvelope,
    ShowRuntimeRequestTimeoutError,
    ShowRuntimeServingState,
    ShowRuntimeStartability,
    ShowRuntimeUnavailableError,
    ShowRuntimeWebSocketTarget,
    _runtime_download_error,
    set_show_runtime_manager_for_tests,
)
from core.managed_runtime import runtime_platform_tag, safe_extract_tar
from core.show_runtime_failures import (
    SHOW_RUNTIME_FAILURE_DECLARATIONS,
    ShowRuntimeFailureClass,
    ShowRuntimeFailureDimension,
    ShowRuntimeFailureEvidence,
    ShowRuntimeRecoveryAction,
    classify_show_runtime_failure,
    show_runtime_recovery_action,
)
from storage import resource_access_service
from tests.ui_server_test_helpers import _mock_interface, _remote_peer, _save_config
from tests.ui_server_test_helpers import csrf_headers, remote_session_cookie
from vibe import remote_access, show_identity, ui_server
from vibe.ui_compat import TEST_REMOTE_ADDR_HEADER
from vibe.ui_server import app
from storage import message_deliveries


def _active_org_cookie(config, email="member@example.com", subject="member-1", *, role="editor"):
    return remote_session_cookie(
        config,
        email,
        subject,
        role=role,
        access_source="organization_group",
        organization_id="org-1",
        organization_member_id=f"membership-{subject}",
        organization_role="member",
        group_ids=[],
    )


class _FakeShowRuntimeManager:
    def __init__(
        self,
        *,
        body: bytes = b"Runtime Show Page",
        fail: bool = False,
        error: Exception | None = None,
        failure_reason: str = "runtime_proxy_failed",
        status_code: int = 200,
        extra_headers: dict[str, str] | None = None,
        headers_by_path: dict[str, dict[str, str]] | None = None,
        bodies_by_path: dict[str, bytes] | None = None,
        status_by_path: dict[str, int] | None = None,
        render_markdown_supported: bool = False,
    ):
        self.body = body
        self.fail = fail
        self.error = error
        self.failure_reason = failure_reason
        self.status_code = status_code
        self.extra_headers = extra_headers or {}
        self.headers_by_path = headers_by_path or {}
        self.bodies_by_path = bodies_by_path or {}
        self.status_by_path = status_by_path or {}
        self.render_markdown_supported = render_markdown_supported
        self.render_markdown_capability_calls = 0
        self.calls = []
        self.automatic_calls = []
        self.websocket_paths = []
        self.websocket_headers = []
        self.stopped = False

    def _unavailable_error(self):
        declaration = SHOW_RUNTIME_FAILURE_DECLARATIONS.get((self.failure_reason, None, None))
        evidence = ShowRuntimeFailureEvidence(
            declaration.dimension if declaration else ShowRuntimeFailureDimension.RUNTIME,
            self.failure_reason,
        )
        failure_class = classify_show_runtime_failure(evidence)
        return ShowRuntimeUnavailableError(
            self.failure_reason,
            failure_class,
            show_runtime_recovery_action(evidence),
        )

    async def request(
        self,
        method,
        path,
        *,
        envelope,
        headers=None,
        body=None,
        base_path=None,
        render_target=None,
        timeout_seconds=None,
        automatic=True,
    ):
        import httpx

        request_headers = {
            key: value
            for key, value in envelope.headers(headers).items()
            if key.lower()
            not in {
                SHOW_RUNTIME_BASE_HEADER.lower(),
                SHOW_RUNTIME_TARGET_HEADER.lower(),
            }
        }
        if base_path is not None:
            request_headers[SHOW_RUNTIME_BASE_HEADER] = base_path
        if render_target is not None:
            request_headers[SHOW_RUNTIME_TARGET_HEADER] = render_target
        self.calls.append((method, path, request_headers, body, timeout_seconds))
        self.automatic_calls.append(automatic)
        if self.error is not None:
            raise self.error
        if self.fail:
            raise self._unavailable_error()
        headers = (
            {
                "content-type": "text/html; charset=utf-8",
                "set-cookie": "__Host-vibe_remote_session=attacker",
                "x-runtime-private-header": "secret",
            }
            | self.extra_headers
            | self.headers_by_path.get(path, {})
        )
        return httpx.Response(
            self.status_by_path.get(path, self.status_code),
            content=self.bodies_by_path.get(path, self.body),
            headers=headers,
        )

    async def supports_render_markdown(self, *, automatic=True):
        self.render_markdown_capability_calls += 1
        if self.fail:
            raise RuntimeError("runtime unavailable")
        return self.render_markdown_supported

    async def request_global(self, method, path, *, headers=None, body=None):
        import httpx

        self.calls.append((method, path, headers or {}, body))
        if self.fail:
            raise self._unavailable_error()
        response_headers = (
            {
                "content-type": "text/html; charset=utf-8",
                "set-cookie": "__Host-vibe_remote_session=attacker",
                "x-runtime-private-header": "secret",
            }
            | self.extra_headers
            | self.headers_by_path.get(path, {})
        )
        return httpx.Response(
            self.status_by_path.get(path, self.status_code),
            content=self.bodies_by_path.get(path, self.body),
            headers=response_headers,
        )

    async def websocket_target(self, path, *, envelope):
        self.websocket_paths.append(path)
        headers = envelope.headers()
        self.websocket_headers.append(headers)
        return ShowRuntimeWebSocketTarget(url=f"ws://127.0.0.1:1{path}", headers=headers)

    def stop(self):
        self.stopped = True


def _runtime_manager_with_failing_transport(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        command=str(tmp_path / "configured-runtime"),
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )

    class FakeProcess:
        pid = 321

        def poll(self):
            return None

    manager._base_url = "http://127.0.0.1:49321"
    manager._process = FakeProcess()
    manager._availability = manager._publish_runtime_availability(
        ShowRuntimeServingState.SERVING,
        manager._base_url,
    )
    monkeypatch.setattr(manager, "_healthy", lambda _base_url: asyncio.sleep(0, result=True))
    monkeypatch.setattr(
        manager,
        "_negotiate_context_key_capability",
        lambda _base_url: asyncio.sleep(0),
    )

    def stop():
        manager._process = None
        manager._base_url = None

    async def fail_request(_client, method, url, **_kwargs):
        raise httpx.ConnectError(
            "Show Runtime transport unavailable",
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(manager, "stop", stop)
    monkeypatch.setattr("core.show_runtime.httpx.AsyncClient.request", fail_request)
    return manager


@pytest.fixture(autouse=True)
def _hermetic_show_runtime(monkeypatch):
    monkeypatch.setattr("core.show_runtime._node_version", lambda node: (22, 16, 0))
    set_show_runtime_manager_for_tests(_FakeShowRuntimeManager(fail=True))
    yield
    set_show_runtime_manager_for_tests(None)


def test_set_show_runtime_manager_stops_previous_manager():
    # Swapping the global manager must stop the one it replaces so a real
    # Node/esbuild subprocess tree can never be orphaned (and then leak past the
    # atexit cleanup) when a later test installs a fake or resets the global.
    import core.show_runtime as srt

    first = _FakeShowRuntimeManager()
    second = _FakeShowRuntimeManager()
    srt.set_show_runtime_manager_for_tests(first)
    try:
        srt.set_show_runtime_manager_for_tests(second)
        assert first.stopped is True
        assert second.stopped is False
    finally:
        srt.set_show_runtime_manager_for_tests(None)
    # Resetting to None also stops the manager being dropped.
    assert second.stopped is True


def _create_show_page(session_id: str, visibility: str) -> str | None:
    page_dir = ensure_show_page_dir(session_id)
    (page_dir / "index.html").write_text("<!doctype html><title>Show</title><h1>Show Page</h1>", encoding="utf-8")
    (page_dir / "app.js").write_text("window.showPage = true;", encoding="utf-8")
    store = ShowPageStore()
    try:
        if visibility == "limited":
            page = store.ensure(session_id)
            result = store.apply_access(
                session_id,
                expected_revision=page.access_revision,
                target_access_mode="limited",
                target_share_id=page.share_id,
                target_emails=["viewer@example.com"],
            )
            assert result.status == "applied"
            page = store.get(session_id)
            assert page is not None
        else:
            page = store.update_visibility(session_id, visibility)
        # §3.2 removed show_page from the Resource ACL: there is no page policy
        # to seed. Runtime access follows the caller's Instance role alone, and
        # `/p` admission follows the sharing axis set above.
        return page.share_id
    finally:
        store.close()


def _screenshot_png(width: int, height: int) -> tuple[bytes, str]:
    def chunk(kind: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    scanlines = (b"\x00" + b"\x00\x00\x00" * width) * height
    raw = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )
    return raw, f"data:image/png;base64,{base64.b64encode(raw).decode('ascii')}"


def _create_show_page_record(session_id: str, visibility: str) -> str | None:
    store = ShowPageStore()
    try:
        page = store.update_visibility(session_id, visibility)
        return page.share_id
    finally:
        store.close()


def _create_agent_session(session_id: str, *, status: str = "active") -> None:
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions
    from storage.settings_service import upsert_scope

    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = messages_service._utc_now_iso()
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_show", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id=session_id,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="anchor_" + session_id,
                native_session_id="",
                status=status,
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )


def _accept_dispatch(payload: dict, message_type: str = "harness") -> None:
    from storage import message_deliveries
    from storage.db import create_sqlite_engine

    with create_sqlite_engine().begin() as conn:
        delivery = message_deliveries.get_delivery(conn, payload["user_message_id"])
        assert delivery is not None
        if message_type == "queued":
            assert message_deliveries.cas_delivery(
                conn,
                delivery["id"],
                expected_version=delivery["version"],
                expected_states=("reserved",),
                values={"state": "queued"},
            )
            return
        turn_id = message_deliveries.new_turn_id()
        message_deliveries.insert_turn(
            conn,
            turn_id=turn_id,
            session_id=delivery["session_id"],
            initial_delivery_id=delivery["id"],
            state="starting",
            backend="codex",
        )
        attempt_id = message_deliveries.new_attempt_id()
        assert message_deliveries.open_start_attempt(
            conn,
            delivery["id"],
            expected_version=delivery["version"],
            turn_id=turn_id,
            attempt_id=attempt_id,
        )
        turn = message_deliveries.get_turn(conn, turn_id)
        assert turn is not None
        assert message_deliveries.bind_native_start(
            conn,
            turn_id,
            expected_version=int(turn["version"]),
            runtime_key=f"runtime:{turn_id}",
            runtime_turn_id=f"runtime-turn:{turn_id}",
            native_turn_id=f"native:{turn_id}",
        ) is not None
        assert message_deliveries.materialize_start_acceptance(
            conn,
            turn_id=turn_id,
            evidence={"kind": "test_native_acceptance"},
        )


def _write_runtime_archive(tmp_path: Path, *, text: str = "#!/usr/bin/env node\n") -> Path:
    archive_root = tmp_path / f"archive-root-{hashlib.sha256(text.encode()).hexdigest()[:8]}"
    cli_path = archive_root / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text(text, encoding="utf-8")
    archive_path = tmp_path / f"vibe-show-runtime-node-{hashlib.sha256(text.encode()).hexdigest()[:8]}.tgz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(archive_root / "node_modules", arcname="node_modules")
    return archive_path


def _write_runtime_archive_with_entrypoints(
    tmp_path: Path,
    entrypoints: tuple[str, ...],
) -> Path:
    archive_path = tmp_path / "vibe-show-runtime-entrypoints.tgz"
    payload = b"#!/usr/bin/env node\n"
    with tarfile.open(archive_path, "w:gz") as archive:
        for entrypoint in entrypoints:
            member = tarfile.TarInfo(entrypoint)
            member.mode = 0o755
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return archive_path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_runtime_manifest(
    tmp_path: Path,
    archive_path: Path,
    *,
    sha256: str | None = None,
    size: int | None = None,
    url: str | None = None,
    runtime_version: str = "runtime-test-ref",
) -> Path:
    manifest_path = tmp_path / "show_runtime_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_version": runtime_version,
                "minimum_node": "^20.19.0 || >=22.12.0",
                "archives": {
                    runtime_platform_tag(): {
                        "name": archive_path.name,
                        "url": url or archive_path.resolve().as_uri(),
                        "sha256": sha256 or _sha256(archive_path),
                        "size": archive_path.stat().st_size if size is None else size,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def _install_remote_manifest_runtime(monkeypatch, tmp_path: Path):
    archive_path = _write_runtime_archive(tmp_path)
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    manifest_url = "https://example.test/show-runtime-manifest.json"
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["test-node"] if command == "node" else None,
    )
    monkeypatch.setattr("core.show_runtime._node_version", lambda _node: (22, 12, 0))
    monkeypatch.setattr(
        "core.managed_runtime.fetch_bytes",
        lambda url, **_kwargs: manifest_path.read_bytes()
        if url == manifest_url
        else pytest.fail(f"unexpected fetch: {url}"),
    )
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_url=manifest_url,
    )
    result = manager.prepare(automatic=True)
    assert result["install"]["state"] == "installed"
    install_dir = Path(result["command"][1]).parents[4]
    return runtime_dir, manifest_url, install_dir, result["command"]


def _write_cached_runtime_install(
    runtime_dir: Path,
    name: str,
    *,
    manifest_source: str = "package:show_runtime_manifest.json",
    mtime: float,
) -> tuple[Path, Path]:
    install_dir = runtime_dir / "versions" / name / runtime_platform_tag() / f"fingerprint-{name}"
    return _write_cached_runtime_install_at(install_dir, name, manifest_source=manifest_source, mtime=mtime)


def _write_cached_runtime_install_at(
    install_dir: Path,
    name: str,
    *,
    manifest_source: str = "package:show_runtime_manifest.json",
    mtime: float,
) -> tuple[Path, Path]:
    manifest_sha256 = hashlib.sha256(f"manifest:{name}".encode()).hexdigest()
    archive_sha256 = hashlib.sha256(f"archive:{name}".encode()).hexdigest()
    cli_path = install_dir / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text(f"{name}\n", encoding="utf-8")
    (install_dir / ".vibe-show-runtime.json").write_text(
        json.dumps(
            {
                "provider": "manifest-cache",
                "manifest_source": manifest_source,
                "manifest_sha256": manifest_sha256,
                "runtime_version": name,
                "platform": runtime_platform_tag(),
                "archive_name": f"{archive_sha256}.tgz",
                "archive_sha256": archive_sha256,
            }
        ),
        encoding="utf-8",
    )
    os.utime(install_dir, (mtime, mtime))
    return install_dir, cli_path


def _write_cached_runtime_pointer(runtime_dir: Path, install_dir: Path) -> None:
    metadata = json.loads(
        (install_dir / ".vibe-show-runtime.json").read_text(encoding="utf-8")
    )
    (runtime_dir / "current.json").write_text(
        json.dumps(
            {
                "provider": "manifest-cache",
                "runtime_version": metadata["runtime_version"],
                "platform": metadata["platform"],
                "install_dir": str(install_dir),
                "manifest_sha256": metadata["manifest_sha256"],
                "archive_sha256": metadata["archive_sha256"],
            }
        ),
        encoding="utf-8",
    )


def _complete_mock_manifest_install(
    manager: ShowRuntimeManager,
    command: list[str],
    *,
    offline: bool,
) -> list[str]:
    manager._shared_manifest_manager(offline=offline)._clean_after_successful_install()
    return command


def test_private_show_page_requires_remote_login(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")

    response = app.test_client().get(
        "/show/ses123/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/auth/login?next=%2Fshow%2Fses123%2F"


def test_retry_now_html_request_preserves_remote_login_redirect(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")

    response = app.test_client().get(
        "/show/ses123/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={
            "Accept": "text/html",
            "X-Avibe-Show-Recovery-Retry": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/auth/login?next=%2Fshow%2Fses123%2F"


def test_private_show_page_serves_locally(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")

    response = app.test_client().get("/show/ses123/", base_url="http://127.0.0.1:5123")

    assert response.status_code == 200
    assert b"Show Page" in response.content


def test_public_show_page_serves_from_authed_route(monkeypatch, tmp_path):
    # Spec amendment (§2.3, 2026-07-13): the authed /show/ surface serves public
    # pages too, so a Show Page pinned to the Dock while public still opens.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "public")

    response = app.test_client().get("/show/ses123/", base_url="http://127.0.0.1:5123")

    assert response.status_code == 200
    assert b"Show Page" in response.content


def _configure_show_identity(config):
    cloud = config.remote_access.vibe_cloud
    cloud.backend_url = "https://backend.test"
    cloud.issuer = "https://backend.test"
    cloud.jwks_uri = "https://backend.test/oauth/jwks.json"
    config.save()


def test_limited_show_page_uses_editor_route_and_redirects_guest_to_identity(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _configure_show_identity(config)
    share_id = _create_show_page("ses123", "limited")

    editor = app.test_client().get(
        "/show/ses123/",
        base_url="http://127.0.0.1:5123",
    )
    shared = app.test_client().get(
        f"/p/{share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )

    assert editor.status_code == 200
    assert b"Show Page" in editor.content
    assert shared.status_code == 302
    authorization_url = urllib.parse.urlsplit(shared.headers["Location"])
    assert authorization_url.path == (
        "/api/v1/instances/inst_123/show-identity/authorize"
    )
    query = urllib.parse.parse_qs(authorization_url.query)
    assert query["redirect_uri"] == [
        "https://alex.avibe.bot/auth/show-identity/callback"
    ]
    state = show_identity.read_show_identity_state(
        config,
        query["state"][0],
        callback_origin="https://alex.avibe.bot",
    )
    assert state.share_id == share_id
    assert state.return_target == f"/p/{share_id}/"
    assert query["nonce"] == [state.nonce]

    editor_client = app.test_client()
    editor_client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "owner-1"),
        domain="alex.avibe.bot",
    )
    original_resolve_session = ui_server._resolved_remote_session_payload
    editor_resolution_was_offloaded: list[bool] = []

    def resolve_session_off_loop(config):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            editor_resolution_was_offloaded.append(True)
        else:
            editor_resolution_was_offloaded.append(False)
        return original_resolve_session(config)

    monkeypatch.setattr(
        ui_server,
        "_resolved_remote_session_payload",
        resolve_session_off_loop,
    )
    editor_shared = editor_client.get(
        f"/p/{share_id}/reports/daily?tab=1",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert editor_shared.status_code == 302
    assert editor_shared.headers["Location"] == "/show/ses123/reports/daily?tab=1"
    assert editor_resolution_was_offloaded
    assert all(editor_resolution_was_offloaded)

    editor_client.set_cookie(
        show_identity.show_guest_cookie_name(share_id),
        show_identity.make_show_guest_lease(
            config,
            page_id="ses123",
            share_id=share_id,
            normalized_email="owner@example.com",
        ),
        domain="alex.avibe.bot",
        path=show_identity.show_guest_cookie_path(share_id),
    )
    upgraded_editor = editor_client.get(
        f"/p/{share_id}/reports/daily?tab=1",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert upgraded_editor.status_code == 302
    assert upgraded_editor.headers["Location"] == "/show/ses123/reports/daily?tab=1"
    assert all(editor_resolution_was_offloaded)

    store = ShowPageStore()
    try:
        access = store.get_access("ses123")
        assert access is not None
        private = store.apply_access(
            "ses123",
            expected_revision=access.revision,
            target_access_mode="private",
            target_share_id=share_id,
            target_emails=[],
        )
        assert private.status == "applied"
    finally:
        store.close()
    private_editor = editor_client.get(
        f"/p/{share_id}/reports/daily?tab=1",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert private_editor.status_code == 302
    assert private_editor.headers["Location"] == "/show/ses123/reports/daily?tab=1"

    asset = app.test_client().get(
        f"/p/{share_id}/app.js",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert asset.status_code == 404


def test_limited_show_page_shows_access_denied_to_authenticated_viewer(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _configure_show_identity(config)
    share_id = _create_show_page("ses123", "limited")
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "other@example.com",
            "viewer-1",
            role="viewer",
        ),
        domain="alex.avibe.bot",
    )

    def fail_oauth(*_args, **_kwargs):
        pytest.fail("an authenticated viewer must not be sent through OAuth")

    with monkeypatch.context() as denied_patch:
        denied_patch.setattr(show_identity, "begin_show_identity_authorization", fail_oauth)
        html_response = client.get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        json_response = client.get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "application/json", "Sec-Fetch-Dest": "empty"},
            follow_redirects=False,
        )

    assert html_response.status_code == 403
    assert "Location" not in html_response.headers
    assert "You do not have access to this page" in html_response.text
    assert "Please contact the page owner" in html_response.text
    assert html_response.headers["Cache-Control"] == "private, no-store"
    assert "Cookie" in html_response.headers["Vary"]
    assert json_response.status_code == 403
    assert json_response.get_json() == {"error": "show_access_forbidden"}

    allowlisted_client = app.test_client()
    allowlisted_client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "viewer@example.com",
            "viewer-1",
            role="viewer",
        ),
        domain="alex.avibe.bot",
    )
    allowlisted = allowlisted_client.get(
        f"/p/{share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert allowlisted.status_code == 302
    assert (
        urllib.parse.urlsplit(allowlisted.headers["Location"]).path
        == "/api/v1/instances/inst_123/show-identity/authorize"
    )

    page_scoped_client = app.test_client()
    page_scoped_client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "other@example.com",
            "other-viewer",
            role="viewer",
        ),
        domain="alex.avibe.bot",
    )
    page_scoped = page_scoped_client.get(
        f"/p/{share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert page_scoped.status_code == 403


def test_limited_show_callback_maps_outages_and_rechecks_share_binding(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _configure_show_identity(config)
    share_id = _create_show_page("ses123", "limited")
    client = app.test_client()

    login = client.get(
        f"/p/{share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    state = urllib.parse.parse_qs(
        urllib.parse.urlsplit(login.headers["Location"]).query
    )["state"][0]
    unavailable = client.post(
        show_identity.CALLBACK_PATH,
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        data={"state": state, "error": "identity_unavailable"},
    )
    assert unavailable.status_code == 503
    assert unavailable.get_json()["error"] == "identity_unavailable"

    not_verified_login = client.get(
        f"/p/{share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    not_verified_state = urllib.parse.parse_qs(
        urllib.parse.urlsplit(not_verified_login.headers["Location"]).query
    )["state"][0]
    not_verified = client.post(
        show_identity.CALLBACK_PATH,
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        data={"state": not_verified_state, "error": "identity_not_verified"},
    )
    assert not_verified.status_code == 404
    assert not_verified.get_json() == {"error": "not_found"}

    verifier_login = client.get(
        f"/p/{share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    verifier_state = urllib.parse.parse_qs(
        urllib.parse.urlsplit(verifier_login.headers["Location"]).query
    )["state"][0]

    def unavailable_verifier(*_args, **_kwargs):
        raise show_identity.ShowIdentityError("identity_unavailable")

    monkeypatch.setattr(
        show_identity,
        "verify_show_identity_assertion",
        unavailable_verifier,
    )
    verifier_unavailable = client.post(
        show_identity.CALLBACK_PATH,
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        data={"state": verifier_state, "assertion": "signed-assertion"},
    )
    assert verifier_unavailable.status_code == 503
    assert verifier_unavailable.get_json()["error"] == "identity_unavailable"

    next_login = client.get(
        f"/p/{share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    next_state = urllib.parse.parse_qs(
        urllib.parse.urlsplit(next_login.headers["Location"]).query
    )["state"][0]
    original_get_access = ShowPageStore.get_access

    def get_rotated_access(store, page_id):
        access = original_get_access(store, page_id)
        assert access is not None
        return SimpleNamespace(
            access_mode=access.access_mode,
            share_id="rotated-share",
            normalized_emails=access.normalized_emails,
            entries=access.entries,
        )

    monkeypatch.setattr(ShowPageStore, "get_access", get_rotated_access)
    monkeypatch.setattr(
        show_identity,
        "verify_show_identity_assertion",
        lambda *_args, **_kwargs: show_identity.VerifiedShowIdentity(
            subject="viewer-1",
            normalized_email="viewer@example.com",
            assertion_id="rotated-assertion",
            expires_at=int(ui_server.time.time()) + 300,
        ),
    )
    rotated = client.post(
        show_identity.CALLBACK_PATH,
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        data={"state": next_state, "assertion": "signed-assertion"},
    )
    assert rotated.status_code == 404
    assert rotated.get_json() == {"error": "not_found"}
    assert "Set-Cookie" not in rotated.headers


def test_limited_show_callback_rejects_offline_page_and_rate_limits_verification(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _configure_show_identity(config)
    share_id = _create_show_page("ses123", "limited")
    client = app.test_client()

    login = client.get(
        f"/p/{share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    state = urllib.parse.parse_qs(
        urllib.parse.urlsplit(login.headers["Location"]).query
    )["state"][0]
    monkeypatch.setattr(
        show_identity,
        "verify_show_identity_assertion",
        lambda *_args, **_kwargs: show_identity.VerifiedShowIdentity(
            subject="viewer-1",
            normalized_email="viewer@example.com",
            assertion_id="offline-assertion",
            expires_at=int(ui_server.time.time()) + 300,
        ),
    )
    store = ShowPageStore()
    try:
        store.update_visibility("ses123", "offline")
    finally:
        store.close()

    offline = client.post(
        show_identity.CALLBACK_PATH,
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        data={"state": state, "assertion": "signed-assertion"},
    )
    assert offline.status_code == 404
    assert offline.get_json() == {"error": "not_found"}
    assert "Set-Cookie" not in offline.headers

    monkeypatch.setattr(ui_server, "_auth_rate_limited", lambda: True)
    monkeypatch.setattr(
        show_identity,
        "verify_show_identity_assertion",
        lambda *_args, **_kwargs: pytest.fail("rate-limited callback verified JWT"),
    )
    limited = client.post(
        show_identity.CALLBACK_PATH,
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        data={"state": state, "assertion": "signed-assertion"},
    )
    assert limited.status_code == 429
    assert limited.headers["Cache-Control"] == "no-store"


def test_show_identity_callback_consumes_assertion_when_page_became_public(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _configure_show_identity(config)
    share_id = _create_show_page("ses123", "limited")
    client = app.test_client()
    login = client.get(
        f"/p/{share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    state = urllib.parse.parse_qs(
        urllib.parse.urlsplit(login.headers["Location"]).query
    )["state"][0]
    store = ShowPageStore()
    try:
        access = store.get_access("ses123")
        assert access is not None
        result = store.apply_access(
            "ses123",
            expected_revision=access.revision,
            target_access_mode="public",
            target_share_id=share_id,
            target_emails=[],
        )
        assert result.status == "applied"
    finally:
        store.close()
    monkeypatch.setattr(
        show_identity,
        "verify_show_identity_assertion",
        lambda *_args, **_kwargs: show_identity.VerifiedShowIdentity(
            subject="viewer-1",
            normalized_email="viewer@example.com",
            assertion_id="became-public-assertion",
            expires_at=int(ui_server.time.time()) + 300,
        ),
    )
    form = {"state": state, "assertion": "signed-assertion"}

    accepted = client.post(
        show_identity.CALLBACK_PATH,
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        data=form,
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    replay = app.test_client().post(
        show_identity.CALLBACK_PATH,
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        data=form,
    )
    assert replay.status_code == 400
    assert replay.get_json()["error"] == "replayed_assertion"


def test_show_identity_callback_does_not_charge_denied_identity_to_replay_ledger(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _configure_show_identity(config)
    share_id = _create_show_page("ses123", "limited")
    client = app.test_client()
    login = client.get(
        f"/p/{share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    state = urllib.parse.parse_qs(
        urllib.parse.urlsplit(login.headers["Location"]).query
    )["state"][0]
    monkeypatch.setattr(
        show_identity,
        "verify_show_identity_assertion",
        lambda *_args, **_kwargs: show_identity.VerifiedShowIdentity(
            subject="unlisted-user",
            normalized_email="unlisted@example.com",
            assertion_id="denied-assertion",
            expires_at=int(ui_server.time.time()) + 300,
        ),
    )
    monkeypatch.setattr(
        show_identity,
        "consume_verified_show_identity",
        lambda _identity: pytest.fail("denied identity consumed replay capacity"),
    )

    denied = client.post(
        show_identity.CALLBACK_PATH,
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        data={"state": state, "assertion": "signed-assertion"},
    )

    assert denied.status_code == 404
    assert denied.get_json() == {"error": "not_found"}
    assert "Set-Cookie" not in denied.headers


def test_show_identity_not_found_is_a_generic_html_page_for_browsers():
    with app.test_request_context(
        show_identity.CALLBACK_PATH,
        method="POST",
        headers={"Accept": "text/html", "Accept-Language": "zh-CN,zh;q=0.9"},
    ):
        response = ui_server._show_identity_not_found_response()

    assert response.status_code == 404
    assert response.headers["Content-Type"].startswith("text/html")
    body = response.body.decode("utf-8")
    assert 'lang="zh"' in body
    assert "此页面暂时不可用" in body
    assert "页面不存在，或已不再提供访问。" in body
    assert b"show_access_forbidden" not in response.body


@pytest.mark.parametrize(
    "accept",
    [
        "application/json, text/html;q=0",
        "application/json, text/html;q=0.5",
        "*/*",
    ],
)
def test_show_identity_not_found_prefers_json_when_html_is_not_preferred(accept):
    with app.test_request_context(
        show_identity.CALLBACK_PATH,
        method="POST",
        headers={"Accept": accept},
    ):
        response = ui_server._show_identity_not_found_response()

    assert response.status_code == 404
    assert json.loads(response.body) == {"error": "not_found"}


def test_show_identity_callback_body_stops_at_the_streaming_limit():
    class StreamingRequest:
        consumed_chunks = 0

        async def stream(self):
            for chunk in (b"a" * (show_identity.MAX_CALLBACK_BODY_BYTES - 1), b"bb", b"unread"):
                self.consumed_chunks += 1
                yield chunk

    streaming_request = StreamingRequest()

    with pytest.raises(show_identity.ShowIdentityError, match="invalid_callback"):
        asyncio.run(ui_server._read_show_identity_callback_body(streaming_request))
    assert streaming_request.consumed_chunks == 2


def test_public_show_ignores_an_existing_limited_guest_lease(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _configure_show_identity(config)
    share_id = _create_show_page("ses123", "limited")
    lease = show_identity.make_show_guest_lease(
        config,
        page_id="ses123",
        share_id=share_id,
        normalized_email="viewer@example.com",
    )
    client = app.test_client()
    client.set_cookie(
        show_identity.show_guest_cookie_name(share_id),
        lease,
        domain="alex.avibe.bot",
        path=show_identity.show_guest_cookie_path(share_id),
    )
    store = ShowPageStore()
    try:
        access = store.get_access("ses123")
        assert access is not None
        result = store.apply_access(
            "ses123",
            expected_revision=access.revision,
            target_access_mode="public",
            target_share_id=share_id,
            target_emails=[],
        )
        assert result.status == "applied"
    finally:
        store.close()

    response = client.get(
        f"/p/{share_id}/app.js",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert response.status_code == 200
    assert response.headers.get("Cache-Control") != "private, no-store"
    assert "Cookie" not in response.headers.get("Vary", "")


def test_rotated_public_share_rejects_an_old_guest_lease(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    old_share_id = _create_show_page("ses123", "limited")
    lease = show_identity.make_show_guest_lease(
        config,
        page_id="ses123",
        share_id=old_share_id,
        normalized_email="viewer@example.com",
    )
    client = app.test_client()
    client.set_cookie(
        show_identity.show_guest_cookie_name(old_share_id),
        lease,
        domain="alex.avibe.bot",
        path=show_identity.show_guest_cookie_path(old_share_id),
    )

    store = ShowPageStore()
    try:
        access = store.get_access("ses123")
        assert access is not None
        result = store.apply_access(
            "ses123",
            expected_revision=access.revision,
            target_access_mode="public",
            target_share_id="rotated-public-share",
            target_emails=[],
        )
        assert result.status == "applied"
    finally:
        store.close()

    old_response = client.get(
        f"/p/{old_share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
    )
    new_response = app.test_client().get(
        "/p/rotated-public-share/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
    )

    assert old_response.status_code == 404
    assert old_response.headers["Cache-Control"] == "private, no-store"
    assert "Cookie" in old_response.headers["Vary"]
    assert new_response.status_code == 200


def test_limited_guest_lease_is_rejected_after_rotation(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _configure_show_identity(config)
    old_share_id = _create_show_page("ses123", "limited")
    lease = show_identity.make_show_guest_lease(
        config,
        page_id="ses123",
        share_id=old_share_id,
        normalized_email="viewer@example.com",
    )
    admitted = app.test_client()
    admitted.set_cookie(
        show_identity.show_guest_cookie_name(old_share_id),
        lease,
        domain="alex.avibe.bot",
        path=show_identity.show_guest_cookie_path(old_share_id),
    )
    store = ShowPageStore()
    try:
        access = store.get_access("ses123")
        assert access is not None
        result = store.apply_access(
            "ses123",
            expected_revision=access.revision,
            target_access_mode="limited",
            target_share_id="rotated-share",
            target_emails=["viewer@example.com"],
        )
        assert result.status == "applied"
    finally:
        store.close()

    continuing = admitted.get(
        f"/p/{old_share_id}/app.js",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    rotated_navigation = admitted.get(
        f"/p/{old_share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={
            "Accept": "text/html",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        },
        follow_redirects=False,
    )
    assert rotated_navigation.status_code == 404
    assert continuing.status_code == 404
    fresh_old_link = app.test_client().get(
        f"/p/{old_share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
    )
    assert fresh_old_link.status_code == 404


def test_limited_show_guest_is_rechecked_after_access_changes(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _configure_show_identity(config)
    share_id = _create_show_page("ses123", "limited")
    manager = _FakeShowRuntimeManager(
        body=(
            b'<!doctype html><html><body><script type="module" '
            b'src="/src/main.tsx"></script></body></html>'
        ),
        bodies_by_path={
            "/sessions/ses123/app/app.js": b"window.guestPage = true;",
        },
        headers_by_path={
            "/sessions/ses123/app/app.js": {
                "content-type": "text/javascript; charset=utf-8"
            },
            "/sessions/ses123/app/api/data": {
                "content-type": "application/json",
                "cache-control": "public, max-age=3600",
            },
        },
    )
    verification_was_offloaded: list[bool] = []

    def verify_identity(*_args, **_kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            verification_was_offloaded.append(True)
        else:
            verification_was_offloaded.append(False)
        return show_identity.VerifiedShowIdentity(
            subject="viewer-1",
            normalized_email="viewer@example.com",
            assertion_id=f"assertion-{_kwargs['expected_nonce']}",
            expires_at=int(ui_server.time.time()) + 300,
        )

    monkeypatch.setattr(
        show_identity,
        "verify_show_identity_assertion",
        verify_identity,
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        client = app.test_client()
        login = client.get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(login.headers["Location"]).query
        )
        callback = client.post(
            show_identity.CALLBACK_PATH,
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            data={"state": query["state"][0], "assertion": "signed-assertion"},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert verification_was_offloaded == [True]
        assert callback.headers["Location"] == f"/p/{share_id}/"
        set_cookie = callback.headers["Set-Cookie"]
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=Lax" in set_cookie
        assert "Expires=" not in set_cookie
        assert "Max-Age=" not in set_cookie
        client.set_cookie(
            remote_access.SESSION_COOKIE_NAME,
            "",
            domain="alex.avibe.bot",
        )

        replay = app.test_client().post(
            show_identity.CALLBACK_PATH,
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            data={"state": query["state"][0], "assertion": "signed-assertion"},
        )
        assert replay.status_code == 400
        assert replay.get_json()["error"] == "replayed_assertion"
        assert show_identity.show_guest_cookie_name(share_id) not in replay.headers.get(
            "Set-Cookie", ""
        )

        page = client.get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
        )
        assert page.status_code == 200
        assert page.headers["Cache-Control"] == "private, no-store"
        assert page.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
        assert b'"authenticated":false' in page.content
        assert b"__show/annotation.js" not in page.content

        api_response = client.post(
            f"/p/{share_id}/api/data",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers=csrf_headers(client, "https://alex.avibe.bot"),
            json={"action": "refresh"},
        )
        assert api_response.status_code == 200
        assert api_response.headers["Cache-Control"] == "private, no-store"
        assert "Cookie" in api_response.headers["Vary"]

        events = client.get(
            f"/p/{share_id}/__show/events",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
        assert events.status_code == 404

        store = ShowPageStore()
        try:
            access = store.get_access("ses123")
            assert access is not None
            removed = store.apply_access(
                "ses123",
                expected_revision=access.revision,
                target_access_mode="limited",
                target_share_id=share_id,
                target_emails=["someone-else@example.com"],
            )
            assert removed.status == "applied"
        finally:
            store.close()

        existing_asset = client.get(
            f"/p/{share_id}/app.js",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
        assert existing_asset.status_code == 404
        assert existing_asset.headers["Cache-Control"] == "private, no-store"
        assert "Cookie" in existing_asset.headers["Vary"]
        html_subresource = client.get(
            f"/p/{share_id}/index.html",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
        )
        assert html_subresource.status_code == 404

        stale_limited_navigation = client.get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert stale_limited_navigation.status_code == 404

        client.set_cookie(
            remote_access.SESSION_COOKIE_NAME,
            remote_session_cookie(
                config,
                "viewer@example.com",
                "viewer-1",
                role="viewer",
            ),
            domain="alex.avibe.bot",
        )
        authenticated_revoked_navigation = client.get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert authenticated_revoked_navigation.status_code == 403
        assert "You do not have access to this page" in authenticated_revoked_navigation.text
        assert "Please contact the page owner" in authenticated_revoked_navigation.text
        assert "Location" not in authenticated_revoked_navigation.headers

        for entry_path in (f"/p/{share_id}/", f"/p/{share_id}/index.html"):
            non_html_entry = client.get(
                entry_path,
                base_url="https://alex.avibe.bot",
                environ_base=_remote_peer(),
                headers={"Accept": "application/json", "Sec-Fetch-Dest": "empty"},
                follow_redirects=False,
            )
            assert non_html_entry.status_code == 404
            assert non_html_entry.get_json() == {"error": "not_found"}

        fresh_client = app.test_client()
        fresh_login = fresh_client.get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        fresh_query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(fresh_login.headers["Location"]).query
        )
        denied = fresh_client.post(
            show_identity.CALLBACK_PATH,
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            data={
                "state": fresh_query["state"][0],
                "assertion": "signed-assertion",
            },
        )
        assert denied.status_code == 404
        assert denied.get_json() == {"error": "not_found"}

        store = ShowPageStore()
        try:
            access = store.get_access("ses123")
            assert access is not None
            made_private = store.apply_access(
                "ses123",
                expected_revision=access.revision,
                target_access_mode="private",
                target_share_id=share_id,
                target_emails=[],
            )
            assert made_private.status == "applied"
        finally:
            store.close()

        still_open = client.get(
            f"/p/{share_id}/app.js",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
        assert still_open.status_code == 404
        stale_private_navigation = client.get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert stale_private_navigation.status_code == 404
        new_visit = app.test_client().get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
        )
        assert new_visit.status_code == 404
        assert b"This page is unavailable" in new_visit.content
        assert b"does not exist or is no longer available" in new_visit.content

        store = ShowPageStore()
        try:
            offline = store.update_visibility("ses123", "offline")
            assert offline.visibility == "offline"
        finally:
            store.close()
        stopped = client.get(
            f"/p/{share_id}/app.js",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
        assert stopped.status_code == 401
    finally:
        set_show_runtime_manager_for_tests(None)


def test_limited_show_page_admits_group_and_organization_entries_readonly(
    monkeypatch,
    tmp_path,
):
    from core.show_pages import ShowPageStore as LiveStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _configure_show_identity(config)
    share_id = _create_show_page("ses123", "limited")
    monkeypatch.setattr(
        LiveStore,
        "_resolve_instance_ownership",
        staticmethod(lambda: {"mode": "organization", "organization_id": "org-1"}),
    )
    store = LiveStore()
    try:
        access = store.get_access("ses123")
        assert access is not None
        applied = store.apply_access(
            "ses123",
            expected_revision=access.revision,
            target_access_mode="limited",
            target_share_id=share_id,
            target_entries=[
                {"kind": "group", "value": "group-7"},
                {"kind": "organization", "value": "org-1"},
            ],
        )
        assert applied.status == "applied"
        assert applied.show_access.normalized_emails == ()
    finally:
        store.close()

    # A full-size membership list: admission must survive it on every request
    # after the callback, not just the callback itself.
    organization = show_identity.ShowIdentityOrganization(
        organization_id="org-1",
        organization_member_id="mem-1",
        organization_role="member",
        group_ids=frozenset({"group-7"})
        | {
            f"group-{index}-{'x' * (SHOW_ACCESS_ENTRY_VALUE_MAX_LENGTH - 16)}"
            for index in range(SHOW_ACCESS_VISITOR_GROUP_MAX_COUNT - 1)
        },
    )
    manager = _FakeShowRuntimeManager(
        body=(
            b'<!doctype html><html><body><script type="module" '
            b'src="/src/main.tsx"></script></body></html>'
        ),
    )

    def verify_identity(*_args, **_kwargs):
        return show_identity.VerifiedShowIdentity(
            subject="member-1",
            normalized_email="member@example.com",
            assertion_id=f"assertion-{_kwargs['expected_nonce']}",
            expires_at=int(ui_server.time.time()) + 300,
            organization=organization,
        )

    monkeypatch.setattr(
        show_identity,
        "verify_show_identity_assertion",
        verify_identity,
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        client = app.test_client()
        login = client.get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(login.headers["Location"]).query
        )
        callback = client.post(
            show_identity.CALLBACK_PATH,
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            data={"state": query["state"][0], "assertion": "signed-assertion"},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        page = client.get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
        )
        assert page.status_code == 200
        assert b'"authenticated":false' in page.content
        assert b"__show/annotation.js" not in page.content
        events = client.get(
            f"/p/{share_id}/__show/events",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
        assert events.status_code == 404
    finally:
        set_show_runtime_manager_for_tests(None)

    def verify_email_only(*_args, **_kwargs):
        return show_identity.VerifiedShowIdentity(
            subject="outsider-1",
            normalized_email="outsider@example.com",
            assertion_id=f"email-only-{_kwargs['expected_nonce']}",
            expires_at=int(ui_server.time.time()) + 300,
        )

    monkeypatch.setattr(
        show_identity,
        "verify_show_identity_assertion",
        verify_email_only,
    )
    denied_client = app.test_client()
    denied_login = denied_client.get(
        f"/p/{share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    denied_query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(denied_login.headers["Location"]).query
    )
    denied = denied_client.post(
        show_identity.CALLBACK_PATH,
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        data={
            "state": denied_query["state"][0],
            "assertion": "signed-assertion",
        },
    )
    assert denied.status_code == 404
    assert denied.get_json() == {"error": "not_found"}

    other_org = show_identity.ShowIdentityOrganization(
        organization_id="org-2",
        organization_member_id="mem-2",
        organization_role="member",
        group_ids=frozenset({"group-7"}),
    )

    def verify_other_org(*_args, **_kwargs):
        return show_identity.VerifiedShowIdentity(
            subject="other-1",
            normalized_email="other@example.com",
            assertion_id=f"other-org-{_kwargs['expected_nonce']}",
            expires_at=int(ui_server.time.time()) + 300,
            organization=other_org,
        )

    monkeypatch.setattr(
        show_identity,
        "verify_show_identity_assertion",
        verify_other_org,
    )
    other_client = app.test_client()
    other_login = other_client.get(
        f"/p/{share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    other_query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(other_login.headers["Location"]).query
    )
    other = other_client.post(
        show_identity.CALLBACK_PATH,
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        data={"state": other_query["state"][0], "assertion": "signed-assertion"},
    )
    assert other.status_code == 404


def test_public_show_page_still_requires_remote_login(monkeypatch, tmp_path):
    # Auth parity: serving public pages here adds no anonymous exposure — the
    # authed route still bounces a remote request without a session to login
    # (anonymous access stays on /p/<share_id> only).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "public")

    response = app.test_client().get(
        "/show/ses123/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/auth/login?next=%2Fshow%2Fses123%2F"


def test_offline_show_page_not_served_by_authed_route(monkeypatch, tmp_path):
    # The amendment serves private + public only; offline still returns the
    # explanatory offline page (never the live surface).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "offline")

    response = app.test_client().get("/show/ses123/", base_url="http://127.0.0.1:5123")

    assert b"This Show Page is offline" in response.content
    assert b"window.showPage" not in response.content  # the live app.js is never served


def test_public_show_page_no_slash_redirects_to_canonical(monkeypatch, tmp_path):
    # The sibling no-trailing-slash canonical redirect must accept public pages
    # too now that /show/ serves them (amendment §2.3), else the slash-less URL
    # 404s while the canonical one works.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "public")

    response = app.test_client().get("/show/ses123", base_url="http://127.0.0.1:5123", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/show/ses123/")


def test_private_show_page_uses_runtime_when_available(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager(body=b"<h1>Runtime Page</h1>")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/",
            base_url="http://127.0.0.1:5123",
            headers={
                "Accept": "text/html",
                "Accept-Encoding": "br, zstd",
                "Authorization": "Bearer secret",
                "Cookie": "__Host-vibe_remote_session=secret",
                "X-Vibe-CSRF-Token": "secret",
            },
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert b"Runtime Page" in response.content
    assert "__Host-vibe_remote_session=attacker" not in "\n".join(response.headers.getlist("set-cookie"))
    assert "x-runtime-private-header" not in response.headers
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert manager.calls[0][0] == "GET"
    assert manager.calls[0][1] == "/sessions/ses123/app/"
    assert manager.calls[0][2]["accept"] == "text/html"
    assert manager.calls[0][2]["X-Avibe-Show-Protocol"] == "1"
    assert manager.calls[0][2]["X-Avibe-Show-Context"] == "private"
    assert "accept-encoding" not in manager.calls[0][2]
    assert "authorization" not in manager.calls[0][2]
    assert "cookie" not in manager.calls[0][2]
    assert "x-vibe-csrf-token" not in manager.calls[0][2]
    assert manager.render_markdown_capability_calls == 0


def _markdown_runtime_manager(
    *,
    body: bytes = b"# Runtime page\n",
    status_code: int = 200,
    content_type: str = "text/markdown; charset=utf-8",
) -> _FakeShowRuntimeManager:
    path = "/sessions/ses123/render-markdown"
    return _FakeShowRuntimeManager(
        render_markdown_supported=True,
        status_by_path={path: status_code},
        bodies_by_path={path: body},
        headers_by_path={
            path: {
                "content-type": content_type,
                "x-avibe-render-cache": "miss",
            }
        },
    )


def _assert_markdown_response_headers(response, *, success: bool) -> None:
    vary = {item.strip().lower() for item in response.headers["vary"].split(",")}
    assert "accept" in vary
    assert response.headers["cache-control"] == "no-store"
    if success:
        assert response.headers["content-type"] == "text/markdown; charset=utf-8"
        assert response.headers["x-avibe-render-cache"] == "miss"
    else:
        assert response.headers["content-type"].startswith("application/json")


def _assert_public_representation_vary(response, *additional: str) -> None:
    vary = {item.strip().lower() for item in response.headers["vary"].split(",")}
    assert {
        "accept",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "user-agent",
        *(item.lower() for item in additional),
    } <= vary


def _public_representation_runtime_manager() -> _FakeShowRuntimeManager:
    manager = _markdown_runtime_manager()
    manager.bodies_by_path["/sessions/ses123/app/"] = b"<h1>HTML page</h1>"
    manager.headers_by_path["/sessions/ses123/app/"] = {
        "content-type": "text/html; charset=utf-8"
    }
    return manager


@pytest.mark.parametrize(
    "headers",
    [
        {"Accept": ""},
        {"Accept": "*/*"},
        {"Accept": "application/json", "User-Agent": "generic-reader/1.0"},
        {"User-Agent": "curl/8.10.1"},
        {
            "User-Agent": "curl/8.10.1",
            "Authorization": "Bearer caller-secret",
            "X-Vibe-CSRF-Token": "caller-secret",
        },
        {
            "User-Agent": (
                "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
                "ChatGPT-User/1.0; +https://openai.com/bot)"
            )
        },
        {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"},
        {
            "User-Agent": (
                "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
                "Claude-User/1.0; +Claude-User@anthropic.com)"
            )
        },
        {
            "User-Agent": (
                "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
                "Perplexity-User/1.0; +https://perplexity.ai/perplexity-user)"
            )
        },
        {
            "User-Agent": (
                "Mozilla/5.0 (compatible; MistralAI-User/1.0; "
                "+https://docs.mistral.ai/robots)"
            )
        },
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Microsoft Windows 10.0.19045; "
                "en-US) WindowsPowerShell/5.1.19041.5608"
            )
        },
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Microsoft Windows 10.0.19045; "
                "en-US) PowerShell/7.2.6"
            )
        },
    ],
)
def test_public_show_page_infers_markdown_for_non_browser_reads(
    monkeypatch,
    tmp_path,
    headers,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _public_representation_runtime_manager()
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers=headers,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b"# Runtime page\n"
    assert manager.calls[0][1] == "/sessions/ses123/render-markdown"
    assert "authorization" not in manager.calls[0][2]
    assert "x-vibe-csrf-token" not in manager.calls[0][2]
    _assert_markdown_response_headers(response, success=True)
    _assert_public_representation_vary(response)


@pytest.mark.parametrize(
    "headers",
    [
        {
            "Accept": "*/*",
            "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
        },
        {
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
            "User-Agent": "curl/8.10.1",
        },
        {
            "Sec-Fetch-Dest": "empty",
            "User-Agent": "ambiguous-client/1.0",
        },
    ],
)
def test_public_show_page_keeps_browser_shaped_reads_on_html(
    monkeypatch,
    tmp_path,
    headers,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _public_representation_runtime_manager()
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers=headers,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert b"HTML page" in response.content
    assert manager.calls[0][1] == "/sessions/ses123/app/"
    assert manager.render_markdown_capability_calls == 0
    _assert_public_representation_vary(response)


@pytest.mark.parametrize(
    ("headers", "expected_path"),
    [
        (
            {"Accept": "text/html", "User-Agent": "curl/8.10.1"},
            "/sessions/ses123/app/",
        ),
        (
            {"Accept": "application/xhtml+xml", "User-Agent": "curl/8.10.1"},
            "/sessions/ses123/app/",
        ),
        (
            {
                "Accept": "text/markdown",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
                "User-Agent": "Mozilla/5.0 Chrome/140.0",
            },
            "/sessions/ses123/render-markdown",
        ),
        (
            {
                "Accept": "text/markdown;q=0.4, text/html;q=0.9",
                "User-Agent": "curl/8.10.1",
            },
            "/sessions/ses123/app/",
        ),
        (
            {
                "Accept": "text/markdown;q=0.9, text/html;q=0.4",
                "User-Agent": "Mozilla/5.0 Chrome/140.0",
            },
            "/sessions/ses123/render-markdown",
        ),
    ],
)
def test_public_show_page_explicit_accept_overrides_client_inference(
    monkeypatch,
    tmp_path,
    headers,
    expected_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _public_representation_runtime_manager()
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers=headers,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert manager.calls[0][1] == expected_path
    _assert_public_representation_vary(response)


def test_private_show_page_keeps_implicit_agent_request_on_html_after_authorization(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _public_representation_runtime_manager()
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "editor@example.com", "editor-1"),
        domain="alex.avibe.bot",
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = client.get(
            "/show/ses123/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"User-Agent": "curl/8.10.1"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert b"HTML page" in response.content
    assert manager.calls[0][1] == "/sessions/ses123/app/"
    assert manager.render_markdown_capability_calls == 0
    vary = {item.strip().lower() for item in response.headers.get("Vary", "").split(",")}
    assert not {
        "sec-fetch-dest",
        "sec-fetch-mode",
        "user-agent",
    } & vary


def test_private_show_page_implicit_agent_request_does_not_bypass_authorization(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _public_representation_runtime_manager()
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"User-Agent": "curl/8.10.1"},
            follow_redirects=False,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 401
    assert manager.calls == []
    assert manager.render_markdown_capability_calls == 0


def test_public_show_page_implicit_markdown_errors_keep_representation_vary(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _FakeShowRuntimeManager(render_markdown_supported=False)
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={
                "Accept": "*/*",
                "Accept-Encoding": "gzip",
                "User-Agent": "curl/8.10.1",
            },
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "renderer_unavailable"
    assert manager.calls == []
    _assert_markdown_response_headers(response, success=False)
    _assert_public_representation_vary(response)


def test_public_show_page_infers_markdown_for_spa_history_routes(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _markdown_runtime_manager()
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/reports/daily?view=week",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "*/*", "User-Agent": "curl/8.10.1"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert manager.calls[0][1] == "/sessions/ses123/render-markdown"
    assert manager.calls[0][2][SHOW_RUNTIME_TARGET_HEADER] == "/reports/daily?view=week"
    _assert_public_representation_vary(response)


@pytest.mark.parametrize(
    ("visibility", "share_id", "status_code", "code"),
    [
        (None, "unknown-share", 404, "session_unknown"),
        ("offline", None, 404, "page_offline"),
        ("limited", None, 401, "authentication_required"),
    ],
)
def test_public_show_page_implicit_markdown_preserves_admission_errors(
    monkeypatch,
    tmp_path,
    visibility,
    share_id,
    status_code,
    code,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    if visibility is not None:
        share_id = _create_show_page("ses123", visibility)

    response = app.test_client().get(
        f"/p/{share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "*/*", "User-Agent": "curl/8.10.1"},
        follow_redirects=False,
    )

    assert response.status_code == status_code
    assert response.get_json()["error"]["code"] == code
    _assert_markdown_response_headers(response, success=False)
    _assert_public_representation_vary(response)


def test_show_cli_guidance_distinguishes_public_and_private_markdown_contracts():
    translations = {
        language: json.loads(
            (Path(ui_server.__file__).parent / "i18n" / f"{language}.json").read_text(
                encoding="utf-8"
            )
        )["show"]["markdown"]["help"]
        for language in ("en", "zh")
    }

    assert "Public Show Page" in translations["en"]
    assert "automatically" in translations["en"]
    assert "authorized private" in translations["en"]
    assert "公共 Show Page" in translations["zh"]
    assert "自动" in translations["zh"]
    assert "已授权的私有页面" in translations["zh"]
    assert all("Accept: text/markdown" in value for value in translations.values())


@pytest.mark.parametrize(
    ("accept", "expected_path"),
    [
        ("text/markdown", "/sessions/ses123/render-markdown"),
        ("text/markdown;q=1, text/html;q=0.5", "/sessions/ses123/render-markdown"),
        ("text/markdown;q=0.8, text/html;q=0.8", "/sessions/ses123/render-markdown"),
        ("text/markdown;q=0.5, text/html;q=0.9", "/sessions/ses123/app/"),
        ("text/html,application/xhtml+xml,*/*;q=0.8", "/sessions/ses123/app/"),
        ("*/*", "/sessions/ses123/app/"),
    ],
)
def test_show_page_markdown_negotiation_respects_accept_quality(
    monkeypatch,
    tmp_path,
    accept,
    expected_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _markdown_runtime_manager()
    manager.bodies_by_path["/sessions/ses123/app/"] = b"<h1>HTML page</h1>"
    manager.headers_by_path["/sessions/ses123/app/"] = {
        "content-type": "text/html; charset=utf-8"
    }
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/",
            base_url="http://127.0.0.1:5123",
            headers={"Accept": accept},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert manager.calls[0][1] == expected_path
    if expected_path.endswith("render-markdown"):
        assert response.content == b"# Runtime page\n"
        _assert_markdown_response_headers(response, success=True)
        assert manager.render_markdown_capability_calls == 1
    else:
        assert b"HTML page" in response.content
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        assert manager.render_markdown_capability_calls == 0


@pytest.mark.parametrize("route_path", ["reports/daily", "users/alice@example.com", "releases/v1.2"])
@pytest.mark.parametrize("surface", ["private", "public"])
def test_show_page_markdown_negotiates_spa_history_routes(
    monkeypatch,
    tmp_path,
    route_path,
    surface,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public" if surface == "public" else "private")
    manager = _markdown_runtime_manager()
    if surface == "private":
        url = f"/show/ses123/{route_path}?view=week&timezone=Asia%2FShanghai"
        request_kwargs = {"base_url": "http://127.0.0.1:5123"}
    else:
        url = f"/p/{share_id}/{route_path}?view=week&timezone=Asia%2FShanghai"
        request_kwargs = {
            "base_url": "https://alex.avibe.bot",
            "environ_base": _remote_peer(),
        }
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            url,
            headers={"Accept": "text/markdown"},
            **request_kwargs,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert [call[1] for call in manager.calls] == ["/sessions/ses123/render-markdown"]
    assert manager.calls[0][2][SHOW_RUNTIME_TARGET_HEADER] == (
        f"/{route_path}?view=week&timezone=Asia%2FShanghai"
    )
    _assert_markdown_response_headers(response, success=True)


@pytest.mark.parametrize("surface", ["private", "public"])
@pytest.mark.parametrize(
    ("document_path", "expected_target"),
    [("about.html", "/about.html"), ("docs/", "/docs/")],
)
def test_show_page_markdown_negotiates_authored_documents(
    monkeypatch,
    tmp_path,
    surface,
    document_path,
    expected_target,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public" if surface == "public" else "private")
    page_dir = ensure_show_page_dir("ses123")
    if document_path.endswith("/"):
        document_dir = page_dir / document_path.rstrip("/")
        document_dir.mkdir()
        (document_dir / "index.html").write_text("<h1>Nested document</h1>", encoding="utf-8")
    else:
        (page_dir / document_path).write_text("<h1>Authored document</h1>", encoding="utf-8")
    manager = _markdown_runtime_manager()
    client = app.test_client()
    if surface == "private":
        client.set_cookie(
            remote_access.SESSION_COOKIE_NAME,
            _active_org_cookie(config, "editor@example.com", "editor-1"),
            domain="alex.avibe.bot",
        )
        url = f"/show/ses123/{document_path}"
        request_kwargs = {
            "base_url": "https://alex.avibe.bot",
            "environ_base": _remote_peer(),
        }
    else:
        url = f"/p/{share_id}/{document_path}"
        request_kwargs = {
            "base_url": "https://alex.avibe.bot",
            "environ_base": _remote_peer(),
        }
    set_show_runtime_manager_for_tests(manager)
    try:
        response = client.get(
            url,
            headers={"Accept": "text/markdown"},
            **request_kwargs,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert manager.calls[0][1] == "/sessions/ses123/render-markdown"
    assert manager.calls[0][2][SHOW_RUNTIME_TARGET_HEADER] == expected_target
    _assert_markdown_response_headers(response, success=True)


@pytest.mark.parametrize("surface", ["private", "public"])
@pytest.mark.parametrize(
    "request_headers",
    [
        {"Accept": "text/markdown"},
        {"Accept": "*/*", "User-Agent": "curl/8.10.1"},
    ],
    ids=["explicit-markdown", "implicit-agent"],
)
def test_show_page_markdown_keeps_extensionless_assets_on_the_app_proxy(
    monkeypatch,
    tmp_path,
    surface,
    request_headers,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public" if surface == "public" else "private")
    (ensure_show_page_dir("ses123") / "report").write_text(
        "extensionless asset",
        encoding="utf-8",
    )
    runtime_path = "/sessions/ses123/app/report"
    manager = _FakeShowRuntimeManager(
        render_markdown_supported=True,
        bodies_by_path={runtime_path: b"extensionless asset"},
        headers_by_path={runtime_path: {"content-type": "text/plain; charset=utf-8"}},
    )
    client = app.test_client()
    if surface == "private":
        client.set_cookie(
            remote_access.SESSION_COOKIE_NAME,
            _active_org_cookie(config, "editor@example.com", "editor-1"),
            domain="alex.avibe.bot",
        )
        url = "/show/ses123/report"
        request_kwargs = {
            "base_url": "https://alex.avibe.bot",
            "environ_base": _remote_peer(),
        }
    else:
        url = f"/p/{share_id}/report"
        request_kwargs = {
            "base_url": "https://alex.avibe.bot",
            "environ_base": _remote_peer(),
        }
    set_show_runtime_manager_for_tests(manager)
    try:
        response = client.get(
            url,
            headers=request_headers,
            **request_kwargs,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b"extensionless asset"
    assert manager.render_markdown_capability_calls == 0
    assert [call[1] for call in manager.calls] == [runtime_path]


def test_public_show_page_classifies_non_document_before_limited_admission(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "limited")
    (ensure_show_page_dir("ses123") / "report").write_text(
        "extensionless asset",
        encoding="utf-8",
    )
    manager = _FakeShowRuntimeManager(render_markdown_supported=True)
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/report",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "*/*", "User-Agent": "curl/8.10.1"},
            follow_redirects=False,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 404
    assert response.get_json() == {"error": "not_found"}
    assert manager.calls == []
    assert manager.render_markdown_capability_calls == 0


def test_private_show_page_markdown_requires_auth_then_strips_identity_headers(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _markdown_runtime_manager()
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "editor@example.com", "editor-1"),
        domain="alex.avibe.bot",
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = client.get(
            "/show/ses123/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={
                "Accept": "text/markdown",
                "Authorization": "Bearer caller-secret",
                "X-Vibe-CSRF-Token": "caller-secret",
                "X-Avibe-Show-Protocol": "999",
                "X-Avibe-Show-Context": "shared",
                "X-Vibe-Show-Base": "/untrusted/",
                "X-Vibe-Show-Target": "https://attacker.example/raw",
            },
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    _assert_markdown_response_headers(response, success=True)
    method, path, headers, body, timeout_seconds = manager.calls[0]
    assert (method, path, body) == ("GET", "/sessions/ses123/render-markdown", None)
    assert timeout_seconds == 30.0
    assert headers["X-Avibe-Show-Protocol"] == "1"
    assert headers["X-Avibe-Show-Context"] == "private"
    assert headers[SHOW_RUNTIME_BASE_HEADER] == "/show/ses123/"
    assert headers[SHOW_RUNTIME_TARGET_HEADER] == "/"
    assert "authorization" not in headers
    assert "cookie" not in headers
    assert "x-vibe-csrf-token" not in headers


def test_public_show_page_markdown_is_anonymous_and_uses_public_base(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _markdown_runtime_manager()
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/markdown"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    _assert_markdown_response_headers(response, success=True)
    _assert_public_representation_vary(response)
    assert manager.calls[0][2]["X-Avibe-Show-Context"] == "shared"
    assert manager.calls[0][2][SHOW_RUNTIME_BASE_HEADER] == f"/p/{share_id}/"


def test_limited_guest_show_page_markdown_reuses_existing_admission(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _configure_show_identity(config)
    share_id = _create_show_page("ses123", "limited")
    client = app.test_client()
    client.set_cookie(
        show_identity.show_guest_cookie_name(share_id),
        show_identity.make_show_guest_lease(
            config,
            page_id="ses123",
            share_id=share_id,
            normalized_email="viewer@example.com",
        ),
        domain="alex.avibe.bot",
        path=show_identity.show_guest_cookie_path(share_id),
    )
    manager = _markdown_runtime_manager()
    set_show_runtime_manager_for_tests(manager)
    try:
        response = client.get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/markdown"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    _assert_markdown_response_headers(response, success=True)
    _assert_public_representation_vary(response, "cookie")
    assert manager.calls[0][2]["X-Avibe-Show-Context"] == "shared"
    assert manager.calls[0][2][SHOW_RUNTIME_BASE_HEADER] == f"/p/{share_id}/"


def test_limited_authenticated_show_page_markdown_head_reuses_get_admission(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _configure_show_identity(config)
    share_id = _create_show_page("ses123", "limited")
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "viewer@example.com", "viewer-1", role="viewer"),
        domain="alex.avibe.bot",
    )
    manager = _markdown_runtime_manager()
    set_show_runtime_manager_for_tests(manager)
    try:
        response = client.request(
            "HEAD",
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/markdown"},
            follow_redirects=False,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert "location" not in response.headers
    assert response.content == b""
    assert manager.calls[0][0] == "GET"
    assert manager.calls[0][2][SHOW_RUNTIME_TARGET_HEADER] == "/"
    _assert_markdown_response_headers(response, success=True)


def test_limited_show_page_markdown_unauthenticated_is_json_not_identity_redirect(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _configure_show_identity(config)
    share_id = _create_show_page("ses123", "limited")

    response = app.test_client().get(
        f"/p/{share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/markdown"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "location" not in response.headers
    assert response.get_json()["error"]["code"] == "authentication_required"
    _assert_markdown_response_headers(response, success=False)


def test_private_show_page_markdown_unauthenticated_is_json_not_redirect(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _markdown_runtime_manager()
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/markdown"},
            follow_redirects=False,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 401
    assert "location" not in response.headers
    assert response.get_json()["error"]["code"] == "authentication_required"
    _assert_markdown_response_headers(response, success=False)
    assert manager.calls == []


def test_private_show_page_markdown_pre_auth_classification_never_probes_filesystem(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    workspace = ensure_show_page_dir("ses123")
    (workspace.parent / "outside-existing").write_text("secret", encoding="utf-8")
    probe_calls = []
    original_asset_exists = ui_server._show_page_runtime_asset_exists
    original_document_exists = ui_server._show_page_runtime_document_exists

    def record_asset_probe(*args):
        probe_calls.append(("asset", args))
        return original_asset_exists(*args)

    def record_document_probe(*args):
        probe_calls.append(("document", args))
        return original_document_exists(*args)

    monkeypatch.setattr(ui_server, "_show_page_runtime_asset_exists", record_asset_probe)
    monkeypatch.setattr(ui_server, "_show_page_runtime_document_exists", record_document_probe)
    client = app.test_client()
    responses = [
        client.get(
            f"/show/ses123/%252e%252e/{target}",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/markdown"},
            follow_redirects=False,
        )
        for target in ("outside-existing", "outside-missing")
    ]

    assert [response.status_code for response in responses] == [401, 401]
    assert responses[0].get_json() == responses[1].get_json()
    assert responses[0].get_json()["error"]["code"] == "authentication_required"
    assert all("location" not in response.headers for response in responses)
    assert probe_calls == []
    for response in responses:
        _assert_markdown_response_headers(response, success=False)


@pytest.mark.parametrize("surface", ["private", "public"])
def test_show_page_markdown_denies_traversal_before_filesystem_refinement(
    monkeypatch,
    tmp_path,
    surface,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public" if surface == "public" else "private")
    workspace = ensure_show_page_dir("ses123")
    (workspace.parent / "outside-existing").write_text("secret", encoding="utf-8")

    def unexpected_refinement(*_args):
        raise AssertionError("filesystem refinement ran before path confinement")

    monkeypatch.setattr(
        ui_server,
        "_show_page_markdown_target_is_document",
        unexpected_refinement,
    )
    client = app.test_client()
    if surface == "private":
        client.set_cookie(
            remote_access.SESSION_COOKIE_NAME,
            _active_org_cookie(config, "editor@example.com", "editor-1"),
            domain="alex.avibe.bot",
        )
        url = "/show/ses123/%252e%252e/outside-existing"
    else:
        url = f"/p/{share_id}/%252e%252e/outside-existing"

    response = client.get(
        url,
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/markdown"},
    )

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "session_unknown"
    assert b"secret" not in response.content
    _assert_markdown_response_headers(response, success=False)


def test_limited_show_page_markdown_forbidden_is_machine_readable(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _configure_show_identity(config)
    share_id = _create_show_page("ses123", "limited")
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "other@example.com",
            "viewer-1",
            role="viewer",
        ),
        domain="alex.avibe.bot",
    )

    response = client.get(
        f"/p/{share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/markdown"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "location" not in response.headers
    assert response.get_json()["error"]["code"] == "forbidden"
    _assert_markdown_response_headers(response, success=False)


@pytest.mark.parametrize("surface", ["private", "public"])
def test_offline_show_page_markdown_maps_to_page_offline(monkeypatch, tmp_path, surface):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "offline")
    if surface == "private":
        url = "/show/ses123/"
        request_kwargs = {"base_url": "http://127.0.0.1:5123"}
    else:
        url = f"/p/{share_id}/"
        request_kwargs = {
            "base_url": "https://alex.avibe.bot",
            "environ_base": _remote_peer(),
        }

    response = app.test_client().get(url, headers={"Accept": "text/markdown"}, **request_kwargs)

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "page_offline"
    _assert_markdown_response_headers(response, success=False)


@pytest.mark.parametrize(
    ("status_code", "code"),
    [
        (400, "invalid_target"),
        (404, "session_unknown"),
        (503, "renderer_unavailable"),
        (504, "render_timeout"),
        (502, "output_too_large"),
        (502, "render_failed"),
    ],
)
@pytest.mark.parametrize("surface", ["private", "public"])
def test_show_page_markdown_preserves_runtime_contract_errors(
    monkeypatch,
    tmp_path,
    status_code,
    code,
    surface,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public" if surface == "public" else "private")
    message = f"runtime {code} hint"
    manager = _markdown_runtime_manager(
        status_code=status_code,
        content_type="application/json",
        body=json.dumps({"error": {"code": code, "message": message}}).encode(),
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        if surface == "public":
            url = f"/p/{share_id}/"
            request_kwargs = {
                "base_url": "https://alex.avibe.bot",
                "environ_base": _remote_peer(),
                "headers": {"Accept": "*/*", "User-Agent": "curl/8.10.1"},
            }
        else:
            url = "/show/ses123/"
            request_kwargs = {
                "base_url": "http://127.0.0.1:5123",
                "headers": {"Accept": "text/markdown"},
            }
        response = app.test_client().get(
            url,
            **request_kwargs,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == status_code
    assert response.get_json() == {"error": {"code": code, "message": message}}
    _assert_markdown_response_headers(response, success=False)
    if surface == "public":
        _assert_public_representation_vary(response)


@pytest.mark.parametrize("failure", ["capability_missing", "route_missing", "unreachable"])
def test_show_page_markdown_unavailable_never_falls_back_to_html(
    monkeypatch,
    tmp_path,
    failure,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    if failure == "capability_missing":
        manager = _FakeShowRuntimeManager(render_markdown_supported=False)
    elif failure == "route_missing":
        manager = _markdown_runtime_manager(
            status_code=404,
            content_type="application/json",
            body=b'{"detail":"Not Found"}',
        )
    else:
        manager = _FakeShowRuntimeManager(fail=True, render_markdown_supported=True)
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/",
            base_url="http://127.0.0.1:5123",
            headers={"Accept": "text/markdown"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["error"]["code"] == "renderer_unavailable"
    assert "Upgrade the Show Runtime" in payload["error"]["message"]
    assert b"Show Page" not in response.content
    _assert_markdown_response_headers(response, success=False)
    if failure == "capability_missing":
        assert manager.render_markdown_capability_calls == 1
        assert manager.calls == []


@pytest.mark.parametrize(
    ("request_failure", "status_code", "code"),
    [
        ("timeout", 504, "render_timeout"),
        ("unreachable", 503, "renderer_unavailable"),
    ],
)
def test_show_page_markdown_maps_runtime_request_failures(
    monkeypatch,
    tmp_path,
    request_failure,
    status_code,
    code,
):
    import httpx

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _markdown_runtime_manager()

    async def fail_request(*_args, **_kwargs):
        if request_failure == "timeout":
            runtime_request = httpx.Request(
                "GET",
                "http://127.0.0.1:4173/sessions/ses123/render-markdown",
            )
            raise httpx.ReadTimeout("render timed out", request=runtime_request)
        raise RuntimeError("runtime disconnected")

    manager.request = fail_request
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/",
            base_url="http://127.0.0.1:5123",
            headers={"Accept": "text/markdown"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == status_code
    assert response.get_json()["error"]["code"] == code
    _assert_markdown_response_headers(response, success=False)


def test_show_page_markdown_rejects_html_success_from_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _markdown_runtime_manager(
        body=b"<h1>not markdown</h1>",
        content_type="text/html; charset=utf-8",
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/",
            base_url="http://127.0.0.1:5123",
            headers={"Accept": "text/markdown"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 502
    assert response.get_json()["error"]["code"] == "render_failed"
    assert b"not markdown" not in response.content
    _assert_markdown_response_headers(response, success=False)


def test_show_page_markdown_head_uses_runtime_get_without_a_body(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _markdown_runtime_manager()
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().request(
            "HEAD",
            "/show/ses123/",
            base_url="http://127.0.0.1:5123",
            headers={"Accept": "text/markdown"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b""
    assert manager.calls[0][0] == "GET"
    _assert_markdown_response_headers(response, success=True)


@pytest.mark.parametrize("surface", ["private", "public"])
def test_show_page_markdown_compresses_large_success_responses(monkeypatch, tmp_path, surface):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public" if surface == "public" else "private")
    body = b"# Runtime page\n" + (b"Repeated Markdown content.\n" * 400)
    manager = _markdown_runtime_manager(body=body)
    client = app.test_client()
    if surface == "public":
        url = f"https://alex.avibe.bot/p/{share_id}/"
        remote_addr = "203.0.113.10"
    else:
        url = "http://127.0.0.1:5123/show/ses123/"
        remote_addr = "127.0.0.1"
    set_show_runtime_manager_for_tests(manager)
    try:
        with client._client.stream(
            "GET",
            url,
            headers={
                "Accept": "text/markdown",
                "Accept-Encoding": "gzip",
                TEST_REMOTE_ADDR_HEADER: remote_addr,
            },
        ) as response:
            compressed = b"".join(response.iter_raw())
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.headers["Content-Encoding"] == "gzip"
    assert response.headers["Content-Length"] == str(len(compressed))
    vary = {item.strip().lower() for item in response.headers["Vary"].split(",")}
    expected_vary = {"accept", "accept-encoding"}
    if surface == "public":
        expected_vary.update({"sec-fetch-dest", "sec-fetch-mode", "user-agent"})
    assert vary == expected_vary
    assert gzip.decompress(compressed) == body


@pytest.mark.parametrize("surface", ["private", "public"])
@pytest.mark.parametrize(
    "asset_path",
    ["app.js", "styles.css", "image.png", "api/data", "__vite_hmr"],
)
@pytest.mark.parametrize(
    "request_headers",
    [
        {"Accept": "text/markdown"},
        {"Accept": "*/*", "User-Agent": "curl/8.10.1"},
    ],
    ids=["explicit-markdown", "implicit-agent"],
)
def test_show_page_assets_never_negotiate_markdown(
    monkeypatch,
    tmp_path,
    surface,
    asset_path,
    request_headers,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public" if surface == "public" else "private")
    runtime_path = f"/sessions/ses123/app/{asset_path}"
    content_type = "application/json" if asset_path.startswith("api/") else "text/javascript"
    manager = _FakeShowRuntimeManager(
        render_markdown_supported=True,
        bodies_by_path={runtime_path: b'{"ok":true}' if asset_path.startswith("api/") else b"window.ok = true"},
        headers_by_path={runtime_path: {"content-type": content_type}},
    )
    if surface == "private":
        url = f"/show/ses123/{asset_path}"
        request_kwargs = {"base_url": "http://127.0.0.1:5123"}
    else:
        url = f"/p/{share_id}/{asset_path}"
        request_kwargs = {
            "base_url": "https://alex.avibe.bot",
            "environ_base": _remote_peer(),
        }
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            url,
            headers=request_headers,
            **request_kwargs,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert manager.render_markdown_capability_calls == 0
    assert [call[1] for call in manager.calls] == [runtime_path]


@pytest.mark.parametrize("surface", ["private", "public"])
@pytest.mark.parametrize("event_path", ["__events", "__show/events"])
@pytest.mark.parametrize(
    "request_headers",
    [
        {"Accept": "text/markdown"},
        {"Accept": "*/*", "User-Agent": "curl/8.10.1"},
    ],
    ids=["explicit-markdown", "implicit-agent"],
)
def test_show_page_event_routes_never_negotiate_markdown(
    monkeypatch,
    tmp_path,
    surface,
    event_path,
    request_headers,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public" if surface == "public" else "private")
    manager = _FakeShowRuntimeManager(render_markdown_supported=True)
    if surface == "private":
        url = f"/show/ses123/{event_path}"
        request_kwargs = {"base_url": "http://127.0.0.1:5123"}
    else:
        url = f"/p/{share_id}/{event_path}"
        request_kwargs = {
            "base_url": "https://alex.avibe.bot",
            "environ_base": _remote_peer(),
        }
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            url,
            headers=request_headers,
            **request_kwargs,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert manager.render_markdown_capability_calls == 0
    assert manager.calls == []


@pytest.mark.parametrize("surface", ["private", "public"])
def test_show_live_005_protocol_context_crosses_non_ascii_avibe_request_boundary(
    monkeypatch,
    tmp_path,
    surface,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    session_id = "ses-unicode-path"
    asset_path = "路径/组件.tsx"
    share_id = _create_show_page(session_id, surface)
    encoded_session = urllib.parse.quote(session_id, safe="")
    encoded_asset = urllib.parse.quote(asset_path, safe="/")
    if surface == "private":
        url = f"/show/{encoded_session}/{encoded_asset}"
        request_kwargs = {"base_url": "http://127.0.0.1:5123"}
        expected_context = "private"
    else:
        url = f"/p/{share_id}/{encoded_asset}"
        request_kwargs = {
            "base_url": "https://alex.avibe.bot",
            "environ_base": _remote_peer(),
        }
        expected_context = "shared"
    manager = _FakeShowRuntimeManager(body=b"export default true")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            url,
            headers={
                "Accept": "text/javascript",
                "X-Avibe-Show-Protocol": "999",
                "X-Avibe-Show-Context": "shared" if surface == "private" else "private",
            },
            **request_kwargs,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert manager.calls[0][1] == f"/sessions/{encoded_session}/app/{encoded_asset}"
    assert manager.calls[0][2]["X-Avibe-Show-Protocol"] == "1"
    assert manager.calls[0][2]["X-Avibe-Show-Context"] == expected_context


@pytest.mark.parametrize("surface", ["private", "public"])
@pytest.mark.parametrize("route_path", ["reports/daily", "users/alice@example.com"])
def test_show_page_history_route_requests_entry_directly(monkeypatch, tmp_path, surface, route_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    if surface == "private":
        _create_show_page("ses123", "private")
        public_path = f"/show/ses123/{route_path}"
        expected_base = "/show/ses123/"
        request_kwargs = {"base_url": "http://127.0.0.1:5123"}
    else:
        share_id = _create_show_page("ses123", "public")
        public_path = f"/p/{share_id}/{route_path}"
        expected_base = f"/p/{share_id}/"
        request_kwargs = {
            "base_url": "https://alex.avibe.bot",
            "environ_base": _remote_peer(),
        }

    entry_runtime_path = "/sessions/ses123/app/?vibe-embed=1"
    entry = (
        '<!doctype html><html><head><base href="/show/ses123/"></head><body>'
        '<script type="module" src="/show/ses123/src/main.tsx"></script></body></html>'
    ).encode()
    manager = _FakeShowRuntimeManager(
        status_by_path={entry_runtime_path: 200},
        bodies_by_path={entry_runtime_path: entry},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"{public_path}?vibe-embed=1",
            headers={"Accept": "text/html"},
            **request_kwargs,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    body = response.content.decode()
    assert f'<base href="{expected_base}">' in body
    assert f'"basePath":"{expected_base}"' in body
    assert response.headers["cache-control"] == "no-store"
    assert [call[1] for call in manager.calls] == [entry_runtime_path]
    expected_context = "private" if surface == "private" else "shared"
    assert all(call[2]["X-Avibe-Show-Protocol"] == "1" for call in manager.calls)
    assert all(call[2]["X-Avibe-Show-Context"] == expected_context for call in manager.calls)
    assert manager.calls[0][2]["accept"] == "text/html"
    assert all("x-vibe-show-base" not in call[2] for call in manager.calls)


@pytest.mark.parametrize("surface", ["private", "public"])
def test_show_page_history_route_uses_recovery_when_runtime_unavailable(monkeypatch, tmp_path, surface):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    if surface == "private":
        _create_show_page("ses123", "private")
        public_path = "/show/ses123/reports/daily"
        request_kwargs = {"base_url": "http://127.0.0.1:5123"}
    else:
        share_id = _create_show_page("ses123", "public")
        public_path = f"/p/{share_id}/reports/daily"
        request_kwargs = {
            "base_url": "https://alex.avibe.bot",
            "environ_base": _remote_peer(),
        }

    manager = _FakeShowRuntimeManager(fail=True)
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            public_path,
            headers={"Accept": "text/html"},
            **request_kwargs,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert b"Loading Show Page" in response.content
    assert b"The Show Runtime is unavailable" in response.content
    if surface == "private":
        assert b"vibe doctor repair show-runtime" in response.content
    else:
        assert b"vibe doctor repair show-runtime" not in response.content
        assert b"Reload this page to try the request again" in response.content
    assert b"src/App.tsx" not in response.content
    assert [call[1] for call in manager.calls] == ["/sessions/ses123/app/"]


@pytest.mark.parametrize(
    ("asset_path", "accept"),
    [
        ("assets/missing.js", "application/javascript"),
        ("api/missing", "text/html"),
        ("__show/unknown", "text/html"),
        ("__show/annotation.js", "text/html"),
    ],
)
def test_private_show_page_does_not_spa_fallback_asset_or_reserved_misses(
    monkeypatch, tmp_path, asset_path, accept
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager(
        status_code=404,
        status_by_path={"/sessions/ses123/app/": 200},
        bodies_by_path={"/sessions/ses123/app/": b"<html>entry</html>"},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/show/ses123/{asset_path}",
            base_url="http://127.0.0.1:5123",
            headers={"Accept": accept},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 404
    assert [call[1] for call in manager.calls] == [f"/sessions/ses123/app/{asset_path}"]


def test_private_show_page_real_extensionless_asset_precedes_spa_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    (paths.get_show_page_dir("ses123") / "robots").write_text("real extensionless asset", encoding="utf-8")
    asset_runtime_path = "/sessions/ses123/app/robots"
    manager = _FakeShowRuntimeManager(
        status_code=404,
        status_by_path={asset_runtime_path: 200, "/sessions/ses123/app/": 200},
        bodies_by_path={asset_runtime_path: b"real extensionless asset"},
        headers_by_path={asset_runtime_path: {"content-type": "text/plain"}},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/robots",
            base_url="http://127.0.0.1:5123",
            headers={"Accept": "text/html"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b"real extensionless asset"
    assert [call[1] for call in manager.calls] == [asset_runtime_path]


def test_private_show_page_real_extensionless_asset_survives_runtime_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    (paths.get_show_page_dir("ses123") / "robots").write_text("real extensionless asset", encoding="utf-8")
    manager = _FakeShowRuntimeManager(fail=True)
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/robots",
            base_url="http://127.0.0.1:5123",
            headers={"Accept": "text/html"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b"real extensionless asset"
    assert [call[1] for call in manager.calls] == ["/sessions/ses123/app/robots"]


def _icon_token(session_id: str) -> str:
    # The correct ?v= token the frontend would send (same source as the payload).
    from core.show_pages import show_page_icon_version

    return show_page_icon_version(session_id) or ""


def test_show_page_icon_endpoint_serves_static_with_hardened_headers(monkeypatch, tmp_path):
    # §7.1f: the dedicated icon endpoint resolves the page's own <link rel=icon>
    # against document semantics and streams the file — statically, never booting
    # the Show Runtime (listing apps would otherwise start a runtime per icon).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    page_dir = ensure_show_page_dir("ses123")
    (page_dir / "index.html").write_text('<link rel="icon" href="favicon.svg">', encoding="utf-8")
    (page_dir / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    manager = _FakeShowRuntimeManager(body=b"<h1>Runtime Page</h1>")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/api/show-pages/ses123/icon?v={_icon_token('ses123')}", base_url="http://127.0.0.1:5123"
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b"<svg/>"
    assert response.headers["content-type"] == "image/svg+xml"
    # Hardened static-asset headers: no sniffing, sandboxed, privately cacheable.
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    # The route sets `sandbox`; the app-wide vault-sandbox hook then composes its
    # frame-src onto it. The bare `sandbox` directive stays present + effective
    # (a page-authored SVG is rendered in an opaque origin with scripts disabled).
    csp_directives = [d.strip() for d in response.headers["Content-Security-Policy"].split(";")]
    assert "sandbox" in csp_directives
    # `immutable` is honest because ?v= is enforced against the served bytes.
    assert response.headers["Cache-Control"] == "private, max-age=604800, immutable"
    # Serving the icon never contacted the Show Runtime.
    assert manager.calls == []


def test_remote_show_page_icon_is_not_persistently_cached(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_show_page("ses123", "private")
    page_dir = ensure_show_page_dir("ses123")
    (page_dir / "index.html").write_text('<link rel="icon" href="favicon.svg">', encoding="utf-8")
    (page_dir / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "owner@example.com", "owner-1"),
        domain="alex.avibe.bot",
    )

    response = client.get(
        f"/api/show-pages/ses123/icon?v={_icon_token('ses123')}",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 200
    assert response.content == b"<svg/>"
    assert response.headers["Cache-Control"] == "private, no-store"


def _show_page_rows() -> dict[str, dict]:
    from sqlalchemy import select

    from core.show_pages import show_pages

    store = ShowPageStore()
    try:
        with store.engine.connect() as connection:
            return {
                row["session_id"]: dict(row)
                for row in connection.execute(select(show_pages)).mappings()
            }
    finally:
        store.close()


def test_show_page_read_returns_a_page_without_writing_the_table(monkeypatch, tmp_path):
    # `GET /api/show-pages/<sid>` is the read-only counterpart of `POST .../ensure`.
    # The property: reading a Show Page NEVER writes the show_pages table. It returns
    # an existing page byte-identical and reports show_page_not_found where ensure
    # would have created one — which is what leaves ensure's one-shot `existed` edge,
    # and the "visualize this session" prompt it triggers, to its single owner.
    # Seeded with one page of every visibility that exists, so a visibility added
    # later is covered here without editing this test.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    seeded = sorted(VISIBILITIES)
    for index, visibility in enumerate(seeded):
        _create_show_page(f"ses{index}", visibility)
    before = _show_page_rows()
    assert len(before) == len(seeded)
    client = app.test_client()
    responses = []

    for index, visibility in enumerate(seeded):
        response = client.get(f"/api/show-pages/ses{index}", base_url="http://127.0.0.1:5123")
        responses.append(response)
        assert response.status_code == 200, visibility
        body = response.get_json()
        assert body["ok"] is True
        assert body["session_id"] == f"ses{index}"
        # The read reports no creation fact, so no caller can consume one.
        assert "existed" not in body

    missing = client.get("/api/show-pages/sesabsent", base_url="http://127.0.0.1:5123")
    responses.append(missing)
    assert missing.status_code == 404
    assert missing.get_json()["code"] == "show_page_not_found"

    # The ensure POST was uncacheable by method. This route is a GET, so caching is
    # opt-out — and the property is per route, not per status: EVERY response it
    # produces carries per-caller page state and must be marked. A 404 is
    # heuristically cacheable, so an unmarked "no page here" could outlive the
    # page's creation and leave the share panel empty until it expired.
    for response in responses:
        assert response.headers["Cache-Control"] == "no-store, private", response.status
        assert response.headers["Vary"] == "Cookie", response.status

    assert _show_page_rows() == before


def test_show_page_read_hides_page_existence_without_project_access(monkeypatch, tmp_path):
    # The route policy screens the Instance role only, so an Editor still reaches this
    # read for sessions in projects they are not bound to. The property: to a caller
    # who fails the project check, a session that HAS a page and a session that has
    # none are indistinguishable. Without it the read is a page-existence oracle over
    # arbitrary session ids -- the create path already checks project access before it
    # acts, and a read must not be the cheaper way to learn what create would refuse.
    from storage import project_access_service
    from storage.db import create_sqlite_engine
    from vibe.authorization import AuthorizationContext

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("seswith")
    _create_agent_session("seswithout")
    _create_show_page("seswith", "private")
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        result = project_access_service.apply_project_access_intent(
            conn,
            {"project_id": "proj_show", "revision": 1, "mode": "restricted", "bindings": []},
        )
    assert result.changed is True
    engine.dispose()
    context = AuthorizationContext(
        instance_role="editor",
        subject="remote-editor",
        email="alice@example.com",
        instance_access_source="email",
        is_remote=True,
    )

    store = ShowPageStore()
    try:
        codes = []
        for session_id in ("seswith", "seswithout"):
            with pytest.raises(ShowPageError) as excinfo:
                store.get_for_use(session_id, user_context=context)
            codes.append(excinfo.value.code)
    finally:
        store.close()

    # Which code the caller gets is not the property -- that one session cannot be
    # told from the other is. Normalizing the pair either way keeps this passing.
    assert len(set(codes)) == 1, codes


def test_remote_personal_owner_can_ensure_show_page(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    calls = []
    monkeypatch.setattr(
        "vibe.api.ensure_show_page",
        lambda session_id, **_kwargs: (
            calls.append(session_id)
            or {"session_id": session_id, "visibility": "private"}
        ),
    )
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "owner-1"),
        domain="alex.avibe.bot",
    )

    response = client.post(
        "/api/show-pages/ses123/ensure",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers=csrf_headers(client, "https://alex.avibe.bot"),
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "session_id": "ses123",
        "visibility": "private",
    }
    assert calls == ["ses123"]


def test_show_page_icon_endpoint_enforces_token_without_selecting_the_file(monkeypatch, tmp_path):
    # §7.1f (token-enforcement): resolution derives ONLY from the sid + workspace —
    # `?v=` NEVER selects the file. The CORRECT token serves the favicon; a
    # wrong/missing/path-shaped token is a 404, and NEVER the file a `v` value names
    # (no traversal, no wrong-file serve). This is the honest-`immutable` guarantee:
    # a URL maps to exactly one byte-content.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    page_dir = ensure_show_page_dir("ses123")
    (page_dir / "index.html").write_text('<link rel="icon" href="favicon.svg">', encoding="utf-8")
    (page_dir / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    # Files a hostile query would try to reach if the endpoint ever RESOLVED via ?v=.
    (page_dir / "secret.svg").write_text("<svg>secret</svg>", encoding="utf-8")
    (tmp_path / "outside.svg").write_text("<svg>outside</svg>", encoding="utf-8")
    client = app.test_client()

    ok = client.get(f"/api/show-pages/ses123/icon?v={_icon_token('ses123')}", base_url="http://127.0.0.1:5123")
    assert ok.status_code == 200
    assert ok.content == b"<svg/>"

    for query in (
        "",  # missing v
        "?v=abc123",  # a wrong token
        "?v=../../secret.svg",  # traversal-shaped
        "?v=../../../outside.svg",
        "?v=%2e%2e%2fsecret.svg",  # encoded traversal-shaped
        "?v=secret.svg",  # names a real in-workspace file
        "?v=" + "z" * 5000,  # junk
    ):
        response = client.get(f"/api/show-pages/ses123/icon{query}", base_url="http://127.0.0.1:5123")
        assert response.status_code == 404, query  # never resolves a different file
        assert b"secret" not in response.content, query
        assert b"outside" not in response.content, query


def test_show_page_icon_endpoint_ignores_range_header(monkeypatch, tmp_path):
    # The icon is a bytes-or-404 chokepoint: a `Range` header must NOT turn it into
    # a 206/416 partial (the materialized plain Response never honors Range) — it
    # always serves the full 200 (Codex).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    page_dir = ensure_show_page_dir("ses123")
    (page_dir / "index.html").write_text('<link rel="icon" href="favicon.svg">', encoding="utf-8")
    (page_dir / "favicon.svg").write_text("<svg>abcdefghij</svg>", encoding="utf-8")

    response = app.test_client().get(
        f"/api/show-pages/ses123/icon?v={_icon_token('ses123')}",
        base_url="http://127.0.0.1:5123",
        headers={"Range": "bytes=0-3"},
    )

    assert response.status_code == 200
    assert response.content == b"<svg>abcdefghij</svg>"
    assert "Content-Range" not in response.headers


def test_show_page_icon_endpoint_404s_when_file_vanishes_after_resolve(monkeypatch, tmp_path):
    # Live-edit race: resolve_show_page_icon accepts the icon, then the file is
    # rebuilt/removed before the bytes are read. Because the endpoint materializes
    # the bytes INSIDE its try, the OSError degrades to the 404 letter fallback —
    # not a 500 raised while a lazy FileResponse streams (Codex).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    vanished = ensure_show_page_dir("ses123") / "vanished.svg"  # never created on disk
    monkeypatch.setattr(
        "core.show_pages.resolve_show_page_icon",
        lambda session_id: (vanished, "image/svg+xml"),
    )

    response = app.test_client().get("/api/show-pages/ses123/icon", base_url="http://127.0.0.1:5123")

    assert response.status_code == 404
    assert response.headers.get("Cache-Control") == "no-store"


def test_show_page_icon_endpoint_serves_offline_pages(monkeypatch, tmp_path):
    # An offline page still advertises a token and is listed in the inventory, so its
    # static icon must serve too — gating by visibility would strand offline rows /
    # pinned offline apps on the letter avatar despite a real icon (Codex).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "offline")
    page_dir = ensure_show_page_dir("ses123")
    (page_dir / "index.html").write_text('<link rel="icon" href="favicon.svg">', encoding="utf-8")
    (page_dir / "favicon.svg").write_text("<svg/>", encoding="utf-8")

    response = app.test_client().get(
        f"/api/show-pages/ses123/icon?v={_icon_token('ses123')}", base_url="http://127.0.0.1:5123"
    )

    assert response.status_code == 200
    assert response.content == b"<svg/>"


def test_show_page_icon_endpoint_not_found_is_uncacheable(monkeypatch, tmp_path):
    # The 404 for a page with no icon carries `no-store` so a heuristically-cached
    # negative response can't strand the letter fallback once the icon is added (Codex).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")  # default scaffold: no icon link

    response = app.test_client().get("/api/show-pages/ses123/icon", base_url="http://127.0.0.1:5123")

    assert response.status_code == 404
    assert response.headers.get("Cache-Control") == "no-store"


def test_show_page_icon_endpoint_404s_malformed_session_id(monkeypatch, tmp_path):
    # A session id that fails validate_session_id raises ShowPageError in store.get;
    # the endpoint must catch it and 404 (letter fallback), never 500 (Codex).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().get("/api/show-pages/!/icon", base_url="http://127.0.0.1:5123")

    assert response.status_code == 404


def test_show_page_icon_endpoint_404s_filesystem_invalid_icon(monkeypatch, tmp_path):
    # A page-authored href that resolves to a filesystem-invalid path (an overlong
    # filename) makes Path.resolve()/stat raise OSError; the endpoint must 404, never
    # 500 (Codex). resolve_show_page_icon contains it; the boundary is belt-and-braces.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    page_dir = ensure_show_page_dir("ses123")
    (page_dir / "index.html").write_text(
        f'<link rel="icon" href="{"a" * 300}.png">', encoding="utf-8"
    )

    response = app.test_client().get("/api/show-pages/ses123/icon", base_url="http://127.0.0.1:5123")

    assert response.status_code == 404


def test_show_page_icon_endpoint_resolves_through_base_href(monkeypatch, tmp_path):
    # The endpoint honors <base href> exactly as the browser would: the icon lives
    # under assets/ and is served, proving the resolver runs server-side (the URL
    # carries only the session id).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    page_dir = ensure_show_page_dir("ses123")
    (page_dir / "index.html").write_text(
        '<head><base href="assets/"><link rel="icon" href="logo.png"></head>', encoding="utf-8"
    )
    (page_dir / "assets").mkdir()
    (page_dir / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n")

    response = app.test_client().get(
        f"/api/show-pages/ses123/icon?v={_icon_token('ses123')}", base_url="http://127.0.0.1:5123"
    )

    assert response.status_code == 200
    assert response.content == b"\x89PNG\r\n"
    assert response.headers["content-type"] == "image/png"


def test_show_page_icon_endpoint_404_when_no_icon(monkeypatch, tmp_path):
    # The default scaffold ships no <link rel=icon>; the endpoint 404s so the tile
    # falls back to the letter avatar.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")  # default index has no icon link

    response = app.test_client().get("/api/show-pages/ses123/icon", base_url="http://127.0.0.1:5123")

    assert response.status_code == 404


def test_show_page_icon_endpoint_404_on_policy_rejections(monkeypatch, tmp_path):
    # Every policy rejection collapses to a 404 (never a redirect, never a partial
    # serve): runtime api/ + __show/ paths, non-image extensions, and traversal
    # escapes — even when the traversal target really exists on disk.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    page_dir = ensure_show_page_dir("ses123")
    (tmp_path / "outside_secret.svg").write_text("<svg>secret</svg>", encoding="utf-8")
    for href in ("api/health", "icon.txt", "../../outside_secret.svg", "__show/events.png"):
        (page_dir / "index.html").write_text(f'<link rel="icon" href="{href}">', encoding="utf-8")
        response = app.test_client().get("/api/show-pages/ses123/icon", base_url="http://127.0.0.1:5123")
        assert response.status_code == 404, href
        assert b"secret" not in response.content, href


def test_show_page_icon_endpoint_404_when_target_missing(monkeypatch, tmp_path):
    # A whitelisted, in-workspace href whose file does not exist is a 404 (the
    # <link> may reference an icon the page never actually shipped).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    page_dir = ensure_show_page_dir("ses123")
    (page_dir / "index.html").write_text('<link rel="icon" href="favicon.svg">', encoding="utf-8")

    response = app.test_client().get("/api/show-pages/ses123/icon", base_url="http://127.0.0.1:5123")

    assert response.status_code == 404


def test_show_page_icon_upload_happy_path(monkeypatch, tmp_path):
    # §7.1j: a multipart upload writes the workspace-root favicon and returns the
    # refreshed payload (fresh icon_version) so the Web UI merges it like any other
    # show-page mutation. The server chose the on-disk name from the type — the client
    # only sent bytes + a filename.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")  # index.html has no <link rel=icon>
    published: list = []
    monkeypatch.setattr(
        "vibe.sse_broker.broker.publish", lambda event_type, data: published.append((event_type, data))
    )
    client = app.test_client()

    response = client.post(
        "/api/show-pages/ses123/icon",
        files={"file": ("logo.svg", b"<svg>UPLOADED</svg>", "image/svg+xml")},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["session_id"] == "ses123"
    assert isinstance(body["icon_version"], str) and body["icon_version"]
    # The server chose favicon.svg at the workspace root and wrote the exact bytes.
    assert (ensure_show_page_dir("ses123") / "favicon.svg").read_bytes() == b"<svg>UPLOADED</svg>"
    # Every already-mounted inventory (Dock, WindowLayer, mobile drawer, search) reloads:
    # a session.activity show_event is broadcast so they pick up the new icon (§7.1j P2).
    assert ("session.activity", {"session_id": "ses123", "scope_id": None, "event": "show_event"}) in published


def test_show_page_icon_upload_length_guard_maps_too_large(monkeypatch, tmp_path):
    # The Content-Length guard rejects an oversized body (413) BEFORE the multipart parser
    # runs; that too_large must surface as icon_too_large/413, not collapse to a generic
    # invalid_icon/400 like a non-multipart body would (§7.1j review P3).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    from core.file_browser_service import FileBrowserError

    def _too_large(*_args, **_kwargs):
        raise FileBrowserError("too_large", "File is too large", 413)

    monkeypatch.setattr("vibe.ui_server._validate_file_upload_content_length", _too_large)
    client = app.test_client()

    response = client.post(
        "/api/show-pages/ses123/icon",
        files={"file": ("logo.svg", b"<svg/>", "image/svg+xml")},
        headers=csrf_headers(client),
    )

    assert response.status_code == 413
    assert response.get_json()["error"]["code"] == "icon_too_large"


def test_show_page_icon_upload_rejects_bad_type(monkeypatch, tmp_path):
    # A non-image type is a clean 415 (never a 500); nothing is written.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    client = app.test_client()

    response = client.post(
        "/api/show-pages/ses123/icon",
        files={"file": ("evil.html", b"<html></html>", "text/html")},
        headers=csrf_headers(client),
    )

    assert response.status_code == 415
    assert response.get_json()["error"]["code"] == "invalid_icon_type"
    assert not list(ensure_show_page_dir("ses123").glob("favicon.*"))


def test_show_page_icon_upload_unknown_page_is_404(monkeypatch, tmp_path):
    # Uploading to a session with no Show Page is a structured 404, not a 500.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    client = app.test_client()

    response = client.post(
        "/api/show-pages/sesnone/icon",
        files={"file": ("logo.svg", b"<svg/>", "image/svg+xml")},
        headers=csrf_headers(client),
    )

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "show_page_not_found"


def test_show_page_icon_upload_rejects_archived_session(monkeypatch, tmp_path):
    # An archived session's page is terminal — the other mutators reject it with
    # session_archived, so a direct icon upload must too, not write into the workspace
    # (§7.1j review P2). Create the page first (while no session row exists → not archived),
    # then insert the archived session row.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("sesarch", "private")
    _create_agent_session("sesarch", status="archived")
    client = app.test_client()

    response = client.post(
        "/api/show-pages/sesarch/icon",
        files={"file": ("logo.svg", b"<svg/>", "image/svg+xml")},
        headers=csrf_headers(client),
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "session_archived"
    assert not (ensure_show_page_dir("sesarch") / "favicon.svg").exists()


def test_show_page_icon_upload_requires_remote_login(monkeypatch, tmp_path):
    # Auth parity with the rest of /api: a remote request without a session is bounced
    # by the same before-request hook (never reaches the handler, so nothing is written).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")

    response = app.test_client().post(
        "/api/show-pages/ses123/icon",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        files={"file": ("logo.svg", b"<svg/>", "image/svg+xml")},
        follow_redirects=False,
    )

    assert response.status_code != 200
    assert not (ensure_show_page_dir("ses123") / "favicon.svg").exists()


def test_show_page_icon_endpoint_requires_remote_login(monkeypatch, tmp_path):
    # Auth parity with the rest of /api: a remote request without a session is
    # bounced, so the icon (which can embed page-authored SVG) is never exposed
    # anonymously. The icon exists, so a 200 here would be a real regression.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    page_dir = ensure_show_page_dir("ses123")
    (page_dir / "index.html").write_text('<link rel="icon" href="favicon.svg">', encoding="utf-8")
    (page_dir / "favicon.svg").write_text("<svg/>", encoding="utf-8")

    response = app.test_client().get(
        "/api/show-pages/ses123/icon",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code != 200  # bounced/denied, never served anonymously
    assert response.content != b"<svg/>"


def test_private_show_page_materializes_workspace_before_runtime_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page_record("ses123", "private")
    page_dir = paths.get_show_pages_dir() / "ses123"
    assert not (page_dir / "src" / "App.tsx").exists()
    manager = _FakeShowRuntimeManager(body=b"<h1>Runtime Page</h1>")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get("/show/ses123/", base_url="http://127.0.0.1:5123")
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert b"Runtime Page" in response.content
    assert (page_dir / "src" / "App.tsx").exists()
    styles_css = (page_dir / "src" / "styles.css").read_text(encoding="utf-8")
    assert styles_css.startswith('@import "tailwindcss";'), styles_css[:60]
    assert '@import "@avibe/show-ui/theme.css";' in styles_css, styles_css[:90]
    assert "background: var(--background)" in styles_css
    assert "--avs-" not in styles_css
    assert manager.calls[0][1] == "/sessions/ses123/app/"


def test_private_show_page_injects_runtime_event_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: f"token-{session_id}")
    manager = _FakeShowRuntimeManager(
        body=b'<!doctype html><html><head></head><body><div id="root"></div><script type="module" src="/src/main.tsx"></script></body></html>',
        extra_headers={
            "cache-control": "public, max-age=3600",
            "etag": '"runtime-etag"',
            "expires": "Wed, 03 Jun 2026 09:00:00 GMT",
            "last-modified": "Wed, 03 Jun 2026 08:00:00 GMT",
        },
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get("/show/ses123/", base_url="http://127.0.0.1:5123")
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    body = response.content.decode("utf-8")
    assert "globalThis.__AVIBE_SHOW__=Object.assign" in body
    assert '"sessionId":"ses123"' in body
    assert '"basePath":"/show/ses123/"' in body
    assert '"eventsPath":"/show/ses123/__show/events"' in body
    assert '"streamPath":"/show/ses123/__show/events?stream=1"' in body
    assert '"writeToken":"token-ses123"' in body
    assert '"annotation":{"authenticated":true,"mePath":"__show/me"}' in body
    assert "__AVIBE_PWA_NAVIGATE_SAME_ORIGIN__" in body
    assert "anchor.hasAttribute('download')" in body
    assert "target.origin!==window.location.origin" in body
    assert '<script type="module" src="/show/ses123/__show/annotation.js"></script>' in body
    assert body.index("globalThis.__AVIBE_SHOW__") < body.index('/src/main.tsx')
    assert body.index('/src/main.tsx') < body.index('/show/ses123/__show/annotation.js')
    assert "cookie" not in manager.calls[0][2]
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers
    assert "expires" not in response.headers
    assert "last-modified" not in response.headers


@pytest.mark.parametrize("authenticated", [False, True])
def test_show_live_035_public_show_page_injects_auth_aware_annotation_config(
    monkeypatch,
    tmp_path,
    authenticated,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _FakeShowRuntimeManager(
        body=b'<!doctype html><html><head></head><body><div id="root"></div><script type="module" src="/src/main.tsx"></script></body></html>'
    )
    set_show_runtime_manager_for_tests(manager)
    client = app.test_client()
    if authenticated:
        client.set_cookie(
            remote_access.SESSION_COOKIE_NAME,
            _active_org_cookie(config, "alex@example.com", "user-1"),
            domain="alex.avibe.bot",
        )
    try:
        response = client.get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.headers.get("location") is None
    assert manager.calls[0][2]["X-Avibe-Show-Context"] == "shared"
    body = response.content.decode("utf-8")
    base_path = f"/p/{share_id}/"
    assert f'"sessionId":"{share_id}"' in body
    assert '"sessionId":"ses123"' not in body
    assert f'"basePath":"{base_path}"' in body
    assert f'"eventsPath":"{base_path}__show/events"' in body
    assert f'"streamPath":"{base_path}__show/events?stream=1"' in body
    expected_auth = "true" if authenticated else "false"
    assert f'"annotation":{{"authenticated":{expected_auth},"mePath":"__show/me"}}' in body
    assert "__AVIBE_PWA_NAVIGATE_SAME_ORIGIN__" in body
    assert f'<script type="module" src="{base_path}__show/annotation.js"></script>' in body
    assert '"writeToken"' not in body
    assert body.index('/src/main.tsx') < body.index(f'{base_path}__show/annotation.js')
    assert response.headers["Referrer-Policy"] == "same-origin"


@pytest.mark.parametrize("surface", ["private", "public"])
def test_show_annotation_bootstrap_asset_proxies_to_runtime(monkeypatch, tmp_path, surface):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    if surface == "private":
        _create_show_page("ses123", "private")
        path = "/show/ses123/__show/annotation.js"
    else:
        share_id = _create_show_page("ses123", "public")
        path = f"/p/{share_id}/__show/annotation.js"
    manager = _FakeShowRuntimeManager(
        body=b"export const mounted = true;",
        extra_headers={"content-type": "text/javascript", "cache-control": "no-cache"},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(path, base_url="http://127.0.0.1:5123")
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b"export const mounted = true;"
    assert manager.calls[0][1] == "/sessions/ses123/app/__show/annotation.js"


@pytest.mark.parametrize("surface", ["private", "public"])
@pytest.mark.parametrize("runtime_path", ["__show/annotation.js", "__show/runtime-probe"])
def test_show_protocol_transport_failure_remains_runtime_unavailable(
    monkeypatch,
    tmp_path,
    surface,
    runtime_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    if surface == "private":
        _create_show_page("ses123", "private")
        path = f"/show/ses123/{runtime_path}"
    else:
        share_id = _create_show_page("ses123", "public")
        path = f"/p/{share_id}/{runtime_path}"
    manager = _FakeShowRuntimeManager(fail=True)
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(path, base_url="http://127.0.0.1:5123")
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 503
    assert response.get_json() == {
        "error": "show_runtime_unavailable",
        "reason": "runtime_proxy_failed",
    }
    assert manager.calls[0][4] is None


def test_private_show_page_does_not_inject_runtime_event_config_into_attachment_html(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: f"token-{session_id}")
    body = b'<!doctype html><script type="module" src="/src/main.tsx"></script>'
    manager = _FakeShowRuntimeManager(
        body=body,
        extra_headers={
            "content-type": "text/html; charset=utf-8",
            "content-disposition": 'attachment; filename="report.html"',
        },
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get("/show/ses123/report.html", base_url="http://127.0.0.1:5123")
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == body
    assert "globalThis.__AVIBE_SHOW__" not in response.content.decode("utf-8")
    assert response.headers["content-disposition"] == 'attachment; filename="report.html"'


def test_private_show_page_does_not_inject_runtime_event_config_into_ranged_html(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: f"token-{session_id}")
    body = b'<!doctype html><script type="module" src="/src/main.tsx"></script>'
    manager = _FakeShowRuntimeManager(
        body=body,
        status_code=206,
        extra_headers={
            "content-type": "text/html; charset=utf-8",
            "content-range": "bytes 0-63/128",
            "accept-ranges": "bytes",
        },
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/",
            base_url="http://127.0.0.1:5123",
            headers={"Range": "bytes=0-63"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 206
    assert response.content == body
    assert "globalThis.__AVIBE_SHOW__" not in response.content.decode("utf-8")
    assert response.headers["content-range"] == "bytes 0-63/128"
    assert manager.calls[0][2]["range"] == "bytes=0-63"


def test_private_show_page_runtime_config_overrides_existing_client_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: f"token-{session_id}")
    manager = _FakeShowRuntimeManager(
        body=b'<!doctype html><script>globalThis.__AVIBE_SHOW__={eventsPath:"runtime-only"}</script><script type="module" src="/src/main.tsx"></script>'
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get("/show/ses123/app/dashboard", base_url="http://127.0.0.1:5123")
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    body = response.content.decode("utf-8")
    assert '"eventsPath":"/show/ses123/__show/events"' in body
    assert '"writeToken":"token-ses123"' in body


def test_public_show_runtime_source_rewrites_private_runtime_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _FakeShowRuntimeManager(
        body=(
            b'import "/show/ses123/@vite/client";\n'
            b'import "/show/ses123/@react-refresh";\n'
            b'const socketPath = "/show/ses123/__vite_hmr";\n'
        ),
        extra_headers={
            "content-type": "text/javascript",
            "cache-control": "no-cache",
            "etag": "source-etag",
        },
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/src/App.tsx?t=1780732068677",
            base_url="http://127.0.0.1:5123",
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert b'"/_show-runtime/client-shim-v1.js"' in response.content
    assert b'"/_show-runtime/react-refresh-shim-v1.js"' in response.content
    assert f'"/p/{share_id}/@vite/client"'.encode() not in response.content
    assert f'"/p/{share_id}/@react-refresh"'.encode() not in response.content
    assert f'"/p/{share_id}/__vite_hmr"'.encode() in response.content
    assert b'"/show/ses123/' not in response.content
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers


def test_public_show_runtime_html_rewrites_private_runtime_client_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _FakeShowRuntimeManager(
        body=(
            b'<script type="module">import { injectIntoGlobalHook } from "/show/ses123/@react-refresh";</script>'
            b'<script type="module" src="/show/ses123/@vite/client"></script>'
            b'<script type="module" src="/show/ses123/src/main.tsx"></script>'
        ),
        extra_headers={
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-cache",
            "etag": "source-etag",
        },
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/",
            base_url="http://127.0.0.1:5123",
            headers={"Accept": "text/html"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert b'"/_show-runtime/client-shim-v1.js"' in response.content
    assert b'"/_show-runtime/react-refresh-shim-v1.js"' in response.content
    assert f'"/p/{share_id}/src/main.tsx"'.encode() in response.content
    assert b'"/show/ses123/' not in response.content
    assert f'"/p/{share_id}/@vite/client"'.encode() not in response.content
    assert f'"/p/{share_id}/@react-refresh"'.encode() not in response.content
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers


def test_public_show_runtime_css_rewrites_private_runtime_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _FakeShowRuntimeManager(
        body=(
            b'@import "/show/ses123/src/theme.css";\n'
            b'@font-face { src: url("/show/ses123/assets/font.woff2") format("woff2"); }\n'
        ),
        extra_headers={
            "content-type": "text/css; charset=utf-8",
            "cache-control": "no-cache",
            "etag": "source-etag",
        },
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/src/styles.css",
            base_url="http://127.0.0.1:5123",
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert f'@import "/p/{share_id}/src/theme.css"'.encode() in response.content
    assert f'url("/p/{share_id}/assets/font.woff2")'.encode() in response.content
    assert b"/show/ses123/" not in response.content
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers


def test_show_runtime_public_client_shims_are_cacheable():
    client = app.test_client()
    vite_client = client.get("/_show-runtime/client-shim-v1.js", base_url="http://127.0.0.1:5123")
    react_refresh = client.get("/_show-runtime/react-refresh-shim-v1.js", base_url="http://127.0.0.1:5123")

    assert vite_client.status_code == 200
    assert react_refresh.status_code == 200
    assert vite_client.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert react_refresh.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert b"export function createHotContext" in vite_client.content
    assert b"export function injectIntoGlobalHook" in react_refresh.content
    assert b"createSignatureFunctionForTransform" in react_refresh.content
    assert b"performReactRefresh" in react_refresh.content
    assert b"__hmr_import" in react_refresh.content
    assert b"validateRefreshBoundaryAndEnqueueUpdate" in react_refresh.content


def test_public_show_runtime_direct_client_paths_return_shims(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _FakeShowRuntimeManager(body=b"real vite client")
    set_show_runtime_manager_for_tests(manager)
    try:
        vite_client = app.test_client().get(
            f"/p/{share_id}/@vite/client",
            base_url="http://127.0.0.1:5123",
        )
        react_refresh = app.test_client().get(
            f"/p/{share_id}/@react-refresh",
            base_url="http://127.0.0.1:5123",
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert vite_client.status_code == 200
    assert react_refresh.status_code == 200
    assert b"export function createHotContext" in vite_client.content
    assert b"export function injectIntoGlobalHook" in react_refresh.content
    assert manager.calls == []


def test_public_show_page_does_not_inject_write_runtime_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: f"token-{session_id}")
    manager = _FakeShowRuntimeManager(
        body=b'<!doctype html><html><body><script type="module" src="/src/main.tsx"></script></body></html>'
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/",
            base_url="http://127.0.0.1:5123",
            headers={"Accept": "text/html"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    body = response.content.decode("utf-8")
    assert "globalThis.__AVIBE_SHOW__=Object.assign" in body
    assert '"writeToken"' not in body
    assert "token-ses123" not in body


def test_private_show_page_falls_back_to_runtime_recovery(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr("core.show_git.show_git_checkpointing_active", lambda: True)
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    set_show_runtime_manager_for_tests(
        _FakeShowRuntimeManager(fail=True, failure_reason="runtime_archive_download_failed")
    )
    try:
        with caplog.at_level("WARNING"):
            response = app.test_client().get("/show/ses123/", base_url="http://127.0.0.1:5123")
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.headers["X-Avibe-Show-Recovery"] == "1"
    assert response.headers["X-Avibe-Show-Recovery-Reason"] == "runtime_archive_download_failed"
    assert response.headers["X-Avibe-Show-Recovery-Class"] == "transient"
    assert b"The Show Runtime is unavailable" in response.content
    assert b"vibe doctor repair show-runtime" in response.content
    assert b"runtime_archive_download_failed" in response.content
    assert b"X-Avibe-Show-Recovery-Poll" not in response.content
    assert b"window.setInterval" not in response.content
    assert b"window.setTimeout" not in response.content
    assert response.content.count(b"fetch(window.location.href") == 1
    assert b"document.write(html)" in response.content
    assert b"(() => {" in response.content
    assert b"})();" in response.content
    assert b'"Accept": "text/html"' in response.content
    assert b"response.redirected || !response.ok || !contentType.includes(\"text/html\")" in response.content
    assert b"window.location.assign(response.url || window.location.href)" in response.content
    assert response.content.index(b"response.redirected") < response.content.index(b"response.text()")
    assert b"window.location.reload" not in response.content
    assert b"X-Avibe-Show-Recovery-Retry" in response.content
    assert b"Math.min(30000, retryDelayMs * 2)" not in response.content
    assert b"checksRemaining" not in response.content
    assert b'|| "runtime_unavailable"' not in response.content
    assert b'|| "unclassified"' not in response.content
    assert b'|| "manual_only"' not in response.content
    assert b"src/App.tsx" not in response.content
    assert b'src="./src/main.tsx"' not in response.content
    assert "Show runtime unavailable (runtime_archive_download_failed)" in caplog.text


def test_show_recovery_retry_now_is_explicit_intent(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager(fail=True, failure_reason="runtime_start_health_timeout")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/",
            base_url="http://127.0.0.1:5123",
            headers={"X-Avibe-Show-Recovery-Retry": "1"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert manager.automatic_calls == [False]
    assert b"runtime_start_health_timeout" in response.content
    assert b"vibe doctor repair show-runtime" in response.content


@pytest.mark.parametrize("surface", ("public", "private-viewer"))
def test_show_recovery_retry_header_cannot_bypass_for_viewers(monkeypatch, tmp_path, surface):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    visibility = "public" if surface == "public" else "private"
    share_id = _create_show_page("ses123", visibility)
    manager = _FakeShowRuntimeManager(fail=True, failure_reason="runtime_start_health_timeout")
    set_show_runtime_manager_for_tests(manager)
    client = app.test_client()
    if surface == "private-viewer":
        client.set_cookie(
            remote_access.SESSION_COOKIE_NAME,
            _active_org_cookie(config, "viewer@example.com", "viewer-1", role="viewer"),
            domain="alex.avibe.bot",
        )
        path = "/show/ses123/"
    else:
        path = f"/p/{share_id}/"
    try:
        headers = {"X-Avibe-Show-Recovery-Retry": "1"}
        if surface == "public":
            headers["Accept"] = "text/html"
        response = client.get(
            path,
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers=headers,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert manager.automatic_calls == [True]
    assert b"show-runtime-retry-now" not in response.content
    assert b"vibe doctor repair show-runtime" not in response.content
    assert b"X-Avibe-Show-Recovery-Poll" not in response.content
    assert b"Reload this page to try the request again" in response.content


@pytest.mark.parametrize(
    ("reason", "language", "expected"),
    (
        ("VIBE_SHOW_RUNTIME_AUTO_INSTALL", "en", "Automatic Show Runtime installation is disabled"),
        ("VIBE_INSTALL_SKIP_SHOW_RUNTIME", "zh", "配置已关闭 Show Runtime 自动安装"),
    ),
)
def test_show_recovery_projects_policy_owner_evidence(monkeypatch, tmp_path, reason, language, expected):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.delenv("VIBE_INSTALL_SKIP_SHOW_RUNTIME", raising=False)
    if reason == "VIBE_INSTALL_SKIP_SHOW_RUNTIME":
        monkeypatch.setenv("VIBE_INSTALL_SKIP_SHOW_RUNTIME", "1")
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
        auto_install=reason != "VIBE_SHOW_RUNTIME_AUTO_INSTALL",
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/",
            base_url="http://127.0.0.1:5123",
            headers={"Accept-Language": language},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    body = response.content.decode("utf-8")
    assert expected in body
    assert reason in body
    assert "runtime_proxy_failed" not in body
    assert "show-runtime-retry-now" in body


@pytest.mark.parametrize(
    ("language", "expected_heading", "expected_status"),
    [
        (
            "en",
            "No action is available here",
            "No action on this machine can change it",
        ),
        (
            "zh",
            "此处没有可用操作",
            "这台机器上的任何操作都无法改变这一结果",
        ),
    ],
)
def test_unsupported_platform_recovery_offers_no_impossible_local_action(
    monkeypatch,
    tmp_path,
    language,
    expected_heading,
    expected_status,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    set_show_runtime_manager_for_tests(
        _FakeShowRuntimeManager(fail=True, failure_reason="runtime_platform_unsupported")
    )
    try:
        response = app.test_client().get(
            "/show/ses123/",
            base_url="http://127.0.0.1:5123",
            headers={"Accept-Language": language},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    body = response.content.decode("utf-8")
    assert expected_heading in body
    assert expected_status in body
    assert "vibe doctor repair show-runtime" not in body
    assert "show-runtime-retry-now" not in body
    assert "X-Avibe-Show-Recovery-Retry" not in body


def test_show_runtime_recovery_uses_request_language(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    set_show_runtime_manager_for_tests(
        _FakeShowRuntimeManager(fail=True, failure_reason="runtime_archive_unavailable_offline")
    )
    try:
        response = app.test_client().get(
            "/show/ses123/",
            base_url="http://127.0.0.1:5123",
            headers={"Accept-Language": "zh-CN,zh;q=0.9"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    body = response.content.decode("utf-8")
    assert '<html lang="zh" data-show-runtime-reason="runtime_archive_unavailable_offline"' in body
    assert "Avibe 当前处于离线模式" in body
    assert "修改下方所示的设置或前置条件" in body


def test_private_show_page_recovery_does_not_blame_page_source_without_git(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr("core.show_git.show_git_checkpointing_active", lambda: False)
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    set_show_runtime_manager_for_tests(_FakeShowRuntimeManager(fail=True))
    try:
        response = app.test_client().get("/show/ses123/", base_url="http://127.0.0.1:5123")
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert b"History is saved automatically around each turn" not in response.content
    assert b"git restore --source" not in response.content
    assert b"src/App.tsx" not in response.content


def test_private_show_page_recovery_does_not_emit_history_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr("core.show_git.show_git_checkpointing_active", lambda: True)
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    (paths.get_show_pages_dir() / "ses123" / ".git").mkdir()
    set_show_runtime_manager_for_tests(_FakeShowRuntimeManager(fail=True))
    try:
        response = app.test_client().get("/show/ses123/", base_url="http://127.0.0.1:5123")
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert b"History is saved automatically around each turn" not in response.content
    assert b"Restore only via" not in response.content
    assert b"vibe doctor repair show-runtime" in response.content


def test_private_show_page_static_fallback_denies_dot_leading_segments(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    page_dir = paths.get_show_pages_dir() / "ses123"
    (page_dir / ".git").mkdir()
    (page_dir / ".git" / "HEAD").write_text("private history", encoding="utf-8")
    (page_dir / "assets" / ".draft").mkdir(parents=True)
    (page_dir / "assets" / ".draft" / "secret.txt").write_text("private draft", encoding="utf-8")
    set_show_runtime_manager_for_tests(_FakeShowRuntimeManager(fail=True))
    try:
        client = app.test_client()
        git_response = client.get("/show/ses123/.git/HEAD", base_url="http://127.0.0.1:5123")
        nested_response = client.get(
            "/show/ses123/assets/.draft/secret.txt",
            base_url="http://127.0.0.1:5123",
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert git_response.status_code == 404
    assert nested_response.status_code == 404
    assert b"private history" not in git_response.content
    assert b"private draft" not in nested_response.content


def test_private_show_page_denies_dot_path_before_runtime_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    (paths.get_show_pages_dir() / "ses123" / ".git").write_text(
        "gitdir: /tmp/show-git/ses123.git\n",
        encoding="utf-8",
    )
    manager = _FakeShowRuntimeManager(body=b"leaked pointer")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get("/show/ses123/.git", base_url="http://127.0.0.1:5123")
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 404
    assert b"show-git" not in response.content
    assert manager.calls == []


def test_private_show_page_proxies_vite_dependency_dot_path(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager(
        body=b"export const react = true",
        extra_headers={"content-type": "text/javascript"},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/node_modules/.vite/deps/react.js",
            base_url="http://127.0.0.1:5123",
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b"export const react = true"
    assert manager.calls[0][1] == "/sessions/ses123/app/node_modules/.vite/deps/react.js"


def test_private_show_page_proxies_root_vite_dependency_dot_path(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager(
        body=b"export const react = true",
        extra_headers={"content-type": "text/javascript"},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/.vite/deps/react.js",
            base_url="http://127.0.0.1:5123",
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b"export const react = true"
    assert manager.calls[0][1] == "/sessions/ses123/app/.vite/deps/react.js"


def test_private_show_page_denies_sensitive_file_before_runtime_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager(body=b"private key")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/config/server.key",
            base_url="http://127.0.0.1:5123",
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 404
    assert manager.calls == []


def test_private_show_page_proxies_workspace_at_fs_path_below_dot_home(monkeypatch, tmp_path):
    avibe_home = tmp_path / ".avibe"
    monkeypatch.setenv("AVIBE_HOME", str(avibe_home))
    _save_config(avibe_home)
    _create_show_page("ses123", "private")
    source_path = paths.get_show_page_dir("ses123") / "src" / "App.tsx"
    manager = _FakeShowRuntimeManager(
        body=b"export default function App() {}",
        extra_headers={"content-type": "text/javascript"},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/show/ses123/@fs/{source_path.as_posix()}",
            base_url="http://127.0.0.1:5123",
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b"export default function App() {}"
    assert manager.calls
    assert manager.calls[0][1].endswith(f"/@fs/{source_path.as_posix()}")


def test_private_show_page_denies_workspace_dot_path_through_at_fs(monkeypatch, tmp_path):
    avibe_home = tmp_path / ".avibe"
    monkeypatch.setenv("AVIBE_HOME", str(avibe_home))
    _save_config(avibe_home)
    _create_show_page("ses123", "private")
    hidden_path = paths.get_show_page_dir("ses123") / ".draft" / "secret.ts"
    manager = _FakeShowRuntimeManager(body=b"export const secret = true")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/show/ses123/@fs/{hidden_path.as_posix()}",
            base_url="http://127.0.0.1:5123",
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 404
    assert manager.calls == []


def test_private_show_page_proxies_single_slash_at_fs_external_dep(monkeypatch, tmp_path):
    # Real Vite emits `/@fs/<abs>` with a SINGLE slash (e.g. the HMR client's
    # env.mjs under the runtime's node_modules). The gate must treat it as an
    # absolute path and, being outside the workspace, defer to the runtime's own
    # allowlist. Use a dep under a custom hidden runtime root (an nvm/global-bin
    # provider), NOT the default `~/.avibe/runtime`, so the gate cannot rely on a
    # hardcoded root. Previously `removeprefix("@fs/")` dropped the leading slash,
    # mis-read it as relative, and denied it — which blanked the private /show/
    # surface (react-refresh preamble could not load env.mjs).
    avibe_home = tmp_path / ".avibe"
    monkeypatch.setenv("AVIBE_HOME", str(avibe_home))
    _save_config(avibe_home)
    _create_show_page("ses123", "private")
    dep_path = (
        tmp_path / ".nvm" / "versions" / "node" / "v20" / "lib" / "node_modules"
        / "vite" / "dist" / "client" / "env.mjs"
    )
    manager = _FakeShowRuntimeManager(
        body=b"export const context = {}",
        extra_headers={"content-type": "text/javascript"},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        # Single slash: "@fs" + an absolute posix path -> ".../@fs/private/...".
        response = app.test_client().get(
            f"/show/ses123/@fs{dep_path.as_posix()}",
            base_url="http://127.0.0.1:5123",
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b"export const context = {}"
    assert manager.calls
    assert manager.calls[0][1].endswith(f"/@fs{dep_path.as_posix()}")


def test_private_show_page_denies_single_slash_at_fs_workspace_dot_path(monkeypatch, tmp_path):
    # The single-slash normalization must still deny a workspace-relative dot path
    # reached through @fs (a hidden draft), not only the double-slash spelling.
    avibe_home = tmp_path / ".avibe"
    monkeypatch.setenv("AVIBE_HOME", str(avibe_home))
    _save_config(avibe_home)
    _create_show_page("ses123", "private")
    hidden_path = paths.get_show_page_dir("ses123") / ".draft" / "secret.ts"
    manager = _FakeShowRuntimeManager(body=b"export const secret = true")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/show/ses123/@fs{hidden_path.as_posix()}",
            base_url="http://127.0.0.1:5123",
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 404
    assert manager.calls == []


def test_private_show_page_proxies_relative_relocated_vite_cache_at_fs(monkeypatch, tmp_path):
    # The synthetic relative relocated-cache form `@fs/.vite-cache/deps/...` must
    # stay allowed (proxied). The normalization must NOT force it to an absolute
    # path (`/.vite-cache/...`) that then looks like an out-of-tree request; it is
    # recognized as a relocated Vite dep and passed through to the runtime.
    avibe_home = tmp_path / ".avibe"
    monkeypatch.setenv("AVIBE_HOME", str(avibe_home))
    _save_config(avibe_home)
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager(
        body=b"export const react = true",
        extra_headers={"content-type": "text/javascript"},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/@fs/.vite-cache/deps/react.js",
            base_url="http://127.0.0.1:5123",
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b"export const react = true"
    assert manager.calls
    assert manager.calls[0][1].endswith("/@fs/.vite-cache/deps/react.js")


def test_public_show_page_denies_at_fs_workspace_symlink_escape(monkeypatch, tmp_path):
    # A workspace file that symlinks OUT of the workspace must NOT be served on the
    # public surface — otherwise a share link could read any host file the service
    # can read. It is denied before proxying. (The private surface keeps it; below.)
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    workspace = paths.get_show_page_dir("ses123")
    secret = tmp_path / "outside_secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")
    link = workspace / "evil.txt"
    os.symlink(secret, link)
    manager = _FakeShowRuntimeManager(body=b"TOPSECRET")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/@fs{link.as_posix()}",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 404
    assert manager.calls == []


def test_public_show_page_allows_at_fs_dependency_outside_workspace(monkeypatch, tmp_path):
    # The public confinement targets workspace symlink escapes only; a genuine
    # dependency @fs path (its parent is literally outside the workspace) is still
    # deferred to the runtime, so public pages keep loading their deps.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    dep = tmp_path / "runtime" / "node_modules" / "vite" / "dist" / "client" / "env.mjs"
    dep.parent.mkdir(parents=True, exist_ok=True)
    dep.write_text("export const x = 1", encoding="utf-8")
    manager = _FakeShowRuntimeManager(
        body=b"export const x = 1", extra_headers={"content-type": "text/javascript"}
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/@fs{dep.as_posix()}",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert manager.calls


class _FakeShowEventStore:
    def __init__(self, events):
        self._events = events

    def list(self, session_id, after_id=None, limit=100):
        return {"events": self._events, "next_after_id": None}

    def close(self):
        return None


def _screenshot_event(local_path: str) -> dict:
    return {
        "id": "evt-1",
        "session_id": "ses123",
        "transcript_text": f"Annotation at {local_path}",
        "payload": {
            "screenshot": {
                "attachmentId": "med_1",
                "path": local_path,
                "mimeType": "image/png",
            }
        },
    }


def test_remote_owner_can_read_private_show_events(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_show_page("ses123", "private")
    local_path = str(tmp_path / "state" / "media" / "shot.png")
    monkeypatch.setattr(
        "vibe.ui_server._show_session_event_store",
        lambda: _FakeShowEventStore([_screenshot_event(local_path)]),
    )
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "owner-1"),
        domain="alex.avibe.bot",
    )

    response = client.get(
        "/show/ses123/__show/events",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 200
    event = response.get_json()["events"][0]
    assert "path" not in event["payload"]["screenshot"]
    assert event["payload"]["screenshot"]["attachmentId"] == "med_1"


def test_local_private_show_events_keep_the_local_screenshot_path(monkeypatch, tmp_path):
    # Local Owner authoring tools still read the materialized file directly.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    local_path = str(tmp_path / "state" / "media" / "shot.png")
    monkeypatch.setattr(
        "vibe.ui_server._show_session_event_store",
        lambda: _FakeShowEventStore([_screenshot_event(local_path)]),
    )

    response = app.test_client().get(
        "/show/ses123/__show/events",
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 200
    assert response.json()["events"][0]["payload"]["screenshot"]["path"] == local_path


def test_remote_private_show_page_denies_at_fs_workspace_symlink_escape(monkeypatch, tmp_path):
    # A REMOTE viewer of a private page is an untrusted viewer: the workspace
    # symlink escape the local author may use must not serve out-of-Project disk
    # files across the tunnel.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_show_page("ses123", "private")
    workspace = paths.get_show_page_dir("ses123")
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("TOPSECRET", encoding="utf-8")
    link = workspace / "pwn.txt"
    os.symlink(outside, link)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "owner@example.com", "owner-1"),
        domain="alex.avibe.bot",
    )
    manager = _FakeShowRuntimeManager(body=b"TOPSECRET")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = client.get(
            f"/show/ses123/@fs{link.as_posix()}",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 404
    assert b"TOPSECRET" not in response.content
    # The Show Runtime was never asked for the escaping path.
    assert manager.calls == []


def test_private_show_page_allows_at_fs_workspace_symlink(monkeypatch, tmp_path):
    # The private authoring surface intentionally allows a workspace symlink to a
    # disk file (a supported feature) for local Owner authors. Public and remote
    # viewers are confined to the workspace.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    workspace = paths.get_show_page_dir("ses123")
    data = tmp_path / "outside_data.txt"
    data.write_text("linked data", encoding="utf-8")
    link = workspace / "data.txt"
    os.symlink(data, link)
    manager = _FakeShowRuntimeManager(
        body=b"linked data", extra_headers={"content-type": "text/plain"}
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/show/ses123/@fs{link.as_posix()}",
            base_url="http://127.0.0.1:5123",
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert manager.calls


def test_public_show_page_denies_at_fs_workspace_dir_symlink_escape(monkeypatch, tmp_path):
    # A symlinked DIRECTORY inside the workspace (assets -> outside) must be confined
    # on the public surface too, not only symlinked files.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    workspace = paths.get_show_page_dir("ses123")
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("TOPSECRET", encoding="utf-8")
    os.symlink(outside_dir, workspace / "assets")
    manager = _FakeShowRuntimeManager(body=b"TOPSECRET")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/@fs{(workspace / 'assets' / 'secret.txt').as_posix()}",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 404
    assert manager.calls == []


def test_public_show_page_denies_at_fs_vite_cache_named_workspace_symlink(monkeypatch, tmp_path):
    # A workspace path that merely contains `vite-cache/deps` must not skip the
    # public symlink confinement: the relocated-cache exception no longer bypasses
    # the absolute @fs checks.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    workspace = paths.get_show_page_dir("ses123")
    (workspace / "vite-cache" / "deps").mkdir(parents=True)
    secret = tmp_path / "cache_secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")
    link = workspace / "vite-cache" / "deps" / "link.js"
    os.symlink(secret, link)
    manager = _FakeShowRuntimeManager(body=b"TOPSECRET")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/@fs{link.as_posix()}",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 404
    assert manager.calls == []


def test_public_show_page_denies_at_fs_symlinked_home_ancestor_escape(monkeypatch, tmp_path):
    # AVIBE_HOME reached through a symlinked ancestor: a request spelled with the
    # UNRESOLVED (symlink) workspace prefix whose real target escapes must still be
    # denied — the confinement checks both the resolved and unresolved spelling.
    real_home = tmp_path / "real_home"
    real_home.mkdir()
    link_home = tmp_path / "link_home"
    os.symlink(real_home, link_home)
    monkeypatch.setenv("AVIBE_HOME", str(link_home))
    _save_config(link_home)
    share_id = _create_show_page("ses123", "public")
    workspace = paths.get_show_page_dir("ses123")  # unresolved link_home spelling
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("TOPSECRET", encoding="utf-8")
    os.symlink(outside, workspace / "pwn.txt")
    manager = _FakeShowRuntimeManager(body=b"TOPSECRET")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/@fs{(workspace / 'pwn.txt').as_posix()}",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 404
    assert manager.calls == []


def test_public_show_page_denies_at_fs_extra_leading_slash_symlink_escape(monkeypatch, tmp_path):
    # An `@fs///<ws>/x` request (one extra slash) must not dodge the workspace
    # confinement: redundant leading slashes are collapsed before the prefix check.
    # Assert on the gate directly so the exact `//` spelling reaches it regardless
    # of any client URL normalization.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "public")
    workspace = paths.get_show_page_dir("ses123")
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("TOPSECRET", encoding="utf-8")
    os.symlink(outside, workspace / "pwn.txt")

    decoded = f"@fs//{workspace.as_posix()}/pwn.txt"  # `@fs///<ws>/pwn.txt`
    assert ui_server._is_show_page_runtime_denied_path(
        decoded, session_id="ses123", confine_to_workspace=True
    )
    # The same request stays allowed for a local Owner author.
    assert not ui_server._is_show_page_runtime_denied_path(
        decoded, session_id="ses123", confine_to_workspace=False
    )


def test_show_page_terminal_runtime_failure_renders_immediately(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    set_show_runtime_manager_for_tests(
        _FakeShowRuntimeManager(fail=True, failure_reason="runtime_node_unsupported")
    )
    try:
        response = app.test_client().get("/show/ses123/", base_url="http://127.0.0.1:5123")
    finally:
        set_show_runtime_manager_for_tests(None)

    body = response.content.decode("utf-8")
    assert "show-recovery-loading-out 0.18s ease 0s forwards" in body
    assert "show-recovery-panel-in 0.22s ease 0s forwards" in body
    assert f"ease {SHOW_PAGE_RUNTIME_RECOVERY_LOADING_DELAY_SECONDS}s forwards" not in body
    assert "The installed Node.js runtime is missing or is not supported." in body


def test_private_show_page_api_does_not_fall_back_to_static(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    (paths.get_show_pages_dir() / "ses123" / "api" / "health.ts").write_text("export const secret = true\n", encoding="utf-8")
    set_show_runtime_manager_for_tests(_FakeShowRuntimeManager(fail=True))
    try:
        response = app.test_client().get("/show/ses123/api/health.ts", base_url="http://127.0.0.1:5123")
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 503
    assert response.get_json()["error"] == "show_runtime_unavailable"
    assert response.get_json()["reason"] == "runtime_proxy_failed"
    assert b"secret" not in response.content


def test_private_show_page_proxies_runtime_api_methods(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager(body=b'{"ok":true}', extra_headers={"content-type": "application/json"})
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().post(
            "/show/ses123/api/health",
            base_url="http://127.0.0.1:5123",
            headers={
                "Origin": "http://127.0.0.1:5123",
                "Content-Type": "application/json",
                "Cookie": "__Host-vibe_remote_session=secret",
            },
            content=b'{"ping":true}',
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b'{"ok":true}'
    assert manager.calls[0][0] == "POST"
    assert manager.calls[0][1] == "/sessions/ses123/app/api/health"
    assert manager.calls[0][2]["content-type"] == "application/json"
    assert manager.calls[0][2]["X-Avibe-Show-Protocol"] == "1"
    assert manager.calls[0][2]["X-Avibe-Show-Context"] == "private"
    assert "cookie" not in manager.calls[0][2]
    assert manager.calls[0][3] == b'{"ping":true}'
    assert manager.calls[0][4] == 90.0
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "x-runtime-private-header" not in response.headers
    assert "__Host-vibe_remote_session=attacker" not in response.headers.get("set-cookie", "")


def test_private_show_page_api_uses_live_v2_config_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.runtime.show_page_api_timeout_seconds = 12.5
    config.save()
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager(body=b'{"ok":true}')
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/api/slow",
            base_url="http://127.0.0.1:5123",
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert manager.calls[0][4] == 12.5


@pytest.mark.parametrize("public", [False, True])
def test_show_page_api_timeout_is_distinct_from_runtime_unavailable(
    monkeypatch,
    tmp_path,
    public,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public" if public else "private")
    timeout = ShowRuntimeRequestTimeoutError("Show Runtime request exceeded 90 seconds")
    manager = _FakeShowRuntimeManager(error=timeout)
    set_show_runtime_manager_for_tests(manager)
    try:
        if public:
            response = app.test_client().get(
                f"/p/{share_id}/api/slow",
                base_url="https://alex.avibe.bot",
                environ_base=_remote_peer(),
            )
        else:
            response = app.test_client().get(
                "/show/ses123/api/slow",
                base_url="http://127.0.0.1:5123",
            )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 504
    assert response.get_json() == {"error": "show_runtime_request_timeout"}


def test_show_page_api_transport_failure_uses_manager_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _runtime_manager_with_failing_transport(monkeypatch, tmp_path)
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/api/slow",
            base_url="http://127.0.0.1:5123",
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 503
    assert response.get_json() == {
        "error": "show_runtime_unavailable",
        "reason": "runtime_proxy_failed",
    }


@pytest.mark.parametrize(
    ("accept_language", "reload_copy"),
    (
        ("en-US,en;q=0.9", "Reload this page to try the request again."),
        ("zh-CN,zh;q=0.9", "请重新加载此页面，再次尝试请求"),
    ),
)
def test_public_show_transport_failure_remains_manually_retryable(
    monkeypatch,
    tmp_path,
    accept_language,
    reload_copy,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _runtime_manager_with_failing_transport(monkeypatch, tmp_path)
    set_show_runtime_manager_for_tests(manager)
    try:
        first = app.test_client().get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html", "Accept-Language": accept_language},
        )
        response = app.test_client().get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html", "Accept-Language": accept_language},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    body = response.content.decode("utf-8")
    first_body = first.content.decode("utf-8")
    assert first.headers["X-Avibe-Show-Recovery-Reason"] == "runtime_proxy_failed"
    assert "X-Avibe-Show-Recovery-Poll" not in first_body
    assert response.status_code == 200
    assert response.headers["X-Avibe-Show-Recovery-Reason"] == "runtime_command_missing"
    assert response.headers["X-Avibe-Show-Recovery-Class"] == "configured"
    assert "X-Avibe-Show-Recovery-Poll" not in body
    assert "show-runtime-retry-now" not in body
    assert "vibe doctor repair show-runtime" not in body
    assert reload_copy in body


def test_show_page_does_not_fabricate_recovery_evidence_for_programming_errors(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    set_show_runtime_manager_for_tests(
        _FakeShowRuntimeManager(error=RuntimeError("unowned failure"))
    )
    try:
        response = app.test_client().get(
            "/show/ses123/",
            base_url="http://127.0.0.1:5123",
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 500
    assert response.headers.get("X-Avibe-Show-Recovery") is None
    assert b"runtime_proxy_failed" not in response.content


def test_private_show_page_records_show_event(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    token = "session-write-token"
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: token)
    published = []
    monkeypatch.setattr("vibe.sse_broker.broker.publish", lambda event_type, data: published.append((event_type, data)))

    response = app.test_client().post(
        "/show/ses123/__show/events",
        base_url="http://127.0.0.1:5123",
        headers={
            "Origin": "http://127.0.0.1:5123",
            "Content-Type": "application/json",
            "X-Vibe-Show-Token": token,
        },
        json={
            "type": "assistant.mark.created",
            "mark": {
                "target": "mark-default-summary",
                "body": "Review this summary.",
            },
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["event"]["type"] == "assistant.mark.created"
    assert payload["event"]["message_id"]
    assert "Review this summary." in payload["event"]["transcript_text"]
    assert [event_type for event_type, _data in published] == ["show.event", "message.new", "session.activity"]
    assert published[1][1]["id"] == payload["event"]["message_id"]
    assert published[2][1]["scope_id"] == payload["event"]["scope_id"]

    events_response = app.test_client().get("/show/ses123/__show/events", base_url="http://127.0.0.1:5123")
    assert events_response.status_code == 200
    assert events_response.get_json()["events"][0]["id"] == payload["event"]["id"]


def test_private_show_me_is_always_available(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")

    response = app.test_client().get(
        "/show/ses123/__show/me",
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "authenticated": True,
        "canAnnotate": True,
        "writeToken": show_event_write_token("ses123"),
    }
    assert response.headers["cache-control"] == "no-store, private"


def test_private_show_page_allows_instance_viewer_read_access(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager(
        body=b'<!doctype html><html><body><script type="module" src="/src/main.tsx"></script></body></html>'
    )
    set_show_runtime_manager_for_tests(manager)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "viewer@example.com",
            "user-viewer",
            role="viewer",
        ),
        domain="alex.avibe.bot",
    )
    try:
        me_response = client.get(
            "/show/ses123/__show/me",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
        page_response = client.get(
            "/show/ses123/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert me_response.status_code == 200
    assert me_response.get_json() == {
        "authenticated": False,
        "canAnnotate": False,
    }
    assert page_response.status_code == 200


def test_public_show_me_is_anonymous_without_oauth_session(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")

    response = app.test_client().get(
        f"/p/{share_id}/__show/me",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 200
    assert response.get_json() == {"authenticated": False, "canAnnotate": False}


def test_public_show_me_accepts_valid_workbench_session(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "alex@example.com", "user-1"),
        domain="alex.avibe.bot",
    )

    response = client.get(
        f"/p/{share_id}/__show/me",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "authenticated": True,
        "canAnnotate": True,
        "writeToken": show_public_event_write_token(share_id, "ses123"),
    }
    assert response.get_json()["writeToken"] != show_event_write_token("ses123")


def test_public_show_me_treats_viewer_as_read_only(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "viewer@example.com",
            "user-viewer",
            role="viewer",
        ),
        domain="alex.avibe.bot",
    )

    response = client.get(
        f"/p/{share_id}/__show/me",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 200
    assert response.get_json() == {"authenticated": False, "canAnnotate": False}


def test_public_show_me_treats_no_oauth_local_access_as_authenticated(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.enabled = False
    config.save()
    share_id = _create_show_page("ses123", "public")

    response = app.test_client().get(
        f"/p/{share_id}/__show/me",
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "authenticated": True,
        "canAnnotate": True,
        "writeToken": show_public_event_write_token(share_id, "ses123"),
    }


def test_private_show_page_rejects_mismatched_event_session_id(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    token = "session-write-token"
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: token)

    response = app.test_client().post(
        "/show/ses123/__show/events",
        base_url="http://127.0.0.1:5123",
        headers={
            "Origin": "http://127.0.0.1:5123",
            "Content-Type": "application/json",
            "X-Vibe-Show-Token": token,
        },
        json={
            "sessionId": "ses_other",
            "type": "human.annotation.created",
            "annotation": {"comment": "Wrong session."},
        },
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "session_mismatch"


def test_private_show_page_rejects_reused_event_id_with_different_contents(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.language = "zh"
    config.save()
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    token = "session-write-token"
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: token)
    headers = {
        "Origin": "http://127.0.0.1:5123",
        "Content-Type": "application/json",
        "X-Vibe-Show-Token": token,
    }
    original = {
        "id": "show_evt_payload_conflict",
        "type": "human.annotation.created",
        "annotation": {
            "intent": "comment",
            "comment": "Original annotation.",
            "dispatch": False,
        },
    }

    first = app.test_client().post(
        "/show/ses123/__show/events",
        base_url="http://127.0.0.1:5123",
        headers=headers,
        json=original,
    )
    conflict = app.test_client().post(
        "/show/ses123/__show/events",
        base_url="http://127.0.0.1:5123",
        headers=headers,
        json={
            **original,
            "annotation": {
                **original["annotation"],
                "comment": "Different annotation.",
            },
        },
    )

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "event_id_conflict"
    assert conflict.get_json()["error"] == "此 Show 事件 ID 已绑定到不同的事件内容。"


def test_private_show_page_idle_dispatch_promotes_visible_harness_row(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    token = "session-write-token"
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: token)
    published = []
    monkeypatch.setattr("vibe.sse_broker.broker.publish", lambda event_type, data: published.append((event_type, data)))
    dispatches = []
    dispatch_done = asyncio.Event()

    async def fake_dispatch_async(payload, **kwargs):
        dispatches.append(payload)
        _accept_dispatch(payload)
        dispatch_done.set()
        return {"status_code": 202, "body": {"ok": True}}

    with patch("vibe.internal_client.dispatch_async", fake_dispatch_async):
        response = app.test_client().post(
            "/show/ses123/__show/events",
            base_url="http://127.0.0.1:5123",
            headers={
                "Origin": "http://127.0.0.1:5123",
                "Content-Type": "application/json",
                "X-Vibe-Show-Token": token,
            },
            json={
                "type": "human.intent.submitted",
                "payload": {
                    "component": "decision",
                    "intent": "choose",
                    "value": "B",
                    "comment": "Pick B.",
                    "dispatch": True,
                },
            },
        )

    assert response.status_code == 201
    asyncio.run(asyncio.wait_for(dispatch_done.wait(), timeout=1))
    assert dispatches
    assert dispatches[0]["session_id"] == "ses123"
    assert "Pick B." in dispatches[0]["text"]
    assert dispatches[0]["user_message_id"] == response.get_json()["event"]["message_id"]
    assert dispatches[0]["files"] == []
    assert "dispatch_owner" not in dispatches[0]
    assert [event_type for event_type, _data in published] == ["show.event"]

    from storage import messages_service
    from storage.db import create_sqlite_engine

    with create_sqlite_engine().connect() as conn:
        transcript = messages_service.list_session_messages(
            conn,
            session_id="ses123",
            limit=50,
            types=messages_service.TRANSCRIPT_TYPES,
            tail=True,
        )
    assert [message["id"] for message in transcript["messages"]] == [
        response.get_json()["event"]["message_id"]
    ]


def test_private_show_page_waits_for_turn_acceptance_before_responding(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    token = "session-write-token"
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: token)
    dispatch_entered = threading.Event()
    release_dispatch = threading.Event()
    result = {}
    dispatch_kwargs = {}

    async def fake_dispatch_async(payload, **kwargs):
        dispatch_kwargs.update(kwargs)
        dispatch_entered.set()
        released = await asyncio.to_thread(release_dispatch.wait, 2)
        assert released
        _accept_dispatch(payload)
        return {"status_code": 202, "body": {"ok": True}}

    def post_event():
        result["response"] = app.test_client().post(
            "/show/ses123/__show/events",
            base_url="http://127.0.0.1:5123",
            headers={
                "Origin": "http://127.0.0.1:5123",
                "Content-Type": "application/json",
                "X-Vibe-Show-Token": token,
            },
            json={
                "type": "human.annotation.created",
                "annotation": {
                    "intent": "comment",
                    "comment": "Wait for queue acceptance.",
                    "dispatch": True,
                },
            },
        )

    with patch("vibe.internal_client.dispatch_async", fake_dispatch_async):
        request_thread = threading.Thread(target=post_event)
        request_thread.start()
        assert dispatch_entered.wait(1)
        assert request_thread.is_alive()
        release_dispatch.set()
        request_thread.join(2)

    assert not request_thread.is_alive()
    assert result["response"].status_code == 201
    assert result["response"].get_json()["event"]["message"]["type"] == "annotation"
    assert dispatch_kwargs == {"timeout": None}


def test_private_show_page_definitive_dispatch_rejection_retires_and_returns_502(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    token = "session-write-token"
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: token)
    published = []
    monkeypatch.setattr(
        "vibe.sse_broker.broker.publish",
        lambda event_type, data: published.append((event_type, data)),
    )

    async def fake_dispatch_async(payload, **kwargs):
        return {"status_code": 500, "body": {"ok": False}}

    with patch("vibe.internal_client.dispatch_async", fake_dispatch_async):
        response = app.test_client().post(
            "/show/ses123/__show/events",
            base_url="http://127.0.0.1:5123",
            headers={
                "Origin": "http://127.0.0.1:5123",
                "Content-Type": "application/json",
                "X-Vibe-Show-Token": token,
            },
            json={
                "type": "human.annotation.created",
                "annotation": {
                    "intent": "comment",
                    "comment": "Record but report failed delivery.",
                    "dispatch": True,
                },
            },
        )

    assert response.status_code == 502
    body = response.get_json()
    assert body["ok"] is False
    assert body["code"] == "show_event_dispatch_failed"
    # No turn started, so the retired submission remains outside the transcript.
    assert body["event"]["message"] is None
    assert body["event"]["message_id"] is None
    assert body["event"]["delivery"]["state"] == "retired"
    assert body["event"]["delivery"]["author_name"] == "show_annotation"
    assert [event_type for event_type, _data in published] == ["show.event"]


def test_private_show_page_concurrent_dispatch_replay_returns_reserved_delivery(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    token = "session-write-token"
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: token)

    async def already_in_flight(_event):
        return ui_server._ShowEventDispatchOutcome.IN_FLIGHT

    monkeypatch.setattr(
        "vibe.ui_server._run_show_event_dispatch",
        already_in_flight,
    )
    response = app.test_client().post(
        "/show/ses123/__show/events",
        base_url="http://127.0.0.1:5123",
        headers={
            "Origin": "http://127.0.0.1:5123",
            "Content-Type": "application/json",
            "X-Vibe-Show-Token": token,
        },
        json={
            "id": "show_evt_concurrent_http",
            "type": "human.annotation.created",
            "annotation": {
                "comment": "The first request still owns this dispatch.",
                "dispatch": True,
            },
        },
    )

    assert response.status_code == 202
    body = response.get_json()
    assert body["ok"] is True
    assert body["dispatch_pending"] is True
    assert body["event"]["message"] is None
    assert body["event"]["delivery"]["state"] == "reserved"


def test_dispatching_show_event_requires_delivery_authority(monkeypatch):

    async def unexpected_dispatch(*_args, **_kwargs):
        pytest.fail("a current annotation without its reserved prompt must not dispatch")

    monkeypatch.setattr("vibe.internal_client.dispatch_async", unexpected_dispatch)
    outcome = asyncio.run(
        ui_server._run_show_event_dispatch(
            {
                "id": "show_evt_missing_prompt",
                "session_id": "ses123",
                "type": "human.annotation.created",
                "transcript_text": "Visible words only",
                "message": {
                    "id": "msg_missing_prompt",
                    "type": "annotation",
                    "content": {
                        "text": "Visible words only",
                        "annotation": {
                            "direction": "user",
                            "action": "created",
                        },
                    },
                    "metadata": {},
                },
            }
        )
    )

    assert outcome is ui_server._ShowEventDispatchOutcome.FAILED


def test_reserved_show_dispatch_rejects_whitespace_only_dispatch_text(monkeypatch):
    async def unexpected_dispatch(*_args, **_kwargs):
        pytest.fail("a blank Show prompt must not start a turn")

    monkeypatch.setattr("vibe.internal_client.dispatch_async", unexpected_dispatch)
    outcome = asyncio.run(
        ui_server._run_show_event_dispatch(
            {
                "id": "show_evt_blank_prompt",
                "session_id": "ses123",
                "delivery": {
                    "id": "msg_blank_prompt",
                    "state": "reserved",
                    "dispatch_text": " \n\t ",
                },
            }
        )
    )

    assert outcome is ui_server._ShowEventDispatchOutcome.FAILED


@pytest.mark.parametrize(
    "delivery_state",
    [
        "queued",
        "claimed",
        "pending_steer",
        "steering",
        "reconciling_steer",
        "interrupt_waiting",
    ],
)
def test_show_dispatch_replay_accepts_existing_admission(monkeypatch, delivery_state):
    async def unexpected_dispatch(*_args, **_kwargs):
        pytest.fail("an admitted Show Delivery must not be dispatched twice")

    monkeypatch.setattr("vibe.internal_client.dispatch_async", unexpected_dispatch)
    outcome = asyncio.run(
        ui_server._run_show_event_dispatch(
            {
                "id": "show_evt_admitted",
                "session_id": "ses123",
                "delivery": {
                    "id": "msg_admitted",
                    "state": delivery_state,
                    "dispatch_text": "already admitted",
                },
            }
        )
    )

    assert outcome is ui_server._ShowEventDispatchOutcome.ACCEPTED


def test_private_show_page_materializes_same_submission_after_synchronous_acceptance(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    token = "session-write-token"
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: token)
    published = []
    monkeypatch.setattr(
        "vibe.sse_broker.broker.publish",
        lambda event_type, data: published.append((event_type, data)),
    )
    settled = {}

    async def fake_dispatch_async(payload, **kwargs):
        _accept_dispatch(payload)
        settled["submission_id"] = payload["user_message_id"]
        return {"status_code": 202, "body": {"ok": True, "drained": True}}

    with patch("vibe.internal_client.dispatch_async", fake_dispatch_async):
        response = app.test_client().post(
            "/show/ses123/__show/events",
            base_url="http://127.0.0.1:5123",
            headers={
                "Origin": "http://127.0.0.1:5123",
                "Content-Type": "application/json",
                "X-Vibe-Show-Token": token,
            },
            json={
                "type": "human.annotation.created",
                "annotation": {
                    "intent": "comment",
                    "comment": "Drain before the 202 returns.",
                    "dispatch": True,
                },
            },
        )

    assert response.status_code == 201
    event = response.get_json()["event"]
    assert event["message_id"] == settled["submission_id"]
    assert event["message"]["id"] == settled["submission_id"]
    assert event["message"]["type"] == "annotation"
    assert event["message"]["author_name"] == "show_annotation"
    # The real manager already publishes the promoted row. The route only
    # published the event before entering our fake adapter and must not emit a
    # stale pending/queued message afterwards.
    assert [event_type for event_type, _data in published] == ["show.event"]


def test_private_show_page_busy_dispatch_queues_without_message_new(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    token = "session-write-token"
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: token)
    published = []
    monkeypatch.setattr("vibe.sse_broker.broker.publish", lambda event_type, data: published.append((event_type, data)))
    dispatch_done = asyncio.Event()

    async def fake_dispatch_async(payload, **kwargs):
        _accept_dispatch(payload, "queued")
        dispatch_done.set()
        return {"status_code": 202, "body": {"ok": True, "queued": True}}

    with patch("vibe.internal_client.dispatch_async", fake_dispatch_async):
        response = app.test_client().post(
            "/show/ses123/__show/events",
            base_url="http://127.0.0.1:5123",
            headers={
                "Origin": "http://127.0.0.1:5123",
                "Content-Type": "application/json",
                "X-Vibe-Show-Token": token,
            },
            json={
                "type": "human.annotation.created",
                "annotation": {
                    "intent": "comment",
                    "comment": "Queue this annotation.",
                    "dispatch": True,
                },
            },
        )

    assert response.status_code == 201
    asyncio.run(asyncio.wait_for(dispatch_done.wait(), timeout=1))
    delivery_id = response.get_json()["event"]["delivery"]["id"]

    from storage import messages_service
    from storage.db import create_sqlite_engine

    with create_sqlite_engine().connect() as conn:
        queued = message_deliveries.list_queued(conn, "ses123")
        transcript = messages_service.list_session_messages(
            conn,
            session_id="ses123",
            limit=50,
            types=messages_service.TRANSCRIPT_TYPES,
            tail=True,
        )
    assert [(message["id"], message["text"]) for message in queued] == [
        (delivery_id, response.get_json()["event"]["transcript_text"])
    ]
    assert transcript["messages"] == []
    assert [event_type for event_type, _data in published] == ["show.event", "queue.updated"]


def test_private_show_page_non_dispatching_annotation_stays_immediately_visible(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    token = "session-write-token"
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: token)
    published = []
    monkeypatch.setattr("vibe.sse_broker.broker.publish", lambda event_type, data: published.append((event_type, data)))

    response = app.test_client().post(
        "/show/ses123/__show/events",
        base_url="http://127.0.0.1:5123",
        headers={
            "Origin": "http://127.0.0.1:5123",
            "Content-Type": "application/json",
            "X-Vibe-Show-Token": token,
        },
        json={
            "type": "human.annotation.created",
            "annotation": {
                "intent": "comment",
                "comment": "Record this without dispatch.",
                "dispatch": False,
            },
        },
    )

    assert response.status_code == 201
    assert response.get_json()["event"]["message"]["type"] == "annotation"
    assert [event_type for event_type, _data in published] == [
        "show.event",
        "message.new",
        "session.activity",
    ]


def test_private_show_page_reverse_mark_publishes_live_annotation(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    token = "session-write-token"
    monkeypatch.setattr(
        "vibe.ui_server.show_event_write_token",
        lambda session_id: token,
    )
    published = []
    monkeypatch.setattr(
        "vibe.sse_broker.broker.publish",
        lambda event_type, data: published.append((event_type, data)),
    )

    response = app.test_client().post(
        "/show/ses123/__show/events",
        base_url="http://127.0.0.1:5123",
        headers={
            "Origin": "http://127.0.0.1:5123",
            "Content-Type": "application/json",
            "X-Vibe-Show-Token": token,
        },
        json={
            "type": "assistant.mark.created",
            "mark": {
                "target": "#summary",
                "body": "Updated the summary.",
            },
            "anchor": {
                "selector": "#summary",
                "text": "Quarterly summary",
            },
        },
    )

    assert response.status_code == 201
    message = response.get_json()["event"]["message"]
    assert message["type"] == "annotation"
    assert message["author"] == "agent"
    assert message["source"] is None
    assert message["text"] == "Updated the summary."
    assert message["content"]["annotation"] == {
        "direction": "agent",
        "action": "created",
        "quote": "Quarterly summary",
    }
    assert [
        data["type"]
        for event_type, data in published
        if event_type == "message.new"
    ] == ["annotation"]


@pytest.mark.parametrize(
    ("message_type", "expects_message"),
    [
        ("annotation", True),
        ("assistant", False),
    ],
)
def test_show_event_live_publish_uses_catalog_transcript_gate(
    monkeypatch,
    message_type,
    expects_message,
):
    published = []
    monkeypatch.setattr(
        "vibe.sse_broker.broker.publish",
        lambda event_type, data: published.append((event_type, data)),
    )

    ui_server._publish_show_session_event(
        {
            "id": "show_evt_publish_gate",
            "session_id": "ses123",
            "scope_id": "scope1",
            "type": "assistant.page.updated",
            "actor": "assistant",
            "payload": {},
            "message": {
                "id": "msg_publish_gate",
                "type": message_type,
            },
        }
    )

    assert (
        "message.new" in [event_type for event_type, _data in published]
    ) is expects_message
    assert [event_type for event_type, _data in published][0] == "show.event"
    assert [event_type for event_type, _data in published][-1] == "session.activity"


def test_private_show_page_publishes_annotation_control_without_message_or_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    token = "session-write-token"
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: token)
    published = []
    monkeypatch.setattr("vibe.sse_broker.broker.publish", lambda event_type, data: published.append((event_type, data)))

    response = app.test_client().post(
        "/show/ses123/__show/events",
        base_url="http://127.0.0.1:5123",
        headers={
            "Origin": "http://127.0.0.1:5123",
            "Content-Type": "application/json",
            "X-Vibe-Show-Token": token,
        },
        json={
            "type": "system.annotation.control",
            "payload": {"action": "enable", "mode": "smart", "dispatch": True},
        },
    )

    assert response.status_code == 201
    event = response.get_json()["event"]
    assert event["payload"] == {"action": "enable", "mode": "smart"}
    assert event["message_id"] is None
    assert event["transcript_text"] == ""
    assert [event_type for event_type, _data in published] == ["show.event", "session.activity"]


def test_private_show_page_dispatches_screenshot_annotation_batch(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    token = "session-write-token"
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: token)
    raw, data_url = _screenshot_png(4, 3)
    dispatches = []
    dispatch_done = asyncio.Event()

    async def fake_dispatch_async(payload, **kwargs):
        dispatches.append(payload)
        _accept_dispatch(payload)
        dispatch_done.set()
        return {"status_code": 202, "body": {"ok": True}}

    with patch("vibe.internal_client.dispatch_async", fake_dispatch_async):
        response = app.test_client().post(
            "/show/ses123/__show/events",
            base_url="http://127.0.0.1:5123",
            headers={
                "Origin": "http://127.0.0.1:5123",
                "Content-Type": "application/json",
                "X-Vibe-Show-Token": token,
            },
            json={
                "type": "human.annotation.created",
                "annotation": {
                    "intent": "review",
                    "comment": "Review this screenshot batch.",
                    "dispatch": True,
                    "screenshot": {
                        "attachmentId": "show_asset_screenshot_1",
                        "mimeType": "image/png",
                        "width": 4,
                        "height": 3,
                        "capturedRegion": {"x": 24, "y": 32, "width": 640, "height": 360},
                        "dataUrl": data_url,
                        "items": [
                            {
                                "label": "1",
                                "comment": "This counter looks stale.",
                                "point": {"x": 120, "y": 80},
                            },
                            {
                                "label": "2",
                                "comment": "Crop this empty area.",
                                "rect": {"x": 420, "y": 240, "width": 160, "height": 72},
                            },
                        ],
                    },
                },
            },
        )

    assert response.status_code == 201
    payload = response.get_json()
    event = payload["event"]
    screenshot = event["payload"]["screenshot"]
    assert event["payload"]["primaryAnchor"] == "screenshot"
    assert "dataUrl" not in screenshot
    assert screenshot["attachmentId"] != "show_asset_screenshot_1"
    assert Path(screenshot["path"]).read_bytes() == raw
    assert event["message"]["text"] == "Review this screenshot batch."
    assert event["message"]["content"]["attachments"] == [
        {
            "url": f"/api/media/{screenshot['attachmentId']}",
            "name": "annotation-region.png",
            "mime": "image/png",
            "kind": "image",
            "width": 4,
            "height": 3,
        }
    ]
    asyncio.run(asyncio.wait_for(dispatch_done.wait(), timeout=1))
    assert dispatches
    transcript = dispatches[0]["text"]
    assert "Anchor kind: screenshot" in transcript
    assert f"Screenshot: {screenshot['path']} (4x3)" in transcript
    assert "Screenshot region: x:24, y:32, 640x360" in transcript
    assert "1. This counter looks stale. (x:120, y:80)" in transcript
    assert "2. Crop this empty area. (x:420, y:240, 160x72)" in transcript

    media_response = app.test_client().get(
        f"/api/media/{screenshot['attachmentId']}",
        base_url="http://127.0.0.1:5123",
    )
    assert media_response.status_code == 200
    assert media_response.content == raw
    assert media_response.headers["content-type"] == "image/png"


def test_private_show_page_rejects_show_event_without_write_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: f"token-{session_id}")

    client = app.test_client()
    page_response = client.get("/show/ses123/", base_url="http://127.0.0.1:5123")
    assert page_response.status_code == 200

    response = client.post(
        "/show/ses123/__show/events",
        base_url="http://127.0.0.1:5123",
        headers={
            "Origin": "http://127.0.0.1:5123",
            "Content-Type": "application/json",
        },
        json={
            "type": "assistant.mark.created",
            "mark": {"target": "mark-default-summary", "body": "Review this summary."},
        },
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "show_event_write_forbidden"


def test_private_show_page_rejects_other_session_write_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: f"token-{session_id}")

    response = app.test_client().post(
        "/show/ses123/__show/events",
        base_url="http://127.0.0.1:5123",
        headers={
            "Origin": "http://127.0.0.1:5123",
            "Content-Type": "application/json",
            "X-Vibe-Show-Token": "token-other-session",
        },
        json={
            "type": "assistant.mark.created",
            "mark": {"target": "mark-default-summary", "body": "Review this summary."},
        },
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "show_event_write_forbidden"


def test_private_show_page_records_remote_oauth_author(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    token = "session-write-token"
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: token)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "alex@example.com", "user-1"),
        domain="alex.avibe.bot",
    )

    response = client.post(
        "/show/ses123/__show/events",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={
            "Origin": "https://alex.avibe.bot",
            "Content-Type": "application/json",
            "X-Vibe-Show-Token": token,
        },
        json={
            "type": "human.annotation.created",
            "annotation": {"comment": "Remote review."},
        },
    )

    assert response.status_code == 201
    event = response.get_json()["event"]
    expected_author = {"kind": "user", "email": "alex@example.com"}
    assert event["payload"]["author"] == expected_author
    assert event["message"]["metadata"]["author"] == expected_author


def test_private_show_page_allows_active_org_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    token = "session-write-token"
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: token)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "alex@example.com", "user-1"),
        domain="alex.avibe.bot",
    )
    dispatched = []

    async def accept_dispatch(event):
        dispatched.append(event)
        return ui_server._ShowEventDispatchOutcome.ACCEPTED

    monkeypatch.setattr("vibe.ui_server._run_show_event_dispatch", accept_dispatch)

    response = client.post(
        "/show/ses123/__show/events",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={
            "Origin": "https://alex.avibe.bot",
            "Content-Type": "application/json",
            "X-Vibe-Show-Token": token,
        },
        json={
            "type": "human.annotation.created",
            "annotation": {"comment": "Try to dispatch.", "dispatch": True},
        },
    )

    assert response.status_code == 201
    assert response.get_json()["ok"] is True
    assert len(dispatched) == 1


def test_private_show_page_accepts_mark_read_receipt_and_records_reader(monkeypatch, tmp_path):
    from core.show_session_events import ShowSessionEventStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    store = ShowSessionEventStore()
    try:
        created = store.append(
            "ses123",
            {
                "type": "assistant.mark.created",
                "mark": {"id": "mark_read", "target": "#summary", "body": "Read this."},
            },
        )
    finally:
        store.close()

    token = "session-write-token"
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: token)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "reader@example.com", "user-1"),
        domain="alex.avibe.bot",
    )
    response = client.post(
        "/show/ses123/__show/events",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={
            "Origin": "https://alex.avibe.bot",
            "Content-Type": "application/json",
            "X-Vibe-Show-Token": token,
        },
        json={
            "type": "assistant.mark.resolved",
            "mark": {
                "id": "mark_read",
                "updatedAt": created["payload"]["updatedAt"],
                "target": "#forged",
                "body": "Forged body.",
                "author": {"kind": "user", "email": "forged@example.com"},
            },
        },
    )

    assert response.status_code == 201
    event = response.get_json()["event"]
    assert event["actor"] == "assistant"
    assert event["payload"]["role"] == "assistant"
    assert event["payload"]["target"] == "#summary"
    assert event["payload"]["body"] == "Read this."
    assert event["payload"]["author"] == {"kind": "user", "email": "reader@example.com"}
    assert event["transcript_text"] == ""
    assert event["message_id"] is None
    assert event["message"] is None
    store = ShowSessionEventStore()
    try:
        assert store.active_marks("ses123") == []
    finally:
        store.close()


def test_private_show_page_sets_show_event_write_cookie(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: f"token-{session_id}")

    response = app.test_client().get("/show/ses123/", base_url="http://127.0.0.1:5123")

    assert response.status_code == 200
    cookies = "\n".join(response.headers.getlist("set-cookie"))
    assert "vibe_show_event_token=token-ses123" in cookies
    assert "Path=/show/ses123/" in cookies
    # 'self' (not 'none'): the workbench frames a private Show Page in the chat
    # view (same origin); cross-origin framing stays blocked.
    assert response.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert "permissions-policy" not in response.headers


def test_public_show_page_clears_show_event_write_cookie(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")

    response = app.test_client().get(
        f"/p/{share_id}/",
        base_url="http://127.0.0.1:5123",
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 200
    cookies = "\n".join(response.headers.getlist("set-cookie"))
    assert "vibe_show_event_token=" in cookies
    assert "Max-Age=0" in cookies
    assert response.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert "sandbox.avibe.bot" not in response.headers.get("content-security-policy", "")
    assert "permissions-policy" not in response.headers


def test_show_events_stream_replays_persisted_pages_with_batch_authorization_checks(
    monkeypatch,
    tmp_path,
):
    from core.show_session_events import ShowSessionEventStore
    from vibe.authorization import instance_owner_context
    from vibe.ui_server import _show_events_stream

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    cookie = _active_org_cookie(config)
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    authorization_checks = []

    async def _authorization_state(*args, **kwargs):
        authorization_checks.append((args, kwargs))
        return "current"

    monkeypatch.setattr(
        ui_server,
        "_remote_stream_authorization_state",
        _authorization_state,
    )
    store = ShowSessionEventStore()
    try:
        for index in range(501):
            store.append(
                "ses123",
                {
                    "id": f"show_evt_{index:03d}",
                    "type": "assistant.mark.created",
                    "mark": {
                        "target": f"target-{index:03d}",
                        "body": f"body-{index:03d}",
                        "createdAt": f"2026-05-30T00:{index // 60:02d}:{index % 60:02d}+00:00",
                    },
                },
            )
    finally:
        store.close()

    async def _collect_replay() -> str:
        response = await _show_events_stream(
            "ses123",
            authorization_context=instance_owner_context(),
            remote_session_identity=identity,
            remote_session_payload=identity,
            remote_session_cookie=cookie,
            remote_request_host="alex.avibe.bot",
            remote_config=config,
        )
        iterator = response.body_iterator.__aiter__()
        chunks = []
        try:
            for _ in range(502):
                chunk = await iterator.__anext__()
                chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
        finally:
            await iterator.aclose()
        return "".join(chunks)

    body = asyncio.run(_collect_replay())

    assert body.startswith(": show events connected")
    assert body.count("event: show.event") == 501
    assert "id: show_evt_000" in body
    assert "id: show_evt_500" in body
    assert '"id": "show_evt_000"' in body
    assert '"id": "show_evt_500"' in body
    assert len(authorization_checks) == 3


def test_show_events_stream_forwards_live_dispatch_events(monkeypatch, tmp_path):
    from vibe.sse_broker import broker
    from vibe.authorization import instance_owner_context
    from vibe.ui_server import _show_events_stream

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")

    async def _collect_live_dispatch() -> str:
        response = await _show_events_stream(
            "ses123",
            authorization_context=instance_owner_context(),
        )
        iterator = response.body_iterator.__aiter__()
        chunks = []
        try:
            chunks.append(await iterator.__anext__())
            broker.publish(
                "show.dispatch",
                {
                    "session_id": "ses123",
                    "scope_id": "scope123",
                    "show_event_id": "show_evt_1",
                    "event": "turn.chunk",
                    "data": {"text": "hello"},
                },
            )
            chunks.append(await asyncio.wait_for(iterator.__anext__(), timeout=1))
        finally:
            await iterator.aclose()
        return "".join(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk for chunk in chunks)

    body = asyncio.run(_collect_live_dispatch())

    assert "event: show.dispatch" in body
    assert '"show_event_id": "show_evt_1"' in body


def test_private_show_events_stream_ends_at_authorization_refresh_deadline(monkeypatch, tmp_path):
    from vibe.ui_server import _show_events_stream

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")

    async def _collect_until_expired() -> list[str | bytes]:
        response = await _show_events_stream(
            "ses123",
            authorization_refresh_at=ui_server.time.time(),
        )
        iterator = response.body_iterator.__aiter__()
        try:
            chunks = [await iterator.__anext__()]
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(iterator.__anext__(), timeout=1)
        finally:
            await iterator.aclose()
        return chunks

    assert len(asyncio.run(_collect_until_expired())) == 1


def test_private_show_events_stream_ignores_project_access_revocation(monkeypatch, tmp_path):
    from storage import project_access_service
    from storage.db import create_sqlite_engine
    from vibe.authorization import AuthorizationContext
    from vibe.sse_broker import broker
    from vibe.ui_server import _show_events_stream

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    context = AuthorizationContext(
        instance_role="editor",
        subject="remote-editor",
        email="alice@example.com",
        instance_access_source="email",
        is_remote=True,
    )

    async def _collect_after_project_revocation() -> str:
        response = await _show_events_stream(
            "ses123",
            authorization_context=context,
        )
        iterator = response.body_iterator.__aiter__()
        try:
            chunks = [await iterator.__anext__()]
            engine = create_sqlite_engine()
            with engine.begin() as conn:
                result = project_access_service.apply_project_access_intent(
                    conn,
                    {
                        "project_id": "proj_show",
                        "revision": 1,
                        "mode": "restricted",
                        "bindings": [],
                    },
                )
            assert result.changed is True
            broker.publish("authorization.changed", {"project_ids": ["proj_show"]})
            # §3.2: /show admission is the Instance role alone, so a Project ACL
            # revocation must NOT close the stream — the residual Project gate is
            # gone. The stream ends only when the Instance role itself is lost.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(iterator.__anext__(), timeout=0.2)
        finally:
            await iterator.aclose()
        return "".join(
            chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            for chunk in chunks
        )

    assert asyncio.run(_collect_after_project_revocation()) == ": show events connected\n\n"


def test_remote_org_show_events_stream_ignores_resource_acl_changes(
    monkeypatch,
    tmp_path,
):
    from vibe.authorization import AuthorizationContext
    from vibe.sse_broker import broker
    from vibe.ui_server import _show_events_stream

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    context = AuthorizationContext(
        instance_role="editor",
        subject="remote-editor",
        email="alice@example.com",
        organization_id="org-1",
        organization_member_id="member-remote-editor",
        organization_role="member",
        group_ids=frozenset({"group-engineering"}),
        instance_access_source="organization_group",
        is_remote=True,
    )

    async def _collect_after_acl_change() -> str:
        response = await _show_events_stream(
            "ses123",
            authorization_context=context,
        )
        iterator = response.body_iterator.__aiter__()
        try:
            chunks = [await iterator.__anext__()]
            # A Resource ACL change must not close the /show events stream:
            # /show admission is instance-role only and no longer reads a
            # resource_access_policies row for the page.
            broker.publish(
                "authorization.changed",
                {"project_ids": [], "resource_kinds": ["agent"]},
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(iterator.__anext__(), timeout=0.2)
        finally:
            await iterator.aclose()
        return "".join(
            chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            for chunk in chunks
        )

    body = asyncio.run(_collect_after_acl_change())
    assert body == ": show events connected\n\n"


def test_public_show_events_stream_redacts_nested_dispatch_ids(monkeypatch, tmp_path):
    from vibe.sse_broker import broker
    from vibe.ui_server import _show_events_stream

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")

    async def _collect_live_dispatch() -> str:
        response = await _show_events_stream(
            "ses123",
            public=True,
            public_share_id=share_id,
        )
        iterator = response.body_iterator.__aiter__()
        chunks = []
        try:
            chunks.append(await iterator.__anext__())
            broker.publish(
                "show.dispatch",
                {
                    "session_id": "ses123",
                    "scope_id": "scope123",
                    "show_event_id": "show_evt_1",
                    "event": "turn.chunk",
                    "data": {
                        "text": "hello",
                        "session_id": "ses123",
                        "message_id": "msg123",
                        "nested": {"scope_id": "scope123", "user_message_id": "msg123"},
                    },
                },
            )
            chunks.append(await asyncio.wait_for(iterator.__anext__(), timeout=1))
        finally:
            await iterator.aclose()
        return "".join(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk for chunk in chunks)

    body = asyncio.run(_collect_live_dispatch())

    assert "event: show.dispatch" in body
    assert '"show_event_id": "show_evt_1"' in body
    assert '"text": "hello"' in body
    assert '"session_id"' not in body
    assert '"scope_id"' not in body
    assert '"message_id"' not in body
    assert '"user_message_id"' not in body


def test_public_show_page_events_redact_internal_ids(monkeypatch, tmp_path):
    from core.show_session_events import ShowSessionEventStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses123",
            {
                "type": "human.annotation.created",
                "annotation": {
                    "comment": "body",
                    "dispatch": True,
                },
            },
            reserve_dispatch=True,
        )
    finally:
        store.close()

    response = app.test_client().get(f"/p/{share_id}/__show/events", base_url="http://127.0.0.1:5123")

    assert response.status_code == 200
    public_event = response.get_json()["events"][0]
    assert public_event["id"] == event["id"]
    assert public_event["type"] == "human.annotation.created"
    assert public_event["payload"]["comment"] == "body"
    assert "session_id" not in public_event
    assert "scope_id" not in public_event
    assert "message_id" not in public_event
    assert "message" not in public_event
    assert "delivery_id" not in public_event
    assert "delivery" not in public_event


def test_public_show_events_stream_redacts_internal_ids(monkeypatch, tmp_path):
    from core.show_session_events import ShowSessionEventStore
    from vibe.ui_server import _show_events_stream

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "public")
    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses123",
            {
                "id": "show_evt_public",
                "type": "human.annotation.created",
                "annotation": {
                    "comment": "body",
                    "dispatch": True,
                },
            },
            reserve_dispatch=True,
        )
    finally:
        store.close()

    async def _collect_replay() -> str:
        response = await _show_events_stream("ses123", public=True)
        iterator = response.body_iterator.__aiter__()
        chunks = []
        try:
            for _ in range(2):
                chunk = await iterator.__anext__()
                chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
        finally:
            await iterator.aclose()
        return "".join(chunks)

    body = asyncio.run(_collect_replay())

    assert f'"id": "{event["id"]}"' in body
    assert '"session_id"' not in body
    assert '"scope_id"' not in body
    assert '"message_id"' not in body
    assert '"message"' not in body
    assert '"delivery_id"' not in body
    assert '"delivery"' not in body


def test_public_show_events_stream_redacts_screenshot_path(monkeypatch, tmp_path):
    from core.show_session_events import ShowSessionEventStore
    from vibe.ui_server import _show_events_stream

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    _, data_url = _screenshot_png(4, 3)
    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses123",
            {
                "type": "human.annotation.created",
                "annotation": {
                    "comment": "Review this screenshot.",
                    "screenshot": {
                        "mimeType": "image/png",
                        "width": 4,
                        "height": 3,
                        "capturedRegion": {"x": 0, "y": 0, "width": 40, "height": 30},
                        "dataUrl": data_url,
                        "items": [],
                    },
                },
            },
        )
    finally:
        store.close()

    async def collect_replay() -> str:
        response = await _show_events_stream(
            "ses123",
            public=True,
            public_share_id=share_id,
        )
        iterator = response.body_iterator.__aiter__()
        try:
            chunks = [await iterator.__anext__(), await iterator.__anext__()]
        finally:
            await iterator.aclose()
        return "".join(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk for chunk in chunks)

    body = asyncio.run(collect_replay())
    screenshot = event["payload"]["screenshot"]
    assert screenshot["path"] not in body
    assert '"path"' not in body
    assert screenshot["attachmentId"] in body
    assert f"/p/{share_id}/__show/media/{screenshot['attachmentId']}" in body


def test_cli_show_event_ingress_records_and_publishes(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    published = []
    monkeypatch.setattr("vibe.sse_broker.broker.publish", lambda event_type, data: published.append((event_type, data)))

    response = app.test_client().post(
        "/api/show/sessions/ses123/events",
        base_url="http://127.0.0.1:5123",
        headers={
            "Content-Type": "application/json",
            "X-Vibe-Show-Client": "cli",
            "X-Vibe-Show-Cli-Token": show_cli_event_token(),
        },
        json={
            "type": "assistant.mark.created",
            "mark": {
                "target": "mark-default-summary",
                "body": "Review this summary.",
            },
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["event"]["type"] == "assistant.mark.created"
    assert payload["event"]["message_id"]
    assert [event_type for event_type, _data in published] == ["show.event", "message.new", "session.activity"]


def test_cli_show_event_dispatch_waits_for_unambiguous_acceptance(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    dispatch_entered = threading.Event()
    release_dispatch = threading.Event()
    result = {}

    async def stalled_dispatch(payload, **kwargs):
        dispatch_entered.set()
        released = await asyncio.to_thread(release_dispatch.wait, 2)
        assert released
        _accept_dispatch(payload)
        return {"status_code": 202, "body": {"ok": True}}

    def post_event():
        result["response"] = app.test_client().post(
            "/api/show/sessions/ses123/events",
            base_url="http://127.0.0.1:5123",
            headers={
                "Content-Type": "application/json",
                "X-Vibe-Show-Client": "cli",
                "X-Vibe-Show-Cli-Token": show_cli_event_token(),
            },
            json={
                "type": "human.annotation.created",
                "annotation": {
                    "intent": "comment",
                    "comment": "Do not duplicate this.",
                    "dispatch": True,
                },
            },
        )

    with patch("vibe.internal_client.dispatch_async", stalled_dispatch):
        request_thread = threading.Thread(target=post_event)
        request_thread.start()
        assert dispatch_entered.wait(1)
        assert request_thread.is_alive()
        release_dispatch.set()
        request_thread.join(2)

    assert not request_thread.is_alive()
    assert result["response"].status_code == 201
    assert result["response"].get_json()["event"]["message"]["type"] == "annotation"


def test_cli_show_event_ingress_requires_cli_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")

    response = app.test_client().post(
        "/api/show/sessions/ses123/events",
        base_url="http://127.0.0.1:5123",
        headers={
            "Content-Type": "application/json",
            "X-Vibe-Show-Client": "cli",
        },
        json={
            "type": "assistant.mark.created",
            "mark": {"target": "mark-default-summary", "body": "Review this summary."},
        },
    )

    assert response.status_code == 403


def test_cli_show_prewarm_ingress_uses_ui_runtime_manager(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    calls = []

    async def fake_prewarm(session_id, *, context):
        calls.append((session_id, context))
        return SimpleNamespace(available=True, reason=None, base_url="http://127.0.0.1:49200")

    monkeypatch.setattr("core.show_runtime.prewarm_show_page_session", fake_prewarm)

    response = app.test_client().post(
        "/api/show/sessions/ses123/prewarm",
        base_url="http://127.0.0.1:5123",
        headers={
            "Content-Type": "application/json",
            "X-Vibe-Show-Client": "cli",
            "X-Vibe-Show-Cli-Token": show_cli_event_token(),
        },
        json={"context": "shared", "base_path": "/p/share123/"},
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert calls == [("ses123", ShowRuntimeContext.SHARED)]


def test_show_live_017_cli_show_prewarm_rejects_missing_context_before_runtime(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    calls = []

    async def fake_prewarm(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(available=True, reason=None, base_url="http://127.0.0.1:49200")

    monkeypatch.setattr("core.show_runtime.prewarm_show_page_session", fake_prewarm)

    response = app.test_client().post(
        "/api/show/sessions/ses123/prewarm",
        base_url="http://127.0.0.1:5123",
        headers={
            "Content-Type": "application/json",
            "X-Vibe-Show-Client": "cli",
            "X-Vibe-Show-Cli-Token": show_cli_event_token(),
        },
        json={"base_path": "/p/share123/"},
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_show_runtime_context"
    assert calls == []


def test_cli_show_prewarm_ingress_requires_cli_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().post(
        "/api/show/sessions/ses123/prewarm",
        base_url="http://127.0.0.1:5123",
        headers={
            "Content-Type": "application/json",
            "X-Vibe-Show-Client": "cli",
        },
        json={},
    )

    assert response.status_code == 403


def test_cli_show_event_ingress_allows_configured_host_with_cli_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.enabled = False
    config.ui.setup_host = "10.1.2.3"
    config.save()
    _create_agent_session("ses123")

    response = app.test_client().post(
        "/api/show/sessions/ses123/events",
        base_url="http://10.1.2.3:5123",
        environ_base={"REMOTE_ADDR": "10.50.0.5"},
        headers={
            "Content-Type": "application/json",
            "X-Vibe-Show-Client": "cli",
            "X-Vibe-Show-Cli-Token": show_cli_event_token(),
        },
        json={
            "type": "assistant.mark.created",
            "mark": {"target": "mark-default-summary", "body": "Review this summary."},
        },
    )

    assert response.status_code == 201


def test_cli_show_event_ingress_rejects_configured_host_without_cli_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.enabled = False
    config.ui.setup_host = "10.1.2.3"
    config.save()
    _create_agent_session("ses123")

    response = app.test_client().post(
        "/api/show/sessions/ses123/events",
        base_url="http://10.1.2.3:5123",
        environ_base={"REMOTE_ADDR": "10.50.0.5"},
        headers={
            "Content-Type": "application/json",
            "X-Vibe-Show-Client": "cli",
        },
        json={
            "type": "assistant.mark.created",
            "mark": {"target": "mark-default-summary", "body": "Review this summary."},
        },
    )

    assert response.status_code == 403


def _public_show_write_headers(
    share_id: str,
    *,
    origin: str = "https://alex.avibe.bot",
    token_share_id: str | None = None,
    token_session_id: str = "ses123",
    referer_share_id: str | None = None,
) -> dict[str, str]:
    return {
        "Origin": origin,
        "Referer": f"{origin}/p/{referer_share_id or share_id}/",
        "Content-Type": "application/json",
        "X-Vibe-Show-Token": show_public_event_write_token(token_share_id or share_id, token_session_id),
    }


def test_public_show_page_events_require_login(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    dispatches = []

    async def unexpected_dispatch(payload, **kwargs):
        dispatches.append(payload)
        return {"status_code": 202, "body": {"ok": True}}

    with patch("vibe.internal_client.dispatch_async", unexpected_dispatch):
        response = app.test_client().post(
            f"/p/{share_id}/__show/events",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={
                "Origin": "https://alex.avibe.bot",
                "Content-Type": "application/json",
            },
            json={
                "type": "human.annotation.created",
                "annotation": {"comment": "Anonymous review.", "dispatch": True},
            },
        )

    assert response.status_code == 403
    assert response.get_json()["code"] == "public_show_events_login_required"
    assert dispatches == []


def test_public_show_page_events_require_share_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "alex@example.com", "user-1"),
        domain="alex.avibe.bot",
    )

    response = client.post(
        f"/p/{share_id}/__show/events",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={
            "Origin": "https://alex.avibe.bot",
            "Referer": f"https://alex.avibe.bot/p/{share_id}/",
            "Content-Type": "application/json",
        },
        json={
            "type": "human.annotation.created",
            "annotation": {"comment": "Missing share token."},
        },
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "show_event_write_forbidden"


@pytest.mark.parametrize(
    "referer",
    [None, "https://alex.avibe.bot/p/other-share/"],
)
def test_public_show_page_events_require_matching_share_referer(monkeypatch, tmp_path, referer):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "alex@example.com", "user-1"),
        domain="alex.avibe.bot",
    )
    headers = _public_show_write_headers(share_id)
    if referer is None:
        headers.pop("Referer")
    else:
        headers["Referer"] = referer

    response = client.post(
        f"/p/{share_id}/__show/events",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers=headers,
        json={"type": "human.annotation.created", "annotation": {"comment": "Wrong page."}},
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "public_show_events_origin_mismatch"


def test_public_show_page_events_reject_cross_share_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "alex@example.com", "user-1"),
        domain="alex.avibe.bot",
    )

    response = client.post(
        f"/p/{share_id}/__show/events",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers=_public_show_write_headers(share_id, token_share_id="other-share"),
        json={"type": "human.annotation.created", "annotation": {"comment": "Wrong token."}},
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "show_event_write_forbidden"


def test_public_show_page_events_reject_token_from_previous_share_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "alex@example.com", "user-1"),
        domain="alex.avibe.bot",
    )

    response = client.post(
        f"/p/{share_id}/__show/events",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers=_public_show_write_headers(share_id, token_session_id="ses-previous"),
        json={"type": "human.annotation.created", "annotation": {"comment": "Stale token."}},
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "show_event_write_forbidden"


def test_public_show_page_events_accept_oauth_user_and_record_author(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    published = []
    monkeypatch.setattr("vibe.ui_server._publish_show_session_event", published.append)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "member@example.com", "user-2"),
        domain="alex.avibe.bot",
    )

    response = client.post(
        f"/p/{share_id}/__show/events",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers=_public_show_write_headers(share_id),
        json={
            "type": "human.annotation.created",
            "annotation": {
                "comment": "Authenticated review.",
                "author": {"kind": "local"},
            },
        },
    )

    assert response.status_code == 201
    event = response.get_json()["event"]
    expected_author = {"kind": "user", "email": "member@example.com"}
    assert event["payload"]["author"] == {"kind": "user"}
    assert "session_id" not in event
    assert "scope_id" not in event
    assert "message_id" not in event
    assert "message" not in event

    assert published[0]["message"]["metadata"]["author"] == expected_author

    listed = client.get(
        f"/p/{share_id}/__show/events",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    ).get_json()["events"][0]
    assert listed["payload"]["author"] == {"kind": "user"}
    assert "member@example.com" not in json.dumps(listed)


def test_public_show_page_redacts_materialized_screenshot_path(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    raw, data_url = _screenshot_png(4, 3)
    published = []
    monkeypatch.setattr("vibe.ui_server._publish_show_session_event", published.append)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "member@example.com", "user-2"),
        domain="alex.avibe.bot",
    )

    response = client.post(
        f"/p/{share_id}/__show/events",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers=_public_show_write_headers(share_id),
        json={
            "type": "human.annotation.created",
            "annotation": {
                "comment": "Review this screenshot.",
                "screenshot": {
                    "attachmentId": "screenshot_client_only",
                    "mimeType": "image/png",
                    "width": 4,
                    "height": 3,
                    "capturedRegion": {"x": 0, "y": 0, "width": 40, "height": 30},
                    "dataUrl": data_url,
                    "items": [],
                },
            },
        },
    )

    assert response.status_code == 201
    internal_event = published[0]
    internal_screenshot = internal_event["payload"]["screenshot"]
    assert Path(internal_screenshot["path"]).is_file()
    assert internal_event["transcript_text"] == "Review this screenshot."
    assert internal_screenshot["path"] not in internal_event["transcript_text"]
    assert internal_event["message"]["content"]["attachments"] == [
        {
            "url": f"/api/media/{internal_screenshot['attachmentId']}",
            "name": "annotation-region.png",
            "mime": "image/png",
            "kind": "image",
            "width": 4,
            "height": 3,
        }
    ]

    public_event = response.get_json()["event"]
    public_screenshot = public_event["payload"]["screenshot"]
    assert "path" not in public_screenshot
    assert public_screenshot["attachmentId"] == internal_screenshot["attachmentId"]
    assert internal_screenshot["path"] not in public_event["transcript_text"]
    assert public_event["transcript_text"] == "Review this screenshot."
    assert public_screenshot["url"] == (
        f"/p/{share_id}/__show/media/{internal_screenshot['attachmentId']}"
    )

    anonymous_media = app.test_client().get(
        public_screenshot["url"],
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert anonymous_media.status_code == 200
    assert anonymous_media.content == raw
    assert anonymous_media.headers["content-type"] == "image/png"

    _create_agent_session("ses456")
    other_share_id = _create_show_page("ses456", "public")
    cross_share_media = app.test_client().get(
        f"/p/{other_share_id}/__show/media/{internal_screenshot['attachmentId']}",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert cross_share_media.status_code == 404

    listed = client.get(
        f"/p/{share_id}/__show/events",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    ).get_json()["events"][0]
    assert "path" not in listed["payload"]["screenshot"]
    assert internal_screenshot["path"] not in json.dumps(listed)


def test_public_show_page_accepts_mark_read_receipt_and_records_reader(monkeypatch, tmp_path):
    from core.show_session_events import ShowSessionEventStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    store = ShowSessionEventStore()
    try:
        created = store.append(
            "ses123",
            {
                "type": "assistant.mark.created",
                "mark": {"id": "mark_public_read", "target": "#summary", "body": "Read this."},
            },
        )
    finally:
        store.close()

    published = []
    monkeypatch.setattr("vibe.ui_server._publish_show_session_event", published.append)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "reader@example.com", "user-2"),
        domain="alex.avibe.bot",
    )
    response = client.post(
        f"/p/{share_id}/__show/events",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers=_public_show_write_headers(share_id),
        json={
            "type": "assistant.mark.resolved",
            "mark": {
                "id": "mark_public_read",
                "updatedAt": created["payload"]["updatedAt"],
                "target": "#forged",
                "body": "Forged body.",
                "author": {"kind": "local"},
            },
        },
    )

    assert response.status_code == 201
    public_event = response.get_json()["event"]
    assert public_event["actor"] == "assistant"
    assert public_event["payload"]["target"] == "#summary"
    assert public_event["payload"]["body"] == "Read this."
    assert public_event["payload"]["author"] == {"kind": "user"}
    assert published[0]["payload"]["author"] == {"kind": "user", "email": "reader@example.com"}
    assert published[0]["message"] is None
    store = ShowSessionEventStore()
    try:
        assert store.active_marks("ses123") == []
    finally:
        store.close()


def test_public_show_page_rejects_resolution_for_unknown_mark(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    published = []
    monkeypatch.setattr("vibe.ui_server._publish_show_session_event", published.append)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "reader@example.com", "user-2"),
        domain="alex.avibe.bot",
    )

    response = client.post(
        f"/p/{share_id}/__show/events",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers=_public_show_write_headers(share_id),
        json={
            "type": "assistant.mark.resolved",
            "mark": {
                "id": "mark_unknown",
                "updatedAt": "2026-07-23T00:00:00Z",
                "target": "#forged",
                "body": "Forged body.",
            },
        },
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "mark_not_active"
    assert published == []


def test_public_show_page_events_accept_injected_share_session_id(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    published = []
    monkeypatch.setattr("vibe.ui_server._publish_show_session_event", published.append)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "member@example.com", "user-2"),
        domain="alex.avibe.bot",
    )

    response = client.post(
        f"/p/{share_id}/__show/events",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers=_public_show_write_headers(share_id),
        json={
            "sessionId": share_id,
            "type": "human.annotation.created",
            "annotation": {"session_id": share_id, "comment": "Authenticated review."},
        },
    )

    assert response.status_code == 201
    assert "sessionId" not in published[0]["payload"]
    assert "session_id" not in published[0]["payload"]


def test_public_show_page_intent_fallback_does_not_expose_author_email(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "member@example.com", "user-2"),
        domain="alex.avibe.bot",
    )

    response = client.post(
        f"/p/{share_id}/__show/events",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers=_public_show_write_headers(share_id),
        json={"type": "human.intent.submitted", "payload": {"intent": "choose"}},
    )

    assert response.status_code == 201
    assert "member@example.com" not in response.content.decode("utf-8")
    listed = client.get(
        f"/p/{share_id}/__show/events",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert "member@example.com" not in listed.content.decode("utf-8")


def test_public_show_page_events_preserve_dispatch_for_authorized_user(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    published = []
    dispatches = []
    monkeypatch.setattr("vibe.ui_server._publish_show_session_event", published.append)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "member@example.com", "user-2"),
        domain="alex.avibe.bot",
    )

    async def fake_dispatch_async(payload, **kwargs):
        dispatches.append(payload)
        _accept_dispatch(payload)
        return {"status_code": 202, "body": {"ok": True}}

    with patch("vibe.internal_client.dispatch_async", fake_dispatch_async):
        response = client.post(
            f"/p/{share_id}/__show/events",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers=_public_show_write_headers(share_id),
            json={
                "id": "forged\nid: injected",
                "type": "human.annotation.created",
                "annotation": {"comment": "Review this.", "dispatch": True},
            },
        )

    assert response.status_code == 201
    event = response.get_json()["event"]
    assert event["id"] != "forged\nid: injected"
    assert "\n" not in event["id"]
    assert event["payload"]["dispatch"] is True
    assert "delivery_id" not in event
    assert published[0]["delivery_id"] == dispatches[0]["user_message_id"]
    assert published[0]["payload"]["dispatch"] is True
    assert dispatches[0]["session_id"] == "ses123"


@pytest.mark.parametrize("event_type", ["assistant.mark.created", "system.annotation.control"])
def test_public_show_page_events_reject_non_human_types(monkeypatch, tmp_path, event_type):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "member@example.com", "user-2"),
        domain="alex.avibe.bot",
    )

    response = client.post(
        f"/p/{share_id}/__show/events",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers=_public_show_write_headers(share_id),
        json={"type": event_type, "payload": {"action": "enable"}},
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "unsupported_event_type"


def test_public_show_page_events_accept_no_oauth_local_access(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.enabled = False
    config.save()
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    published = []
    monkeypatch.setattr("vibe.ui_server._publish_show_session_event", published.append)

    response = app.test_client().post(
        f"/p/{share_id}/__show/events",
        base_url="http://127.0.0.1:5123",
        headers=_public_show_write_headers(share_id, origin="http://127.0.0.1:5123"),
        json={
            "type": "human.annotation.created",
            "annotation": {"comment": "Local review."},
        },
    )

    assert response.status_code == 201
    event = response.get_json()["event"]
    assert event["payload"]["author"] == {"kind": "local"}
    assert "session_id" not in event
    assert "scope_id" not in event
    assert "message_id" not in event
    assert "message" not in event
    assert published[0]["message"]["metadata"]["author"] == {"kind": "local"}


def test_private_show_page_api_mutation_rejects_missing_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager(body=b'{"ok":true}', extra_headers={"content-type": "application/json"})
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().post(
            "/show/ses123/api/health",
            base_url="http://127.0.0.1:5123",
            headers={"Content-Type": "application/json"},
            content=b'{"ping":true}',
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 403
    assert response.get_json()["message"] == "Forbidden: missing origin header"
    assert manager.calls == []


def test_private_show_page_api_mutation_rejects_cross_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager(body=b'{"ok":true}', extra_headers={"content-type": "application/json"})
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().post(
            "/show/ses123/api/health",
            base_url="http://127.0.0.1:5123",
            headers={
                "Origin": "http://evil.example",
                "Content-Type": "application/json",
            },
            content=b'{"ping":true}',
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 403
    assert response.get_json()["message"] == "Forbidden: invalid origin"
    assert manager.calls == []


def test_private_show_page_preserves_runtime_redirect_location(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager(
        body=b"",
        status_code=302,
        extra_headers={"location": "/sessions/ses123/app/foo/"},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/foo",
            base_url="http://127.0.0.1:5123",
            follow_redirects=False,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 302
    assert response.headers["location"] == "/show/ses123/foo/"
    assert "__Host-vibe_remote_session=attacker" not in "\n".join(response.headers.getlist("set-cookie"))


def test_private_show_page_rewrites_absolute_runtime_redirect_location(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager(
        body=b"",
        status_code=302,
        extra_headers={"location": "http://127.0.0.1:49321/sessions/ses123/app/foo/?x=1#top"},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/show/ses123/foo",
            base_url="http://127.0.0.1:5123",
            follow_redirects=False,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 302
    assert response.headers["location"] == "/show/ses123/foo/?x=1#top"


@pytest.mark.parametrize(
    "external_location",
    [
        "https://example.test/show/ses123/foo?x=1#top",
        "https://example.test/sessions/ses123/app/foo?x=1#top",
    ],
)
def test_public_show_page_preserves_external_redirect_location(monkeypatch, tmp_path, external_location):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _FakeShowRuntimeManager(
        body=b"",
        status_code=302,
        extra_headers={"location": external_location},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/foo",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 302
    assert response.headers["location"] == external_location


def test_show_runtime_manager_reports_missing_command(tmp_path):
    manager = ShowRuntimeManager(
        command="definitely-missing-avibe-show-runtime",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )

    result = asyncio.run(manager.ensure())

    assert result.available is False
    assert result.reason == "runtime_command_missing"


@pytest.mark.parametrize(
    ("dimension", "reason", "expected"),
    [
        (ShowRuntimeFailureDimension.INSTALL, "runtime_archive_download_failed", ShowRuntimeFailureClass.TRANSIENT),
        (ShowRuntimeFailureDimension.INSTALL, "runtime_manifest_download_failed", ShowRuntimeFailureClass.TRANSIENT),
        (ShowRuntimeFailureDimension.INSTALL, "runtime_install_failed", ShowRuntimeFailureClass.UNCLASSIFIED),
        (ShowRuntimeFailureDimension.INSTALL, "runtime_node_missing", ShowRuntimeFailureClass.CONFIGURED),
        (ShowRuntimeFailureDimension.RUNTIME, "runtime_start_url_timeout", ShowRuntimeFailureClass.UNCLASSIFIED),
        (
            ShowRuntimeFailureDimension.RUNTIME,
            "runtime_start_process_unavailable",
            ShowRuntimeFailureClass.UNCLASSIFIED,
        ),
        (ShowRuntimeFailureDimension.RUNTIME, "runtime_start_health_timeout", ShowRuntimeFailureClass.UNCLASSIFIED),
        (ShowRuntimeFailureDimension.RUNTIME, "runtime_start_attempt_failed", ShowRuntimeFailureClass.UNCLASSIFIED),
        (
            ShowRuntimeFailureDimension.RUNTIME,
            "runtime_start_command_unavailable",
            ShowRuntimeFailureClass.UNCLASSIFIED,
        ),
        (ShowRuntimeFailureDimension.RUNTIME, "runtime_start_command_invalid", ShowRuntimeFailureClass.CONFIGURED),
        (ShowRuntimeFailureDimension.RUNTIME, "runtime_start_node_command_invalid", ShowRuntimeFailureClass.CONFIGURED),
        (ShowRuntimeFailureDimension.RUNTIME, "runtime_proxy_failed", ShowRuntimeFailureClass.UNCLASSIFIED),
        (ShowRuntimeFailureDimension.INSTALL, "runtime_archive_checksum_mismatch", ShowRuntimeFailureClass.CHECKSUM),
        (ShowRuntimeFailureDimension.INSTALL, "runtime_platform_unsupported", ShowRuntimeFailureClass.PERMANENT),
        (ShowRuntimeFailureDimension.INSTALL, "runtime_node_unsupported", ShowRuntimeFailureClass.CONFIGURED),
        (ShowRuntimeFailureDimension.INSTALL, "runtime_source_unsupported", ShowRuntimeFailureClass.PERMANENT),
        (ShowRuntimeFailureDimension.INSTALL, "runtime_archive_url_unsupported", ShowRuntimeFailureClass.PERMANENT),
        (
            ShowRuntimeFailureDimension.INSTALL,
            "runtime_archive_unavailable_offline",
            ShowRuntimeFailureClass.CONFIGURED,
        ),
        (
            ShowRuntimeFailureDimension.INSTALL,
            "runtime_manifest_unavailable_offline",
            ShowRuntimeFailureClass.CONFIGURED,
        ),
    ],
)
def test_show_runtime_failure_classification(dimension, reason, expected):
    assert classify_show_runtime_failure(ShowRuntimeFailureEvidence(dimension, reason)) is expected


@pytest.mark.parametrize(
    ("provenance", "expected_class", "expected_action"),
    (
        (None, ShowRuntimeFailureClass.UNCLASSIFIED, ShowRuntimeRecoveryAction.REPAIR),
        ("configured", ShowRuntimeFailureClass.CONFIGURED, ShowRuntimeRecoveryAction.CHANGE_SETTING),
    ),
)
def test_runtime_child_exit_classification_uses_command_owner(
    provenance,
    expected_class,
    expected_action,
):
    evidence = ShowRuntimeFailureEvidence(
        ShowRuntimeFailureDimension.RUNTIME,
        "runtime_start_process_unavailable",
        provenance,
    )

    assert classify_show_runtime_failure(evidence) is expected_class
    assert show_runtime_recovery_action(evidence) is expected_action


@pytest.mark.parametrize(
    ("provenance", "expected_class", "expected_action"),
    (
        ("configured", ShowRuntimeFailureClass.CONFIGURED, ShowRuntimeRecoveryAction.CHANGE_SETTING),
        ("packaged", ShowRuntimeFailureClass.UNCLASSIFIED, ShowRuntimeRecoveryAction.REPAIR),
    ),
)
def test_manifest_failure_classification_uses_published_provenance(
    provenance,
    expected_class,
    expected_action,
):
    evidence = ShowRuntimeFailureEvidence(
        ShowRuntimeFailureDimension.INSTALL,
        "runtime_manifest_invalid",
        provenance,
    )

    assert classify_show_runtime_failure(evidence) is expected_class
    assert show_runtime_recovery_action(evidence) is expected_action


@pytest.mark.parametrize(
    ("provenance", "expected_class", "expected_action"),
    (
        ("configured", ShowRuntimeFailureClass.CONFIGURED, ShowRuntimeRecoveryAction.CHANGE_SETTING),
        ("packaged", ShowRuntimeFailureClass.UNCLASSIFIED, ShowRuntimeRecoveryAction.REPAIR),
    ),
)
def test_archive_failure_classification_uses_published_provenance(
    provenance,
    expected_class,
    expected_action,
):
    evidence = ShowRuntimeFailureEvidence(
        ShowRuntimeFailureDimension.INSTALL,
        "runtime_archive_missing",
        provenance,
    )

    assert classify_show_runtime_failure(evidence) is expected_class
    assert show_runtime_recovery_action(evidence) is expected_action


@pytest.mark.parametrize(
    ("reason", "provenance", "retryable", "expected_class", "expected_action"),
    (
        (
            "runtime_manifest_download_failed",
            "configured",
            False,
            ShowRuntimeFailureClass.CONFIGURED,
            ShowRuntimeRecoveryAction.CHANGE_SETTING,
        ),
        (
            "runtime_manifest_download_failed",
            "configured",
            True,
            ShowRuntimeFailureClass.TRANSIENT,
            ShowRuntimeRecoveryAction.REPAIR,
        ),
        (
            "runtime_archive_download_failed",
            "configured",
            False,
            ShowRuntimeFailureClass.CONFIGURED,
            ShowRuntimeRecoveryAction.CHANGE_SETTING,
        ),
        (
            "runtime_archive_download_failed",
            "packaged",
            False,
            ShowRuntimeFailureClass.UNCLASSIFIED,
            ShowRuntimeRecoveryAction.REPAIR,
        ),
        (
            "runtime_archive_download_failed",
            "configured",
            True,
            ShowRuntimeFailureClass.TRANSIENT,
            ShowRuntimeRecoveryAction.REPAIR,
        ),
        (
            "runtime_archive_download_failed",
            "packaged",
            True,
            ShowRuntimeFailureClass.TRANSIENT,
            ShowRuntimeRecoveryAction.REPAIR,
        ),
    ),
)
def test_download_failure_classification_uses_owner_evidence(
    reason,
    provenance,
    retryable,
    expected_class,
    expected_action,
):
    evidence = ShowRuntimeFailureEvidence(
        ShowRuntimeFailureDimension.INSTALL,
        reason,
        provenance,
        retryable,
    )

    assert classify_show_runtime_failure(evidence) is expected_class
    assert show_runtime_recovery_action(evidence) is expected_action


def test_show_runtime_failure_declarations_are_total_and_owner_safe():
    assert len(SHOW_RUNTIME_FAILURE_DECLARATIONS) == len(set(SHOW_RUNTIME_FAILURE_DECLARATIONS))
    assert all(
        declaration.owning_artifact and declaration.dimension
        for declaration in SHOW_RUNTIME_FAILURE_DECLARATIONS.values()
    )
    assert all(
        declaration.user_owned
        for declaration in SHOW_RUNTIME_FAILURE_DECLARATIONS.values()
        if declaration.failure_class is ShowRuntimeFailureClass.CONFIGURED
    )


def test_download_failure_declaration_keys_cover_owner_stored_fields():
    details_tree = ast.parse(textwrap.dedent(inspect.getsource(dependency_network.dependency_error_details)))
    owner_stored_fields: set[str] = set()

    class DownloadDetailFieldVisitor(ast.NodeVisitor):
        def visit_Dict(self, node: ast.Dict) -> None:
            owner_stored_fields.update(
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            )
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Attribute) and node.func.attr == "update":
                owner_stored_fields.update(keyword.arg for keyword in node.keywords if keyword.arg)
            self.generic_visit(node)

    DownloadDetailFieldVisitor().visit(details_tree)
    declaration_lookup_tree = ast.parse(
        textwrap.dedent(inspect.getsource(show_runtime_failures_module._failure_declaration))
    )
    declaration_key_fields = {
        node.attr
        for node in ast.walk(declaration_lookup_tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "evidence"
    }
    explicitly_irrelevant = {
        "attempts",
        "exception_type",
        "host",
        "http_status",
        "kind",
        "message",
        "retry_after_seconds",
        "url",
    }

    assert owner_stored_fields - explicitly_irrelevant <= declaration_key_fields


def test_archive_failure_provenance_census_matches_archive_path_emissions():
    manager_tree = ast.parse(textwrap.dedent(inspect.getsource(ShowRuntimeManager))).body[0]
    configured_archive_reasons: set[str] = set()

    def archive_path_guard(test: ast.expr) -> bool | None:
        if (
            isinstance(test, ast.Attribute)
            and isinstance(test.value, ast.Name)
            and test.value.id == "self"
            and test.attr == "archive_path"
        ):
            return True
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            nested = archive_path_guard(test.operand)
            return None if nested is None else not nested
        if (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None
        ):
            nested = archive_path_guard(test.left)
            if nested is None:
                return None
            if isinstance(test.ops[0], (ast.IsNot, ast.NotEq)):
                return True
            if isinstance(test.ops[0], (ast.Is, ast.Eq)):
                return False
        return None

    class ArchivePathEmissionVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.archive_path_is_set = False

        def visit_If(self, node: ast.If) -> None:
            previous = self.archive_path_is_set
            guard = archive_path_guard(node.test)
            self.archive_path_is_set = previous or guard is True
            for child in node.body:
                self.visit(child)
            self.archive_path_is_set = previous or guard is False
            for child in node.orelse:
                self.visit(child)
            self.archive_path_is_set = previous

        def visit_Constant(self, node: ast.Constant) -> None:
            if (
                self.archive_path_is_set
                and isinstance(node.value, str)
                and node.value.startswith("runtime_archive_")
            ):
                configured_archive_reasons.add(node.value)

    ArchivePathEmissionVisitor().visit(manager_tree)
    declared_provenance = {
        reason: {
            provenance
            for (declared_reason, provenance, _retryable), declaration in SHOW_RUNTIME_FAILURE_DECLARATIONS.items()
            if declared_reason == reason and "archive" in declaration.owning_artifact
        }
        for reason in configured_archive_reasons
    }
    assert declared_provenance == {
        reason: {"configured", "packaged"} for reason in configured_archive_reasons
    }
    assert {
        declaration.reason
        for declaration in SHOW_RUNTIME_FAILURE_DECLARATIONS.values()
        if declaration.provenance == "configured" and declaration.owning_artifact == "configured-archive"
    } == configured_archive_reasons


def test_show_runtime_reason_literals_have_declared_evidence():
    source = inspect.getsource(show_runtime)
    reason_literals = {
        value
        for value in re.findall(r'(["\'])(runtime_[a-z0-9_]+)\1', source)
        if value[1] not in {"runtime_id", "runtime_source", "runtime_version"}
    }
    declared = {reason for reason, _provenance, _retryable in SHOW_RUNTIME_FAILURE_DECLARATIONS}
    assert {value for _quote, value in reason_literals} <= declared


def test_show_runtime_dimensions_are_serialized_only_by_availability_owner():
    module_tree = ast.parse(inspect.getsource(show_runtime))
    offenders: list[tuple[int, tuple[str, ...], tuple[str, ...]]] = []

    class DimensionPayloadVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Dict(self, node: ast.Dict) -> None:
            keys = {
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            dimension_shape = "state" in keys and "reason" in keys and bool(
                keys & {"failure_class", "recovery_action", "base_url", "command", "install_dir"}
            )
            if dimension_shape and self.scope[-2:] != ["ShowRuntimeAvailability", "as_payload"]:
                offenders.append((node.lineno, tuple(self.scope), tuple(sorted(keys))))
            self.generic_visit(node)

    DimensionPayloadVisitor().visit(module_tree)
    assert offenders == []


def test_runtime_request_uses_existing_base_without_health_preprobe(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        command="/bin/runtime-cli",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )
    manager._base_url = "http://127.0.0.1:4173"
    manager._availability = manager._publish_runtime_availability(
        ShowRuntimeServingState.SERVING,
        manager._base_url,
    )
    monkeypatch.setattr(
        manager,
        "_healthy",
        lambda _base_url: (_ for _ in ()).throw(AssertionError("request path must not pre-probe")),
    )
    monkeypatch.setattr(manager, "_negotiate_context_key_capability", AsyncMock())
    transport = AsyncMock(return_value=SimpleNamespace(status_code=200))
    monkeypatch.setattr(manager, "_request_runtime_transport", transport)

    asyncio.run(
        manager.request(
            "GET",
            "/sessions/ses123/app/",
            envelope=ShowRuntimeProtocolEnvelope(ShowRuntimeContext.PRIVATE),
        )
    )

    assert transport.await_count == 1


def test_unreadable_manifest_is_typed_install_evidence(monkeypatch, tmp_path):
    manifest_path = tmp_path / "runtime-manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="manifest",
        manifest_path=manifest_path,
    )
    real_read_bytes = Path.read_bytes

    def unreadable(path):
        if path == manifest_path:
            raise PermissionError("manifest is unreadable")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)

    payload = manager.prepare(automatic=True)

    assert payload["ok"] is False
    assert payload["install"]["reason"] == "runtime_manifest_invalid"
    assert payload["install"]["failure_class"] == "configured"
    assert payload["install"]["recovery_action"] == "change_setting"


def test_invalid_packaged_manifest_remains_repairable(monkeypatch, tmp_path):
    packaged_manifest = tmp_path / show_runtime._RUNTIME_MANIFEST_RESOURCE
    packaged_manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("core.show_runtime.package_resources.files", lambda _package: tmp_path)
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="manifest",
    )

    payload = manager.prepare(automatic=True)

    assert payload["install"]["reason"] == "runtime_manifest_invalid"
    assert payload["install"]["failure_class"] == "unclassified"
    assert payload["install"]["recovery_action"] == "repair"


def test_new_install_admission_does_not_reuse_stale_failure_evidence(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
    )
    manager._install_reason = "runtime_node_missing"
    manager._download_error = {"kind": "network", "message": "stale download"}
    monkeypatch.setattr(
        manager,
        "_install_managed_runtime_locked",
        lambda **_kwargs: (_ for _ in ()).throw(PermissionError("runtime directory is not writable")),
    )

    payload = manager.prepare(automatic=True)

    assert payload["install"]["reason"] == "runtime_install_failed"
    assert payload["install"]["failure_class"] == "unclassified"
    assert payload["install"]["recovery_action"] == "repair"
    assert payload["status"]["download_error"] is None


def test_show_runtime_install_io_exception_is_structured(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
    )
    monkeypatch.setattr(
        manager,
        "_install_managed_runtime_locked",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("spawn failed")),
    )

    payload = manager.prepare(automatic=True)

    assert payload["ok"] is False
    assert payload["reason"] == "runtime_install_failed"


def test_install_guard_contention_is_not_an_attempt_failure(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
    )
    monkeypatch.setattr(
        manager,
        "_install_guard_locked",
        lambda: nullcontext((False, "runtime_install_guard_unavailable")),
    )

    payload = manager.prepare(automatic=True)

    assert payload["ok"] is False
    assert payload["reason"] == "runtime_install_guard_unavailable"
    assert payload["install"]["failure_class"] == "unclassified"


def test_failed_forced_replacement_does_not_block_installed_runtime(monkeypatch, tmp_path):
    command = [str(tmp_path / "runtime-cli")]
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
    )

    def install(*, force=None):
        manager._install_reason = "runtime_install_failed"
        return None

    monkeypatch.setattr(manager, "_install_npm_runtime", install)
    monkeypatch.setattr(manager, "_installed_managed_runtime_command", lambda *, offline: command)

    payload = manager.prepare(force=True, automatic=False)

    assert payload["ok"] is False
    assert payload["install"]["state"] == "installed"
    assert asyncio.run(manager._resolve_managed_command()) == command


def test_start_admission_stays_serialized(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        command="/bin/runtime-cli",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )
    observed = []
    real_admission = manager._admit_runtime_start

    async def observe_admission(*, automatic):
        observed.append(manager._lock.locked())
        return await real_admission(automatic=automatic)

    monkeypatch.setattr(manager, "_admit_runtime_start", observe_admission)
    monkeypatch.setattr(manager, "stop", lambda: None)
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: [command])
    monkeypatch.setattr(manager, "_read_startup_url", lambda *, deadline: asyncio.sleep(0, result=None))
    monkeypatch.setattr(
        "core.show_runtime.subprocess.Popen", lambda *_args, **_kwargs: SimpleNamespace(poll=lambda: None)
    )

    asyncio.run(manager.ensure())

    assert observed == [True]


@pytest.mark.parametrize(
    ("configured_env", "command", "expected_reason"),
    ((None, "'unterminated", "runtime_start_command_invalid"),),
)
def test_start_admission_publishes_malformed_configured_commands(
    monkeypatch,
    tmp_path,
    configured_env,
    command,
    expected_reason,
):
    if configured_env is None:
        monkeypatch.delenv("VIBE_SHOW_RUNTIME_NODE_BIN", raising=False)
    else:
        monkeypatch.setenv("VIBE_SHOW_RUNTIME_NODE_BIN", configured_env)
    manager = ShowRuntimeManager(
        command=command,
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )

    result = asyncio.run(manager.ensure())

    assert result.runtime_reason == expected_reason
    assert result.runtime_failure_class is ShowRuntimeFailureClass.CONFIGURED
    assert result.runtime_recovery_action is ShowRuntimeRecoveryAction.CHANGE_SETTING


def test_malformed_explicit_command_evidence_is_shared_by_status_prepare_and_start(
    monkeypatch,
    tmp_path,
):
    manager = ShowRuntimeManager(
        command="'unterminated",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
    )
    monkeypatch.setattr(
        manager,
        "_managed_bin_path",
        lambda: pytest.fail("an explicit command must not inspect a managed provider"),
    )
    monkeypatch.setattr(
        manager,
        "_install_managed_runtime_locked",
        lambda **_kwargs: pytest.fail("an explicit command must not enter managed installation"),
    )

    status = manager.status()
    prepared = manager.prepare()
    started = asyncio.run(manager.ensure())

    for runtime in (status["runtime"], prepared["runtime"], started.as_payload()["runtime"]):
        assert runtime == {
            "state": "start_failed",
            "reason": "runtime_start_command_invalid",
            "failure_class": "configured",
            "recovery_action": "change_setting",
            "base_url": None,
        }
    assert status["install"]["state"] == "absent"
    assert status["command"] is None
    assert status["reason"] == "runtime_start_command_invalid"
    assert prepared["ok"] is False
    assert prepared["reason"] == "runtime_start_command_invalid"


def test_managed_status_runtime_dimension_uses_availability_schema(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda _command: None)

    assert manager.status()["runtime"] == {
        "state": "unchecked",
        "reason": None,
        "failure_class": None,
        "recovery_action": None,
        "base_url": None,
    }


@pytest.mark.parametrize(
    ("provider", "resolver_name"),
    (
        ("manifest-cache", "_installed_manifest_runtime_command"),
        ("npm", None),
    ),
)
def test_installed_managed_provider_is_resolved_before_mutation(
    monkeypatch,
    tmp_path,
    provider,
    resolver_name,
):
    command = [str(tmp_path / f"{provider}-runtime")]
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source=provider,
    )
    if resolver_name == "_installed_manifest_runtime_command":
        monkeypatch.setattr(manager, resolver_name, lambda *, offline: command)
    elif resolver_name is not None:
        monkeypatch.setattr(manager, resolver_name, lambda: command)
    else:
        monkeypatch.setattr(manager, "_managed_bin_path", lambda: Path(command[0]))
        monkeypatch.setattr("core.show_runtime._resolve_executable_path", lambda _path: command[0])
    monkeypatch.setattr(
        manager,
        "_attempt_managed_install",
        lambda **_kwargs: pytest.fail("an installed runtime must not enter a mutating provider"),
    )

    availability = asyncio.run(manager._resolve_managed_availability())

    assert availability.command == command
    assert availability.install.value == "installed"


@pytest.mark.parametrize(
    ("installed", "force"),
    ((False, False), (True, True)),
    ids=("absent", "forced"),
)
def test_managed_resolution_mutates_when_install_is_owed(monkeypatch, tmp_path, installed, force):
    existing_command = [str(tmp_path / "existing-runtime")]
    installed_command = [str(tmp_path / "installed-runtime")]
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
        force_install=force,
    )
    disk_reads = []
    attempts = []

    def read_installed(*, offline):
        disk_reads.append(offline)
        return existing_command if installed else None

    def install(**kwargs):
        attempts.append(kwargs)
        availability = manager._publish_install_availability(command=installed_command)
        operation = show_runtime._ShowRuntimeOperationOutcome(
            show_runtime._ShowRuntimeOperationState.COMPLETED,
            None,
        )
        return availability, operation

    monkeypatch.setattr(manager, "_safe_installed_managed_runtime_command", read_installed)
    monkeypatch.setattr(manager, "_attempt_managed_install", install)

    availability = asyncio.run(manager._resolve_managed_availability())

    assert disk_reads == ([] if force else [True])
    assert attempts == [{"force": force, "offline": False, "automatic": True}]
    assert availability.command == installed_command


def test_remote_manifest_bypasses_disk_resolution_until_admission(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="manifest-cache",
        manifest_url="https://example.test/show-runtime-manifest.json",
    )
    command = [str(tmp_path / "installed-runtime")]
    attempts = []
    monkeypatch.setattr(
        manager,
        "_safe_installed_managed_runtime_command",
        lambda **_kwargs: pytest.fail("a remote manifest must be admitted before disk reuse"),
    )

    def install(**kwargs):
        attempts.append(kwargs)
        availability = manager._publish_install_availability(command=command)
        operation = show_runtime._ShowRuntimeOperationOutcome(
            show_runtime._ShowRuntimeOperationState.COMPLETED,
            None,
        )
        return availability, operation

    monkeypatch.setattr(manager, "_attempt_managed_install", install)

    availability = asyncio.run(manager._resolve_managed_availability())

    assert attempts == [{"force": False, "offline": False, "automatic": True}]
    assert availability.command == command


def test_explicit_runtime_ignores_unrelated_malformed_node_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_SHOW_RUNTIME_NODE_BIN", "'unterminated")
    manager = ShowRuntimeManager(
        command="/bin/runtime-cli",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr(manager, "_healthy", lambda _base_url: asyncio.sleep(0, result=False))
    monkeypatch.setattr(manager, "stop", lambda: None)
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: [command])
    monkeypatch.setattr(
        "core.show_runtime.subprocess.Popen", lambda *_args, **_kwargs: SimpleNamespace(poll=lambda: None)
    )
    monkeypatch.setattr(
        manager,
        "_read_startup_url",
        lambda *, deadline: asyncio.sleep(0, result=None),
    )

    result = asyncio.run(manager.ensure())

    assert result.runtime_reason == "runtime_start_url_timeout"
    assert result.runtime_reason != "runtime_start_node_command_invalid"


def test_managed_runtime_normalizes_malformed_node_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_SHOW_RUNTIME_NODE_BIN", "'unterminated")
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="archive",
    )

    result = manager.prepare(automatic=True)

    assert result["reason"] == "runtime_node_missing"
    assert result["install"]["failure_class"] == "configured"
    assert result["install"]["recovery_action"] == "change_setting"


def test_archive_explicit_attempt_does_not_fall_through_to_generic_install(monkeypatch, tmp_path):
    from core import show_runtime as srt

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="archive",
    )
    attempts = []

    def failed_archive(*, force, offline, automatic):
        attempts.append((force, offline, automatic))
        admission = manager._publish_install_availability(install_reason="runtime_archive_download_failed")
        return admission, srt._ShowRuntimeOperationOutcome(
            srt._ShowRuntimeOperationState.FAILED,
            "runtime_archive_download_failed",
        )

    monkeypatch.setattr(manager, "_attempt_managed_install", failed_archive)

    result = asyncio.run(manager._resolve_managed_availability(automatic=False))

    assert result.install_reason == "runtime_archive_download_failed"
    assert attempts == [(False, False, False)]


@pytest.mark.parametrize("entrypoint", ("request", "request_global"))
def test_runtime_request_transport_boundary_publishes_recovery_evidence(
    monkeypatch,
    tmp_path,
    entrypoint,
):
    manager = _runtime_manager_with_failing_transport(monkeypatch, tmp_path)

    async def request_runtime():
        if entrypoint == "request":
            return await manager.request(
                "GET",
                "/sessions/ses123/app/",
                envelope=ShowRuntimeProtocolEnvelope(ShowRuntimeContext.PRIVATE),
            )
        return await manager.request_global("GET", "/_show-runtime/vendor/hash/runtime.js")

    with pytest.raises(ShowRuntimeUnavailableError) as raised:
        asyncio.run(request_runtime())

    assert raised.value.reason == "runtime_proxy_failed"
    assert raised.value.failure_class is ShowRuntimeFailureClass.UNCLASSIFIED
    assert raised.value.recovery_action is ShowRuntimeRecoveryAction.REPAIR
    assert manager._base_url is None
    assert manager._availability.runtime is ShowRuntimeServingState.SERVING

    admissions = []

    async def admit(*_args, **_kwargs):
        admissions.append(True)
        return SimpleNamespace(available=True, base_url="http://127.0.0.1:49322")

    async def successful_request(_client, method, url, **_kwargs):
        return httpx.Response(200, request=httpx.Request(method, url))

    monkeypatch.setattr(manager, "ensure", admit)
    monkeypatch.setattr("core.show_runtime.httpx.AsyncClient.request", successful_request)
    assert asyncio.run(request_runtime()).status_code == 200
    assert admissions == [True]


def test_runtime_transport_response_does_not_invalidate_base_url(monkeypatch, tmp_path):
    manager = _runtime_manager_with_failing_transport(monkeypatch, tmp_path)
    base_url = manager._base_url

    async def server_error(_client, method, url, **_kwargs):
        return httpx.Response(503, request=httpx.Request(method, url))

    monkeypatch.setattr("core.show_runtime.httpx.AsyncClient.request", server_error)

    response = asyncio.run(manager.request_global("GET", "/health"))

    assert response.status_code == 503
    assert manager._base_url == base_url


def test_delayed_transport_failure_does_not_invalidate_replacement_process(monkeypatch, tmp_path):
    manager = _runtime_manager_with_failing_transport(monkeypatch, tmp_path)
    base_url = manager._base_url
    replacement_process = SimpleNamespace(pid=654, poll=lambda: None)

    async def fail_after_replacement(_client, method, url, **_kwargs):
        manager._process = replacement_process
        manager._base_url = base_url
        raise httpx.ConnectError("old runtime failed late", request=httpx.Request(method, url))

    monkeypatch.setattr("core.show_runtime.httpx.AsyncClient.request", fail_after_replacement)

    with pytest.raises(ShowRuntimeUnavailableError):
        asyncio.run(manager.request_global("GET", "/health"))

    assert manager._process is replacement_process
    assert manager._base_url == base_url


class _FailingWebSocketHandshake:
    def __init__(self, error, *, before_failure=None):
        self.error = error
        self.before_failure = before_failure

    async def _fail(self):
        if self.before_failure is not None:
            self.before_failure()
        raise self.error

    def __await__(self):
        return self._fail().__await__()

    async def __aenter__(self):
        return await self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _FailingWebSocketSession:
    def __init__(self, handshake):
        self.handshake = handshake

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    def ws_connect(self, *_args, **_kwargs):
        return self.handshake


def test_websocket_transport_failure_invalidates_runtime_and_readmits(monkeypatch, tmp_path):
    manager = _runtime_manager_with_failing_transport(monkeypatch, tmp_path)
    set_show_runtime_manager_for_tests(manager)
    handshake = _FailingWebSocketHandshake(ClientConnectionError("runtime connection refused"))
    monkeypatch.setattr(ui_server, "ClientSession", lambda: _FailingWebSocketSession(handshake))
    websocket = SimpleNamespace(url=SimpleNamespace(query=""))

    with pytest.raises(ClientConnectionError):
        asyncio.run(ui_server._proxy_show_runtime_websocket(websocket, "ses123"))

    assert manager._base_url is None

    admissions = []
    replacement_process = SimpleNamespace(pid=654, poll=lambda: None)

    async def admit(*, automatic=True):
        admissions.append((automatic, manager._base_url))
        manager._process = replacement_process
        manager._base_url = "http://127.0.0.1:49322"
        return manager._publish_runtime_availability(
            ShowRuntimeServingState.SERVING,
            manager._base_url,
        )

    monkeypatch.setattr(manager, "ensure", admit)

    target = asyncio.run(
        manager.websocket_target(
            "/show/ses123/__vite_hmr",
            envelope=ShowRuntimeProtocolEnvelope(ShowRuntimeContext.PRIVATE),
        )
    )

    assert admissions == [(True, None)]
    assert target.url == "ws://127.0.0.1:49322/show/ses123/__vite_hmr"


def test_delayed_websocket_failure_does_not_invalidate_replacement_process(monkeypatch, tmp_path):
    manager = _runtime_manager_with_failing_transport(monkeypatch, tmp_path)
    set_show_runtime_manager_for_tests(manager)
    base_url = manager._base_url
    replacement_process = SimpleNamespace(pid=654, poll=lambda: None)

    def replace_runtime():
        manager._process = replacement_process
        manager._base_url = base_url

    handshake = _FailingWebSocketHandshake(
        ClientConnectionError("old runtime failed late"),
        before_failure=replace_runtime,
    )
    monkeypatch.setattr(ui_server, "ClientSession", lambda: _FailingWebSocketSession(handshake))
    websocket = SimpleNamespace(url=SimpleNamespace(query=""))

    with pytest.raises(ClientConnectionError):
        asyncio.run(ui_server._proxy_show_runtime_websocket(websocket, "ses123"))

    assert manager._process is replacement_process
    assert manager._base_url == base_url


def test_websocket_handshake_response_does_not_invalidate_runtime(monkeypatch, tmp_path):
    manager = _runtime_manager_with_failing_transport(monkeypatch, tmp_path)
    set_show_runtime_manager_for_tests(manager)
    base_url = manager._base_url
    handshake = _FailingWebSocketHandshake(
        WSServerHandshakeError(
            request_info=None,
            history=(),
            status=503,
            message="runtime rejected websocket upgrade",
            headers=None,
        )
    )
    monkeypatch.setattr(ui_server, "ClientSession", lambda: _FailingWebSocketSession(handshake))
    websocket = SimpleNamespace(url=SimpleNamespace(query=""))

    with pytest.raises(WSServerHandshakeError):
        asyncio.run(ui_server._proxy_show_runtime_websocket(websocket, "ses123"))

    assert manager._base_url == base_url


def test_runtime_transport_programming_error_stays_loud(monkeypatch, tmp_path):
    manager = _runtime_manager_with_failing_transport(monkeypatch, tmp_path)
    base_url = manager._base_url
    error = TypeError("request construction defect")

    async def fail_programming(_client, _method, _url, **_kwargs):
        raise error

    monkeypatch.setattr("core.show_runtime.httpx.AsyncClient.request", fail_programming)

    with pytest.raises(TypeError) as raised:
        asyncio.run(manager.request_global("GET", "/health"))

    assert raised.value is error
    assert manager._base_url == base_url


def test_recovery_fact_consumption_census_has_no_default_producer():
    facts = ("reason", "failure_class", "recovery_action")
    projection = ast.parse(textwrap.dedent(inspect.getsource(ui_server._show_page_runtime_failure_evidence)))
    assert not any(isinstance(node, (ast.If, ast.IfExp)) for node in ast.walk(projection))
    returns = [node for node in ast.walk(projection) if isinstance(node, ast.Return)]
    assert len(returns) == 1
    assert isinstance(returns[0].value, ast.Tuple)
    assert tuple(element.attr for element in returns[0].value.elts if isinstance(element, ast.Attribute)) == facts

    exception_owner = ast.parse(textwrap.dedent(inspect.getsource(ShowRuntimeUnavailableError.__init__)))
    assignments = {
        target.attr
        for node in ast.walk(exception_owner)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self"
    }
    assert assignments == set(facts)

    response_source = inspect.getsource(ui_server._show_page_recovery_response)
    html_source = inspect.getsource(show_pages_module.show_page_runtime_recovery_html)
    for fact, header, dataset in (
        ("reason", "X-Avibe-Show-Recovery-Reason", "data-show-runtime-reason"),
        ("failure_class", "X-Avibe-Show-Recovery-Class", "data-show-runtime-class"),
    ):
        assert response_source.count(header) == 1
        assert fact in response_source
        assert html_source.count(dataset) == 1
        assert fact in html_source

    recovery_source = inspect.getsource(show_pages_module._show_runtime_recovery_script)
    assert '|| "runtime_unavailable"' not in recovery_source
    assert '|| "unclassified"' not in recovery_source
    assert "checksRemaining" not in recovery_source
    assert "X-Avibe-Show-Recovery-Poll" not in recovery_source


def test_runtime_http_transport_census_closes_every_direct_client_path():
    manager_tree = ast.parse(textwrap.dedent(inspect.getsource(ShowRuntimeManager)))
    functions = {
        node.name: node
        for node in manager_tree.body[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    direct_clients = {
        name
        for name, function in functions.items()
        if any(isinstance(node, ast.Attribute) and node.attr == "AsyncClient" for node in ast.walk(function))
    }
    assert direct_clients == {
        "_healthy",
        "_probe_capabilities_payload",
        "_request_runtime_transport",
    }
    for request_owner in ("request", "request_global"):
        calls = [
            node
            for node in ast.walk(functions[request_owner])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_request_runtime_transport"
        ]
        assert len(calls) == 1
    for internal_probe in ("_healthy", "_probe_capabilities_payload"):
        assert any(isinstance(node, ast.Try) and node.handlers for node in ast.walk(functions[internal_probe]))


def test_provider_install_entrypoints_converge_on_single_admission_owner():
    manager_tree = ast.parse(textwrap.dedent(inspect.getsource(ShowRuntimeManager))).body[0]
    functions = {
        node.name: node for node in manager_tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    provider_methods = {
        "_install_manifest_runtime_locked",
        "_install_archive_runtime",
        "_install_npm_runtime",
    }
    direct_provider_callers = {
        function.name
        for function in functions.values()
        if any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in provider_methods
            for node in ast.walk(function)
        )
    }
    assert direct_provider_callers == {"_install_managed_runtime_locked"}

    owner_callers = {
        function.name
        for function in functions.values()
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_install_managed_runtime_locked"
            for node in ast.walk(function)
        )
    }
    assert owner_callers == {"_attempt_managed_install"}

    admission_callers = {
        function.name
        for function in functions.values()
        if any(
            isinstance(node, ast.Attribute) and node.attr == "_attempt_managed_install" for node in ast.walk(function)
        )
    }
    assert admission_callers == {"_resolve_managed_availability", "_prepare"}


def test_explicit_command_resolution_has_one_owner_and_four_consumers():
    manager_tree = ast.parse(textwrap.dedent(inspect.getsource(ShowRuntimeManager))).body[0]
    functions = {
        node.name: node for node in manager_tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    direct_resolvers = {
        function.name
        for function in functions.values()
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_resolve_command"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and isinstance(node.args[0].value, ast.Name)
            and node.args[0].value.id == "self"
            and node.args[0].attr == "command"
            for node in ast.walk(function)
        )
    }
    assert direct_resolvers == {"_resolve_explicit_command_availability"}

    consumers = {
        function.name
        for function in functions.values()
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_resolve_explicit_command_availability"
            for node in ast.walk(function)
        )
    }
    assert consumers == {
        "_admit_runtime_start",
        "_managed_install_preflight",
        "_resolve_managed_availability",
        "_status",
    }


@pytest.mark.parametrize(
    ("spawn_error", "expected_reason", "expected_class", "expected_action"),
    (
        (
            FileNotFoundError(errno.ENOENT, "command disappeared"),
            "runtime_start_command_invalid",
            ShowRuntimeFailureClass.CONFIGURED,
            ShowRuntimeRecoveryAction.CHANGE_SETTING,
        ),
        (
            OSError(errno.EMFILE, "process table unavailable"),
            "runtime_start_attempt_failed",
            ShowRuntimeFailureClass.UNCLASSIFIED,
            ShowRuntimeRecoveryAction.REPAIR,
        ),
    ),
)
def test_start_admission_normalizes_spawn_exceptions(
    monkeypatch,
    tmp_path,
    spawn_error,
    expected_reason,
    expected_class,
    expected_action,
):
    manager = ShowRuntimeManager(
        command="/bin/runtime-cli",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )
    spawn_calls = []

    def fail_spawn(*_args, **_kwargs):
        spawn_calls.append(True)
        raise spawn_error

    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: [command])
    monkeypatch.setattr("core.show_runtime.subprocess.Popen", fail_spawn)

    first = asyncio.run(manager.ensure())
    second = asyncio.run(manager.ensure())

    assert first.reason == expected_reason
    assert first.runtime_failure_class is expected_class
    assert first.runtime_recovery_action is expected_action
    assert second.reason == expected_reason
    assert second.runtime_recovery_action is expected_action
    assert spawn_calls == [True, True]


def test_managed_spawn_command_loss_is_repairable_evidence(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
    )

    async def managed_command(*, automatic):
        return manager._publish_install_availability(command=["managed-runtime"])

    monkeypatch.setattr(manager, "_resolve_managed_availability", managed_command)
    monkeypatch.setattr(
        "core.show_runtime.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError(errno.ENOENT, "gone")),
    )

    result = asyncio.run(manager.ensure())

    assert result.runtime_reason == "runtime_start_command_unavailable"
    assert result.runtime_failure_class is ShowRuntimeFailureClass.UNCLASSIFIED
    assert result.runtime_recovery_action is ShowRuntimeRecoveryAction.REPAIR


@pytest.mark.parametrize("failure_phase", ("establish", "readiness"))
def test_start_admission_closes_establishment_and_readiness_exceptions(
    monkeypatch,
    tmp_path,
    failure_phase,
):
    manager = ShowRuntimeManager(
        command="/bin/runtime-cli",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )
    attempts = []

    class FakeProcess:
        def poll(self):
            return None

    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: [command])

    def stop():
        manager._process = None
        manager._base_url = None

    monkeypatch.setattr(manager, "stop", stop)
    if failure_phase == "establish":
        real_mkdir = Path.mkdir

        def fail_runtime_mkdir(path, *args, **kwargs):
            if path == manager.runtime_dir:
                attempts.append("establish")
                raise PermissionError("runtime directory is read-only")
            return real_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fail_runtime_mkdir)
    else:
        monkeypatch.setattr(
            "core.show_runtime.subprocess.Popen",
            lambda *_args, **_kwargs: attempts.append("readiness") or FakeProcess(),
        )

        async def fail_readiness(*, deadline):
            raise OSError("startup log became unreadable")

        monkeypatch.setattr(manager, "_read_startup_url", fail_readiness)

    first = asyncio.run(manager.ensure())
    second = asyncio.run(manager.ensure())

    assert first.reason == "runtime_start_attempt_failed"
    assert first.runtime_failure_class is ShowRuntimeFailureClass.UNCLASSIFIED
    assert second.reason == "runtime_start_attempt_failed"
    assert attempts == [failure_phase, failure_phase]


def test_cancelled_start_admission_stops_spawned_child_before_propagating(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        command="/bin/runtime-cli",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )
    started = asyncio.Event()
    stopped = []

    class FakeProcess:
        def poll(self):
            return None

    async def wait_forever(*, deadline):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: [command])
    monkeypatch.setattr(
        "core.show_runtime.subprocess.Popen",
        lambda *_args, **_kwargs: FakeProcess(),
    )
    monkeypatch.setattr(manager, "_read_startup_url", wait_forever)

    def stop():
        if manager._process is not None:
            stopped.append(manager._process)
        manager._process = None
        manager._base_url = None

    monkeypatch.setattr(manager, "stop", stop)

    async def cancel_start():
        task = asyncio.create_task(manager.ensure())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_start())

    assert len(stopped) == 1
    assert manager._process is None


def test_start_owner_normalizes_install_resolution_io_failure(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
    )

    async def fail_install_resolution(*, automatic):
        raise OSError("install owner escaped unexpectedly")

    monkeypatch.setattr(manager, "_resolve_managed_availability", fail_install_resolution)

    result = asyncio.run(manager.ensure())

    assert result.runtime_reason == "runtime_start_attempt_failed"
    assert result.runtime_failure_class is ShowRuntimeFailureClass.UNCLASSIFIED
    assert result.runtime_recovery_action is ShowRuntimeRecoveryAction.REPAIR


@pytest.mark.parametrize("owner", ("install", "start"))
def test_admission_owners_propagate_programming_errors(monkeypatch, tmp_path, owner):
    manager = ShowRuntimeManager(
        command="/bin/runtime-cli" if owner == "start" else None,
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
    )
    if owner == "install":
        monkeypatch.setattr(
            manager,
            "_install_managed_runtime_locked",
            lambda **_kwargs: (_ for _ in ()).throw(TypeError("provider contract defect")),
        )
        operation = lambda: manager.prepare(automatic=True)
    else:
        monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: [command])
        monkeypatch.setattr(
            "core.show_runtime.subprocess.Popen",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(TypeError("spawn contract defect")),
        )
        operation = lambda: asyncio.run(manager.ensure())

    with pytest.raises(TypeError, match="contract defect"):
        operation()


def test_show_runtime_manager_passes_runtime_options(monkeypatch, tmp_path):
    captured = {}

    class FakeProcess:
        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return FakeProcess()

    async def fake_startup_url(*, deadline):
        captured["startup_deadline"] = deadline
        return "http://127.0.0.1:12345"

    manager = ShowRuntimeManager(
        command="/bin/echo",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: [command])
    monkeypatch.setattr("core.show_runtime.subprocess.Popen", fake_popen)
    monkeypatch.setattr(manager, "_read_startup_url", fake_startup_url)
    monkeypatch.setattr(manager, "_healthy", lambda _base_url: asyncio.sleep(0, result=True))

    result = asyncio.run(manager.ensure())

    assert result.available is True
    cache_index = captured["command"].index("--cache-root")
    assert captured["command"][cache_index + 1] == str(tmp_path / "runtime" / "vite-cache")
    index = captured["command"].index("--fallback-delay-seconds")
    assert captured["command"][index + 1] == str(SHOW_RUNTIME_CLI_FALLBACK_DELAY_SECONDS)
    assert captured["startup_deadline"] > 0


def test_show_runtime_manager_retries_health_until_ready(monkeypatch, tmp_path):
    class FakeProcess:
        def poll(self):
            return None

    health_results = iter([False, True])
    manager = ShowRuntimeManager(
        command="/bin/runtime-cli",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )

    monkeypatch.setattr("core.show_runtime._STARTUP_READY_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr("core.show_runtime._STARTUP_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: [command])
    monkeypatch.setattr("core.show_runtime.subprocess.Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(
        manager,
        "_read_startup_url",
        lambda *, deadline: asyncio.sleep(0, result="http://127.0.0.1:12345"),
    )
    monkeypatch.setattr(
        manager,
        "_healthy",
        lambda _base_url: asyncio.sleep(0, result=next(health_results)),
    )

    result = asyncio.run(manager.ensure())

    assert result.available is True
    assert result.base_url == "http://127.0.0.1:12345"


def test_show_runtime_manager_accepts_slow_health_within_shared_startup_budget(monkeypatch, tmp_path):
    class FakeProcess:
        def poll(self):
            return None

    manager = ShowRuntimeManager(
        command="/bin/runtime-cli",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )
    stopped_live_processes = []
    health_attempts = []

    def fake_stop():
        stopped_live_processes.append(manager._process is not None)
        manager._process = None
        manager._base_url = None

    async def slow_startup_url(*, deadline):
        await asyncio.sleep(0.02)
        return "http://127.0.0.1:12345"

    async def slow_health(_base_url):
        health_attempts.append(True)
        await asyncio.sleep(0.02)
        return len(health_attempts) > 1

    monkeypatch.setattr("core.show_runtime._STARTUP_READY_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr("core.show_runtime._STARTUP_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: [command])
    monkeypatch.setattr("core.show_runtime.subprocess.Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(manager, "_read_startup_url", slow_startup_url)
    monkeypatch.setattr(manager, "_healthy", slow_health)
    monkeypatch.setattr(manager, "stop", fake_stop)

    result = asyncio.run(manager.ensure())

    assert result.available is True
    assert len(health_attempts) == 2
    assert stopped_live_processes == [False]


def test_show_runtime_manager_reports_health_timeout_after_retrying(monkeypatch, tmp_path):
    class FakeProcess:
        def poll(self):
            return None

    manager = ShowRuntimeManager(
        command="/bin/runtime-cli",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )
    health_attempts = []
    stopped_live_processes = []

    def fake_stop():
        stopped_live_processes.append(manager._process is not None)
        manager._process = None
        manager._base_url = None

    monkeypatch.setattr("core.show_runtime._STARTUP_READY_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr("core.show_runtime._STARTUP_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: [command])
    monkeypatch.setattr("core.show_runtime.subprocess.Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(
        manager,
        "_read_startup_url",
        lambda *, deadline: asyncio.sleep(0, result="http://127.0.0.1:12345"),
    )
    monkeypatch.setattr(
        manager,
        "_healthy",
        lambda _base_url: health_attempts.append(True) or asyncio.sleep(0, result=False),
    )
    monkeypatch.setattr(manager, "stop", fake_stop)

    result = asyncio.run(manager.ensure())

    assert result.available is False
    assert result.reason == "runtime_start_health_timeout"
    assert len(health_attempts) > 1
    assert stopped_live_processes == [False, True]


def test_show_runtime_manager_bounds_in_flight_health_probe_by_shared_deadline(monkeypatch, tmp_path):
    class FakeProcess:
        def poll(self):
            return None

    manager = ShowRuntimeManager(
        command="/bin/runtime-cli",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )

    monkeypatch.setattr("core.show_runtime._STARTUP_READY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: [command])
    monkeypatch.setattr("core.show_runtime.subprocess.Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(
        manager,
        "_read_startup_url",
        lambda *, deadline: asyncio.sleep(0, result="http://127.0.0.1:12345"),
    )
    monkeypatch.setattr(manager, "_healthy", lambda _base_url: asyncio.sleep(1, result=True))
    monkeypatch.setattr(manager, "stop", lambda: setattr(manager, "_process", None))

    result = asyncio.run(manager.ensure())

    assert result.available is False
    assert result.reason == "runtime_start_health_timeout"


def test_show_runtime_manager_reports_url_timeout_separately(monkeypatch, tmp_path):
    class FakeProcess:
        def poll(self):
            return None

    manager = ShowRuntimeManager(
        command="/bin/runtime-cli",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )

    def fake_stop():
        manager._process = None
        manager._base_url = None

    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: [command])
    monkeypatch.setattr("core.show_runtime.subprocess.Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(manager, "_read_startup_url", lambda *, deadline: asyncio.sleep(0, result=None))
    monkeypatch.setattr(manager, "stop", fake_stop)

    result = asyncio.run(manager.ensure())

    assert result.available is False
    assert result.reason == "runtime_start_url_timeout"


def test_explicit_runtime_child_exit_publishes_configured_recovery_evidence(monkeypatch, tmp_path):
    started = tmp_path / "configured-runtime-started"
    runtime_script = tmp_path / "configured-runtime.py"
    runtime_script.write_text(
        "from pathlib import Path\n"
        "from time import sleep\n"
        f"Path({str(started)!r}).touch()\n"
        "sleep(0.05)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_SHOW_RUNTIME_BIN", shlex.join([sys.executable, str(runtime_script)]))
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr(manager, "_sweep_orphan_runtime_servers", lambda: None)

    result = asyncio.run(manager.ensure())

    assert started.exists()
    assert result.reason == "runtime_start_process_unavailable"
    assert result.failure_class is ShowRuntimeFailureClass.CONFIGURED
    assert result.recovery_action is ShowRuntimeRecoveryAction.CHANGE_SETTING


def test_show_runtime_manager_rejects_process_that_exits_before_health(monkeypatch, tmp_path):
    health_checks = []

    class ExitedProcess:
        def poll(self):
            return 1

    manager = ShowRuntimeManager(
        command="/bin/runtime-cli",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: [command])
    monkeypatch.setattr("core.show_runtime.subprocess.Popen", lambda *_args, **_kwargs: ExitedProcess())
    monkeypatch.setattr(
        manager,
        "_read_startup_url",
        lambda *, deadline: asyncio.sleep(0, result="http://127.0.0.1:12345"),
    )
    monkeypatch.setattr(
        manager,
        "_healthy",
        lambda base_url: health_checks.append(base_url) or asyncio.sleep(0, result=True),
    )

    result = asyncio.run(manager.ensure())

    assert result.available is False
    assert result.reason == "runtime_start_process_unavailable"
    assert health_checks == []
    assert manager._process is None


def test_show_runtime_manager_rejects_process_that_exits_after_health(monkeypatch, tmp_path):
    class ExitsAfterHealthProcess:
        health_completed = False

        def poll(self):
            return 1 if self.health_completed else None

    process = ExitsAfterHealthProcess()
    manager = ShowRuntimeManager(
        command="/bin/runtime-cli",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )

    async def healthy_then_exit(_base_url):
        process.health_completed = True
        return True

    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: [command])
    monkeypatch.setattr("core.show_runtime.subprocess.Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        manager,
        "_read_startup_url",
        lambda *, deadline: asyncio.sleep(0, result="http://127.0.0.1:12345"),
    )
    monkeypatch.setattr(manager, "_healthy", healthy_then_exit)

    result = asyncio.run(manager.ensure())

    assert result.available is False
    assert result.reason == "runtime_start_process_unavailable"
    assert manager._process is None


def test_show_runtime_manager_prewarm_loads_entry_module(monkeypatch, tmp_path):
    responses = {
        "/sessions/ses123/app/": (
            200,
            b'<script type="module" src="/show/ses123/src/main.tsx"></script>',
            {"content-type": "text/html"},
        ),
        "/sessions/ses123/app/src/main.tsx": (
            200,
            b'import App from "/show/ses123/src/App.tsx";',
            {"content-type": "text/javascript"},
        ),
        "/sessions/ses123/app/src/App.tsx": (
            200,
            b'import { Button } from "/show/ses123/@fs/runtime/packages/ui/dist/button.js";',
            {"content-type": "text/javascript"},
        ),
        "/sessions/ses123/app/@fs/runtime/packages/ui/dist/button.js": (
            200,
            b'import { jsx } from "/show/ses123/@fs/runtime/vite-cache/deps/react_jsx-runtime.js?v=abc";',
            {"content-type": "text/javascript"},
        ),
        "/sessions/ses123/app/@fs/runtime/vite-cache/deps/react_jsx-runtime.js?v=abc": (
            200,
            b"export const jsx = () => null;",
            {"content-type": "text/javascript"},
        ),
    }
    calls = []

    async def fake_request(self, method, path, *, envelope, headers=None, body=None):
        import httpx

        calls.append((method, path, envelope.headers(headers), body))
        status, content, headers_out = responses[path]
        return httpx.Response(status, content=content, headers=headers_out)

    manager = ShowRuntimeManager(
        command="/bin/echo",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr(ShowRuntimeManager, "request", fake_request)

    result = asyncio.run(
        manager.prewarm_session(
            "ses123",
            context=ShowRuntimeContext.PRIVATE,
        )
    )

    assert result.available is True
    private_headers = {
        "X-Avibe-Show-Protocol": "1",
        "X-Avibe-Show-Context": "private",
    }
    assert calls == [
        ("GET", "/sessions/ses123/app/", private_headers, None),
        ("GET", "/sessions/ses123/app/src/main.tsx", private_headers, None),
        ("GET", "/sessions/ses123/app/src/App.tsx", private_headers, None),
        (
            "GET",
            "/sessions/ses123/app/@fs/runtime/packages/ui/dist/button.js",
            private_headers,
            None,
        ),
        (
            "GET",
            "/sessions/ses123/app/@fs/runtime/vite-cache/deps/react_jsx-runtime.js?v=abc",
            private_headers,
            None,
        ),
    ]


def test_show_runtime_manager_prewarm_reports_nested_module_failures(monkeypatch, tmp_path):
    responses = {
        "/sessions/ses123/app/": (
            200,
            b'<script type="module" src="/show/ses123/src/main.tsx"></script>',
            {"content-type": "text/html"},
        ),
        "/sessions/ses123/app/src/main.tsx": (
            200,
            b'import App from "/show/ses123/src/App.tsx";',
            {"content-type": "text/javascript"},
        ),
        "/sessions/ses123/app/src/App.tsx": (
            504,
            b"timeout",
            {"content-type": "text/plain"},
        ),
    }

    async def fake_request(self, method, path, *, envelope, headers=None, body=None):
        import httpx

        status, content, headers_out = responses[path]
        return httpx.Response(status, content=content, headers=headers_out)

    manager = ShowRuntimeManager(
        command="/bin/echo",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr(ShowRuntimeManager, "request", fake_request)

    result = asyncio.run(
        manager.prewarm_session(
            "ses123",
            context=ShowRuntimeContext.SHARED,
        )
    )

    assert result.available is False
    assert result.reason == "session_prewarm_module_failed:504:/sessions/ses123/app/src/App.tsx"


def test_show_runtime_manager_uses_managed_runtime_bin(tmp_path):
    runtime_dir = tmp_path / "runtime with spaces"
    bin_path = runtime_dir / "package" / "node_modules" / ".bin" / "avibe-show-runtime"
    bin_path.parent.mkdir(parents=True)
    bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_path.chmod(0o755)

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="npm",
        auto_install=False,
    )

    assert asyncio.run(manager._resolve_managed_command()) == [str(bin_path)]


def test_show_runtime_archive_platform_tag_maps_macos_universal2_to_machine(monkeypatch):
    monkeypatch.setattr("core.managed_runtime.get_platform", lambda: "macosx-14.0-universal2")
    monkeypatch.setattr("core.managed_runtime.platform.machine", lambda: "arm64")

    assert runtime_platform_tag() == "darwin-arm64"


def test_show_runtime_manager_installs_from_prebuilt_archive(monkeypatch, tmp_path):
    archive_root = tmp_path / "archive-root"
    cli_path = archive_root / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    archive_path = tmp_path / "vibe-show-runtime-node.tgz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(archive_root / "node_modules", arcname="node_modules")

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="archive",
        archive_path=archive_path,
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)

    command = manager._install_managed_runtime_locked(force=False, offline=False).command

    assert command is not None
    assert command[0] == "/bin/node"
    assert Path(command[1]).parent.parent.parent.parent.parent.parent == tmp_path / "runtime" / "prebuilt" / "versions"
    assert json.loads((tmp_path / "runtime" / "prebuilt" / "current.json").read_text())["install_dir"] in command[1]
    assert manager._install_reason is None


def test_show_runtime_manager_installs_prebuilt_archive_with_internal_symlinks(monkeypatch, tmp_path):
    archive_root = tmp_path / "archive-root"
    package_dir = archive_root / "packages" / "runtime"
    cli_path = package_dir / "dist" / "cli.js"
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    scope_dir = archive_root / "node_modules" / "@avibe"
    bin_dir = archive_root / "node_modules" / ".bin"
    scope_dir.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    (scope_dir / "show-runtime").symlink_to("../../packages/runtime")
    (bin_dir / "avibe-show-runtime").symlink_to("../@avibe/show-runtime/dist/cli.js")
    archive_path = tmp_path / "vibe-show-runtime-node.tgz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(archive_root / "packages", arcname="packages")
        tar.add(archive_root / "node_modules", arcname="node_modules")

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="archive",
        archive_path=archive_path,
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)

    command = manager._install_managed_runtime_locked(force=False, offline=False).command

    assert command is not None
    assert command[0] == "/bin/node"
    assert "/prebuilt/versions/" in command[1]
    assert Path(command[1]).resolve().read_text(encoding="utf-8") == "#!/usr/bin/env node\n"
    assert manager._install_reason is None


def test_show_runtime_safe_extract_rejects_external_symlink(tmp_path):
    archive_root = tmp_path / "archive-root"
    archive_root.mkdir()
    (archive_root / "escape").symlink_to("../../outside")
    archive_path = tmp_path / "unsafe.tgz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(archive_root / "escape", arcname="escape")

    with tarfile.open(archive_path, "r:gz") as tar:
        with pytest.raises(ValueError, match="Unsafe managed runtime archive link target"):
            safe_extract_tar(tar, tmp_path / "destination")


def test_show_runtime_safe_extract_rejects_external_hardlink(tmp_path):
    archive_path = tmp_path / "unsafe-hardlink.tgz"
    with tarfile.open(archive_path, "w:gz") as tar:
        data = b"safe\n"
        safe = tarfile.TarInfo("safe")
        safe.size = len(data)
        tar.addfile(safe, io.BytesIO(data))
        hardlink = tarfile.TarInfo("dir/h")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "../outside"
        tar.addfile(hardlink)

    with tarfile.open(archive_path, "r:gz") as tar:
        with pytest.raises(ValueError, match="Unsafe managed runtime archive link target"):
            safe_extract_tar(tar, tmp_path / "destination")


def test_show_runtime_manager_reuses_installed_prebuilt_runtime_without_archive(monkeypatch, tmp_path):
    cli_path = tmp_path / "runtime" / "prebuilt" / "current" / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text("#!/usr/bin/env node\n", encoding="utf-8")

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="archive",
        archive_path=tmp_path / "missing.tgz",
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)

    assert manager._install_managed_runtime_locked(force=False, offline=False).command == ["/bin/node", str(cli_path)]
    assert manager._install_reason is None


@pytest.mark.parametrize(
    ("configured", "expected_class", "expected_action"),
    (
        (True, "configured", "change_setting"),
        (False, "unclassified", "repair"),
    ),
)
def test_missing_archive_publishes_source_owned_recovery_evidence(
    monkeypatch,
    tmp_path,
    configured,
    expected_class,
    expected_action,
):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="archive",
        archive_path=tmp_path / "missing.tgz" if configured else None,
        archive_url=None if configured else "",
    )
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )
    monkeypatch.setattr(manager, "_copy_packaged_runtime_archive", lambda: None)

    result = manager.prepare()

    assert result["reason"] == "runtime_archive_missing"
    assert result["install"]["failure_class"] == expected_class
    assert result["install"]["recovery_action"] == expected_action


def test_show_runtime_manager_forced_archive_fallback_reports_failed_operation_and_installed_state(
    monkeypatch,
    tmp_path,
):
    cli_path = (
        tmp_path
        / "runtime"
        / "prebuilt"
        / "current"
        / "node_modules"
        / "@avibe"
        / "show-runtime"
        / "dist"
        / "cli.js"
    )
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="archive",
        archive_path=tmp_path / "missing.tgz",
    )
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )

    result = manager.prepare(force=True)

    assert result["ok"] is False
    assert result["reason"] == "runtime_archive_missing"
    assert result["command"] == ["/bin/node", str(cli_path)]
    assert result["install"]["state"] == "installed"
    assert result["install"]["reason"] is None
    assert result["status"]["install"]["state"] == "installed"
    assert result["status"]["command"] == ["/bin/node", str(cli_path)]
    assert result["status"]["reason"] is None


def test_show_runtime_manager_archive_source_honors_offline_mode(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="archive",
        offline=True,
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)
    monkeypatch.setattr(manager, "_download_runtime_archive", lambda archive_url: (_ for _ in ()).throw(AssertionError("network")))

    result = manager.prepare()

    assert result["ok"] is False
    assert result["reason"] == "runtime_archive_unavailable_offline"


def test_show_runtime_manager_refreshes_stale_prebuilt_archive(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    installed_cli = runtime_dir / "prebuilt" / "current" / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"
    installed_cli.parent.mkdir(parents=True)
    installed_cli.write_text("old runtime\n", encoding="utf-8")

    archive_root = tmp_path / "archive-root"
    archive_cli = archive_root / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"
    archive_cli.parent.mkdir(parents=True)
    archive_cli.write_text("new runtime\n", encoding="utf-8")
    archive_path = tmp_path / "vibe-show-runtime-node.tgz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(archive_root / "node_modules", arcname="node_modules")

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="archive",
        archive_path=archive_path,
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)

    command = asyncio.run(manager._resolve_managed_command())

    assert command is not None
    assert command != ["/bin/node", str(installed_cli)]
    assert Path(command[1]).read_text(encoding="utf-8") == "new runtime\n"
    assert installed_cli.read_text(encoding="utf-8") == "old runtime\n"


def test_show_runtime_manager_force_refreshes_matching_prebuilt_archive(monkeypatch, tmp_path):
    archive_root = tmp_path / "archive-root"
    archive_cli = archive_root / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"
    archive_cli.parent.mkdir(parents=True)
    archive_cli.write_text("healthy runtime\n", encoding="utf-8")
    archive_path = tmp_path / "vibe-show-runtime-node.tgz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(archive_root / "node_modules", arcname="node_modules")

    runtime_dir = tmp_path / "runtime"
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="archive",
        archive_path=archive_path,
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)

    first = manager.prepare()
    installed_cli = Path(first["command"][1])
    installed_cli.write_text("corrupt runtime\n", encoding="utf-8")

    repaired = manager.prepare(force=True)

    assert repaired["ok"] is True
    assert repaired["command"] != first["command"]
    assert Path(repaired["command"][1]).read_text(encoding="utf-8") == "healthy runtime\n"
    assert installed_cli.read_text(encoding="utf-8") == "corrupt runtime\n"


def test_show_runtime_manager_installs_from_manifest_cache(monkeypatch, tmp_path):
    archive_path = _write_runtime_archive(tmp_path)
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    runtime_dir = tmp_path / "runtime"
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)

    result = manager.prepare()
    shared_manager = manager._shared_manifest_manager(offline=True)
    manifest = shared_manager.load_manifest(allow_network=False)
    assert manifest is not None
    installed_cli = Path(result["command"][1])

    assert result["ok"] is True
    assert result["command"] == ["/bin/node", str(installed_cli)]
    assert manager._install_reason is None
    assert (runtime_dir / "downloads" / f"{_sha256(archive_path)}.tgz").exists()
    metadata = json.loads((installed_cli.parents[4] / ".vibe-show-runtime.json").read_text(encoding="utf-8"))
    assert metadata["provider"] == "manifest-cache"
    assert metadata["manifest_sha256"] == manifest.digest
    assert metadata["archive_sha256"] == _sha256(archive_path)
    status = manager.status()
    assert status["install"]["state"] == "installed"
    assert status["install"]["matches_manifest"] is True


def test_show_runtime_manager_forced_manifest_fallback_reports_failed_operation_and_installed_state(
    monkeypatch,
    tmp_path,
):
    archive_path = _write_runtime_archive(tmp_path)
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest_path,
    )
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )
    installed = manager.prepare()
    assert installed["ok"] is True

    def fail_archive_resolution(shared_manager, _archive):
        shared_manager._install_reason = "runtime_archive_download_failed"
        return None

    monkeypatch.setattr(
        "core.show_runtime._ShowManifestRuntimeManager._resolve_manifest_archive",
        fail_archive_resolution,
    )

    result = manager.prepare(force=True)

    assert result["ok"] is False
    assert result["reason"] == "runtime_archive_download_failed"
    assert result["command"] == installed["command"]
    assert result["install"]["state"] == "installed"
    assert result["install"]["reason"] is None
    assert result["status"]["install"]["state"] == "installed"
    assert result["status"]["command"] == installed["command"]
    assert result["status"]["reason"] is None


def test_show_runtime_manager_force_refreshes_matching_manifest_install(monkeypatch, tmp_path):
    archive_path = _write_runtime_archive(tmp_path, text="healthy runtime\n")
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest_path,
    )
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )
    installed = manager.prepare()
    installed_cli = Path(installed["command"][1])
    installed_cli.write_text("corrupt runtime\n", encoding="utf-8")

    replaced = manager.prepare(force=True)

    assert replaced["ok"] is True
    assert replaced["command"] != installed["command"]
    assert Path(replaced["command"][1]).read_text(encoding="utf-8") == "healthy runtime\n"
    assert installed_cli.read_text(encoding="utf-8") == "corrupt runtime\n"


def test_show_runtime_repair_skips_healthy_install_without_replacement(monkeypatch, tmp_path):
    archive_path = _write_runtime_archive(tmp_path)
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest_path,
    )
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )
    installed = manager.prepare()
    versions_before = set((tmp_path / "runtime" / "versions").glob("**/.vibe-show-runtime.json"))
    monkeypatch.setattr(manager, "_verify_startability", lambda _command: ShowRuntimeStartability.startable())

    result = manager.repair()

    assert result["outcome"] == "healthy"
    assert result["repair_attempted"] is False
    assert result["command"] == installed["command"]
    assert set((tmp_path / "runtime" / "versions").glob("**/.vibe-show-runtime.json")) == versions_before


def test_show_runtime_repair_does_not_mutate_when_verification_is_undetermined(monkeypatch, tmp_path):
    archive_path = _write_runtime_archive(tmp_path)
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest_path,
    )
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )
    installed = manager.prepare()
    pointer_before = (tmp_path / "runtime" / "current.json").read_bytes()
    monkeypatch.setattr(
        manager,
        "_verify_startability",
        lambda _command: ShowRuntimeStartability.undetermined("verification workspace denied"),
    )

    result = manager.repair()

    assert result["ok"] is False
    assert result["reason"] == "runtime_start_verification_failed"
    assert result["repair_attempted"] is False
    assert result["command"] == installed["command"]
    assert (tmp_path / "runtime" / "current.json").read_bytes() == pointer_before


def test_show_runtime_repair_publishes_only_a_startable_immutable_candidate(monkeypatch, tmp_path):
    archive_path = _write_runtime_archive(tmp_path, text="healthy bytes\n")
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest_path,
    )
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )
    installed = manager.prepare()
    old_cli = Path(installed["command"][1])
    old_cli.write_text("broken bytes\n", encoding="utf-8")
    outcomes = iter(
        [
            ShowRuntimeStartability.not_startable("runtime_start_health_timeout"),
            ShowRuntimeStartability.startable(),
        ]
    )
    monkeypatch.setattr(manager, "_verify_startability", lambda _command: next(outcomes))

    result = manager.repair()

    assert result["outcome"] == "repaired"
    assert result["repair_attempted"] is True
    assert result["command"] != installed["command"]
    assert Path(result["command"][1]).read_text(encoding="utf-8") == "healthy bytes\n"
    assert old_cli.read_text(encoding="utf-8") == "broken bytes\n"


def test_show_runtime_repair_rejects_unverified_candidate_and_preserves_old_install(
    monkeypatch,
    tmp_path,
):
    archive_path = _write_runtime_archive(tmp_path)
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    runtime_dir = tmp_path / "runtime"
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
    )
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )
    installed = manager.prepare()
    old_cli = Path(installed["command"][1])
    pointer_before = (runtime_dir / "current.json").read_bytes()
    outcomes = iter(
        [
            ShowRuntimeStartability.not_startable("runtime_start_health_timeout"),
            ShowRuntimeStartability.undetermined("candidate probe crashed"),
        ]
    )
    monkeypatch.setattr(manager, "_verify_startability", lambda _command: next(outcomes))

    result = manager.repair()

    assert result["ok"] is False
    assert result["reason"] == "runtime_start_verification_failed"
    assert result["verification_phase"] == "after"
    assert result["command"] == installed["command"]
    assert old_cli.exists()
    assert (runtime_dir / "current.json").read_bytes() == pointer_before
    assert len(list((runtime_dir / "versions").glob("**/.vibe-show-runtime.json"))) == 1


@pytest.mark.parametrize("provider", ("manifest-cache", "archive", "npm"))
def test_show_runtime_candidate_reference_failure_never_publishes_pointer(
    monkeypatch,
    tmp_path,
    provider,
):
    runtime_dir = tmp_path / "runtime"
    archive_path = _write_runtime_archive(tmp_path)
    manager_kwargs = {
        "workspace_root": tmp_path / "show",
        "runtime_dir": runtime_dir,
        "runtime_source": provider,
    }
    if provider == "manifest-cache":
        manager_kwargs["manifest_path"] = _write_runtime_manifest(tmp_path, archive_path)
        pointer = runtime_dir / "current.json"
    elif provider == "archive":
        manager_kwargs["archive_path"] = archive_path
        pointer = runtime_dir / "prebuilt" / "current.json"
    else:
        pointer = runtime_dir / "package" / "current.json"

        def install_npm(argv, **_kwargs):
            prefix = Path(argv[argv.index("--prefix") + 1])
            binary = ShowRuntimeManager._npm_bin_path(prefix)
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr("core.show_runtime.subprocess.run", install_npm)

    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: [command] if command in {"node", "npm"} else None,
    )
    manager = ShowRuntimeManager(**manager_kwargs)
    monkeypatch.setattr(manager, "_retain_install_dir_locked", lambda _install_dir: False)
    monkeypatch.setattr(manager, "_retain_managed_command", lambda _command: False)

    result = manager.prepare()

    assert result["ok"] is False
    assert result["reason"] == "runtime_install_guard_unavailable"
    assert pointer.exists() is False
    assert list(runtime_dir.rglob(".vibe-show-runtime.json")) == []


@pytest.mark.parametrize("shared_owner", [False, True])
def test_show_runtime_finalizer_can_collect_inside_reference_publication(tmp_path, shared_owner):
    # A subprocess deadline turns a regressed deadlock into a useful failure.
    probe = textwrap.dedent("""
        import faulthandler
        import gc
        import sys
        import weakref
        from pathlib import Path
        from core.show_runtime import ShowRuntimeManager

        faulthandler.dump_traceback_later(10, exit=True)
        gc.disable()
        root = Path(sys.argv[1])
        runtime = root / "runtime"
        old_install = runtime / "versions" / "old"
        new_install = runtime / "versions" / "new"
        old_install.mkdir(parents=True)
        new_install.mkdir(parents=True)
        def manager():
            return ShowRuntimeManager(runtime_dir=runtime, workspace_root=root / "show", offline=True)

        previous = manager()
        assert previous._retain_install_dir_locked(old_install)
        old_marker, = previous._install_reference_dir(old_install.resolve()).glob("*.lock")
        survivor = manager()
        shared = sys.argv[2] == "True"
        if shared:
            assert survivor._retain_install_dir_locked(old_install)
        previous.cycle = previous
        old_ref = weakref.ref(previous)
        del previous

        current = manager()
        original = current._install_reference_dir
        def collect_during_publication(path):
            gc.collect()
            assert old_ref() is None
            return original(path)
        current._install_reference_dir = collect_during_publication
        assert current._retain_install_dir_locked(new_install)
        assert current._install_dir_has_live_reference(new_install)
        assert current._install_dir_has_live_reference(old_install) is shared
        assert old_marker.exists() is shared
        survivor._install_reference_finalizer()
        assert not current._install_dir_has_live_reference(old_install)
        assert not old_marker.exists()
        current._install_reference_finalizer()
        assert not current._install_dir_has_live_reference(new_install)
        print("finalizer completed; live ownership preserved")
    """)
    result = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path), str(shared_owner)],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "live ownership preserved" in result.stdout


def test_show_runtime_reference_guard_still_excludes_other_threads():
    acquired = []

    def attempt():
        locked = show_runtime._INSTALL_REFERENCE_LOCKS_GUARD.acquire(timeout=0.05)
        acquired.append(locked)
        if locked:
            show_runtime._INSTALL_REFERENCE_LOCKS_GUARD.release()

    with show_runtime._INSTALL_REFERENCE_LOCKS_GUARD:
        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert acquired == [False]


def test_show_runtime_live_cached_install_survives_distinct_identity_cleanup(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: [sys.executable] if command == "node" else None,
    )

    def install(version: str, text: str) -> tuple[ShowRuntimeManager, Path]:
        source_dir = tmp_path / version
        archive = _write_runtime_archive(source_dir, text=text)
        manifest = _write_runtime_manifest(
            source_dir,
            archive,
            runtime_version=version,
        )
        runtime_manager = ShowRuntimeManager(
            workspace_root=tmp_path / f"show-{version}",
            runtime_dir=runtime_dir,
            manifest_path=manifest,
        )
        prepared = runtime_manager.prepare()
        assert prepared["ok"] is True
        return runtime_manager, Path(prepared["command"][1])

    live_manager, live_cli = install("runtime-v1", "runtime-v1\n")
    gate = tmp_path / "read-now"
    reader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import pathlib,sys,time\n"
            "gate=pathlib.Path(sys.argv[1])\n"
            "while not gate.exists():\n"
            "    time.sleep(.01)\n"
            "print(pathlib.Path(sys.argv[2]).read_text(), end='')\n",
            str(gate),
            str(live_cli),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second_manager, second_cli = install("runtime-v2", "runtime-v2\n")
    second_manager._install_reference_finalizer()
    third_manager, _third_cli = install("runtime-v3", "runtime-v3\n")
    third_manager._install_reference_finalizer()
    fourth_manager, _fourth_cli = install("runtime-v4", "runtime-v4\n")

    gate.write_text("go\n", encoding="utf-8")
    stdout, stderr = reader.communicate(timeout=5)

    assert reader.returncode == 0, stderr
    assert stdout == "runtime-v1\n"
    assert live_cli.exists()
    assert not second_cli.exists()
    live_manager._install_reference_finalizer()
    fourth_manager._install_reference_finalizer()


def test_show_runtime_manager_force_publication_failure_preserves_cached_command(
    monkeypatch,
    tmp_path,
):
    archive_path = _write_runtime_archive(tmp_path, text="healthy runtime\n")
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    runtime_dir = tmp_path / "runtime"
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
    )
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )
    installed = manager.prepare()
    installed_cli = Path(installed["command"][1])
    original_write_pointer = show_runtime._ShowManifestRuntimeManager._write_current_pointer
    writes = 0

    def fail_first_pointer_write(shared_manager, install_dir, manifest, archive):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("pointer publication failed")
        return original_write_pointer(shared_manager, install_dir, manifest, archive)

    monkeypatch.setattr(
        show_runtime._ShowManifestRuntimeManager,
        "_write_current_pointer",
        fail_first_pointer_write,
    )

    failed = manager.prepare(force=True)

    assert failed["ok"] is False
    assert failed["reason"] == "runtime_install_failed"
    assert failed["command"] == installed["command"]
    assert manager._managed_command == installed["command"]
    assert installed_cli.exists()
    assert len(list((runtime_dir / "references").glob("*/*.lock"))) == 1

    repaired = manager.prepare()

    assert repaired["ok"] is True
    assert repaired["command"] == installed["command"]
    assert installed_cli.read_text(encoding="utf-8") == "healthy runtime\n"


def test_show_runtime_manager_preserves_structured_http_download_error(monkeypatch, tmp_path):
    archive_path = _write_runtime_archive(tmp_path)
    archive_url = "https://github.com/avibe-bot/avibe/releases/download/v-test/runtime.tgz?token=secret"
    manifest_path = _write_runtime_manifest(tmp_path, archive_path, url=archive_url)
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest_path,
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)

    def fail_download(*_args, **_kwargs):
        raise urllib.error.HTTPError(archive_url, 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr("core.show_runtime.urllib.request.urlopen", fail_download)

    result = manager.prepare()

    assert result["ok"] is False
    assert result["reason"] == "runtime_archive_download_failed"
    assert result["install"]["failure_class"] == "configured"
    assert result["install"]["recovery_action"] == "change_setting"
    assert result["status"]["download_error"] == {
        "kind": "http",
        "message": "HTTP 404 Not Found",
        "url": "https://github.com/avibe-bot/avibe/releases/download/v-test/runtime.tgz",
        "host": "github.com",
        "exception_type": "HTTPError",
        "http_status": 404,
        "retryable": False,
        "attempts": 1,
    }
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize(
    ("retryable", "expected_class", "expected_action"),
    (
        (False, "configured", "change_setting"),
        (True, "transient", "repair"),
    ),
)
def test_manifest_download_failure_publishes_measured_retryability(
    monkeypatch,
    tmp_path,
    retryable,
    expected_class,
    expected_action,
):
    manifest_url = "https://example.test/show-runtime-manifest.json"
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        manifest_url=manifest_url,
    )
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )

    def fail_manifest(*_args, **_kwargs):
        raise dependency_network.DependencyNetworkError(
            {
                "kind": "network" if retryable else "http",
                "message": "manifest unavailable",
                "url": manifest_url,
                "host": "example.test",
                "exception_type": "HTTPError",
                "retryable": retryable,
                "attempts": 1,
            }
        )

    monkeypatch.setattr("core.managed_runtime.fetch_bytes", fail_manifest)

    result = manager.prepare()

    assert result["reason"] == "runtime_manifest_download_failed"
    assert result["install"]["failure_class"] == expected_class
    assert result["install"]["recovery_action"] == expected_action


@pytest.mark.parametrize(
    ("configured", "expected_class", "expected_action"),
    (
        (True, "configured", "change_setting"),
        (False, "unclassified", "repair"),
    ),
)
def test_archive_download_failure_publishes_url_provenance(
    monkeypatch,
    tmp_path,
    configured,
    expected_class,
    expected_action,
):
    archive_url = "https://example.test/runtime.tgz"
    monkeypatch.delenv("VIBE_SHOW_RUNTIME_ARCHIVE_URL", raising=False)
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="archive",
        archive_url=archive_url if configured else None,
    )
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )
    monkeypatch.setattr(manager, "_copy_packaged_runtime_archive", lambda: None)

    def fail_archive(*_args, **_kwargs):
        raise dependency_network.DependencyNetworkError(
            {
                "kind": "http",
                "message": "HTTP 404 Not Found",
                "url": archive_url,
                "host": "example.test",
                "exception_type": "HTTPError",
                "http_status": 404,
                "retryable": False,
                "attempts": 1,
            }
        )

    monkeypatch.setattr("core.show_runtime.fetch_to_path", fail_archive)

    result = manager.prepare()

    assert result["reason"] == "runtime_archive_download_failed"
    assert result["install"]["failure_class"] == expected_class
    assert result["install"]["recovery_action"] == expected_action


def test_show_runtime_manager_retries_transient_archive_failure(monkeypatch, tmp_path):
    archive_path = _write_runtime_archive(tmp_path)
    archive_url = "https://example.test/runtime.tgz"
    manifest_path = _write_runtime_manifest(tmp_path, archive_path, url=archive_url)
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest_path,
    )
    attempts = 0
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)

    def opener(_request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.URLError(ConnectionResetError("reset"))
        return io.BytesIO(archive_path.read_bytes())

    monkeypatch.setattr("core.show_runtime.urllib.request.urlopen", opener)
    monkeypatch.setattr("core.dependency_network.time.sleep", lambda _delay: None)

    result = manager.prepare()

    assert result["ok"] is True
    assert attempts == 2


def test_show_runtime_status_refresh_does_not_erase_archive_download_error(monkeypatch, tmp_path):
    archive_path = _write_runtime_archive(tmp_path)
    archive_url = "https://github.com/avibe-bot/avibe/releases/download/v-test/runtime.tgz"
    manifest_path = _write_runtime_manifest(tmp_path, archive_path, url=archive_url)
    manifest_url = "https://example.test/show-runtime-manifest.json"
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        manifest_url=manifest_url,
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)

    class ManifestResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return manifest_path.read_bytes()

    def fake_urlopen(request, **_kwargs):
        url = request.full_url if hasattr(request, "full_url") else request
        if url == manifest_url:
            return ManifestResponse()
        raise urllib.error.HTTPError(archive_url, 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr("core.show_runtime.urllib.request.urlopen", fake_urlopen)

    result = manager.prepare()

    assert result["reason"] == "runtime_archive_download_failed"
    assert result["status"]["download_error"]["http_status"] == 404


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (urllib.error.URLError(socket.gaierror(-2, "Name or service not known")), "dns"),
        (urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed")), "tls"),
        (urllib.error.URLError(TimeoutError("timed out")), "timeout"),
    ],
)
def test_show_runtime_download_error_classifies_network_failures(exc, kind):
    error = _runtime_download_error(exc, "https://github.com/avibe-bot/avibe/releases/download/v-test/runtime.tgz")

    assert error["kind"] == kind
    assert error["host"] == "github.com"


def test_show_runtime_archive_probe_uses_body_free_head_request(monkeypatch, tmp_path):
    archive_path = _write_runtime_archive(tmp_path)
    archive_url = "https://github.com/avibe-bot/avibe/releases/download/v-test/runtime.tgz"
    manifest_path = _write_runtime_manifest(tmp_path, archive_path, url=archive_url)
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest_path,
    )
    requests = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return self.status

        def geturl(self):
            return "https://release-assets.githubusercontent.com/runtime.tgz"

    def fake_urlopen(request, **_kwargs):
        requests.append(request)
        return FakeResponse()

    monkeypatch.setattr("core.show_runtime.urllib.request.urlopen", fake_urlopen)

    result = manager.probe_archive_reachability()

    assert result["ok"] is True
    assert result["http_status"] == 200
    assert result["final_host"] == "release-assets.githubusercontent.com"
    assert requests[0].get_method() == "HEAD"


def test_show_runtime_manifest_probe_fetches_without_mutating_remote_cache(
    monkeypatch,
    tmp_path,
):
    archive_path = _write_runtime_archive(tmp_path)
    manifest_path = _write_runtime_manifest(
        tmp_path,
        archive_path,
        url="https://example.test/runtime.tgz",
    )
    manifest_url = "https://example.test/show-runtime-manifest.json"
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        manifest_url=manifest_url,
    )
    shared_manager = manager._shared_manifest_manager(offline=False)
    cached_manifest = shared_manager._remote_manifest_cache_path()
    cached_manifest.parent.mkdir(parents=True)
    cached_manifest.write_bytes(b"existing Show manifest cache")
    monkeypatch.setattr(
        "core.managed_runtime.fetch_bytes",
        lambda url, **_kwargs: (
            manifest_path.read_bytes()
            if url == manifest_url
            else pytest.fail(f"unexpected fetch: {url}")
        ),
    )
    monkeypatch.setattr(
        "core.managed_runtime.write_atomic",
        lambda *_args, **_kwargs: pytest.fail("Show diagnostic probe wrote the manifest cache"),
    )
    monkeypatch.setattr(
        "core.show_runtime.probe_url",
        lambda *_args, **_kwargs: {"ok": True, "checked": True},
    )

    result = manager.probe_archive_reachability()

    assert result == {"ok": True, "checked": True}
    assert cached_manifest.read_bytes() == b"existing Show manifest cache"


def test_show_runtime_manager_manifest_install_dir_includes_runtime_and_archive_identity(monkeypatch, tmp_path):
    old_archive_path = _write_runtime_archive(tmp_path / "old", text="old runtime\n")
    old_manifest_path = _write_runtime_manifest(tmp_path / "old", old_archive_path)
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)

    old_manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=old_manifest_path,
    )
    old_result = old_manager.prepare()
    old_cli = Path(old_result["command"][1])
    assert old_cli.read_text(encoding="utf-8") == "old runtime\n"

    new_archive_path = _write_runtime_archive(tmp_path / "new", text="new runtime\n")
    new_manifest_path = _write_runtime_manifest(tmp_path / "new", new_archive_path)
    new_manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=new_manifest_path,
    )

    new_result = new_manager.prepare()
    new_cli = Path(new_result["command"][1])

    assert new_cli != old_cli
    assert new_cli.read_text(encoding="utf-8") == "new runtime\n"
    assert old_cli.read_text(encoding="utf-8") == "old runtime\n"
    assert new_manager.status()["install"]["matches_manifest"] is True


@pytest.mark.parametrize("manifest_source", ("path", "packaged"))
def test_show_runtime_manager_revalidates_selected_manifest_identity_before_reuse(
    monkeypatch,
    tmp_path,
    manifest_source,
):
    old_archive = _write_runtime_archive(tmp_path / "old", text="old runtime\n")
    old_manifest = _write_runtime_manifest(tmp_path / "old", old_archive)
    if manifest_source == "path":
        selected_manifest = old_manifest
        manager_kwargs = {"manifest_path": selected_manifest}
    else:
        package_root = tmp_path / "package"
        selected_manifest = package_root / show_runtime._RUNTIME_MANIFEST_RESOURCE
        selected_manifest.parent.mkdir(parents=True)
        selected_manifest.write_bytes(old_manifest.read_bytes())
        monkeypatch.setattr(
            "core.managed_runtime.package_resources.files",
            lambda _package: package_root,
        )
        manager_kwargs = {}

    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )
    old_manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        **manager_kwargs,
    )
    old_result = old_manager.prepare()
    old_cli = Path(old_result["command"][1])

    new_archive = _write_runtime_archive(tmp_path / "new", text="new runtime\n")
    new_manifest = _write_runtime_manifest(
        tmp_path / "new",
        new_archive,
        runtime_version="runtime-new-ref",
    )
    selected_manifest.write_bytes(new_manifest.read_bytes())
    new_manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        **manager_kwargs,
    )

    result = new_manager.prepare()

    new_cli = Path(result["command"][1])
    assert result["ok"] is True
    assert new_cli != old_cli
    assert new_cli.read_text(encoding="utf-8") == "new runtime\n"
    assert old_cli.read_text(encoding="utf-8") == "old runtime\n"


def test_show_runtime_manager_manifest_install_identity_ignores_other_platform_edits(monkeypatch, tmp_path):
    archive_path = _write_runtime_archive(tmp_path, text="current platform runtime\n")
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)

    initial_manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
    )
    initial_result = initial_manager.prepare()
    initial_shared_manager = initial_manager._shared_manifest_manager(offline=True)
    initial_manifest = initial_shared_manager.load_manifest(allow_network=False)
    assert initial_manifest is not None

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["archives"]["other-platform"] = {
        "name": "vibe-show-runtime-node-other-platform.tgz",
        "url": "https://example.test/other-platform.tgz",
        "sha256": "f" * 64,
        "size": 1,
    }
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    edited_manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
        offline=True,
    )
    edited_shared_manager = edited_manager._shared_manifest_manager(offline=True)
    edited_manifest = edited_shared_manager.load_manifest(allow_network=False)
    assert edited_manifest is not None
    assert edited_manifest.digest != initial_manifest.digest

    def _unexpected_archive_resolution(_shared_manager, _archive):
        raise AssertionError("an unrelated platform edit must not trigger archive resolution")

    monkeypatch.setattr(
        "core.show_runtime._ShowManifestRuntimeManager._resolve_manifest_archive",
        _unexpected_archive_resolution,
    )
    edited_result = edited_manager.prepare()

    assert edited_result["ok"] is True
    assert edited_result["command"] == initial_result["command"]
    assert edited_result["status"]["install"]["matches_manifest"] is True


def test_show_runtime_manager_adopts_previous_fingerprint_and_preserves_gc_protection(monkeypatch, tmp_path):
    archive_path = _write_runtime_archive(tmp_path, text="previous fingerprint runtime\n")
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)

    previous_manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
    )
    previous_shared_manager = previous_manager._shared_manifest_manager(offline=True)
    previous_manifest = previous_shared_manager.load_manifest(allow_network=False)
    assert previous_manifest is not None
    archive = previous_shared_manager.archive_for_platform(previous_manifest)
    assert archive is not None
    previous_fingerprint = hashlib.sha256(
        f"{previous_manifest.digest}:{archive.sha256}".encode("utf-8")
    ).hexdigest()[:16]
    previous_install_dir = previous_shared_manager._manifest_install_dir(previous_manifest, archive).parent / previous_fingerprint
    previous_cli = previous_install_dir / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"
    previous_cli.parent.mkdir(parents=True)
    previous_cli.write_text("previous fingerprint runtime\n", encoding="utf-8")
    previous_shared_manager._write_manifest_install_metadata(
        previous_install_dir,
        previous_manifest,
        archive,
        binary_sha256=None,
    )

    downloads_dir = runtime_dir / "downloads"
    downloads_dir.mkdir(parents=True)
    adopted_archive = downloads_dir / f"{archive.sha256}.tgz"
    adopted_archive.write_bytes(archive_path.read_bytes())
    os.utime(adopted_archive, (1, 1))
    stale_sha256 = "e" * 64
    stale_install_dir = runtime_dir / "versions" / "stale-runtime" / archive.platform / "stale-fingerprint"
    stale_install_dir.mkdir(parents=True)
    (stale_install_dir / ".vibe-show-runtime.json").write_text(
        json.dumps(
            {
                "provider": "manifest-cache",
                "manifest_sha256": "d" * 64,
                "runtime_version": "stale-runtime",
                "platform": archive.platform,
                "archive_name": "stale.tgz",
                "archive_sha256": stale_sha256,
                "manifest_source": str(manifest_path),
            }
        ),
        encoding="utf-8",
    )
    stale_archive = downloads_dir / f"{stale_sha256}.tgz"
    stale_archive.write_bytes(b"stale")
    os.utime(stale_archive, (1, 1))
    (runtime_dir / "current.json").write_text(
        json.dumps(
            {
                "provider": "manifest-cache",
                "runtime_version": "stale-runtime",
                "platform": archive.platform,
                "install_dir": str(stale_install_dir),
                "manifest_sha256": "d" * 64,
                "archive_sha256": stale_sha256,
            }
        ),
        encoding="utf-8",
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["archives"]["other-platform"] = {
        "name": "vibe-show-runtime-node-other-platform.tgz",
        "url": "https://example.test/other-platform.tgz",
        "sha256": "f" * 64,
        "size": 1,
    }
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
        offline=True,
    )
    shared_manager = manager._shared_manifest_manager(offline=True)
    current_manifest = shared_manager.load_manifest(allow_network=False)
    assert current_manifest is not None
    current_install_dir = shared_manager._manifest_install_dir(current_manifest, archive)
    assert current_install_dir != previous_install_dir

    def _unexpected_archive_resolution(_shared_manager, _archive):
        raise AssertionError("a previous-fingerprint install must be adopted without a download")

    monkeypatch.setattr(
        "core.show_runtime._ShowManifestRuntimeManager._resolve_manifest_archive",
        _unexpected_archive_resolution,
    )
    result = manager.prepare()

    assert result["ok"] is True
    assert result["command"] == ["/bin/node", str(previous_cli)]
    assert previous_install_dir.exists() is True
    assert current_install_dir.exists() is False
    assert archive.sha256 == shared_manager._current_archive_sha256()
    current_pointer = json.loads((runtime_dir / "current.json").read_text(encoding="utf-8"))
    assert current_pointer["runtime_version"] == current_manifest.runtime_version
    assert current_pointer["install_dir"] == str(previous_install_dir)
    assert current_pointer["archive_sha256"] == archive.sha256

    clean_result = manager.clean(keep_previous=0)

    assert str(previous_install_dir) not in clean_result["removed"]
    assert previous_install_dir.exists() is True
    assert adopted_archive.exists() is True
    assert stale_install_dir.exists() is False
    assert stale_archive.exists() is False

    restarted_manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
        auto_install=False,
    )
    restarted = restarted_manager.prepare(automatic=True)

    assert restarted["policy"]["state"] == "skipped"
    assert restarted["install"]["state"] == "installed"
    assert restarted["command"] == ["/bin/node", str(previous_cli)]


def test_show_runtime_clean_prunes_stale_manifest_fingerprints(monkeypatch, tmp_path):
    old_archive_path = _write_runtime_archive(tmp_path / "old", text="old runtime\n")
    old_manifest_path = _write_runtime_manifest(tmp_path / "old", old_archive_path)
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)

    old_manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=old_manifest_path,
    )
    old_result = old_manager.prepare()
    old_install_dir = Path(old_result["command"][1]).parents[4]

    new_archive_path = _write_runtime_archive(tmp_path / "new", text="new runtime\n")
    new_manifest_path = _write_runtime_manifest(tmp_path / "new", new_archive_path)
    new_manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=new_manifest_path,
    )
    new_result = new_manager.prepare()
    new_install_dir = Path(new_result["command"][1]).parents[4]

    old_manager._install_reference_finalizer()
    result = new_manager.clean(keep_previous=0)

    assert result["ok"] is True
    assert str(old_install_dir) in result["removed"]
    assert old_install_dir.exists() is False
    assert new_install_dir.exists() is True


def test_show_runtime_prepare_prunes_old_packaged_installs_and_keeps_rollback(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    old_install, _old_cli = _write_cached_runtime_install(runtime_dir, "old", mtime=100)
    previous_install, _previous_cli = _write_cached_runtime_install(runtime_dir, "previous", mtime=200)
    current_install, current_cli = _write_cached_runtime_install(runtime_dir, "current", mtime=300)
    _write_cached_runtime_pointer(runtime_dir, current_install)
    custom_install, _custom_cli = _write_cached_runtime_install(
        runtime_dir,
        "custom",
        manifest_source=str(tmp_path / "development-manifest.json"),
        mtime=50,
    )
    unrelated_source = runtime_dir / "source" / "custom" / "runtime"
    unrelated_source.mkdir(parents=True)
    (unrelated_source / "README.md").write_text("custom runtime\n", encoding="utf-8")
    local_bin = runtime_dir / "package" / "node_modules" / ".bin" / "avibe-show-runtime"
    local_bin.parent.mkdir(parents=True)
    local_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="manifest-cache",
    )
    monkeypatch.setattr(
        manager,
        "_install_manifest_runtime_locked",
        lambda *, force, offline: _complete_mock_manifest_install(
            manager,
            ["/bin/node", str(current_cli)],
            offline=offline,
        ),
    )
    monkeypatch.setattr(manager, "status", lambda **_kwargs: {})

    result = manager.prepare()

    assert result["ok"] is True
    assert current_install.exists() is True
    assert previous_install.exists() is True
    assert old_install.exists() is False
    assert custom_install.exists() is True
    assert unrelated_source.exists() is True
    assert local_bin.exists() is True


@pytest.mark.parametrize(("parent_mtime", "child_mtime"), ((100, 200), (200, 100)))
def test_show_runtime_prepare_preserves_nested_retained_rollback(monkeypatch, tmp_path, parent_mtime, child_mtime):
    runtime_dir = tmp_path / "runtime"
    old_install, _old_cli = _write_cached_runtime_install(runtime_dir, "old", mtime=10)
    current_install, current_cli = _write_cached_runtime_install(runtime_dir, "current", mtime=300)
    _write_cached_runtime_pointer(runtime_dir, current_install)
    rollback_parent = runtime_dir / "versions" / "rollback" / runtime_platform_tag()
    _rollback_parent, rollback_parent_cli = _write_cached_runtime_install_at(
        rollback_parent,
        "rollback-legacy",
        mtime=parent_mtime,
    )
    rollback_install, rollback_cli = _write_cached_runtime_install_at(
        rollback_parent / "fingerprint",
        "rollback",
        mtime=child_mtime,
    )
    stale_sibling, _stale_cli = _write_cached_runtime_install_at(
        rollback_parent / "stale-fingerprint",
        "stale-rollback",
        mtime=20,
    )

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="manifest-cache",
    )
    monkeypatch.setattr(
        manager,
        "_install_manifest_runtime_locked",
        lambda *, force, offline: _complete_mock_manifest_install(
            manager,
            ["/bin/node", str(current_cli)],
            offline=offline,
        ),
    )
    monkeypatch.setattr(manager, "status", lambda **_kwargs: {})

    result = manager.prepare()

    assert result["ok"] is True
    assert current_install.exists() is True
    assert rollback_install.exists() is True
    assert rollback_cli.exists() is True
    assert rollback_parent.exists() is True
    assert rollback_parent_cli.exists() is True
    assert stale_sibling.exists() is False
    assert old_install.exists() is False


def test_show_runtime_prepare_prunes_siblings_under_current_legacy_parent(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    old_install, _old_cli = _write_cached_runtime_install(runtime_dir, "old", mtime=100)
    previous_install, _previous_cli = _write_cached_runtime_install(runtime_dir, "previous", mtime=250)
    current_parent = runtime_dir / "versions" / "current" / runtime_platform_tag()
    _parent_install, parent_cli = _write_cached_runtime_install_at(current_parent, "current-legacy", mtime=400)
    current_install, current_cli = _write_cached_runtime_install_at(
        current_parent / "current-fingerprint",
        "current",
        mtime=300,
    )
    _write_cached_runtime_pointer(runtime_dir, current_install)
    stale_sibling, _stale_cli = _write_cached_runtime_install_at(
        current_parent / "stale-fingerprint",
        "stale-current",
        mtime=200,
    )

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="manifest-cache",
    )
    monkeypatch.setattr(
        manager,
        "_install_manifest_runtime_locked",
        lambda *, force, offline: _complete_mock_manifest_install(
            manager,
            ["/bin/node", str(current_cli)],
            offline=offline,
        ),
    )
    monkeypatch.setattr(manager, "status", lambda **_kwargs: {})

    result = manager.prepare()

    assert result["ok"] is True
    assert current_install.exists() is True
    assert current_cli.exists() is True
    assert current_parent.exists() is True
    assert parent_cli.exists() is True
    assert previous_install.exists() is True
    assert stale_sibling.exists() is False
    assert old_install.exists() is False


def test_show_runtime_prepare_preserves_descendants_of_current_legacy_parent(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    old_install, _old_cli = _write_cached_runtime_install(runtime_dir, "old", mtime=100)
    previous_install, _previous_cli = _write_cached_runtime_install(runtime_dir, "previous", mtime=250)
    current_parent = runtime_dir / "versions" / "current" / runtime_platform_tag()
    _parent_install, parent_cli = _write_cached_runtime_install_at(current_parent, "current-legacy", mtime=400)
    _write_cached_runtime_pointer(runtime_dir, current_parent)
    current_child, current_child_cli = _write_cached_runtime_install_at(
        current_parent / "current-fingerprint",
        "current-child",
        mtime=300,
    )

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="manifest-cache",
    )
    monkeypatch.setattr(
        manager,
        "_install_manifest_runtime_locked",
        lambda *, force, offline: _complete_mock_manifest_install(
            manager,
            ["/bin/node", str(parent_cli)],
            offline=offline,
        ),
    )
    monkeypatch.setattr(manager, "status", lambda **_kwargs: {})

    result = manager.prepare()

    assert result["ok"] is True
    assert current_parent.exists() is True
    assert parent_cli.exists() is True
    assert current_child.exists() is True
    assert current_child_cli.exists() is True
    assert previous_install.exists() is True
    assert old_install.exists() is False


def test_show_runtime_prepare_preserves_custom_child_under_stale_packaged_parent(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    old_install, _old_cli = _write_cached_runtime_install(runtime_dir, "old", mtime=20)
    previous_install, _previous_cli = _write_cached_runtime_install(runtime_dir, "previous", mtime=250)
    current_install, current_cli = _write_cached_runtime_install(runtime_dir, "current", mtime=300)
    _write_cached_runtime_pointer(runtime_dir, current_install)
    stale_parent = runtime_dir / "versions" / "stale-parent" / runtime_platform_tag()
    _parent_install, parent_cli = _write_cached_runtime_install_at(stale_parent, "stale-parent", mtime=80)
    custom_child, custom_cli = _write_cached_runtime_install_at(
        stale_parent / "custom-fingerprint",
        "custom-child",
        manifest_source=str(tmp_path / "custom-manifest.json"),
        mtime=70,
    )
    stale_child, _stale_child_cli = _write_cached_runtime_install_at(
        stale_parent / "stale-fingerprint",
        "stale-child",
        mtime=60,
    )

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="manifest-cache",
    )
    monkeypatch.setattr(
        manager,
        "_install_manifest_runtime_locked",
        lambda *, force, offline: _complete_mock_manifest_install(
            manager,
            ["/bin/node", str(current_cli)],
            offline=offline,
        ),
    )
    monkeypatch.setattr(manager, "status", lambda **_kwargs: {})

    result = manager.prepare()

    assert result["ok"] is True
    assert current_install.exists() is True
    assert previous_install.exists() is True
    assert stale_parent.exists() is True
    assert parent_cli.exists() is True
    assert custom_child.exists() is True
    assert custom_cli.exists() is True
    assert stale_child.exists() is False
    assert old_install.exists() is False


def test_show_runtime_prepare_with_explicit_command_does_not_clean_managed_installs(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    install_dirs = [
        _write_cached_runtime_install(runtime_dir, name, mtime=mtime)[0]
        for name, mtime in (("old", 100), ("previous", 200), ("current", 300))
    ]
    local_bin = tmp_path / "development" / "show-runtime"
    local_bin.parent.mkdir()
    local_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    manager = ShowRuntimeManager(
        command=str(local_bin),
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: [command])

    result = manager.prepare()

    assert result["ok"] is True
    assert all(path.exists() for path in install_dirs)
    assert local_bin.exists() is True


def test_show_runtime_forced_prepare_refuses_explicit_command_replacement(monkeypatch, tmp_path):
    local_bin = tmp_path / "development" / "show-runtime"
    local_bin.parent.mkdir()
    local_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    manager = ShowRuntimeManager(
        command=str(local_bin),
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: [command])
    monkeypatch.setattr(
        manager,
        "_install_managed_runtime_locked",
        lambda **_kwargs: pytest.fail("an explicit command must not enter managed replacement"),
    )

    result = manager.prepare(force=True)

    assert result["ok"] is False
    assert result["reason"] == "VIBE_SHOW_RUNTIME_BIN"
    assert result["policy"] == {
        "state": "allowed",
        "reason": None,
        "failure_class": None,
        "recovery_action": None,
    }
    assert result["install"]["state"] == "installed"
    assert result["install"]["command"] == [str(local_bin)]
    assert result["status"]["install"]["state"] == "installed"


def test_show_runtime_failed_prepare_does_not_clean_managed_installs(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    install_dirs = [
        _write_cached_runtime_install(runtime_dir, name, mtime=mtime)[0]
        for name, mtime in (("old", 100), ("previous", 200), ("current", 300))
    ]
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="manifest-cache",
    )
    monkeypatch.setattr(manager, "_install_manifest_runtime_locked", lambda *, force, offline: None)
    monkeypatch.setattr(manager, "status", lambda **_kwargs: {})

    result = manager.prepare()

    assert result["ok"] is False
    assert all(path.exists() for path in install_dirs)


def test_show_runtime_manager_reuses_legacy_manifest_install_offline(monkeypatch, tmp_path):
    archive_path = _write_runtime_archive(tmp_path, text="legacy runtime\n")
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    runtime_dir = tmp_path / "runtime"
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
        offline=True,
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)
    shared_manager = manager._shared_manifest_manager(offline=True)
    manifest = shared_manager.load_manifest(allow_network=False)
    assert manifest is not None
    archive = shared_manager.archive_for_platform(manifest)
    assert archive is not None
    legacy_install_dir = shared_manager._manifest_install_dir(manifest, archive).parent
    legacy_cli = legacy_install_dir / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"
    legacy_cli.parent.mkdir(parents=True)
    legacy_cli.write_text("legacy runtime\n", encoding="utf-8")
    shared_manager._write_manifest_install_metadata(
        legacy_install_dir,
        manifest,
        archive,
        binary_sha256=None,
    )

    result = manager.prepare()

    assert result["ok"] is True
    assert result["command"] == ["/bin/node", str(legacy_cli)]


def test_show_runtime_clean_skips_legacy_parent_of_current_fingerprint(monkeypatch, tmp_path):
    archive_path = _write_runtime_archive(tmp_path, text="current runtime\n")
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    runtime_dir = tmp_path / "runtime"
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)
    result = manager.prepare()
    current_install_dir = Path(result["command"][1]).parents[4]
    legacy_parent = current_install_dir.parent
    shared_manager = manager._shared_manifest_manager(offline=True)
    manifest = shared_manager.load_manifest(allow_network=False)
    assert manifest is not None
    archive = shared_manager.archive_for_platform(manifest)
    assert archive is not None
    shared_manager._write_manifest_install_metadata(
        legacy_parent,
        manifest,
        archive,
        binary_sha256=None,
    )

    clean_result = manager.clean(keep_previous=0)

    assert str(legacy_parent) not in clean_result["removed"]
    assert current_install_dir.exists() is True
    assert Path(result["command"][1]).exists() is True


@pytest.mark.parametrize("include_canonical", (False, True))
def test_show_runtime_manager_rejects_noncanonical_manifest_entrypoint(
    monkeypatch,
    tmp_path,
    include_canonical,
):
    canonical = "node_modules/@avibe/show-runtime/dist/cli.js"
    alternate = "node_modules/@avibe/show-runtime/dist/alternate.js"
    entrypoints = (alternate, canonical) if include_canonical else (alternate,)
    archive_path = _write_runtime_archive_with_entrypoints(tmp_path, entrypoints)
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["archives"][runtime_platform_tag()]["bin_path"] = alternate
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest_path,
    )
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )

    result = manager.prepare()

    assert result["ok"] is False
    assert result["reason"] == "runtime_manifest_invalid"
    assert result["command"] is None
    assert not (manager.runtime_dir / "current.json").exists()


@pytest.mark.parametrize(
    ("selection_error", "expected_reason"),
    (
        ("missing_platform", "runtime_platform_unsupported"),
        ("noncanonical_entrypoint", "runtime_manifest_invalid"),
    ),
)
def test_show_runtime_status_reports_selected_manifest_archive_error_with_installed_runtime(
    monkeypatch,
    tmp_path,
    selection_error,
    expected_reason,
):
    archive_path = _write_runtime_archive(tmp_path)
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )
    installed = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
    ).prepare()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    platform_tag = runtime_platform_tag()
    if selection_error == "missing_platform":
        payload["archives"]["fixture-unsupported"] = payload["archives"].pop(platform_tag)
    else:
        payload["archives"][platform_tag]["bin_path"] = (
            "node_modules/@avibe/show-runtime/dist/alternate.js"
        )
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
    )

    status = manager.status()

    assert status["install"]["state"] == "installed"
    assert status["install"]["runtime_version"] == "runtime-test-ref"
    assert status["command"] == installed["command"]
    assert status["reason"] == expected_reason


@pytest.mark.parametrize(
    "minimum_node",
    (20, [">=22.12.0"], {"range": ">=22.12.0"}),
    ids=("number", "list", "object"),
)
def test_show_runtime_manager_rejects_non_string_manifest_node_requirement(
    monkeypatch,
    tmp_path,
    minimum_node,
):
    archive_path = _write_runtime_archive(tmp_path)
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["minimum_node"] = minimum_node
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest_path,
    )
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )

    result = manager.prepare()

    assert result["ok"] is False
    assert result["reason"] == "runtime_manifest_invalid"
    assert result["command"] is None
    assert not (manager.runtime_dir / "current.json").exists()


def test_show_runtime_manager_rejects_node_below_manifest_minimum(monkeypatch, tmp_path):
    archive_path = _write_runtime_archive(tmp_path)
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest_path,
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)
    monkeypatch.setattr("core.show_runtime._node_version", lambda node: (20, 18, 0))

    result = manager.prepare()

    assert result["ok"] is False
    assert result["reason"] == "runtime_node_unsupported"
    assert result["status"]["node_supported"] is False


def test_show_runtime_manager_revalidates_changed_node_prerequisite_before_reuse(
    monkeypatch,
    tmp_path,
):
    archive_path = _write_runtime_archive(tmp_path, text="installed runtime\n")
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )
    monkeypatch.setattr("core.show_runtime._node_version", lambda _node: (22, 12, 0))
    initial = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
    ).prepare()
    installed_cli = Path(initial["command"][1])

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["minimum_node"] = ">=99.0.0"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
    )

    result = manager.prepare()

    assert result["ok"] is False
    assert result["reason"] == "runtime_node_unsupported"
    assert result["command"] is None
    assert installed_cli.read_text(encoding="utf-8") == "installed runtime\n"


def test_show_runtime_manager_rejects_manifest_archive_checksum_mismatch(monkeypatch, tmp_path):
    archive_path = _write_runtime_archive(tmp_path)
    manifest_path = _write_runtime_manifest(tmp_path, archive_path, sha256="0" * 64)
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest_path,
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)

    result = manager.prepare()

    assert result["ok"] is False
    assert result["reason"] == "runtime_archive_checksum_mismatch"


def test_show_runtime_manager_does_not_reuse_stale_manifest_install_after_checksum_failure(monkeypatch, tmp_path):
    old_archive_path = _write_runtime_archive(tmp_path, text="old runtime\n")
    old_manifest_path = _write_runtime_manifest(tmp_path / "old", old_archive_path)
    runtime_dir = tmp_path / "runtime"
    old_manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=old_manifest_path,
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)
    assert old_manager.prepare()["ok"] is True

    new_archive_path = _write_runtime_archive(tmp_path, text="new runtime\n")
    new_manifest_path = _write_runtime_manifest(tmp_path / "new", new_archive_path, sha256="f" * 64)
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=new_manifest_path,
    )

    result = manager.prepare()

    assert result["ok"] is False
    assert result["reason"] == "runtime_archive_checksum_mismatch"


def test_show_runtime_manager_installs_manifest_archive_from_verified_offline_cache(monkeypatch, tmp_path):
    archive_path = _write_runtime_archive(tmp_path)
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    digest = _sha256(archive_path)
    runtime_dir = tmp_path / "runtime"
    cached = runtime_dir / "downloads" / f"{digest}.tgz"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(archive_path.read_bytes())
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["archives"][runtime_platform_tag()]["url"] = "https://example.invalid/runtime.tgz"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
        offline=True,
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: ["/bin/node"] if command == "node" else None)

    result = manager.prepare()

    assert result["ok"] is True
    assert result["reason"] is None


def test_show_runtime_manager_falls_back_to_installed_record_when_manifest_source_is_missing(
    monkeypatch,
    tmp_path,
):
    archive_path = _write_runtime_archive(tmp_path)
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )
    installed = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
    ).prepare()
    manifest_path.unlink()
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
    )

    availability = asyncio.run(manager._resolve_managed_availability())

    assert availability.command == installed["command"]
    assert manager.status()["reason"] == "runtime_manifest_missing"


@pytest.mark.parametrize("manifest_source", ("path", "packaged"))
def test_show_runtime_manager_falls_back_when_selected_manifest_is_invalid(
    monkeypatch,
    tmp_path,
    manifest_source,
):
    archive_path = _write_runtime_archive(tmp_path)
    valid_manifest = _write_runtime_manifest(tmp_path, archive_path)
    if manifest_source == "path":
        selected_manifest = valid_manifest
        manager_kwargs = {"manifest_path": selected_manifest}
    else:
        package_root = tmp_path / "package"
        selected_manifest = package_root / show_runtime._RUNTIME_MANIFEST_RESOURCE
        selected_manifest.parent.mkdir(parents=True)
        selected_manifest.write_bytes(valid_manifest.read_bytes())
        monkeypatch.setattr(
            "core.managed_runtime.package_resources.files",
            lambda _package: package_root,
        )
        manager_kwargs = {}

    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )
    installed = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        **manager_kwargs,
    ).prepare()
    selected_manifest.write_text("not-json", encoding="utf-8")
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        **manager_kwargs,
    )

    availability = asyncio.run(manager._resolve_managed_availability())

    assert availability.command == installed["command"]
    assert manager.status()["reason"] == "runtime_manifest_invalid"


def test_show_runtime_manager_status_does_not_read_manifest_for_legacy_sources(tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
        auto_install=False,
    )

    status = manager.status()

    assert status["provider"] == "npm"
    assert status["manifest"] is None
    assert status["reason"] is None


def test_show_runtime_manager_can_disable_auto_install(tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
        auto_install=False,
    )

    availability = asyncio.run(manager._resolve_managed_availability())

    assert availability.policy.value == "skipped"
    assert availability.policy_reason == "VIBE_SHOW_RUNTIME_AUTO_INSTALL"
    assert availability.install.value == "absent"


@pytest.mark.parametrize(
    ("install_skip", "auto_install", "skip_reason"),
    (
        pytest.param(True, True, "VIBE_INSTALL_SKIP_SHOW_RUNTIME", id="install-skip"),
        pytest.param(False, False, "VIBE_SHOW_RUNTIME_AUTO_INSTALL", id="auto-install-off"),
        pytest.param(False, True, None, id="allowed"),
    ),
)
@pytest.mark.parametrize("automatic", (True, False), ids=("automatic", "explicit"))
@pytest.mark.parametrize("force", (False, True), ids=("reuse", "force"))
def test_show_runtime_prepare_policy_truth_table(
    monkeypatch,
    tmp_path,
    install_skip,
    auto_install,
    skip_reason,
    automatic,
    force,
):
    monkeypatch.delenv("VIBE_INSTALL_SKIP_SHOW_RUNTIME", raising=False)
    if install_skip:
        monkeypatch.setenv("VIBE_INSTALL_SKIP_SHOW_RUNTIME", "1")
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
        auto_install=auto_install,
    )
    command = [str(tmp_path / "runtime" / "avibe-show-runtime")]
    calls = []
    monkeypatch.setattr(
        manager,
        "_install_managed_runtime_locked",
        lambda *, force, offline: calls.append((force, offline)) or command,
    )
    monkeypatch.setattr(manager, "status", lambda **_kwargs: {})

    result = manager.prepare(force=force, automatic=automatic)

    expected_skip = automatic and skip_reason is not None
    assert result["policy"] == {
        "state": "skipped" if expected_skip else "allowed",
        "reason": skip_reason if expected_skip else None,
        "failure_class": "configured" if expected_skip else None,
        "recovery_action": "change_setting" if expected_skip else None,
    }
    assert result["install"]["state"] == ("absent" if expected_skip else "installed")
    assert result["runtime"]["state"] == "unchecked"
    assert result["ok"] is (not expected_skip)
    assert calls == ([] if expected_skip else [(force, False)])


def test_show_runtime_automatic_opt_out_does_not_fetch_remote_manifest(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="manifest-cache",
        manifest_url="https://example.invalid/show-runtime-manifest.json",
        auto_install=False,
    )
    monkeypatch.setattr(
        "core.managed_runtime.fetch_bytes",
        lambda *_args, **_kwargs: pytest.fail("automatic opt-out must not access the network"),
    )

    result = manager.prepare(automatic=True)

    assert result["policy"]["state"] == "skipped"
    assert result["install"]["state"] == "absent"


def test_show_runtime_remote_manifest_install_remains_installed_when_source_is_unavailable(monkeypatch, tmp_path):
    runtime_dir, manifest_url, install_dir, command = _install_remote_manifest_runtime(monkeypatch, tmp_path)
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_url=manifest_url,
        auto_install=False,
    )
    monkeypatch.setattr(
        "core.managed_runtime.fetch_bytes",
        lambda *_args, **_kwargs: pytest.fail("policy skip must derive install state from disk"),
    )

    skipped = manager.prepare(automatic=True)

    assert skipped["policy"] == {
        "state": "skipped",
        "reason": "VIBE_SHOW_RUNTIME_AUTO_INSTALL",
        "failure_class": "configured",
        "recovery_action": "change_setting",
    }
    assert skipped["install"]["state"] == "installed"
    assert skipped["runtime"]["state"] == "unchecked"
    assert skipped["command"] == command
    assert skipped["status"]["install"]["state"] == "installed"
    assert skipped["status"]["install"]["install_dir"] == str(install_dir)

    def fail_manifest_fetch(*_args, **_kwargs):
        raise OSError("manifest host unavailable")

    monkeypatch.setattr("core.managed_runtime.fetch_bytes", fail_manifest_fetch)
    status = manager.status()

    assert status["install"]["state"] == "installed"
    assert status["runtime"]["state"] == "unchecked"
    assert status["install"]["runtime_version"] == "runtime-test-ref"
    assert status["install"]["matches_manifest"] is None
    assert status["install"]["matches_manifest"] is None
    assert status["command"] == command
    assert status["manifest"]["source"] == manifest_url
    assert status["reason"] == "runtime_manifest_download_failed"


def test_show_runtime_offline_remote_cache_record_resolves_for_fresh_manager(monkeypatch, tmp_path):
    archive_path = _write_runtime_archive(tmp_path)
    manifest_path = _write_runtime_manifest(tmp_path, archive_path)
    manifest_url = "https://example.test/show-runtime-manifest.json"
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["test-node"] if command == "node" else None,
    )
    monkeypatch.setattr("core.show_runtime._node_version", lambda _node: (22, 12, 0))
    monkeypatch.setattr(
        "core.managed_runtime.fetch_bytes",
        lambda url, **_kwargs: manifest_path.read_bytes()
        if url == manifest_url
        else pytest.fail(f"unexpected fetch: {url}"),
    )
    warming_manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_url=manifest_url,
    )._shared_manifest_manager(offline=False)
    assert warming_manager.load_manifest(allow_network=True) is not None
    digest = _sha256(archive_path)
    cached_archive = runtime_dir / "downloads" / f"{digest}.tgz"
    cached_archive.parent.mkdir(parents=True, exist_ok=True)
    cached_archive.write_bytes(archive_path.read_bytes())

    offline_manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_url=manifest_url,
        offline=True,
    )
    installed = offline_manager.prepare()
    install_dir = Path(installed["command"][1]).parents[4]
    metadata = json.loads(
        (install_dir / ".vibe-show-runtime.json").read_text(encoding="utf-8")
    )
    assert metadata["manifest_source"] == f"cache:{manifest_url}"

    fresh_manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_url=manifest_url,
        offline=True,
        auto_install=False,
    )
    resolved = fresh_manager.prepare(automatic=True)

    assert resolved["install"]["state"] == "installed"
    assert resolved["command"] == installed["command"]


def test_show_runtime_status_keeps_installed_identity_separate_from_selected_manifest(monkeypatch, tmp_path):
    runtime_dir, manifest_url, install_dir, command = _install_remote_manifest_runtime(monkeypatch, tmp_path)
    selected_archive = _write_runtime_archive(tmp_path, text="#!/usr/bin/env node\n// selected runtime\n")
    selected_manifest = _write_runtime_manifest(
        tmp_path,
        selected_archive,
        url="https://example.test/selected-runtime.tgz",
        runtime_version="runtime-selected-ref",
    )
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_url=manifest_url,
        auto_install=False,
    )
    monkeypatch.setattr(
        "core.managed_runtime.fetch_bytes",
        lambda url, **_kwargs: selected_manifest.read_bytes()
        if url == manifest_url
        else pytest.fail(f"unexpected fetch: {url}"),
    )

    status = manager.status()

    assert status["install"] == {
        "state": "installed",
        "reason": None,
        "failure_class": None,
        "recovery_action": None,
        "command": command,
        "install_dir": str(install_dir),
        "runtime_version": "runtime-test-ref",
        "matches_manifest": False,
    }
    assert status["manifest"]["runtime_version"] == "runtime-selected-ref"
    assert "installed" not in status
    assert "installed_matches_manifest" not in status
    assert "install_dir" not in status


def test_show_runtime_disk_install_fact_does_not_require_node(monkeypatch, tmp_path):
    runtime_dir, manifest_url, _install_dir, _command = _install_remote_manifest_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda _command: None)
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_url=manifest_url,
        auto_install=False,
    )
    monkeypatch.setattr(
        "core.managed_runtime.fetch_bytes",
        lambda *_args, **_kwargs: pytest.fail("policy skip must not fetch the manifest"),
    )

    result = manager.prepare(automatic=True)

    assert result["policy"]["state"] == "skipped"
    assert result["install"]["state"] == "installed"
    assert result["install"]["command"] is None
    assert result["runtime"]["state"] == "unchecked"
    assert result["ok"] is False
    assert result["reason"] == "VIBE_SHOW_RUNTIME_AUTO_INSTALL"


@pytest.mark.parametrize(
    "corruption",
    (
        "outside-versions",
        "source-lineage",
        "unrelated-cache-source",
        "malformed-cache-source",
        "non-string-source",
        "pointer-metadata",
        "invalid-json",
    ),
)
def test_show_runtime_disk_install_pointer_fails_closed(monkeypatch, tmp_path, corruption):
    runtime_dir, manifest_url, install_dir, _command = _install_remote_manifest_runtime(monkeypatch, tmp_path)
    pointer_path = runtime_dir / "current.json"
    metadata_path = install_dir / ".vibe-show-runtime.json"
    if corruption == "outside-versions":
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        pointer["install_dir"] = str(tmp_path)
        pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    elif corruption == "source-lineage":
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["manifest_source"] = "https://other.example/show-runtime-manifest.json"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    elif corruption == "unrelated-cache-source":
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["manifest_source"] = "cache:https://other.example/show-runtime-manifest.json"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    elif corruption == "malformed-cache-source":
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["manifest_source"] = "cache:"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    elif corruption == "non-string-source":
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["manifest_source"] = {"cache": manifest_url}
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    elif corruption == "pointer-metadata":
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        pointer["archive_sha256"] = "f" * 64
        pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    else:
        pointer_path.write_text("not-json", encoding="utf-8")

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_url=manifest_url,
        auto_install=False,
    )
    monkeypatch.setattr(
        "core.managed_runtime.fetch_bytes",
        lambda *_args, **_kwargs: pytest.fail("policy skip must not fetch the manifest"),
    )

    result = manager.prepare(automatic=True)

    assert result["policy"]["state"] == "skipped"
    assert result["install"]["state"] == "absent"
    assert result["command"] is None


def test_show_runtime_policy_skip_preserves_independent_installed_state(tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
        auto_install=False,
    )
    manager._managed_command = [str(tmp_path / "runtime" / "avibe-show-runtime")]

    result = manager.prepare(automatic=True)

    assert result["policy"]["state"] == "skipped"
    assert result["install"]["state"] == "installed"
    assert result["runtime"]["state"] == "unchecked"


@pytest.mark.parametrize(
    ("reason", "expected_class", "expected_action"),
    (
        ("runtime_archive_download_failed", "transient", "repair"),
        ("runtime_platform_unsupported", "permanent", "no_local_action"),
        ("runtime_archive_unavailable_offline", "configured", "change_setting"),
        ("runtime_archive_checksum_mismatch", "checksum", "repair"),
    ),
)
def test_show_runtime_availability_classifies_install_failure(
    monkeypatch,
    tmp_path,
    reason,
    expected_class,
    expected_action,
):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
    )

    def fail_install(*, force, offline):
        manager._install_reason = reason
        return None

    monkeypatch.setattr(manager, "_install_managed_runtime_locked", fail_install)

    result = manager.prepare(automatic=True)

    assert result["policy"]["state"] == "allowed"
    assert result["install"] == {
        "state": "failed",
        "reason": reason,
        "failure_class": expected_class,
        "recovery_action": expected_action,
        "command": None,
        "install_dir": None,
        "runtime_version": None,
        "matches_manifest": None,
    }
    assert result["runtime"]["state"] == "unchecked"


def test_show_runtime_prepare_options_do_not_mutate_shared_manager_state(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
        force_install=False,
        offline=False,
    )
    calls = []
    monkeypatch.setattr(
        manager,
        "_install_managed_runtime_locked",
        lambda *, force, offline: calls.append((force, offline)) or ["/tmp/runtime"],
    )

    result = manager.prepare(force=True, offline=True)

    assert result["install"]["state"] == "installed"
    assert calls == [(True, True)]
    assert manager.force_install is False
    assert manager.offline is False


def test_show_runtime_prepare_and_request_share_one_install_admission(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
    )
    entered = threading.Event()
    release = threading.Event()
    command = [str(tmp_path / "runtime" / "avibe-show-runtime")]
    calls = []

    def install(*, force, offline):
        calls.append((force, offline))
        entered.set()
        assert release.wait(timeout=5)
        return command

    monkeypatch.setattr(manager, "_install_managed_runtime_locked", install)
    prepared = []
    prepare_thread = threading.Thread(target=lambda: prepared.append(manager.prepare()))
    prepare_thread.start()
    assert entered.wait(timeout=5)

    async def resolve_during_prepare():
        resolving = asyncio.create_task(manager._resolve_managed_availability())
        await asyncio.sleep(0.05)
        release.set()
        return await resolving

    resolved = asyncio.run(resolve_during_prepare())
    prepare_thread.join(timeout=5)

    assert calls == [(False, False)]
    assert prepared[0]["install"]["state"] == "installed"
    assert resolved.install.value == "installed"
    assert resolved.command == command


def test_show_runtime_manager_installs_without_blocking_event_loop(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )

    def fake_install():
        bin_path = manager._managed_bin_path()
        bin_path.parent.mkdir(parents=True)
        bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
        bin_path.chmod(0o755)
        return [str(bin_path)]

    monkeypatch.setattr(
        manager,
        "_install_managed_runtime_locked",
        lambda *, force, offline: fake_install(),
    )
    calls = []

    async def fake_to_thread(func, *args, **kwargs):
        calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr("core.show_runtime.asyncio.to_thread", fake_to_thread)

    assert asyncio.run(manager._resolve_managed_command()) == [str(manager._managed_bin_path())]
    assert [call.__name__ for call in calls] == [
        "_safe_installed_managed_runtime_command",
        "_attempt_managed_install",
    ]


def test_show_runtime_manager_fails_closed_when_manifest_is_absent(tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        manifest_path=tmp_path / "missing-manifest.json",
    )

    assert manager.runtime_source == "manifest-cache"
    assert manager.status()["reason"] == "runtime_manifest_missing"


def test_show_runtime_manager_defaults_to_manifest_provider(tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )

    assert manager.runtime_source == "manifest-cache"


def test_show_runtime_manager_can_use_npm_source(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
    )
    called = []
    monkeypatch.setattr(
        manager,
        "_install_npm_runtime",
        lambda *, force: called.append(force) or ["/tmp/avibe-show-runtime"],
    )

    assert manager._install_managed_runtime_locked(force=False, offline=False).command == ["/tmp/avibe-show-runtime"]
    assert called == [False]


def test_show_runtime_destructive_replacement_invalidates_cached_install_before_removal(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    managed_tree = runtime_dir / "package" / "node_modules"
    managed_bin = managed_tree / ".bin" / "avibe-show-runtime"
    managed_bin.parent.mkdir(parents=True)
    managed_bin.write_text("old runtime\n", encoding="utf-8")
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="npm",
    )
    manager._publish_install_availability(command=[str(managed_bin)])
    real_rmtree = shutil.rmtree

    def remove_after_observing_invalidated_state(path):
        assert manager._managed_command is None
        assert manager._availability.command is None
        assert manager._availability.install.value == "absent"
        real_rmtree(path)

    monkeypatch.setattr("core.show_runtime.shutil.rmtree", remove_after_observing_invalidated_state)

    assert manager._remove_managed_runtime_tree_for_replacement(managed_tree, label="test") is True


def test_show_runtime_manager_forced_npm_replacement_preserves_old_tree_on_failed_install(
    monkeypatch,
    tmp_path,
):
    runtime_dir = tmp_path / "runtime"
    managed_bin = runtime_dir / "package" / "node_modules" / ".bin" / "avibe-show-runtime"
    managed_bin.parent.mkdir(parents=True)
    managed_bin.write_text("old runtime\n", encoding="utf-8")
    managed_bin.chmod(0o755)
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="npm",
    )
    manager._managed_command = [str(managed_bin)]
    install_calls = []
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/npm"] if command == "npm" else None,
    )
    monkeypatch.setattr(
        "core.show_runtime.subprocess.run",
        lambda *_args, **_kwargs: install_calls.append(True) or SimpleNamespace(returncode=1),
    )

    result = manager.prepare(force=True)

    assert result["ok"] is False
    assert result["reason"] == "runtime_install_failed"
    assert result["status"]["install"]["state"] == "installed"
    assert managed_bin.read_text(encoding="utf-8") == "old runtime\n"
    assert install_calls == [True]


def test_show_runtime_manager_forced_npm_replacement_publishes_immutable_tree(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    managed_bin = runtime_dir / "package" / "node_modules" / ".bin" / "avibe-show-runtime"
    managed_bin.parent.mkdir(parents=True)
    managed_bin.write_text("old runtime\n", encoding="utf-8")
    managed_bin.chmod(0o755)
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="npm",
    )
    manager._managed_command = [str(managed_bin)]
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/npm"] if command == "npm" else None,
    )

    def install_without_removing_old(command, **_kwargs):
        assert managed_bin.read_text(encoding="utf-8") == "old runtime\n"
        prefix = Path(command[command.index("--prefix") + 1])
        installed_bin = manager._npm_bin_path(prefix)
        installed_bin.parent.mkdir(parents=True)
        installed_bin.write_text("new runtime\n", encoding="utf-8")
        installed_bin.chmod(0o755)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("core.show_runtime.subprocess.run", install_without_removing_old)

    result = manager.prepare(force=True)

    assert result["ok"] is True
    assert result["command"] != [str(managed_bin)]
    assert Path(result["command"][0]).read_text(encoding="utf-8") == "new runtime\n"
    assert managed_bin.read_text(encoding="utf-8") == "old runtime\n"


@pytest.mark.parametrize(
    "install_error",
    (
        OSError("npm spawn failed"),
        subprocess.TimeoutExpired(cmd=["npm", "install"], timeout=180),
    ),
)
def test_show_runtime_manager_forced_npm_replacement_reports_delegate_exception(
    monkeypatch,
    tmp_path,
    install_error,
):
    runtime_dir = tmp_path / "runtime"
    managed_bin = runtime_dir / "package" / "node_modules" / ".bin" / "avibe-show-runtime"
    managed_bin.parent.mkdir(parents=True)
    managed_bin.write_text("old runtime\n", encoding="utf-8")
    managed_bin.chmod(0o755)
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="npm",
    )
    manager._managed_command = [str(managed_bin)]
    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/npm"] if command == "npm" else None,
    )

    install_calls = []

    def fail_install(*_args, **_kwargs):
        install_calls.append(True)
        assert managed_bin.read_text(encoding="utf-8") == "old runtime\n"
        raise install_error

    monkeypatch.setattr("core.show_runtime.subprocess.run", fail_install)

    result = manager.prepare(force=True)
    retried = manager.prepare()

    assert result["ok"] is False
    assert result["reason"] == "runtime_install_failed"
    assert result["install"]["state"] == "installed"
    assert result["status"]["install"]["state"] == "installed"
    assert retried["ok"] is True
    assert manager._managed_command == [str(managed_bin)]
    assert install_calls == [True]
    assert managed_bin.read_text(encoding="utf-8") == "old runtime\n"


def test_show_runtime_shutdown_stops_manager():
    from vibe.ui_server import stop_show_runtime_on_shutdown

    manager = _FakeShowRuntimeManager()
    set_show_runtime_manager_for_tests(manager)
    try:
        stop_show_runtime_on_shutdown()
    finally:
        set_show_runtime_manager_for_tests(None)

    assert manager.stopped is True


def test_show_runtime_shutdown_cancels_startup_reconcile_before_stopping_manager():
    from vibe.ui_server import _stop_startup_dependency_reconcile, stop_show_runtime_on_shutdown

    shutdown_handlers = app.router.on_shutdown

    assert shutdown_handlers.index(_stop_startup_dependency_reconcile) < shutdown_handlers.index(stop_show_runtime_on_shutdown)


def test_startup_dependency_reconcile_prewarms_runtime_after_prepare(monkeypatch):
    from vibe.ui_server import _reconcile_startup_dependencies_task

    called = {"reconcile": 0, "runtime": 0, "sessions": []}

    def fake_reconcile():
        called["reconcile"] += 1
        return {
            "ok": True,
            "show_runtime": {
                "ok": True,
                "policy": {"state": "allowed", "reason": None},
                "install": {"state": "installed", "reason": None},
                "runtime": {"state": "unchecked", "reason": None},
            },
            "askill": {"ok": True},
        }

    async def fake_runtime_prewarm():
        called["runtime"] += 1
        return SimpleNamespace(available=True, reason=None)

    async def fake_session_prewarm(session_id, *, context):
        called["sessions"].append((session_id, context))
        return SimpleNamespace(available=True, reason=None)

    monkeypatch.setattr("vibe.api.reconcile_startup_dependencies", fake_reconcile)
    monkeypatch.setattr("vibe.ui_server._server", SimpleNamespace(started=True))
    monkeypatch.setattr(
        "vibe.api.startup_show_page_prewarm_targets",
        lambda: {
            "ok": True,
            "limit": 2,
            "pages": [
                {"session_id": "ses_private", "context": "private"},
                {"session_id": "ses_public", "context": "shared"},
            ],
        },
    )
    monkeypatch.setattr("core.show_runtime.prewarm_show_runtime", fake_runtime_prewarm)
    monkeypatch.setattr("core.show_runtime.prewarm_show_page_session", fake_session_prewarm)

    asyncio.run(_reconcile_startup_dependencies_task())

    assert called == {
        "reconcile": 1,
        "runtime": 1,
        "sessions": [
            ("ses_private", ShowRuntimeContext.PRIVATE),
            ("ses_public", ShowRuntimeContext.SHARED),
        ],
    }


def test_startup_dependency_reconcile_does_not_prewarm_policy_skip(monkeypatch):
    from vibe.ui_server import _reconcile_startup_dependencies_task

    monkeypatch.setattr(
        "vibe.api.reconcile_startup_dependencies",
        lambda: {
            "ok": True,
            "show_runtime": {
                "ok": True,
                "status": "skipped",
                "policy": {
                    "state": "skipped",
                    "reason": "VIBE_SHOW_RUNTIME_AUTO_INSTALL",
                },
                "install": {"state": "absent", "reason": None},
                "runtime": {"state": "unchecked", "reason": None},
            },
        },
    )
    monkeypatch.setattr(
        "core.show_runtime.prewarm_show_runtime",
        lambda: pytest.fail("a policy skip must not enter prewarm"),
    )
    monkeypatch.setattr("vibe.ui_server._server", SimpleNamespace(started=True))

    asyncio.run(_reconcile_startup_dependencies_task())


def test_startup_dependency_reconcile_waits_until_the_ui_host_is_ready(monkeypatch):
    from vibe.ui_server import _reconcile_startup_dependencies_task

    server = SimpleNamespace(started=False)
    calls: list[str] = []
    monkeypatch.setattr("vibe.ui_server._server", server)
    monkeypatch.setattr(
        "vibe.api.reconcile_startup_dependencies",
        lambda: calls.append("reconcile")
        or {
            "ok": True,
            "show_runtime": {
                "ok": True,
                "policy": {"state": "skipped"},
                "install": {"state": "absent"},
            },
        },
    )

    async def run() -> None:
        task = asyncio.create_task(_reconcile_startup_dependencies_task())
        await asyncio.sleep(0.01)
        assert calls == []
        server.started = True
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())
    assert calls == ["reconcile"]


def test_show_runtime_proxy_logs_entry_timing(monkeypatch, tmp_path, caplog):
    caplog.set_level("INFO")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager(body=b"<html><body><div id=\"root\">ready</div></body></html>")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get("/show/ses123/", base_url="http://127.0.0.1:5123")
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert "Show Runtime proxy GET /sessions/ses123/app/ session=ses123 asset=<entry>" in caplog.text


def test_private_show_page_hmr_websocket_requires_private_page(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "offline")

    try:
        with app.test_client().websocket_connect(
            "/show/ses123/__vite_hmr",
            headers={"host": "127.0.0.1:5123"},
            subprotocols=["vite-hmr"],
        ):
            raise AssertionError("websocket should not connect")
    except Exception as exc:
        assert getattr(exc, "code", None) == 1008


def test_private_show_page_hmr_websocket_requires_remote_session(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")

    with app.test_client().websocket_connect(
        "wss://alex.avibe.bot/show/ses123/__vite_hmr",
        headers={"host": "alex.avibe.bot"},
        subprotocols=["vite-hmr"],
    ) as websocket:
        with pytest.raises(WebSocketDisconnect) as exc:
            websocket.receive_text()

    assert exc.value.code == ui_server._AUTHORIZATION_LOGIN_REQUIRED_WEBSOCKET_CLOSE_CODE


def test_private_show_page_hmr_websocket_requires_editor(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager()
    set_show_runtime_manager_for_tests(manager)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "viewer@example.com", "viewer-1", role="viewer"),
        domain="alex.avibe.bot",
    )
    try:
        with client.websocket_connect(
            "wss://alex.avibe.bot/show/ses123/__vite_hmr",
            headers={"host": "alex.avibe.bot"},
            subprotocols=["vite-hmr"],
        ) as websocket:
            websocket.receive_text()
            raise AssertionError("viewer HMR should not connect")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008
    finally:
        set_show_runtime_manager_for_tests(None)
    assert manager.calls == []


def test_show_page_viewer_is_read_only_but_editor_can_mutate(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_show_page("ses123", "private")

    def _client(role: str):
        client = app.test_client()
        client.set_cookie(
            remote_access.SESSION_COOKIE_NAME,
            _active_org_cookie(config, f"{role}@example.com", f"{role}-1", role=role),
            domain="alex.avibe.bot",
        )
        return client

    manager = _FakeShowRuntimeManager(body=b'{"ok":true}', extra_headers={"content-type": "application/json"})
    set_show_runtime_manager_for_tests(manager)
    try:
        viewer = _client("viewer")
        read = viewer.get(
            "/show/ses123/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert read.status_code == 200
        manager.calls.clear()
        for method in ("post", "put", "patch", "delete"):
            response = getattr(viewer, method)(
                "/show/ses123/api/health",
                base_url="https://alex.avibe.bot",
                environ_base=_remote_peer(),
                headers={
                    "Origin": "https://alex.avibe.bot",
                    "Content-Type": "application/json",
                },
                content=b'{"ping":true}',
            )
            assert response.status_code == 403
        assert manager.calls == []

        editor = _client("editor")
        mutation = editor.post(
            "/show/ses123/api/health",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={
                "Origin": "https://alex.avibe.bot",
                "Content-Type": "application/json",
            },
            content=b'{"ping":true}',
        )
        assert mutation.status_code == 200
        assert [call[0] for call in manager.calls] == ["POST"]
    finally:
        set_show_runtime_manager_for_tests(None)


def test_private_show_page_hmr_websocket_accepts_remote_session(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager()
    set_show_runtime_manager_for_tests(manager)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _active_org_cookie(config, "alex@example.com", "user-1"),
        domain="alex.avibe.bot",
    )
    try:
        with client.websocket_connect(
            "wss://alex.avibe.bot/show/ses123/__vite_hmr",
            headers={"host": "alex.avibe.bot"},
            subprotocols=["vite-hmr"],
        ) as websocket:
            websocket.receive_text()
    except Exception as exc:
        assert getattr(exc, "code", None) == 1011
    finally:
        set_show_runtime_manager_for_tests(None)


def test_private_show_page_hmr_websocket_closes_when_authorization_is_unavailable(
    monkeypatch,
    tmp_path,
):
    class RecordingWebSocket:
        headers = {"host": "alex.avibe.bot"}

        def __init__(self):
            self.calls = []

        async def accept(self, *, subprotocol=None):
            self.calls.append(("accept", subprotocol))

        async def close(self, code=1000):
            self.calls.append(("close", code))

    proxy_calls = []

    async def blocking_proxy(websocket, session_id):
        proxy_calls.append(("started", session_id))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            proxy_calls.append(("cancelled", session_id))
            raise

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    monkeypatch.setattr(ui_server, "_show_runtime_hmr_origin_allowed", lambda websocket: True)
    monkeypatch.setattr(ui_server, "_websocket_is_local_request", lambda *args: False)
    monkeypatch.setattr(ui_server, "_show_runtime_websocket_authorized", lambda websocket, **kwargs: True)
    monkeypatch.setattr(ui_server, "_project_session_access_allowed", lambda *args: True)
    monkeypatch.setattr(
        ui_server,
        "_show_runtime_websocket_resource_context",
        lambda websocket, **kwargs: resource_access_service.ResourceUserContext(
            instance_role="editor",
            subject="remote-member",
            instance_access_source="organization_group",
            organization_id="org-1",
            organization_member_id="membership-1",
            organization_role="member",
            is_remote=True,
        ),
    )
    remote_payload = {
        "sub": "remote-member",
        "vibe_instance_role": "editor",
        "vibe_instance_access_source": "organization_group",
        "vibe_organization_id": "org-1",
        "vibe_organization_member_id": "membership-1",
        "vibe_organization_role": "member",
    }

    async def authorize(*args, **kwargs):
        return {"sub": "remote-member"}, remote_access.AuthorizationResolution(
            "current",
            payload=remote_payload,
        )

    async def authorization_loss(*args, **kwargs):
        return "unavailable"

    monkeypatch.setattr(ui_server, "_remote_access_websocket_authorization", authorize)
    monkeypatch.setattr(
        ui_server,
        "_wait_for_remote_session_authorization_loss",
        authorization_loss,
    )
    monkeypatch.setattr(ui_server, "_proxy_show_runtime_websocket", blocking_proxy)
    websocket = RecordingWebSocket()

    asyncio.run(ui_server.show_runtime_hmr_websocket(websocket, "ses123"))

    assert websocket.calls == [
        ("accept", "vite-hmr"),
        ("close", ui_server._AUTHORIZATION_UNAVAILABLE_WEBSOCKET_CLOSE_CODE),
    ]
    assert proxy_calls == [("started", "ses123"), ("cancelled", "ses123")]


def test_remote_org_show_page_hmr_ignores_resource_acl_changes(
    monkeypatch,
    tmp_path,
):
    from vibe.sse_broker import broker

    class RecordingWebSocket:
        headers = {"host": "alex.avibe.bot"}

        def __init__(self):
            self.calls = []

        async def accept(self, *, subprotocol=None):
            self.calls.append(("accept", subprotocol))

        async def close(self, code=1000):
            self.calls.append(("close", code))

    proxy_started = asyncio.Event()
    proxy_calls = []

    async def blocking_proxy(websocket, session_id):
        proxy_calls.append(("started", session_id))
        proxy_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            proxy_calls.append(("cancelled", session_id))
            raise

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
    monkeypatch.setattr(ui_server, "_show_runtime_hmr_origin_allowed", lambda websocket: True)
    monkeypatch.setattr(ui_server, "_websocket_is_local_request", lambda *args: False)
    monkeypatch.setattr(ui_server, "_show_runtime_websocket_authorized", lambda websocket, **kwargs: True)
    remote_payload = {
        "sub": "remote-editor",
        "email": "alice@example.com",
        "vibe_instance_role": "editor",
        "vibe_instance_access_source": "organization_group",
        "vibe_organization_id": "org-1",
        "vibe_organization_member_id": "member-remote-editor",
        "vibe_organization_role": "member",
        "vibe_group_ids": ["group-engineering"],
    }

    async def authorize(*args, **kwargs):
        return {"sub": "remote-editor"}, remote_access.AuthorizationResolution(
            "current",
            payload=remote_payload,
        )

    async def authorization_loss(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(ui_server, "_remote_access_websocket_authorization", authorize)
    monkeypatch.setattr(
        ui_server,
        "_wait_for_remote_session_authorization_loss",
        authorization_loss,
    )
    monkeypatch.setattr(
        ui_server,
        "_show_runtime_websocket_resource_context",
        lambda websocket, **kwargs: resource_access_service.ResourceUserContext(
            subject="remote-editor",
            email="alice@example.com",
            organization_id="org-1",
            organization_member_id="member-remote-editor",
            organization_role="member",
            group_ids=frozenset({"group-engineering"}),
            instance_role="editor",
            instance_access_source="organization_group",
            is_remote=True,
        ),
    )
    monkeypatch.setattr(ui_server, "_proxy_show_runtime_websocket", blocking_proxy)
    websocket = RecordingWebSocket()

    async def _run_after_acl_change() -> None:
        task = asyncio.create_task(ui_server.show_runtime_hmr_websocket(websocket, "ses123"))
        await asyncio.wait_for(proxy_started.wait(), timeout=1)
        # A Resource ACL change must not close the /show HMR socket: /show
        # admission is instance-role only and no longer reads a page policy.
        broker.publish(
            "authorization.changed",
            {"project_ids": [], "resource_kinds": ["agent"]},
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_run_after_acl_change())

    assert websocket.calls == [("accept", "vite-hmr")]
    assert proxy_calls == [("started", "ses123"), ("cancelled", "ses123")]


def test_private_show_page_hmr_websocket_accepts_setup_host_local_peer(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.ui.setup_host = "192.168.2.3"
    config.save()
    _mock_interface(monkeypatch, "192.168.2.3", 24)
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager()
    set_show_runtime_manager_for_tests(manager)
    try:
        with app.test_client().websocket_connect(
            "/show/ses123/__vite_hmr",
            headers={
                "host": "192.168.2.3:5123",
                "x-vibe-test-remote-addr": "192.168.2.44",
            },
            subprotocols=["vite-hmr"],
        ) as websocket:
            websocket.receive_text()
    except Exception as exc:
        assert getattr(exc, "code", None) == 1011
    finally:
        set_show_runtime_manager_for_tests(None)

    assert manager.websocket_paths == ["/show/ses123/__vite_hmr"]
    assert manager.websocket_headers == [
        {
            "X-Avibe-Show-Protocol": "1",
            "X-Avibe-Show-Context": "private",
        }
    ]


@pytest.mark.parametrize("visibility", ["limited", "public"])
def test_shared_mode_show_page_hmr_websocket_accepts_local_peer(
    monkeypatch,
    tmp_path,
    visibility,
):
    # The HMR socket serves all authenticated editor modes, so a shared-mode
    # /show/ HMR socket gets PAST the audience gate (then fails at the fake
    # runtime proxy with 1011 — not the 1008 visibility rejection an offline page
    # would get), keeping live HMR when the audience changes.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.ui.setup_host = "192.168.2.3"
    config.save()
    _mock_interface(monkeypatch, "192.168.2.3", 24)
    _create_show_page("ses123", visibility)
    manager = _FakeShowRuntimeManager()
    set_show_runtime_manager_for_tests(manager)
    try:
        with app.test_client().websocket_connect(
            "/show/ses123/__vite_hmr",
            headers={
                "host": "192.168.2.3:5123",
                "x-vibe-test-remote-addr": "192.168.2.44",
            },
            subprotocols=["vite-hmr"],
        ) as websocket:
            websocket.receive_text()
    except Exception as exc:
        assert getattr(exc, "code", None) == 1011  # accepted past the visibility gate; proxy then fails
    finally:
        set_show_runtime_manager_for_tests(None)

    assert manager.websocket_paths == ["/show/ses123/__vite_hmr"]
    assert manager.websocket_headers[0]["X-Avibe-Show-Context"] == "private"


def test_private_show_page_hmr_websocket_accepts_trusted_public_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, "127.0.0.1")
    monkeypatch.setenv(ui_server.TRUSTED_PUBLIC_ORIGINS_ENV, "https://avibe.example.com")
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.enabled = False
    config.save()
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager()
    set_show_runtime_manager_for_tests(manager)
    try:
        with app.test_client().websocket_connect(
            "/show/ses123/__vite_hmr",
            headers={
                "host": "127.0.0.1:5123",
                "origin": "https://avibe.example.com",
                "x-forwarded-proto": "https",
                "x-forwarded-host": "avibe.example.com",
                "x-forwarded-for": "203.0.113.10",
                "x-vibe-test-remote-addr": "127.0.0.1",
            },
            subprotocols=["vite-hmr"],
        ) as websocket:
            websocket.receive_text()
    except Exception as exc:
        assert getattr(exc, "code", None) == 1011
    finally:
        set_show_runtime_manager_for_tests(None)

    assert manager.websocket_paths == ["/show/ses123/__vite_hmr"]
    assert manager.websocket_headers[0]["X-Avibe-Show-Context"] == "private"


def test_private_show_page_hmr_websocket_rejects_trusted_public_origin_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, "127.0.0.1")
    monkeypatch.setenv(ui_server.TRUSTED_PUBLIC_ORIGINS_ENV, "https://avibe.example.com")
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.enabled = False
    config.save()
    _create_show_page("ses123", "private")

    try:
        with app.test_client().websocket_connect(
            "/show/ses123/__vite_hmr",
            headers={
                "host": "127.0.0.1:5123",
                "origin": "https://evil.example.com",
                "x-forwarded-proto": "https",
                "x-forwarded-host": "avibe.example.com",
                "x-forwarded-for": "203.0.113.10",
                "x-vibe-test-remote-addr": "127.0.0.1",
            },
            subprotocols=["vite-hmr"],
        ):
            raise AssertionError("websocket should not connect")
    except Exception as exc:
        assert getattr(exc, "code", None) == 1008


def test_public_show_page_hmr_websocket_uses_share_path(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _FakeShowRuntimeManager()
    set_show_runtime_manager_for_tests(manager)
    try:
        with app.test_client().websocket_connect(
            f"wss://alex.avibe.bot/p/{share_id}/__vite_hmr?token=test-token",
            headers={"host": "alex.avibe.bot"},
            subprotocols=["vite-hmr"],
        ) as websocket:
            websocket.receive_text()
    except Exception as exc:
        assert getattr(exc, "code", None) == 1011
    finally:
        set_show_runtime_manager_for_tests(None)

    assert manager.websocket_paths == [f"/p/{share_id}/__vite_hmr?token=test-token"]
    assert manager.websocket_headers == [
        {
            "X-Avibe-Show-Protocol": "1",
            "X-Avibe-Show-Context": "shared",
        }
    ]


def test_public_show_page_hmr_websocket_requires_public_page(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "private")

    try:
        with app.test_client().websocket_connect(
            f"wss://alex.avibe.bot/p/{share_id}/__vite_hmr",
            headers={"host": "alex.avibe.bot"},
            subprotocols=["vite-hmr"],
        ):
            raise AssertionError("websocket should not connect")
    except Exception as exc:
        assert getattr(exc, "code", None) == 1008


def test_private_show_page_redirects_without_trailing_slash(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")

    response = app.test_client().get("/show/ses123", base_url="http://127.0.0.1:5123", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"] == "/show/ses123/"

    followed = app.test_client().get("/show/ses123", base_url="http://127.0.0.1:5123", follow_redirects=True)
    assert followed.status_code == 200
    assert b"Show Page" in followed.content


def test_public_show_page_skips_remote_login_but_requires_public_host(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    set_show_runtime_manager_for_tests(_FakeShowRuntimeManager(fail=True))

    try:
        response = app.test_client().get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )

        assert response.status_code == 200
        assert b"Loading Show Page" in response.content
        assert b"The Show Runtime is unavailable" in response.content
        assert b"vibe doctor repair show-runtime" not in response.content
        assert b"Reload this page to try the request again" in response.content
        assert b'src="./src/main.tsx"' not in response.content

        mismatch = app.test_client().get(
            f"/p/{share_id}/",
            base_url="https://evil.example",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert mismatch.status_code == 503
    assert mismatch.get_json()["error"] == "remote_access_host_mismatch"


def test_public_show_page_uses_runtime_when_available(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _FakeShowRuntimeManager(body=b"<h1>Public Runtime Page</h1>")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert b"Public Runtime Page" in response.content
    assert manager.calls[0][0] == "GET"
    assert manager.calls[0][1] == "/sessions/ses123/app/"
    assert "x-vibe-show-base" not in manager.calls[0][2]
    assert manager.calls[0][2]["X-Avibe-Show-Protocol"] == "1"
    assert manager.calls[0][2]["X-Avibe-Show-Context"] == "shared"


def test_private_and_public_surfaces_share_one_stable_runtime_base(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _FakeShowRuntimeManager(
        body=b'<script type="module" src="/show/ses123/src/main.tsx"></script>'
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        private_before = app.test_client().get(
            "/show/ses123/",
            base_url="http://127.0.0.1:5123",
        )
        public = app.test_client().get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
        )
        private_after = app.test_client().get(
            "/show/ses123/",
            base_url="http://127.0.0.1:5123",
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert [response.status_code for response in (private_before, public, private_after)] == [200, 200, 200]
    assert b'/show/ses123/src/main.tsx' in private_before.content
    assert f'/p/{share_id}/src/main.tsx'.encode() in public.content
    assert b'/show/ses123/src/main.tsx' in private_after.content
    assert [call[2]["X-Avibe-Show-Context"] for call in manager.calls] == [
        "private",
        "shared",
        "private",
    ]
    assert all("x-vibe-show-base" not in call[2] for call in manager.calls)


def test_public_show_page_materializes_workspace_before_runtime_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page_record("ses123", "public")
    page_dir = paths.get_show_pages_dir() / "ses123"
    assert not (page_dir / "src" / "App.tsx").exists()
    manager = _FakeShowRuntimeManager(body=b"<h1>Public Runtime Page</h1>")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert b"Public Runtime Page" in response.content
    assert (page_dir / "src" / "App.tsx").exists()
    assert manager.calls[0][1] == "/sessions/ses123/app/"
    assert "x-vibe-show-base" not in manager.calls[0][2]


@pytest.mark.parametrize(
    "runtime_location",
    [
        "/sessions/ses123/app/foo/",
        "/show/ses123/foo/",
    ],
)
def test_public_show_page_rewrites_runtime_redirect_location(monkeypatch, tmp_path, runtime_location):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _FakeShowRuntimeManager(
        body=b"",
        status_code=302,
        extra_headers={"location": runtime_location},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/foo",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 302
    assert response.headers["location"] == f"/p/{share_id}/foo/"


def test_public_show_page_rewrites_runtime_source_map_headers(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _FakeShowRuntimeManager(
        body=b"export default true",
        extra_headers={
            "content-type": "text/javascript",
            "sourcemap": "/show/ses123/src/App.tsx.map?x=1",
            "x-sourcemap": "https://example.test/show/ses123/external.map",
        },
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/src/App.tsx",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.headers["sourcemap"] == f"/p/{share_id}/src/App.tsx.map?x=1"
    assert response.headers["x-sourcemap"] == "https://example.test/show/ses123/external.map"


@pytest.mark.parametrize("asset_path", ["docs/", "robots"])
def test_public_show_page_preserves_vite_public_dir_assets(monkeypatch, tmp_path, asset_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    public_path = paths.get_show_page_dir("ses123") / "public" / asset_path
    if asset_path.endswith("/"):
        public_path.mkdir(parents=True)
        (public_path / "index.html").write_text("<h1>docs</h1>", encoding="utf-8")
    else:
        public_path.parent.mkdir(parents=True, exist_ok=True)
        public_path.write_text("robots", encoding="utf-8")
    manager = _FakeShowRuntimeManager(body=b"public asset", extra_headers={"content-type": "text/plain"})
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/{asset_path}",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={"Accept": "text/html"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b"public asset"
    assert manager.calls[0][1] == f"/sessions/ses123/app/{asset_path}"


def test_public_show_page_proxies_runtime_api_methods(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _FakeShowRuntimeManager(body=b'{"ok":true}', extra_headers={"content-type": "application/json"})
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().post(
            f"/p/{share_id}/api/health",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={
                "Origin": "https://alex.avibe.bot",
                "Content-Type": "application/json",
            },
            content=b'{"ping":true}',
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b'{"ok":true}'
    assert manager.calls[0][0] == "POST"
    assert manager.calls[0][1] == "/sessions/ses123/app/api/health"
    assert manager.calls[0][2]["content-type"] == "application/json"
    assert "x-vibe-show-base" not in manager.calls[0][2]
    assert manager.calls[0][2]["X-Avibe-Show-Protocol"] == "1"
    assert manager.calls[0][2]["X-Avibe-Show-Context"] == "shared"
    assert "cookie" not in manager.calls[0][2]
    assert manager.calls[0][3] == b'{"ping":true}'
    assert manager.calls[0][4] == 90.0
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "x-runtime-private-header" not in response.headers
    assert "__Host-vibe_remote_session=attacker" not in response.headers.get("set-cookie", "")


def test_public_show_page_api_mutation_rejects_cross_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _FakeShowRuntimeManager(body=b'{"ok":true}', extra_headers={"content-type": "application/json"})
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().post(
            f"/p/{share_id}/api/health",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            headers={
                "Origin": "https://evil.example",
                "Content-Type": "application/json",
            },
            content=b'{"ping":true}',
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 403
    assert response.get_json()["message"] == "Forbidden: invalid origin"
    assert manager.calls == []


def test_public_show_page_api_does_not_fall_back_to_static(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    (paths.get_show_pages_dir() / "ses123" / "api" / "health.ts").write_text("export const secret = true\n", encoding="utf-8")
    set_show_runtime_manager_for_tests(_FakeShowRuntimeManager(fail=True))
    try:
        response = app.test_client().get(
            f"/p/{share_id}/api/health.ts",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 503
    assert response.get_json()["error"] == "show_runtime_unavailable"
    assert b"secret" not in response.content


def test_public_show_page_static_fallback_denies_dot_leading_segments(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    page_dir = paths.get_show_pages_dir() / "ses123"
    (page_dir / ".git").mkdir()
    (page_dir / ".git" / "config").write_text("public history", encoding="utf-8")
    set_show_runtime_manager_for_tests(_FakeShowRuntimeManager(fail=True))
    try:
        response = app.test_client().get(
            f"/p/{share_id}/.git/config",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 404
    assert b"public history" not in response.content


def test_public_show_page_denies_dot_path_before_runtime_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    (paths.get_show_pages_dir() / "ses123" / ".git").write_text(
        "gitdir: /tmp/show-git/ses123.git\n",
        encoding="utf-8",
    )
    manager = _FakeShowRuntimeManager(body=b"leaked pointer")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/.git",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 404
    assert b"show-git" not in response.content
    assert manager.calls == []


def test_public_show_page_proxies_vite_dependency_dot_path(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _FakeShowRuntimeManager(
        body=b"export const react = true",
        extra_headers={"content-type": "text/javascript"},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/node_modules/.vite/deps/react.js",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b"export const react = true"
    assert manager.calls[0][1] == "/sessions/ses123/app/node_modules/.vite/deps/react.js"


def test_public_show_page_proxies_relocated_vite_cache_at_fs_path(monkeypatch, tmp_path):
    avibe_home = tmp_path / ".avibe"
    monkeypatch.setenv("AVIBE_HOME", str(avibe_home))
    _save_config(avibe_home)
    share_id = _create_show_page("ses123", "public")
    dependency_path = paths.get_runtime_dir() / "show-runtime" / ".vite-cache" / "deps" / "react.js"
    manager = _FakeShowRuntimeManager(
        body=b"export const react = true",
        extra_headers={"content-type": "text/javascript"},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/@fs/{dependency_path.as_posix()}",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b"export const react = true"
    assert manager.calls
    assert manager.calls[0][1].endswith(f"/@fs/{dependency_path.as_posix()}")


def test_public_show_page_redirects_without_trailing_slash(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")

    response = app.test_client().get(f"/p/{share_id}", base_url="http://127.0.0.1:5123", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"] == f"/p/{share_id}/"

    followed = app.test_client().get(
        f"/p/{share_id}",
        base_url="http://127.0.0.1:5123",
        headers={"Accept": "text/html"},
        follow_redirects=True,
    )
    assert followed.status_code == 200
    assert b"Show Page" in followed.content


def test_public_and_private_paths_are_canonical_by_visibility(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")

    # Amendment (§2.3, 2026-07-13): the authed /show/ surface now serves public
    # pages too (a page pinned while public must open), so this is 200, not 404.
    authed_response = app.test_client().get("/show/ses123/", base_url="http://127.0.0.1:5123")
    assert authed_response.status_code == 200

    store = ShowPageStore()
    try:
        store.update_visibility("ses123", "private")
    finally:
        store.close()

    # The anonymous /p/<share_id> surface still serves ONLY public pages — a
    # private page is never reachable there.
    public_response = app.test_client().get(f"/p/{share_id}/", base_url="http://127.0.0.1:5123")
    assert public_response.status_code == 404


def test_public_show_not_found_is_a_generic_html_page_for_browsers():
    response = app.test_client().get(
        "/p/unknown-share/",
        base_url="http://127.0.0.1:5123",
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 404
    assert response.headers["Content-Type"].startswith("text/html")
    assert b"This page is unavailable" in response.content
    assert b"not_found" not in response.content


def test_public_show_not_found_respects_html_quality():
    response = app.test_client().get(
        "/p/unknown-share/",
        base_url="http://127.0.0.1:5123",
        headers={"Accept": "application/json, text/html;q=0", "Sec-Fetch-Dest": "empty"},
    )

    assert response.status_code == 404
    assert response.get_json() == {"error": "not_found"}


def test_rotated_public_share_url_stops_working(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    old_share_id = _create_show_page("ses123", "public")

    store = ShowPageStore()
    try:
        page, _ = store.rotate_share("ses123")
    finally:
        store.close()

    old_response = app.test_client().get(
        f"/p/{old_share_id}/",
        base_url="http://127.0.0.1:5123",
        headers={"Accept": "text/html"},
    )
    new_response = app.test_client().get(
        f"/p/{page.share_id}/",
        base_url="http://127.0.0.1:5123",
        headers={"Accept": "text/html"},
    )

    assert old_response.status_code == 404
    assert new_response.status_code == 200


def test_offline_show_page_returns_explanatory_page(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")

    store = ShowPageStore()
    try:
        store.update_visibility("ses123", "offline")
    finally:
        store.close()

    response = app.test_client().get(
        f"/p/{share_id}/",
        base_url="http://127.0.0.1:5123",
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 401
    assert b"offline" in response.content
    assert b"deleted" not in response.content.lower()


def test_show_page_rejects_path_traversal(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")

    response = app.test_client().get(f"/p/{share_id}/../secret.txt", base_url="http://127.0.0.1:5123")

    assert response.status_code == 404
    assert b"secret" not in response.content


def test_show_page_serves_assets_with_strict_headers(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")

    response = app.test_client().get(f"/p/{share_id}/app.js", base_url="http://127.0.0.1:5123")

    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert b"window.showPage" in response.content


def test_show_runtime_vendor_asset_proxy_is_immutable(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    manager = _FakeShowRuntimeManager(
        body=b"export const React = {};",
        extra_headers={
            "content-type": "text/javascript; charset=utf-8",
            "cache-control": "public, max-age=31536000, immutable",
        },
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/_show-runtime/vendor/abc123/react.js",
            base_url="http://127.0.0.1:5123",
            headers={
                "X-Avibe-Show-Protocol": "999",
                "X-Avibe-Show-Context": "private",
            },
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b"export const React = {};"
    # The vendor prefix is forwarded verbatim, never under a per-session base path.
    assert manager.calls[-1][0] == "GET"
    assert manager.calls[-1][1] == "/_show-runtime/vendor/abc123/react.js"
    assert "X-Avibe-Show-Protocol" not in manager.calls[-1][2]
    assert "X-Avibe-Show-Context" not in manager.calls[-1][2]
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["content-type"] == "text/javascript; charset=utf-8"
    assert "set-cookie" not in response.headers
    # The shared, anonymous vendor response must not carry a CSRF cookie that would
    # defeat caching across users.
    assert not any(
        cookie.startswith("vibe_csrf_token=") for cookie in response.headers.getlist("set-cookie")
    )


def test_show_runtime_vendor_asset_proxy_honors_gzip_q0(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    body = b"export const React = {};\n" * 200
    manager = _FakeShowRuntimeManager(
        body=body,
        extra_headers={
            "content-type": "text/javascript; charset=utf-8",
            "cache-control": "public, max-age=31536000, immutable",
        },
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/_show-runtime/vendor/abc123/react.js",
            base_url="http://127.0.0.1:5123",
            headers={"Accept-Encoding": "br, gzip;q=0"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == body
    assert "content-encoding" not in response.headers
    assert "Accept-Encoding" in response.headers["vary"]


def test_show_runtime_vendor_asset_proxy_forwards_query_and_is_public(monkeypatch, tmp_path):
    # No remote login configured here: the vendor namespace is referenced by the
    # anonymous public `/p/<share>/` surface via the runtime's import map, so it must be
    # reachable without authentication just like the public surface itself.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    manager = _FakeShowRuntimeManager(
        body=b".vendor{}",
        extra_headers={"content-type": "text/css; charset=utf-8"},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/_show-runtime/vendor/abc123/index.css?v=1",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            follow_redirects=False,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b".vendor{}"
    assert manager.calls[-1][1] == "/_show-runtime/vendor/abc123/index.css?v=1"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_show_runtime_vendor_asset_proxy_does_not_mark_errors_immutable(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    manager = _FakeShowRuntimeManager(
        body=b'{"error":"Not found"}',
        status_code=404,
        extra_headers={"content-type": "application/json", "cache-control": "no-store"},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/_show-runtime/vendor/abc123/missing.js",
            base_url="http://127.0.0.1:5123",
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


def test_retired_show_runtime_deps_route_is_gone(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    manager = _FakeShowRuntimeManager(body=b"export default {}")
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            "/_show-runtime/deps/r9-d6d38251/react.js?v=d6d38251",
            base_url="http://127.0.0.1:5123",
            follow_redirects=False,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    # The old per-session dep re-sharing layer is fully retired: there is no proxy route
    # at this path anymore, so it falls through to the SPA static handler (404) and never
    # touches the Show Runtime.
    assert response.status_code == 404
    assert manager.calls == []


def test_private_show_page_passes_runtime_importmap_through(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    monkeypatch.setattr("vibe.ui_server.show_event_write_token", lambda session_id: f"token-{session_id}")
    import_map = '{\n  "imports": {\n    "react": "/_show-runtime/vendor/abc123/react.js"\n  }\n}'
    vendor_link = '<link rel="stylesheet" href="/_show-runtime/vendor/abc123/index.css">'
    body = (
        "<!doctype html><html><head>"
        f'<script type="importmap">{import_map}</script>'
        f"{vendor_link}"
        '</head><body><div id="root"></div>'
        '<script type="module" src="/src/main.tsx"></script>'
        "</body></html>"
    ).encode("utf-8")
    manager = _FakeShowRuntimeManager(body=body)
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get("/show/ses123/", base_url="http://127.0.0.1:5123")
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    text = response.content.decode("utf-8")
    # The runtime-injected import map + vendor link must survive untouched...
    assert f'<script type="importmap">{import_map}</script>' in text
    assert vendor_link in text
    assert '"/_show-runtime/vendor/abc123/react.js"' in text
    # ...while avibe still injects its private show config before the app module.
    assert "globalThis.__AVIBE_SHOW__=Object.assign" in text
    assert text.index("globalThis.__AVIBE_SHOW__") < text.index('src="/src/main.tsx"')
    # The import map sits before the injected config (head-prepended by the runtime).
    assert text.index('type="importmap"') < text.index("globalThis.__AVIBE_SHOW__")


def test_public_show_page_passes_runtime_importmap_through_unmodified(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    import_map = '{"imports":{"react":"/_show-runtime/vendor/abc123/react.js","@avibe/show-ui/":"/_show-runtime/vendor/abc123/@avibe_show-ui/"}}'
    body = (
        "<!doctype html><html><head>"
        f'<script type="importmap">{import_map}</script>'
        '<link rel="stylesheet" href="/_show-runtime/vendor/abc123/index.css">'
        "</head><body>"
        '<script type="module" src="/p/' + share_id + '/src/main.tsx"></script>'
        "</body></html>"
    ).encode("utf-8")
    manager = _FakeShowRuntimeManager(
        body=body,
        extra_headers={"content-type": "text/html; charset=utf-8"},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/",
            base_url="http://127.0.0.1:5123",
            headers={"Accept": "text/html"},
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    text = response.content.decode("utf-8")
    # The absolute, session-independent vendor URLs in the import map must pass through
    # the public-surface rewriter untouched (they are not under the `/show/<id>/` base).
    assert f'<script type="importmap">{import_map}</script>' in text
    assert '<link rel="stylesheet" href="/_show-runtime/vendor/abc123/index.css">' in text
    # Public pages receive read/auth config but never the private write token.
    assert "globalThis.__AVIBE_SHOW__=Object.assign" in text
    assert '"writeToken"' not in text


def test_show_runtime_manager_defaults_to_manifest_when_package_manifest_exists(monkeypatch, tmp_path):
    monkeypatch.setattr("core.show_runtime._packaged_runtime_manifest_exists", lambda: True)

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )

    assert manager.runtime_source == "manifest-cache"
