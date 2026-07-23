import asyncio
import base64
import hashlib
import io
import json
import os
import socket
import ssl
import struct
import tarfile
import urllib.error
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from config import paths
from core.show_pages import (
    SHOW_RUNTIME_RECOVERY_LOADING_DELAY_SECONDS,
    ShowPageStore,
    ensure_show_page_dir,
    show_cli_event_token,
    show_event_write_token,
    show_public_event_write_token,
)
from core.show_runtime import (
    ShowRuntimeManager,
    _runtime_download_error,
    _runtime_platform_tag,
    _safe_extract_tar,
    set_show_runtime_manager_for_tests,
)
from tests.test_ui_remote_access_auth import _mock_interface, _remote_peer, _save_config
from tests.ui_server_test_helpers import csrf_headers
from vibe import remote_access, ui_server
from vibe.ui_server import app


class _FakeShowRuntimeManager:
    def __init__(
        self,
        *,
        body: bytes = b"Runtime Show Page",
        fail: bool = False,
        status_code: int = 200,
        extra_headers: dict[str, str] | None = None,
        headers_by_path: dict[str, dict[str, str]] | None = None,
        bodies_by_path: dict[str, bytes] | None = None,
    ):
        self.body = body
        self.fail = fail
        self.status_code = status_code
        self.extra_headers = extra_headers or {}
        self.headers_by_path = headers_by_path or {}
        self.bodies_by_path = bodies_by_path or {}
        self.calls = []
        self.websocket_paths = []
        self.stopped = False

    async def request(self, method, path, *, headers=None, body=None):
        import httpx

        self.calls.append((method, path, headers, body))
        if self.fail:
            raise RuntimeError("runtime unavailable")
        headers = {
            "content-type": "text/html; charset=utf-8",
            "set-cookie": "__Host-vibe_remote_session=attacker",
            "x-runtime-private-header": "secret",
        } | self.extra_headers | self.headers_by_path.get(path, {})
        return httpx.Response(self.status_code, content=self.bodies_by_path.get(path, self.body), headers=headers)

    async def websocket_url(self, path):
        self.websocket_paths.append(path)
        return f"ws://127.0.0.1:1{path}"

    def stop(self):
        self.stopped = True


@pytest.fixture(autouse=True)
def _show_runtime_node_version(monkeypatch):
    monkeypatch.setattr("core.show_runtime._node_version", lambda node: (22, 16, 0))


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
        page = store.update_visibility(session_id, visibility)
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


