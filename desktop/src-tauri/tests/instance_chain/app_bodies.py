"""Finite Runtime data for the exact built W1 React bodies.

Native launch decisions and UI modules are never replaced. Files, sessions and
Show content below are synthetic server data; no filesystem or shell API runs.
"""

import mimetypes
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def response(method, destination):
    """Return a private response, or leave the existing account fixture owner."""
    parsed = urlsplit(destination)
    path = parsed.path
    query = parse_qs(parsed.query)
    root = Path(os.environ["DESKTOP_APP_UI_DIST"]).resolve()
    if path.startswith("/assets/"):
        asset = (root / path.lstrip("/")).resolve()
        if asset.is_relative_to(root) and asset.is_file():
            return 200, mimetypes.guess_type(asset.name)[0] or "application/octet-stream", asset.read_bytes()
        return 404, "application/json", {"error": "fixture_asset_missing"}
    if path == "/" or path.startswith("/apps/"):
        return 200, "text/html", (root / "index.html").read_bytes()
    if path.startswith("/show/"):
        return 200, "text/html", '<label>Show draft<textarea id="show-draft"></textarea></label>'
    if path == "/status":
        return 200, "application/json", {"state": "running"}
    if path in ("/api/config/auth", "/api/session", "/api/csrf-token") or path.startswith(("/api/desktop/", "/api/mobile/")):
        return None
    if not path.startswith("/api/"):
        return None
    if method != "GET":
        if method == "DELETE" and path.startswith("/api/terminal/"):
            return 204, "application/json", b""
        return 405, "application/json", {"error": "fixture_write_denied"}
    value = {}
    content_type = "application/json"
    if path == "/api/config":
        value = {
            "version": "v2", "setup_completed": False, "setup_state": {"needs_setup": False},
            "mode": "local", "language": "en", "platforms": {"primary": "slack", "enabled": []},
            "runtime": {}, "agents": {}, "agent": {"default_cwd": "/fixture/work"},
        }
    elif path.startswith(("/api/projects", "/api/workbench/projects-bootstrap")):
        value = {"projects": [], "sessions": {}}
    elif path.startswith("/api/sessions/"):
        value = {"id": path.rsplit("/", 1)[-1], "status": "active", "title": "Fixture Show"}
    elif path.startswith("/api/sessions"):
        value = {"sessions": []}
    elif path.startswith("/api/agents"):
        value = {"ok": True, "agents": [], "default_agent_name": None}
    elif path == "/api/bots":
        value = {"ok": True, "bots": [{
            "handle": "main", "display_name": "", "avatar": {"kind": "mascot"}, "role": "Fixture",
            "agent_name": "main", "session_id": "fixture-main", "builtin": True,
        }]}
    elif path.startswith("/api/inbox"):
        value = {"sessions": [], "unread": {}}
    elif path == "/api/notifications/preferences":
        value = {"ok": True, "preferences": {"enabled": False, "bots": False, "sessions": False}}
    elif path == "/api/home/preferences":
        value = {"ok": True, "preferences": {"setup_not_needed": [], "tips_enabled": False}}
    elif path == "/api/remote-access/status":
        value = {"ok": True, "enabled": False, "paired": False, "running": False}
    elif path == "/api/web-push/status":
        value = {"ok": True, "configured": False, "public_key": "", "subscription_count": 0}
    elif path.startswith("/api/vault/requests"):
        value = {"ok": True, "requests": []}
    elif path == "/api/version":
        value = {"current": "2.4.0", "latest": "2.4.0", "has_update": False, "build": {"kind": "package"}}
    elif path.startswith("/api/events"):
        content_type, value = "text/event-stream", "retry: 60000\n: fixture idle\n\n"
    elif path == "/api/dock":
        value = {"dock": {"order": ["files", "terminal", "editor", "library"], "pins": []}}
    elif path == "/api/show-pages":
        value = {"pages": [{"session_id": "fixture-session", "title": "Fixture Show", "visibility": "private", "offline": False}]}
    elif path == "/api/files/list":
        directory = query.get("path", ["/fixture/work"])[0]
        value = {"ok": True, "path": directory, "parent": "/fixture", "entries": [
            {"name": "fixture.txt", "kind": "file", "size": 22, "mtime": 1, "ext": "txt"},
            {"name": "child-folder", "kind": "dir", "size": None, "mtime": None, "ext": ""},
        ]}
    elif path == "/api/files/content":
        if query.get("path", [""])[0].endswith(".svg"):
            content_type = "image/svg+xml"
            value = '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120"><rect width="120" height="120" fill="teal"/></svg>'
        else:
            content_type, value = "text/plain", "fixture file contents\n"
    elif path.startswith("/api/terminal/"):
        # Preserve real socket construction/cwd, without starting a PTY. The
        # native consumer test records the request and its retained tab DOM.
        return 503, "application/json", {"error": "fixture_has_no_shell"}
    return 200, content_type, value
