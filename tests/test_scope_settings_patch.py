"""PATCH /api/settings/scope: one edit changes only the fields it names on one scope.

The Web settings pages hold a copy of every channel and user from when they
loaded. Saving that copy back reverted other writers' changes, deleted channels
enabled since, and restored revoked admin rights; these tests pin the contract
that replaced it.
"""

from __future__ import annotations

import pytest

from config.v2_settings import LEGACY_ROUTING_ALIASES, ChannelSettings, RoutingSettings, SettingsStore, UserSettings
from core.vibe_agents import VibeAgentStore
from storage.settings_service import ScopeAgentUnavailableError, StaleScopeAgentBindingError
from vibe import api


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    agents = VibeAgentStore()
    agents.create(name="pm", backend="claude")
    agents.create(name="qa", backend="claude")
    agents.close()
    store = SettingsStore.get_instance()
    yield store
    SettingsStore.reset_instance()


def _patch(scope: str, native_id: str, changes: dict, *, platform: str = "slack", **extra) -> dict:
    return api.patch_scope_settings({"platform": platform, "scope": scope, "id": native_id, "changes": changes, **extra})


def test_an_edit_writes_only_its_fields_of_its_scope(store):
    store.set_channels_for_platform("slack", {"A": ChannelSettings(enabled=True, custom_cwd="/a")})
    store.set_users_for_platform("slack", {"U": UserSettings(display_name="Pat", is_admin=True)})
    store.save()
    # Written elsewhere after a settings page loaded: a new channel, A's workdir,
    # and the user's admin and enabled flags.
    store.set_channels_for_platform("slack", {
        "A": ChannelSettings(enabled=True, custom_cwd="/a-elsewhere"),
        "B": ChannelSettings(enabled=True, custom_cwd="/b"),
    })
    store.set_users_for_platform("slack", {"U": UserSettings(display_name="Pat", is_admin=False, enabled=False)})
    store.save()

    channel = _patch("channel", "A", {"require_mention": True})
    user = _patch("user", "U", {"custom_cwd": "/u"})

    assert channel["settings"]["require_mention"] is True
    assert channel["settings"]["custom_cwd"] == "/a-elsewhere"
    store = SettingsStore.get_instance()  # reloads what the request committed
    assert store.find_channel("B", platform="slack").custom_cwd == "/b"
    saved_user = store.get_user("U", platform="slack")
    assert (saved_user.custom_cwd, saved_user.is_admin, saved_user.enabled) == ("/u", False, False)
    assert user["settings"]["custom_cwd"] == "/u"


@pytest.mark.parametrize(
    ("stored_agent", "expected", "routing", "outcome"),
    [
        # The page's own earlier save bound the Agent its next edit is based on.
        (None, None, {"agent_name": "pm", "model": "m1"}, ("pm", "m1", None)),
        ("pm", "pm", {"reasoning_effort": "high"}, ("pm", "m0", "high")),
        # Another writer moved the scope to pm: a model picked for the old Agent is refused...
        ("pm", None, {"model": "m1"}, StaleScopeAgentBindingError),
        ("pm", "qa", {"agent_name": "qa", "model": "m1"}, StaleScopeAgentBindingError),
        # ...unless the edit itself sets the stored Agent.
        ("pm", None, {"agent_name": "pm", "model": "m1"}, ("pm", "m1", None)),
        # A new binding still needs an enabled Agent.
        ("pm", "pm", {"agent_name": "missing"}, ScopeAgentUnavailableError),
    ],
)
def test_a_routing_edit_names_the_agent_its_view_was_based_on(store, stored_agent, expected, routing, outcome):
    store.set_channels_for_platform("slack", {
        "A": ChannelSettings(enabled=True, routing=RoutingSettings(agent_name=stored_agent, model="m0")),
    })
    store.save()

    request = {"routing": routing}
    if isinstance(outcome, type):
        with pytest.raises(outcome):
            _patch("channel", "A", request, expected_agent_name=expected)
        saved = SettingsStore.get_instance().find_channel("A", platform="slack").routing
        assert (saved.agent_name, saved.model) == (stored_agent, "m0")
    else:
        result = _patch("channel", "A", request, expected_agent_name=expected)["settings"]
        assert (result["routing"]["agent_name"], result["routing"]["model"], result["routing"]["reasoning_effort"]) == outcome
        assert result["expected_agent_name"] == outcome[0]