def _write_runtime_archive(tmp_path: Path, *, text: str = "#!/usr/bin/env node\n") -> Path:
    archive_root = tmp_path / f"archive-root-{hashlib.sha256(text.encode()).hexdigest()[:8]}"
    cli_path = archive_root / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text(text, encoding="utf-8")
    archive_path = tmp_path / f"vibe-show-runtime-node-{hashlib.sha256(text.encode()).hexdigest()[:8]}.tgz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(archive_root / "node_modules", arcname="node_modules")
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
) -> Path:
    manifest_path = tmp_path / "show_runtime_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_version": "runtime-test-ref",
                "minimum_node": "^20.19.0 || >=22.12.0",
                "archives": {
                    _runtime_platform_tag(): {
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


def _write_cached_runtime_install(
    runtime_dir: Path,
    name: str,
    *,
    manifest_source: str = "package:show_runtime_manifest.json",
    mtime: float,
) -> tuple[Path, Path]:
    install_dir = runtime_dir / "versions" / name / _runtime_platform_tag() / f"fingerprint-{name}"
    return _write_cached_runtime_install_at(install_dir, name, manifest_source=manifest_source, mtime=mtime)


def _write_cached_runtime_install_at(
    install_dir: Path,
    name: str,
    *,
    manifest_source: str = "package:show_runtime_manifest.json",
    mtime: float,
) -> tuple[Path, Path]:
    cli_path = install_dir / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text(f"{name}\n", encoding="utf-8")
    (install_dir / ".vibe-show-runtime.json").write_text(
        json.dumps(
            {
                "provider": "manifest-cache",
                "manifest_source": manifest_source,
                "runtime_version": name,
                "platform": _runtime_platform_tag(),
            }
        ),
        encoding="utf-8",
    )
    os.utime(install_dir, (mtime, mtime))
    return install_dir, cli_path


def test_private_show_page_requires_remote_login(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")

    response = app.test_client().get(
        "/show/ses123/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].startswith("https://backend.test/oauth/authorize?")


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
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].startswith("https://backend.test/oauth/authorize?")


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
    assert "accept-encoding" not in manager.calls[0][2]
    assert "authorization" not in manager.calls[0][2]
    assert "cookie" not in manager.calls[0][2]
    assert "x-vibe-csrf-token" not in manager.calls[0][2]


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
    assert '<script type="module" src="/show/ses123/__show/annotation.js"></script>' in body
    assert body.index("globalThis.__AVIBE_SHOW__") < body.index('/src/main.tsx')
    assert body.index('/src/main.tsx') < body.index('/show/ses123/__show/annotation.js')
    assert "cookie" not in manager.calls[0][2]
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers
    assert "expires" not in response.headers
    assert "last-modified" not in response.headers


@pytest.mark.parametrize("authenticated", [False, True])
def test_public_show_page_injects_auth_aware_annotation_config(monkeypatch, tmp_path, authenticated):
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
            remote_access.make_session_cookie(config, "alex@example.com", "user-1"),
            domain="alex.avibe.bot",
        )
    try:
        response = client.get(
            f"/p/{share_id}/",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    body = response.content.decode("utf-8")
    base_path = f"/p/{share_id}/"
    assert f'"sessionId":"{share_id}"' in body
    assert '"sessionId":"ses123"' not in body
    assert f'"basePath":"{base_path}"' in body
    assert f'"eventsPath":"{base_path}__show/events"' in body
    assert f'"streamPath":"{base_path}__show/events?stream=1"' in body
    expected_auth = "true" if authenticated else "false"
    assert f'"annotation":{{"authenticated":{expected_auth},"mePath":"__show/me"}}' in body
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
        response = app.test_client().get(f"/p/{share_id}/", base_url="http://127.0.0.1:5123")
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    body = response.content.decode("utf-8")
    assert "globalThis.__AVIBE_SHOW__=Object.assign" in body
    assert '"writeToken"' not in body
    assert "token-ses123" not in body


def test_private_show_page_falls_back_to_static_when_runtime_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr("core.show_git.show_git_checkpointing_active", lambda: True)
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    set_show_runtime_manager_for_tests(_FakeShowRuntimeManager(fail=True))
    try:
        response = app.test_client().get("/show/ses123/", base_url="http://127.0.0.1:5123")
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert b"Loading Show Page" in response.content
    assert b"Ready to visualize" in response.content
    assert b"Copy prompt" in response.content
    assert b"History is saved automatically around each turn" in response.content
    assert b"Never add remotes, push, or publish" in response.content
    assert b'src="./src/main.tsx"' not in response.content


def test_private_show_page_recovery_reports_history_unavailable_without_git(monkeypatch, tmp_path):
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
    assert b"Automatic Show Page history is unavailable" in response.content
    assert b"History is saved automatically around each turn" not in response.content
    assert b"git restore --source" not in response.content


def test_private_show_page_recovery_uses_self_managed_history_contract(monkeypatch, tmp_path):
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
    assert b"shadow history continues automatically" in response.content
    assert b"not Avibe history" in response.content
    assert b"Only if the user explicitly asks to recover from Avibe history" in response.content
    assert b"History is saved automatically around each turn" not in response.content
    assert b"Restore only via" not in response.content


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


def test_private_show_page_allows_at_fs_workspace_symlink(monkeypatch, tmp_path):
    # The private authoring surface intentionally allows a workspace symlink to a
    # disk file (a supported feature). Only the public surface confines it.
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
        decoded, session_id="ses123", public=True
    )
    # The same request stays allowed on the private authoring surface.
    assert not ui_server._is_show_page_runtime_denied_path(
        decoded, session_id="ses123", public=False
    )


def test_show_page_recovery_loading_holds_before_ready(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_show_page("ses123", "private")
    set_show_runtime_manager_for_tests(_FakeShowRuntimeManager(fail=True))
    try:
        response = app.test_client().get("/show/ses123/", base_url="http://127.0.0.1:5123")
    finally:
        set_show_runtime_manager_for_tests(None)

    body = response.content.decode("utf-8")
    loading_delay = f"{SHOW_RUNTIME_RECOVERY_LOADING_DELAY_SECONDS}s"
    assert f"show-recovery-loading-out 0.18s ease {loading_delay} forwards" in body
    assert f"show-recovery-panel-in 0.22s ease {loading_delay} forwards" in body
    assert "ease 5s forwards" not in body


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
    assert "cookie" not in manager.calls[0][2]
    assert manager.calls[0][3] == b'{"ping":true}'


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
        remote_access.make_session_cookie(config, "alex@example.com", "user-1"),
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


def test_private_show_page_dispatches_human_show_event(monkeypatch, tmp_path):
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

    async def fake_stream_dispatch(payload, **kwargs):
        dispatches.append(payload)
        dispatch_done.set()
        yield "turn.start", {"session_id": payload["session_id"]}
        yield "turn.end", {"session_id": payload["session_id"]}

    with patch("vibe.internal_client.stream_dispatch", fake_stream_dispatch):
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
    assert "show.dispatch" in [event_type for event_type, _data in published]


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

    async def fake_stream_dispatch(payload, **kwargs):
        dispatches.append(payload)
        dispatch_done.set()
        yield "turn.start", {"session_id": payload["session_id"]}
        yield "turn.end", {"session_id": payload["session_id"]}

    with patch("vibe.internal_client.stream_dispatch", fake_stream_dispatch):
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
        remote_access.make_session_cookie(config, "alex@example.com", "user-1"),
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
        remote_access.make_session_cookie(config, "reader@example.com", "user-1"),
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

    response = app.test_client().get(f"/p/{share_id}/", base_url="http://127.0.0.1:5123")

    assert response.status_code == 200
    cookies = "\n".join(response.headers.getlist("set-cookie"))
    assert "vibe_show_event_token=" in cookies
    assert "Max-Age=0" in cookies
    assert response.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert "sandbox.avibe.bot" not in response.headers.get("content-security-policy", "")
    assert "permissions-policy" not in response.headers


def test_show_events_stream_replays_all_persisted_pages_before_live(monkeypatch, tmp_path):
    from core.show_session_events import ShowSessionEventStore
    from vibe.ui_server import _show_events_stream

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")
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
        response = await _show_events_stream("ses123")
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


def test_show_events_stream_forwards_live_dispatch_events(monkeypatch, tmp_path):
    from vibe.sse_broker import broker
    from vibe.ui_server import _show_events_stream

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses123")
    _create_show_page("ses123", "private")

    async def _collect_live_dispatch() -> str:
        response = await _show_events_stream("ses123")
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
                "type": "assistant.mark.created",
                "mark": {
                    "target": "summary",
                    "body": "body",
                },
            },
        )
    finally:
        store.close()

    response = app.test_client().get(f"/p/{share_id}/__show/events", base_url="http://127.0.0.1:5123")

    assert response.status_code == 200
    public_event = response.get_json()["events"][0]
    assert public_event["id"] == event["id"]
    assert public_event["type"] == "assistant.mark.created"
    assert public_event["payload"]["body"] == "body"
    assert "session_id" not in public_event
    assert "scope_id" not in public_event
    assert "message_id" not in public_event
    assert "message" not in public_event


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
                "type": "assistant.mark.created",
                "mark": {
                    "target": "summary",
                    "body": "body",
                },
            },
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

    async def fake_prewarm(session_id, *, base_path=None):
        calls.append((session_id, base_path))
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

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert calls == [("ses123", "/p/share123/")]


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
            "annotation": {"comment": "Anonymous review."},
        },
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "public_show_events_login_required"


