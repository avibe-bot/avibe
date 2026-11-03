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
        "hide_details": True,
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
    shown = client.patch("/api/bots/cso", json={"hide_details": False}, headers=csrf_headers(client))
    assert shown.get_json()["bot"]["hide_details"] is False
    assert {row["handle"]: row["hide_details"] for row in client.get("/api/bots").get_json()["bots"]} == {
        "main": True,
        "cso": False,
    }
    invalid = client.patch("/api/bots/cso", json={"hide_details": "no"}, headers=csrf_headers(client))
    assert (invalid.status_code, invalid.get_json()["error"]["code"]) == (400, "bot_hide_details_invalid")

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


def test_the_main_bots_avatar_is_refused_over_http_in_the_readers_language(client):
    from PIL import Image

    zh = {**csrf_headers(client), "Accept-Language": "zh-CN"}
    patched = client.patch("/api/bots/main", json={"avatar": {"emoji": "🦊", "hue": 30}}, headers=zh)
    error = patched.get_json()["error"]
    assert (patched.status_code, error["code"], error["message"]) == (
        409, "bot_main_avatar", "主伙伴始终使用 Vibey 吉祥物头像，不能更换。"
    )
    image = io.BytesIO()
    Image.new("RGB", (64, 64), "blue").save(image, format="PNG")
    uploaded = client.put("/api/bots/main/avatar", content=image.getvalue(), headers={**zh, "content-type": "image/png"})
    assert (uploaded.status_code, uploaded.get_json()["error"]["code"]) == (409, "bot_main_avatar")
    assert client.get("/api/bots").get_json()["bots"][0]["avatar"] == {"kind": "mascot"}


def test_the_limit_rides_with_the_list_and_refusals_speak_the_readers_language(client):
    """bots.md D12: the Web knows the limit from the Bot list, and a refusal that still happens says it."""
    from core.bots import BotService

    assert client.get("/api/bots").get_json()["limit"] == {"count": 0, "max": 5}
    with BotService() as service:
        for name in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon"):
            service.create_bot(display_name=name)
        service.store.create(name="zeta", backend="vibey")
    assert client.get("/api/bots").get_json()["limit"] == {"count": 5, "max": 5}

    zh = {**csrf_headers(client), "Accept-Language": "zh-CN"}
    for path, body in (("/api/bots", {"display_name": "Zeta"}), ("/api/bots/set", {"agent_name": "zeta", "display_name": "Zeta"})):
        refused = client.post(path, json=body, headers=zh)
        payload = refused.get_json()
        assert (refused.status_code, payload["error"]["code"]) == (409, "bot_limit_reached"), path
        assert payload["error"]["message"] == "最多 5 位伙伴，移出一位后可以再添加。移出的伙伴会保留对话和记忆。"
        assert (payload["details"]["limit"], payload["details"]["count"]) == (5, 5)
    assert len(client.get("/api/bots").get_json()["bots"]) == 6


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


def test_a_bots_agent_is_refused_as_the_default_over_http(client):
    client.post("/api/bots", json={"display_name": "Researcher"}, headers=csrf_headers(client))
    refused = client.post("/api/agents/default", json={"name": "researcher"}, headers=csrf_headers(client))
    assert (refused.status_code, refused.get_json()["code"]) == (409, "agent_is_bot")
    vibey = client.post("/api/bots/set", json={"agent_name": "vibey", "display_name": "V"}, headers=csrf_headers(client))
    assert (vibey.status_code, vibey.get_json()["error"]["code"]) == (409, "bot_agent_builtin")


def _new_route_target(client, surface: str, tmp_path, name: str) -> str:
    if surface == "scope":
        return name
    folder = tmp_path / name
    folder.mkdir()
    project = client.post("/api/projects", json={"folder_path": str(folder)}, headers=csrf_headers(client)).get_json()
    if surface == "project":
        return project["id"]
    return client.post("/api/sessions", json={"project_id": project["id"]}, headers=csrf_headers(client)).get_json()["id"]


def _route_to(client, surface: str, target: str, agent: str, *, model: str | None = None, stored: str | None = None):
    """Point one Session, Project, or IM channel at ``agent`` through its Web route."""
    if surface == "session":
        return client.patch(f"/api/sessions/{target}", json={"agent_name": agent, "model": model}, headers=csrf_headers(client))
    if surface == "project":
        return client.patch(f"/api/projects/{target}", json={"agent_name": agent, "model": model}, headers=csrf_headers(client))
    return client.patch(
        "/api/settings/scope",
        json={
            "platform": "slack",
            "scope": "channel",
            "id": target,
            "changes": {"routing": {"agent_name": agent, "model": model}},
            "expected_agent_name": stored,
        },
        headers=csrf_headers(client),
    )


@pytest.fixture()
def settings_store():
    from config.v2_settings import SettingsStore

    SettingsStore.reset_instance()
    yield
    SettingsStore.reset_instance()


@pytest.mark.parametrize("surface", ["session", "project", "scope"])
def test_no_new_route_names_a_bots_agent_and_one_already_on_it_keeps_it(client, settings_store, tmp_path, surface):
    """A Bot's Agent is internal to its Bot (``bots.md`` D1); refusals are the localized ``agent_is_bot``."""
    from core.vibe_agents import VibeAgentStore

    store = VibeAgentStore()
    try:
        store.create(name="writer", backend="vibey")
    finally:
        store.close()
    kept = _new_route_target(client, surface, tmp_path, "kept")
    fresh = _new_route_target(client, surface, tmp_path, "fresh")
    assert _route_to(client, surface, kept, "writer").status_code == 200
    made = client.post("/api/bots/set", json={"agent_name": "writer", "display_name": "Writer"}, headers=csrf_headers(client))
    assert made.status_code == 200, made.get_json()

    # A route already on the Bot's Agent keeps it while its other fields change.
    assert _route_to(client, surface, kept, "writer", model="m2", stored="writer").status_code == 200
    for agent in ("writer", "main"):
        refused = _route_to(client, surface, fresh, agent)
        error = refused.get_json()["error"]
        assert (refused.status_code, error["code"]) == (409, "agent_is_bot"), refused.get_json()
        assert error["message"] == f"Agent `{agent}` belongs to a Bot and can't be chosen as an Agent."

    # A removed Bot's Agent is an ordinary Agent again.
    assert client.delete("/api/bots/writer", headers=csrf_headers(client)).status_code == 200
    assert _route_to(client, surface, fresh, "writer").status_code == 200
