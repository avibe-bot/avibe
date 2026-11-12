"""Actual Runtime HTTP fixture; no controller, tunnel, or live state."""

import http.server
import fcntl
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit
import requests
from config.v2_config import V2Config
from vibe import remote_access, ui_server

_START_CONNECTOR = remote_access.start


def publish_mapping(mapping_file, host, local):
    with mapping_file.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        mappings = json.loads(mapping_file.read_text())
        if mappings.get(host) != local:
            mappings[host] = local
            temporary = mapping_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(mappings))
            temporary.replace(mapping_file)


def start(monkeypatch, info, mapping_file):
    assets = Path(os.environ["DESKTOP_RUNTIME_UI"]).resolve()
    assert (assets / "index.html").is_file()
    monkeypatch.setattr(ui_server, "get_ui_dist_path", lambda: assets)
    # Execute the real first-pair start owner through durable request recording.
    # The external connector binary is deliberately unavailable; no installer,
    # tunnel process or live heartbeat runs.
    monkeypatch.setattr(remote_access, "start", _START_CONNECTOR)
    monkeypatch.setattr(
        remote_access, "_ensure_cloudflared_pin", lambda _: {"ok": False, "error": "cloudflared_install_failed"}
    )
    monkeypatch.setattr(remote_access, "_resolve_binary", lambda _: None)
    monkeypatch.setattr(remote_access, "status", lambda config=None, **_: {"ok": True, "running": False})
    outbound = requests.sessions.Session.request

    def request(session, method, url, **kwargs):
        parsed = urlsplit(str(url))
        if parsed.hostname == "avibe.bot":
            kwargs["headers"] = {**kwargs.get("headers", {}), "Host": "avibe.bot"}
            url = info["base"] + parsed.path + ("?" + parsed.query if parsed.query else "")
        assert urlsplit(str(url)).hostname == "127.0.0.1", f"fixture denied egress: {urlsplit(str(url)).hostname}"
        session.trust_env = False
        return outbound(session, method, url, **kwargs)

    monkeypatch.setattr(requests.sessions.Session, "request", request)

    def redeem(url, payload, **_):
        assert url == "https://avibe.bot/api/v1/pairing/redeem"
        result = requests.post(url, json=payload, timeout=20)
        if result.status_code >= 400:
            raise remote_access.BackendRequestError(result.status_code, result.json())
        return result.json()

    monkeypatch.setattr(remote_access, "_json_request", redeem)
    jwks = remote_access.PyJWKClient

    def mapped_jwks(url):
        assert url == "https://avibe.bot/oauth/jwks.json"
        return jwks(info["base"] + "/oauth/jwks.json")

    monkeypatch.setattr(remote_access, "PyJWKClient", mapped_jwks)

    class Runtime(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def route(self):
            host = self.headers["Host"]
            scheme = self.headers.get("X-Fixture-Scheme", "http")
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in ("host", "x-fixture-scheme", "content-length")
            }
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else None
            client = ui_server.app.test_client()
            try:
                response = client.request(
                    self.command,
                    self.path,
                    content=body,
                    headers=headers,
                    base_url=f"{scheme}://{host}",
                    environ_base={"REMOTE_ADDR": "127.0.0.1"},
                )
            finally:
                client._client.close()
            self.send_response(response.status_code)
            for key, value in response.headers.multi_items():
                if key.lower() not in ("content-length", "transfer-encoding", "connection", "content-encoding"):
                    self.send_header(key, value)
            content = response.content
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            cloud = V2Config.load().remote_access.vibe_cloud
            if cloud.public_url:
                publish_mapping(mapping_file, urlsplit(cloud.public_url).hostname, local)

        do_GET = route
        do_POST = route
        do_DELETE = route

    runtime = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Runtime)
    local = f"http://127.0.0.1:{runtime.server_port}"
    publish_mapping(mapping_file, urlsplit(local).netloc, local)
    config = V2Config.load()
    config.ui.setup_host = "127.0.0.1"
    config.ui.setup_port = runtime.server_port
    config.setup_completed = True
    config.save()
    monkeypatch.setenv("VIBE_UI_PORT", str(runtime.server_port))
    threading.Thread(target=runtime.serve_forever, daemon=True).start()
    return runtime, local