def test_public_show_page_events_require_share_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_access.make_session_cookie(config, "alex@example.com", "user-1"),
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
        remote_access.make_session_cookie(config, "alex@example.com", "user-1"),
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
        remote_access.make_session_cookie(config, "alex@example.com", "user-1"),
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
        remote_access.make_session_cookie(config, "alex@example.com", "user-1"),
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
        remote_access.make_session_cookie(config, "member@example.com", "user-2"),
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
        remote_access.make_session_cookie(config, "member@example.com", "user-2"),
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
    assert internal_screenshot["path"] in internal_event["transcript_text"]

    public_event = response.get_json()["event"]
    public_screenshot = public_event["payload"]["screenshot"]
    assert "path" not in public_screenshot
    assert public_screenshot["attachmentId"] == internal_screenshot["attachmentId"]
    assert internal_screenshot["path"] not in public_event["transcript_text"]
    assert f"Screenshot: {internal_screenshot['attachmentId']} (4x3)" in public_event["transcript_text"]
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
        remote_access.make_session_cookie(config, "reader@example.com", "user-2"),
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
        remote_access.make_session_cookie(config, "reader@example.com", "user-2"),
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
        remote_access.make_session_cookie(config, "member@example.com", "user-2"),
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
        remote_access.make_session_cookie(config, "member@example.com", "user-2"),
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


