"""Removal keeps old configuration and opaque user data safe."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from config.v2_config import V2Config
from tests.test_api_save_config_merge import _full_config_payload


@pytest.mark.parametrize("obsolete", [None, True, "malformed", {"enabled": True}, {"enabled": {"bad": []}}])
@pytest.mark.parametrize("linked", [False, True])
def test_legacy_data_identity_survives_config_and_service_probes(tmp_path, monkeypatch, obsolete, linked):
    home = Path.home()
    assert home.is_relative_to(tmp_path)
    root = home / ".avibe"
    legacy = home / ".vibe_remote"
    root.mkdir(parents=True)
    if linked:
        legacy.symlink_to(root, target_is_directory=True)
    else:
        legacy.mkdir()
    monkeypatch.setenv("AVIBE_HOME", str(root))
    sentinels = []
    for directory in (root, legacy):
        data = directory / "memory"
        data.mkdir(exist_ok=True)
        opaque = data / "opaque.sqlite"
        opaque.write_bytes(b"\x00old user data\xff")
        link = data / "record-link"
        if not link.is_symlink():
            link.symlink_to("opaque.sqlite")
        sentinels.extend([data, opaque, link])
    sentinels.append(legacy)

    def snapshot():
        return [(str(p), p.lstat().st_ino, p.lstat().st_mode,
                 os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else None)
                for p in sentinels]

    before = snapshot()
    payload = _full_config_payload()
    payload["memory"] = obsolete
    path = root / "config/config.json"
    path.parent.mkdir()
    path.write_text(json.dumps(payload))
    cfg = V2Config.load()
    assert not cfg.load_warnings
    assert not hasattr(cfg, "memory")
    cfg.save()
    assert "memory" not in json.loads(path.read_text())
    assert V2Config.load().ack_mode == payload["ack_mode"]
    result = subprocess.run([sys.executable, "-m", "vibe", "status"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert snapshot() == before


def test_removed_cli_command_is_unknown():
    result = subprocess.run([sys.executable, "-m", "vibe", "memory", "status"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert "invalid choice: 'memory'" in result.stderr


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "status"), ("GET", "settings"), ("PATCH", "settings"),
    ("GET", "profile"), ("GET", "processing-record"), ("GET", "failures"),
    ("GET", "maintenance"), ("GET", "processing-record/entries"),
    ("GET", "processing-record/entry"), ("GET", "projects"),
    ("POST", "search"), ("POST", "list"), ("POST", "runtime/wake"),
    ("POST", "repair"), ("POST", "delete-data"),
])
def test_removed_http_routes_are_not_registered(method, path):
    from vibe.ui_server import app
    from tests.ui_server_test_helpers import csrf_headers
    V2Config.from_payload(_full_config_payload()).save()
    client = app.test_client()
    origin = "http://127.0.0.1:15131"
    response = client.request(method, f"/api/memory/{path}", base_url=origin,
                           headers=csrf_headers(client, origin))
    assert response.status_code == 404


def test_unknown_api_methods_preserve_existing_method_errors_and_spa(tmp_path, monkeypatch):
    from vibe import ui_server
    from tests.ui_server_test_helpers import csrf_headers
    V2Config.from_payload(_full_config_payload()).save()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>navigation control</html>")
    monkeypatch.setattr(ui_server, "get_ui_dist_path", lambda: dist)
    client = ui_server.app.test_client()
    origin = "http://127.0.0.1:15131"
    headers = csrf_headers(client, origin)
    for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        assert client.request(method, "/api/unknown-product-path", base_url=origin, headers=headers).status_code == 404
    assert client.request("DELETE", "/api/csrf-token", base_url=origin, headers=headers).status_code == 405
    assert client.get("/settings/general", base_url=origin).status_code == 200
