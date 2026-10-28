"""The use-avibe Skill's Web API calls reach the routes the Web UI serves.

Its pages named bare paths (`POST /config`, `GET /settings`) long after the API
moved under `/api/`; an Agent following them got the SPA's HTML back.
"""

from __future__ import annotations

import importlib.util
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from vibe import ui_server

SKILL = Path(__file__).resolve().parents[1] / "skills" / "use-avibe"
# `GET /api/x`, `vibe_api.py POST /api/x '{}'`, `GET '/api/x?platform=...'`
CALL = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE) '?(/[^\s'`?\"]*)")


def _documented_calls() -> list[tuple[str, str, str]]:
    calls = set()
    for page in sorted(SKILL.rglob("*.md")):
        for method, path in CALL.findall(page.read_text(encoding="utf-8")):
            calls.add((page.relative_to(SKILL).as_posix(), method, path))
    return sorted(calls)


def _served_routes() -> list[tuple[frozenset[str], re.Pattern[str]]]:
    served = []
    for route in ui_server.app.routes:
        path = getattr(route, "path", None)
        # The SPA fallback (`/{path:path}`) answers any path with the app's HTML:
        # exactly what a stale documented path gets back, so it serves no call.
        if not path or ":path}" in path:
            continue
        pattern = re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(path))
        served.append((frozenset(getattr(route, "methods", None) or ()), re.compile(f"{pattern}$")))
    return served


def _is_served(method: str, path: str) -> bool:
    concrete = re.sub(r"<[^>]+>", "x", path)
    return any(method in methods and pattern.match(concrete) for methods, pattern in _served_routes())


def test_the_skill_documents_web_api_calls() -> None:
    assert len(_documented_calls()) > 40


def test_a_path_only_the_spa_answers_is_not_served() -> None:
    assert _is_served("GET", "/api/config") and _is_served("GET", "/status")
    for method, path in (("GET", "/config"), ("GET", "/settings"), ("GET", "/version"), ("POST", "/config")):
        assert not _is_served(method, path), f"{method} {path}"


@pytest.mark.parametrize(("page", "method", "path"), _documented_calls())
def test_every_documented_call_names_a_served_route(page: str, method: str, path: str) -> None:
    assert _is_served(method, path), f"{page}: {method} {path} is not a Web UI route"


def _load_helper():
    spec = importlib.util.spec_from_file_location("vibe_api_helper", SKILL / "scripts" / "vibe_api.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_the_helper_sends_same_origin_evidence_on_reads(tmp_path: Path) -> None:
    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            seen.append(dict(self.headers))
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        client = _load_helper().VibeApiClient(base, cookie_jar_path=tmp_path / "cookies")
        assert client.call("GET", "/api/cli/detect?binary=codex") == {"ok": True}
    finally:
        server.shutdown()
        server.server_close()

    # A protected read (`/api/cli/detect`) is refused without it.
    assert seen[0].get("Origin") == base