def test_public_show_page_events_ignore_client_event_id_and_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    published = []
    dispatch_calls = []
    monkeypatch.setattr("vibe.ui_server._publish_show_session_event", published.append)
    monkeypatch.setattr("vibe.ui_server._dispatch_show_event_if_requested", dispatch_calls.append)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_access.make_session_cookie(config, "member@example.com", "user-2"),
        domain="alex.avibe.bot",
    )

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
    assert "dispatch" not in event["payload"]
    assert "dispatch" not in published[0]["payload"]
    assert dispatch_calls == []


@pytest.mark.parametrize("event_type", ["assistant.mark.created", "system.annotation.control"])
def test_public_show_page_events_reject_non_human_types(monkeypatch, tmp_path, event_type):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_agent_session("ses123")
    share_id = _create_show_page("ses123", "public")
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_access.make_session_cookie(config, "member@example.com", "user-2"),
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


def test_show_runtime_manager_reports_missing_command(tmp_path):
    manager = ShowRuntimeManager(
        command="definitely-missing-avibe-show-runtime",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )

    result = asyncio.run(manager.ensure())

    assert result.available is False
    assert result.reason == "runtime_command_missing"


def test_show_runtime_manager_passes_runtime_options(monkeypatch, tmp_path):
    from core.show_pages import SHOW_RUNTIME_RECOVERY_LOADING_DELAY_SECONDS

    captured = {}

    class FakeProcess:
        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return FakeProcess()

    async def fake_startup_url():
        return "http://127.0.0.1:12345"

    manager = ShowRuntimeManager(
        command="/bin/echo",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: [command])
    monkeypatch.setattr("core.show_runtime.subprocess.Popen", fake_popen)
    monkeypatch.setattr(manager, "_read_startup_url", fake_startup_url)

    result = asyncio.run(manager.ensure())

    assert result.available is True
    cache_index = captured["command"].index("--cache-root")
    assert captured["command"][cache_index + 1] == str(tmp_path / "runtime" / "vite-cache")
    index = captured["command"].index("--fallback-delay-seconds")
    assert captured["command"][index + 1] == str(SHOW_RUNTIME_RECOVERY_LOADING_DELAY_SECONDS)


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

    async def fake_request(self, method, path, *, headers=None, body=None):
        import httpx

        calls.append((method, path, headers, body))
        status, content, headers_out = responses[path]
        return httpx.Response(status, content=content, headers=headers_out)

    manager = ShowRuntimeManager(
        command="/bin/echo",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr(ShowRuntimeManager, "request", fake_request)

    result = asyncio.run(manager.prewarm_session("ses123", base_path="/show/ses123/"))

    assert result.available is True
    assert calls == [
        ("GET", "/sessions/ses123/app/", {"x-vibe-show-base": "/show/ses123/"}, None),
        ("GET", "/sessions/ses123/app/src/main.tsx", {"x-vibe-show-base": "/show/ses123/"}, None),
        ("GET", "/sessions/ses123/app/src/App.tsx", {"x-vibe-show-base": "/show/ses123/"}, None),
        (
            "GET",
            "/sessions/ses123/app/@fs/runtime/packages/ui/dist/button.js",
            {"x-vibe-show-base": "/show/ses123/"},
            None,
        ),
        (
            "GET",
            "/sessions/ses123/app/@fs/runtime/vite-cache/deps/react_jsx-runtime.js?v=abc",
            {"x-vibe-show-base": "/show/ses123/"},
            None,
        ),
    ]


def test_show_runtime_manager_prewarm_reports_nested_module_failures(monkeypatch, tmp_path):
    responses = {
        "/sessions/ses123/app/": (
            200,
            b'<script type="module" src="/p/share123/src/main.tsx"></script>',
            {"content-type": "text/html"},
        ),
        "/sessions/ses123/app/src/main.tsx": (
            200,
            b'import App from "/p/share123/src/App.tsx";',
            {"content-type": "text/javascript"},
        ),
        "/sessions/ses123/app/src/App.tsx": (
            504,
            b"timeout",
            {"content-type": "text/plain"},
        ),
    }

    async def fake_request(self, method, path, *, headers=None, body=None):
        import httpx

        status, content, headers_out = responses[path]
        return httpx.Response(status, content=content, headers=headers_out)

    manager = ShowRuntimeManager(
        command="/bin/echo",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr(ShowRuntimeManager, "request", fake_request)

    result = asyncio.run(manager.prewarm_session("ses123", base_path="/p/share123/"))

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
    monkeypatch.setattr("core.show_runtime.get_platform", lambda: "macosx-14.0-universal2")
    monkeypatch.setattr("core.show_runtime.platform.machine", lambda: "arm64")

    assert _runtime_platform_tag() == "darwin-arm64"


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

    assert manager._install_managed_runtime() == [
        "/bin/node",
        str(tmp_path / "runtime" / "prebuilt" / "current" / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"),
    ]
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

    command = manager._install_managed_runtime()

    assert command == [
        "/bin/node",
        str(tmp_path / "runtime" / "prebuilt" / "current" / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"),
    ]
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
        with pytest.raises(ValueError, match="Unsafe archive link target"):
            _safe_extract_tar(tar, tmp_path / "destination")


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
        with pytest.raises(ValueError, match="Unsafe archive link target"):
            _safe_extract_tar(tar, tmp_path / "destination")


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

    assert manager._install_managed_runtime() == ["/bin/node", str(cli_path)]
    assert manager._install_reason is None


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

    assert asyncio.run(manager._resolve_managed_command()) == ["/bin/node", str(installed_cli)]
    assert installed_cli.read_text(encoding="utf-8") == "new runtime\n"


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
    manifest = manager._load_runtime_manifest()
    assert manifest is not None
    archive = manager._manifest_archive_for_platform(manifest)
    assert archive is not None
    installed_cli = Path(manager._manifest_runtime_command(manager._manifest_install_dir(manifest, archive), ["/bin/node"])[1])

    assert result["ok"] is True
    assert result["command"] == ["/bin/node", str(installed_cli)]
    assert manager._install_reason is None
    assert (runtime_dir / "downloads" / f"{_sha256(archive_path)}.tgz").exists()
    metadata = json.loads((installed_cli.parents[4] / ".vibe-show-runtime.json").read_text(encoding="utf-8"))
    assert metadata["provider"] == "manifest-cache"
    assert metadata["archive_sha256"] == _sha256(archive_path)
    status = manager.status()
    assert status["installed"] is True
    assert status["installed_matches_manifest"] is True


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


def test_show_runtime_manager_manifest_install_dir_includes_manifest_and_archive_identity(monkeypatch, tmp_path):
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
    assert new_manager.status()["installed_matches_manifest"] is True


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
    custom_install, _custom_cli = _write_cached_runtime_install(
        runtime_dir,
        "custom",
        manifest_source=str(tmp_path / "development-manifest.json"),
        mtime=50,
    )
    github_source = runtime_dir / "source" / "github" / "avibe-bot_vibe-show-runtime" / "main"
    github_source.mkdir(parents=True)
    (github_source / "README.md").write_text("development checkout\n", encoding="utf-8")
    local_bin = runtime_dir / "package" / "node_modules" / ".bin" / "avibe-show-runtime"
    local_bin.parent.mkdir(parents=True)
    local_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="manifest-cache",
    )
    monkeypatch.setattr(manager, "_install_manifest_runtime", lambda: ["/bin/node", str(current_cli)])
    monkeypatch.setattr(manager, "status", lambda: {})

    result = manager.prepare()

    assert result["ok"] is True
    assert current_install.exists() is True
    assert previous_install.exists() is True
    assert old_install.exists() is False
    assert custom_install.exists() is True
    assert github_source.exists() is True
    assert local_bin.exists() is True


@pytest.mark.parametrize(("parent_mtime", "child_mtime"), ((100, 200), (200, 100)))
def test_show_runtime_prepare_preserves_nested_retained_rollback(monkeypatch, tmp_path, parent_mtime, child_mtime):
    runtime_dir = tmp_path / "runtime"
    old_install, _old_cli = _write_cached_runtime_install(runtime_dir, "old", mtime=10)
    current_install, current_cli = _write_cached_runtime_install(runtime_dir, "current", mtime=300)
    rollback_parent = runtime_dir / "versions" / "rollback" / _runtime_platform_tag()
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
    monkeypatch.setattr(manager, "_install_manifest_runtime", lambda: ["/bin/node", str(current_cli)])
    monkeypatch.setattr(manager, "status", lambda: {})

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
    current_parent = runtime_dir / "versions" / "current" / _runtime_platform_tag()
    _parent_install, parent_cli = _write_cached_runtime_install_at(current_parent, "current-legacy", mtime=400)
    current_install, current_cli = _write_cached_runtime_install_at(
        current_parent / "current-fingerprint",
        "current",
        mtime=300,
    )
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
    monkeypatch.setattr(manager, "_install_manifest_runtime", lambda: ["/bin/node", str(current_cli)])
    monkeypatch.setattr(manager, "status", lambda: {})

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
    current_parent = runtime_dir / "versions" / "current" / _runtime_platform_tag()
    _parent_install, parent_cli = _write_cached_runtime_install_at(current_parent, "current-legacy", mtime=400)
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
    monkeypatch.setattr(manager, "_install_manifest_runtime", lambda: ["/bin/node", str(parent_cli)])
    monkeypatch.setattr(manager, "status", lambda: {})

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
    stale_parent = runtime_dir / "versions" / "stale-parent" / _runtime_platform_tag()
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
    monkeypatch.setattr(manager, "_install_manifest_runtime", lambda: ["/bin/node", str(current_cli)])
    monkeypatch.setattr(manager, "status", lambda: {})

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
    monkeypatch.setattr(manager, "_install_manifest_runtime", lambda: None)
    monkeypatch.setattr(manager, "status", lambda: {})

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
    manifest = manager._load_runtime_manifest()
    assert manifest is not None
    archive = manager._manifest_archive_for_platform(manifest)
    assert archive is not None
    legacy_install_dir = manager._legacy_manifest_install_dir(manifest, archive)
    legacy_cli = legacy_install_dir / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"
    legacy_cli.parent.mkdir(parents=True)
    legacy_cli.write_text("legacy runtime\n", encoding="utf-8")
    manager._write_manifest_install_metadata(legacy_install_dir, manifest, archive)

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
    manifest = manager._load_runtime_manifest()
    assert manifest is not None
    archive = manager._manifest_archive_for_platform(manifest)
    assert archive is not None
    manager._write_manifest_install_metadata(legacy_parent, manifest, archive)

    clean_result = manager.clean(keep_previous=0)

    assert str(legacy_parent) not in clean_result["removed"]
    assert current_install_dir.exists() is True
    assert Path(result["command"][1]).exists() is True


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
    manifest["archives"][_runtime_platform_tag()]["url"] = "https://example.invalid/runtime.tgz"
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

    assert asyncio.run(manager._resolve_managed_command()) is None
    assert manager._install_reason == "runtime_command_missing"


def test_show_runtime_manager_installs_without_blocking_event_loop(monkeypatch, tmp_path):
    monkeypatch.setattr("core.show_runtime._packaged_runtime_manifest_exists", lambda: True)
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

    monkeypatch.setattr(manager, "_install_managed_runtime", fake_install)
    calls = []

    async def fake_to_thread(func):
        calls.append(func)
        return func()

    monkeypatch.setattr("core.show_runtime.asyncio.to_thread", fake_to_thread)

    assert asyncio.run(manager._resolve_managed_command()) == [str(manager._managed_bin_path())]
    assert calls == [fake_install]


def test_show_runtime_manager_defaults_to_archive_when_package_manifest_is_absent(monkeypatch, tmp_path):
    monkeypatch.setattr("core.show_runtime._packaged_runtime_manifest_exists", lambda: False)

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )

    assert manager.runtime_source == "archive"


def test_show_runtime_manager_defaults_to_manifest_when_package_manifest_exists(monkeypatch, tmp_path):
    monkeypatch.setattr("core.show_runtime._packaged_runtime_manifest_exists", lambda: True)

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )

    assert manager.runtime_source == "manifest-cache"


def test_show_runtime_manager_installs_from_github_source(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    source_dir = runtime_dir / "source" / "github" / "avibe-bot_vibe-show-runtime" / "main"
    commands = []

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="github",
        github_repo="https://github.com/avibe-bot/vibe-show-runtime.git",
        github_ref="main",
    )

    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: [f"/bin/{command}"] if command in {"git", "npm", "node"} else None,
    )

    def fake_run(command, *, cwd=None):
        commands.append((command, cwd))
        if command[:2] == ["/bin/npm", "run"]:
            cli_path = source_dir / "packages" / "runtime" / "dist" / "cli.js"
            cli_path.parent.mkdir(parents=True, exist_ok=True)
            cli_path.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        return True

    monkeypatch.setattr(manager, "_run_install_command", fake_run)

    assert manager._install_managed_runtime() == ["/bin/node", str(source_dir / "packages" / "runtime" / "dist" / "cli.js")]
    assert commands == [
        (
            [
                "/bin/git",
                "clone",
                "--depth",
                "1",
                "--branch",
                "main",
                "https://github.com/avibe-bot/vibe-show-runtime.git",
                str(source_dir),
            ],
            None,
        ),
        (["/bin/npm", "ci"], source_dir),
        (["/bin/npm", "run", "build"], source_dir),
    ]


