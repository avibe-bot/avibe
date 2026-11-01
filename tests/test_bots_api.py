"""The Bots Web API (``docs/plans/bots.md`` "Web API (owner-only)")."""

from __future__ import annotations

import io

import pytest

from storage.importer import ensure_sqlite_state
from tests.ui_server_test_helpers import csrf_headers
from vibe.ui_server import app


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from core.vibe_agents import VibeAgentStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    store = VibeAgentStore()
    try:
        store.ensure_builtin_default_agents([])
    finally:
        store.close()
    return app.test_client()


def test_every_bots_route_is_owner_only():
    from vibe.authorization import http_authorization_policy

    for method, path in (
        ("GET", "/api/bots"),
        ("POST", "/api/bots"),
        ("POST", "/api/bots/set"),
        ("POST", "/api/bots/handle-suggestion"),
        ("PATCH", "/api/bots/cso"),
        ("DELETE", "/api/bots/cso"),
        ("PUT", "/api/bots/cso/avatar"),
        ("GET", "/api/bots/cso/avatar"),
    ):
        assert http_authorization_policy(method, path).minimum_role == "owner", (method, path)


def test_the_bot_lifecycle_over_http(client, tmp_path):
    listed = client.get("/api/bots")
    assert listed.status_code == 200, listed.get_json()
    (main,) = listed.get_json()["bots"]
    assert main == {
        "handle": "main",
        "display_name": "",
        "avatar": {"kind": "mascot"},
        "role": "The user's main Bot and first point of contact.",
        "agent_name": "main",
        "session_id": "ses_main",
        "position": 0,
        "builtin": True,
    }
    assert client.get("/api/sessions/ses_main").status_code == 200

    created = client.post(
        "/api/bots",
        json={"display_name": "首席安全官", "handle": "cso", "role": "Reviews security.", "avatar": {"emoji": "🛡️", "hue": 210}},
        headers=csrf_headers(client),
    )
    assert created.status_code == 201, created.get_json()
    bot = created.get_json()["bot"]
    assert (bot["handle"], bot["session_id"], bot["agent_name"], bot["position"]) == ("cso", "ses_cso", "cso", 1)

    taken = client.post("/api/bots", json={"display_name": "Again", "handle": "cso"}, headers=csrf_headers(client))
    assert (taken.status_code, taken.get_json()["error"]["code"]) == (409, "bot_handle_taken")

    patched = client.patch("/api/bots/cso", json={"display_name": "安全官"}, headers=csrf_headers(client))
    assert patched.get_json()["bot"]["display_name"] == "安全官"
    assert client.get("/api/sessions/ses_cso").get_json()["title"] == "安全官"
    refused = client.patch("/api/bots/cso", json={"handle": "x"}, headers=csrf_headers(client))
    assert refused.status_code == 400

    image = io.BytesIO()
    from PIL import Image

    Image.new("RGB", (64, 64), "blue").save(image, format="PNG")
    uploaded = client.put(
        "/api/bots/cso/avatar", content=image.getvalue(), headers={**csrf_headers(client), "content-type": "image/png"}
    )
    assert uploaded.status_code == 200, uploaded.get_json()
    url = uploaded.get_json()["bot"]["avatar"]["url"]
    served = client.get(url)
    assert (served.status_code, served.headers["content-type"]) == (200, "image/png")
    assert (tmp_path / "bots" / "cso" / "avatar.png").read_bytes() == served.content

    assert client.delete("/api/bots/main", headers=csrf_headers(client)).status_code == 409
    removed = client.delete("/api/bots/cso", headers=csrf_headers(client))
    assert removed.get_json() == {"ok": True, "removed": "cso", "agent_name": "cso", "session_id": "ses_cso"}
    assert [row["handle"] for row in client.get("/api/bots").get_json()["bots"]] == ["main"]

    again = client.post("/api/bots/set", json={"agent_name": "cso", "display_name": "CSO"}, headers=csrf_headers(client))
    assert again.get_json()["bot"]["session_id"] == "ses_cso"


def test_bot_sessions_refuse_archive_and_title_edits(client):
    client.post("/api/bots", json={"display_name": "Researcher"}, headers=csrf_headers(client))
    archived = client.delete("/api/sessions/ses_researcher", headers=csrf_headers(client))
    assert (archived.status_code, archived.get_json()["error"]["code"]) == (403, "reserved_session")
    renamed = client.patch("/api/sessions/ses_researcher", json={"title": "x"}, headers=csrf_headers(client))
    assert renamed.status_code == 403
    assert client.patch(
        "/api/sessions/ses_researcher", json={"pinned": True}, headers=csrf_headers(client)
    ).status_code == 200
    searched = client.get("/api/sessions?q=Research").get_json()["sessions"]
    assert all(row["id"] != "ses_researcher" for row in searched)
