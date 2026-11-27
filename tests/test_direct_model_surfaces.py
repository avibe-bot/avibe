"""Every surface offers one Direct menu: the Web pickers, `vibe agent models` and the IM routing form.

A removed model leaves all of them at once and comes back only when restored; the restore
list is the rows the backend offers now that the menu hides. Through the real store and HTTP routes; only the
backend's catalog (the supply) and the OpenCode server are fixtures.
"""

import asyncio
from types import SimpleNamespace

import pytest

from core.modals import BackendRouteMenu
from vibe import api, backend_model_catalog

CODEX = {
    "ok": True,
    "backend": "codex",
    "models": ["gpt-6.2-sol", "gpt-5.5"],
    "model_labels": {"gpt-6.2-sol": "GPT-6.2-Sol"},
    "reasoning_options": {"": [{"value": "high", "label": "High"}], "gpt-6.2-sol": [], "gpt-5.5": []},
}


@pytest.fixture
def owner(tmp_path, monkeypatch):
    from config.v2_config import V2Config
    from tests.ui_server_test_helpers import csrf_headers
    from vibe.ui_server import app

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    V2Config.default().save()
    monkeypatch.setattr(backend_model_catalog, "backend_model_snapshot", lambda backend, **_: dict(CODEX))
    client = app.test_client()
    return client, csrf_headers(client)


def _im_menu(handler_loader, backend: str, hub_agent=None) -> list[str]:
    from core.handlers.settings_handler import SettingsHandler

    handler = SettingsHandler.__new__(SettingsHandler)

    async def read_agent_sources(_backend):
        return hub_agent or {"mode": "direct"}

    handler.controller = SimpleNamespace(
        get_cwd=lambda context: "/tmp",
        model_hub_service=SimpleNamespace(read_agent_sources=read_agent_sources),
    )
    menu = BackendRouteMenu(backend)
    asyncio.run(handler_loader(handler, None, menu, None, set()))
    return [option.value for option in menu.models]


def test_removed_model_leaves_every_surface_until_restored(owner, monkeypatch):
    from core.handlers.settings_handler import SettingsHandler

    client, headers = owner
    monkeypatch.setattr(api, "codex_agents", lambda cwd: {"ok": True, "agents": []})
    url = "/api/backend/codex/models/visibility"

    def surfaces():
        return {
            "web": client.get("/api/codex/models").get_json()["models"],
            "cli": [row["value"] for row in api.route_menu_options("codex")["models"]],
            "im": _im_menu(SettingsHandler._load_codex_route_menu, "codex"),
            "restore": [row["id"] for row in client.get(url).get_json()["removed"]],
        }

    everywhere = ["gpt-6.2-sol", "gpt-5.5"]
    assert surfaces() == {"web": everywhere, "cli": everywhere, "im": everywhere, "restore": []}
    assert client.get("/api/backend/vibey/models/visibility").status_code == 400

    assert client.put(url, json={"model_id": "gpt-6.2-sol", "removed": True}, headers=headers).status_code == 200
    assert surfaces() == {"web": ["gpt-5.5"], "cli": ["gpt-5.5"], "im": ["gpt-5.5"], "restore": ["gpt-6.2-sol"]}
    assert client.get(url).get_json() == {
        "ok": True,
        "models": [{"id": "gpt-5.5", "label": "gpt-5.5"}],
        "removed": [{"id": "gpt-6.2-sol", "label": "GPT-6.2-Sol"}],
    }
    # A Session or Agent already on the removed model keeps it: checks of a stored choice still find it.
    from vibe import cli

    assert cli._catalog_mismatches("codex", "gpt-6.2-sol", None) == ([], False)
    assert cli._menu_entry(api.route_menu_options("codex"), "gpt-6.2-sol") is not None
    assert "gpt-6.2-sol" not in client.get("/api/codex/models").get_json()["model_labels"]

    assert client.put(url, json={"model_id": "gpt-6.2-sol", "removed": False}, headers=headers).status_code == 200
    assert surfaces() == {"web": everywhere, "cli": everywhere, "im": everywhere, "restore": []}


def test_im_form_reads_the_hub_menu_for_a_hub_backend(monkeypatch):
    """Before, the IM form offered Claude and Codex their Direct catalog even on the gateway."""
    from core.handlers.settings_handler import SettingsHandler

    def direct_read(*_args, **_kwargs):
        raise AssertionError("a Hub backend's form read its Direct catalog")

    monkeypatch.setattr(api, "codex_models", direct_read)
    monkeypatch.setattr(api, "codex_agents", lambda cwd: {"ok": True, "agents": []})
    hub = {
        "mode": "hub",
        "catalog_models": [
            {"id": "gpt-6.2-astra", "routeable": True, "reasoning_efforts": ["high", "xhigh"], "effort_menu": {"omitted": ["xhigh"]}},
            {"id": "default", "routeable": False},
        ],
    }
    assert _im_menu(SettingsHandler._load_codex_route_menu, "codex", hub) == ["gpt-6.2-astra"]


def test_opencode_menu_and_restore_list_split_on_the_picker_key(owner, monkeypatch):
    """Settings reads OpenCode from its cached options (never starting it); the menu drops `provider/id`."""
    import os
    import time

    client, headers = owner

    async def no_live_read(*_args, **_kwargs):
        raise AssertionError("opening Settings read OpenCode live")

    url = "/api/backend/opencode/models/visibility"
    with monkeypatch.context() as patched:
        patched.setattr(api, "opencode_options_async", no_live_read)
        assert client.get(url).get_json() == {"ok": True, "models": None, "removed": []}

    from config.v2_config import V2Config

    config = V2Config.load()
    config.model_hub.agents["opencode"].mode = "direct"
    config.save()
    data = {
        "models": {
            "providers": [
                {"id": "openai", "models": {"gpt-6.2-sol": {}, "gpt-5.5": {}}},
                {"id": "relay", "models": [{"id": "gpt-6.2-sol"}]},
            ]
        },
        "reasoning_options": {"openai/gpt-6.2-sol": [], "openai/gpt-5.5": [], "relay/gpt-6.2-sol": []},
    }
    monkeypatch.setitem(
        api._OPENCODE_OPTIONS_CACHE,
        os.path.expanduser("~"),
        {"data": data, "updated_at": time.monotonic(), "model_hub_projection": "{}", "mode": "direct"},
    )
    assert client.put(url, json={"model_id": "openai/gpt-6.2-sol", "removed": True}, headers=headers).status_code == 200

    def ids(rows):
        return [row["id"] for row in rows]

    answer = client.get(url).get_json()
    assert ids(answer["models"]) == ["openai/gpt-5.5", "relay/gpt-6.2-sol"]
    assert ids(answer["removed"]) == ["openai/gpt-6.2-sol"]
    picker = client.post("/api/opencode/options", json={"cwd": "~"}, headers=headers).get_json()
    assert picker["cached"] is True
    assert api.opencode_menu_values(picker["data"]) == ["openai/gpt-5.5", "relay/gpt-6.2-sol"]
    assert set(picker["data"]["reasoning_options"]) == {"openai/gpt-5.5", "relay/gpt-6.2-sol"}
    # The cache keeps the full live list, so restoring needs no new read.
    assert api.opencode_menu_values(api._OPENCODE_OPTIONS_CACHE[os.path.expanduser("~")]["data"]) == [
        "openai/gpt-6.2-sol",
        "openai/gpt-5.5",
        "relay/gpt-6.2-sol",
    ]