@pytest.mark.parametrize(
    ("routing", "outcome"),
    [
        # A page from an older release clears every retired alias beside its edit.
        ({"model": "m1", "reasoning_effort": None, "codex_model": None, "claude_reasoning_effort": None},
         ("m1", None)),
        # An older API caller names the model only by the route's own alias...
        ({"codex_model": "m2", "codex_reasoning_effort": "high"}, ("m2", "high")),
        # ...which never overrides a canonical field the edit names.
        ({"model": "m1", "codex_model": "m2"}, ("m1", None)),
        # Another backend's alias applies to no field of this route.
        ({"claude_model": "m2"}, ("m0", None)),
    ],
)
def test_a_routing_edit_still_reads_retired_aliases_but_never_stores_them(store, routing, outcome):
    agents = VibeAgentStore()
    agents.create(name="codex", backend="codex")
    agents.close()
    store.set_channels_for_platform("slack", {
        "A": ChannelSettings(enabled=True, routing=RoutingSettings(agent_name="codex", model="m0")),
    })
    store.save()

    result = _patch("channel", "A", {"routing": routing}, expected_agent_name="codex")["settings"]["routing"]

    assert (result["agent_name"], result["model"], result["reasoning_effort"]) == ("codex", *outcome)
    assert not LEGACY_ROUTING_ALIASES & set(result)
    saved = SettingsStore.get_instance().find_channel("A", platform="slack").routing
    assert (saved.model, saved.reasoning_effort) == outcome


def test_a_topic_override_starts_from_its_channel(store, monkeypatch):
    monkeypatch.setattr(api, "_stored_platform_config", lambda _platform: type("Config", (), {"require_mention": False}))
    store.set_channels_for_platform("telegram", {
        "chat": ChannelSettings(enabled=True, custom_cwd="/chat", routing=RoutingSettings(agent_name="qa")),
    })
    store.save()

    created = _patch("thread", "chat", {}, platform="telegram", thread_id="7")["settings"]
    edited = _patch("thread", "chat", {"custom_cwd": "/topic"}, platform="telegram", thread_id="7")["settings"]

    assert (created["routing"]["agent_name"], created["custom_cwd"], created["require_mention"]) == ("qa", "/chat", False)
    assert (edited["routing"]["agent_name"], edited["custom_cwd"]) == ("qa", "/topic")
    assert SettingsStore.get_instance().find_channel("chat", platform="telegram").custom_cwd == "/chat"


@pytest.mark.parametrize(
    ("request_fields", "code", "status"),
    [
        ({"scope": "guild", "id": "G", "changes": {}}, "invalid_scope", 400),
        ({"scope": "thread", "id": "C", "thread_id": "1", "changes": {}}, "invalid_scope", 400),
        ({"scope": "channel", "id": "A", "changes": {"is_admin": True}}, "invalid_changes", 400),
        ({"scope": "user", "id": "U", "changes": {"require_bind": True}}, "invalid_changes", 400),
        ({"scope": "channel", "id": "A", "changes": {"enabled": "yes"}}, "invalid_changes", 400),
        ({"scope": "channel", "id": "A", "changes": {"routing": {"agent_name": "pm"}}}, "invalid_changes", 400),
        ({"scope": "channel", "id": "A", "changes": {"routing": {"agent": "pm"}}, "expected_agent_name": None},
         "invalid_changes", 400),
        ({"scope": "user", "id": "nobody", "changes": {"custom_cwd": "/x"}}, "user_not_found", 404),
    ],
)
def test_requests_the_api_cannot_apply_are_refused_without_writing(store, request_fields, code, status):
    revision = store._service.current_revision()

    with pytest.raises(api.ScopeSettingsRequestError) as refused:
        api.patch_scope_settings({"platform": "slack", **request_fields})

    assert (refused.value.code, refused.value.status) == (code, status)
    assert store._service.current_revision() == revision
