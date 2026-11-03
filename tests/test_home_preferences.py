"""The account's Workbench home preferences: Getting started put off or not needed, and the tips."""

from __future__ import annotations

import pytest

from storage.importer import ensure_sqlite_state
from tests.ui_server_test_helpers import csrf_headers
from vibe.ui_server import app

_BASE = "http://127.0.0.1:5123"
_DEFAULTS = {"setup_later_on": None, "setup_not_needed": [], "tip_hidden_on": None, "tips_enabled": True}


@pytest.fixture()
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    yield tmp_path


def test_preferences_merge_known_fields_and_keep_the_rest(isolated_state):
    from core.chat_discovery import get_state_meta, set_state_meta
    from core.home_preferences import (
        get_home_preferences,
        preferences_key,
        public_home_preferences,
        set_home_preferences,
    )

    assert public_home_preferences(get_home_preferences("local")) == _DEFAULTS

    set_state_meta(preferences_key("local"), {"tips_enabled": False, "setup_not_needed": ["phone", "nope"], "future": 1})
    stored = set_home_preferences("local", {"setup_later_on": "2026-11-04"})
    assert public_home_preferences(stored) == {
        "setup_later_on": "2026-11-04",
        # An unknown item read back is dropped, a known one kept.
        "setup_not_needed": ["phone"],
        "tip_hidden_on": None,
        "tips_enabled": False,
    }
    assert get_state_meta(preferences_key("local"))["future"] == 1

    # "Not needed" only ever adds: an item one device dismissed survives another device's dismissal.
    stored = set_home_preferences("local", {"setup_not_needed": ["notifications"]})
    assert stored["setup_not_needed"] == ["notifications", "phone"]
    stored = set_home_preferences("local", {"setup_not_needed": []})
    assert stored["setup_not_needed"] == ["notifications", "phone"]

    # A day is cleared with null.
    assert set_home_preferences("local", {"setup_later_on": None})["setup_later_on"] is None


@pytest.mark.parametrize(
    "updates",
    [
        {"setup_later_on": "tomorrow"},
        {"tip_hidden_on": 20261104},
        {"setup_not_needed": "phone"},
        {"setup_not_needed": ["fax"]},
        {"tips_enabled": "no"},
    ],
)
def test_preferences_refuse_malformed_values(isolated_state, updates):
    from core.home_preferences import get_home_preferences, set_home_preferences

    with pytest.raises(ValueError):
        set_home_preferences("local", updates)
    assert get_home_preferences("local")["setup_not_needed"] == []


def test_preferences_are_per_account(isolated_state):
    from core.home_preferences import get_home_preferences, set_home_preferences

    set_home_preferences("remote:alice", {"tips_enabled": False})
    assert get_home_preferences("remote:alice")["tips_enabled"] is False
    assert get_home_preferences("remote:bob")["tips_enabled"] is True
    assert get_home_preferences("local")["tips_enabled"] is True


def test_preferences_api_round_trip_and_csrf(isolated_state):
    client = app.test_client()

    default = client.get("/api/home/preferences", base_url=_BASE)
    assert default.status_code == 200
    assert default.get_json() == {"ok": True, "preferences": _DEFAULTS}

    forbidden = client.put("/api/home/preferences", json={"tips_enabled": False}, base_url=_BASE)
    assert forbidden.status_code == 403

    headers = csrf_headers(client, _BASE)
    updated = client.put(
        "/api/home/preferences",
        json={"tip_hidden_on": "2026-11-04", "setup_not_needed": ["chat_apps"], "ignored": True},
        headers=headers,
        base_url=_BASE,
    )
    assert updated.status_code == 200
    assert updated.get_json()["preferences"] == {**_DEFAULTS, "tip_hidden_on": "2026-11-04", "setup_not_needed": ["chat_apps"]}
    assert client.get("/api/home/preferences", base_url=_BASE).get_json()["preferences"]["setup_not_needed"] == ["chat_apps"]

    for body in ({}, {"ignored": True}, {"tips_enabled": "no"}):
        refused = client.put("/api/home/preferences", json=body, headers=headers, base_url=_BASE)
        assert refused.status_code == 400