def test_show_runtime_manager_reuses_installed_github_runtime_when_update_fails(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    source_dir = runtime_dir / "source" / "github" / "avibe-bot_vibe-show-runtime" / "main"
    cli_path = source_dir / "packages" / "runtime" / "dist" / "cli.js"
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    commands = []

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="github",
        github_repo="https://github.com/avibe-bot/vibe-show-runtime.git",
        github_ref="main",
    )

    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: [f"/bin/{command}"] if command in {"git", "npm", "node"} else None,
    )

    def fake_run(command, *, cwd=None):
        commands.append((command, cwd))
        return False

    monkeypatch.setattr(manager, "_run_install_command", fake_run)

    assert manager._install_managed_runtime() == ["/bin/node", str(cli_path)]
    assert manager._install_reason is None
    assert commands == [
        (
            ["/bin/git", "-C", str(source_dir), "fetch", "--depth", "1", "origin", "main"],
            None,
        )
    ]


def test_show_runtime_manager_reuses_installed_github_runtime_without_git(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    source_dir = runtime_dir / "source" / "github" / "avibe-bot_vibe-show-runtime" / "main"
    cli_path = source_dir / "packages" / "runtime" / "dist" / "cli.js"
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text("#!/usr/bin/env node\n", encoding="utf-8")

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="github",
        github_repo="https://github.com/avibe-bot/vibe-show-runtime.git",
        github_ref="main",
    )

    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )

    assert manager._install_managed_runtime() == ["/bin/node", str(cli_path)]
    assert manager._install_reason is None


def test_show_runtime_manager_reuses_github_runtime_after_install_attempt(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    source_dir = runtime_dir / "source" / "github" / "avibe-bot_vibe-show-runtime" / "main"
    cli_path = source_dir / "packages" / "runtime" / "dist" / "cli.js"
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text("#!/usr/bin/env node\n", encoding="utf-8")

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="github",
        github_repo="https://github.com/avibe-bot/vibe-show-runtime.git",
        github_ref="main",
    )
    manager._install_attempted = True

    monkeypatch.setattr(
        "core.show_runtime._resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )

    assert asyncio.run(manager._resolve_managed_command()) == ["/bin/node", str(cli_path)]
    assert manager._managed_command == ["/bin/node", str(cli_path)]


def test_show_runtime_manager_reuses_cached_managed_command_after_install_attempt(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="github",
    )
    manager._install_attempted = True
    manager._managed_command = ["/bin/node", "/tmp/runtime/cli.js"]
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: None)

    assert asyncio.run(manager._resolve_managed_command()) == ["/bin/node", "/tmp/runtime/cli.js"]


def test_show_runtime_manager_can_use_npm_source(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
    )
    called = []
    monkeypatch.setattr(manager, "_install_npm_runtime", lambda: called.append("npm") or ["/tmp/avibe-show-runtime"])

    assert manager._install_managed_runtime() == ["/tmp/avibe-show-runtime"]
    assert called == ["npm"]


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
        return {"ok": True, "show_runtime": {"ok": True}, "askill": {"ok": True}}

    async def fake_runtime_prewarm():
        called["runtime"] += 1
        return SimpleNamespace(available=True, reason=None)

    async def fake_session_prewarm(session_id, *, base_path=None):
        called["sessions"].append((session_id, base_path))
        return SimpleNamespace(available=True, reason=None)

    monkeypatch.setattr("vibe.api.reconcile_startup_dependencies", fake_reconcile)
    monkeypatch.setattr(
        "vibe.api.startup_show_page_prewarm_targets",
        lambda: {
            "ok": True,
            "limit": 2,
            "pages": [
                {"session_id": "ses_private", "base_path": None},
                {"session_id": "ses_public", "base_path": "/p/share123/"},
            ],
        },
    )
    monkeypatch.setattr("core.show_runtime.prewarm_show_runtime", fake_runtime_prewarm)
    monkeypatch.setattr("core.show_runtime.prewarm_show_page_session", fake_session_prewarm)

    asyncio.run(_reconcile_startup_dependencies_task())

    assert called == {
        "reconcile": 1,
        "runtime": 1,
        "sessions": [("ses_private", None), ("ses_public", "/p/share123/")],
    }


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

    try:
        with app.test_client().websocket_connect(
            "wss://alex.avibe.bot/show/ses123/__vite_hmr",
            headers={"host": "alex.avibe.bot"},
            subprotocols=["vite-hmr"],
        ):
            raise AssertionError("websocket should not connect")
    except Exception as exc:
        assert getattr(exc, "code", None) == 1008


def test_private_show_page_hmr_websocket_accepts_remote_session(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _create_show_page("ses123", "private")
    manager = _FakeShowRuntimeManager()
    set_show_runtime_manager_for_tests(manager)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_access.make_session_cookie(config, "alex@example.com", "user-1"),
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


def test_public_show_page_hmr_websocket_accepts_local_peer(monkeypatch, tmp_path):
    # Amendment §2.3: the HMR socket serves public pages too, so a public page's
    # /show/ HMR socket gets PAST the visibility gate (then fails at the fake
    # runtime proxy with 1011 — not the 1008 visibility rejection an offline page
    # would get), keeping live HMR for a page pinned while public.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.ui.setup_host = "192.168.2.3"
    config.save()
    _mock_interface(monkeypatch, "192.168.2.3", 24)
    _create_show_page("ses123", "public")
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
            follow_redirects=False,
        )

        assert response.status_code == 200
        assert b"Loading Show Page" in response.content
        assert b"Ready to visualize" in response.content
        assert b"Copy prompt" in response.content
        assert b'src="./src/main.tsx"' not in response.content

        mismatch = app.test_client().get(
            f"/p/{share_id}/",
            base_url="https://evil.example",
            environ_base=_remote_peer(),
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
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert b"Public Runtime Page" in response.content
    assert manager.calls[0][0] == "GET"
    assert manager.calls[0][1] == "/sessions/ses123/app/"
    assert manager.calls[0][2]["x-vibe-show-base"] == f"/p/{share_id}/"


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
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert b"Public Runtime Page" in response.content
    assert (page_dir / "src" / "App.tsx").exists()
    assert manager.calls[0][1] == "/sessions/ses123/app/"
    assert manager.calls[0][2]["x-vibe-show-base"] == f"/p/{share_id}/"


def test_public_show_page_rewrites_runtime_redirect_location(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    share_id = _create_show_page("ses123", "public")
    manager = _FakeShowRuntimeManager(
        body=b"",
        status_code=302,
        extra_headers={"location": "/sessions/ses123/app/foo/"},
    )
    set_show_runtime_manager_for_tests(manager)
    try:
        response = app.test_client().get(
            f"/p/{share_id}/foo",
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
            follow_redirects=False,
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 302
    assert response.headers["location"] == f"/p/{share_id}/foo/"


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
    assert manager.calls[0][2]["x-vibe-show-base"] == f"/p/{share_id}/"
    assert "cookie" not in manager.calls[0][2]
    assert manager.calls[0][3] == b'{"ping":true}'


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

    followed = app.test_client().get(f"/p/{share_id}", base_url="http://127.0.0.1:5123", follow_redirects=True)
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


def test_rotated_public_share_url_stops_working(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    old_share_id = _create_show_page("ses123", "public")

    store = ShowPageStore()
    try:
        page, _ = store.rotate_share("ses123")
    finally:
        store.close()

    old_response = app.test_client().get(f"/p/{old_share_id}/", base_url="http://127.0.0.1:5123")
    new_response = app.test_client().get(f"/p/{page.share_id}/", base_url="http://127.0.0.1:5123")

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

    response = app.test_client().get(f"/p/{share_id}/", base_url="http://127.0.0.1:5123")

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
        )
    finally:
        set_show_runtime_manager_for_tests(None)

    assert response.status_code == 200
    assert response.content == b"export const React = {};"
    # The vendor prefix is forwarded verbatim, never under a per-session base path.
    assert manager.calls[-1][0] == "GET"
    assert manager.calls[-1][1] == "/_show-runtime/vendor/abc123/react.js"
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
        response = app.test_client().get(f"/p/{share_id}/", base_url="http://127.0.0.1:5123")
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
