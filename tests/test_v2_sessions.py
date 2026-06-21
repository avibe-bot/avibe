import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import paths
from config.v2_sessions import SessionsStore
from modules.sessions_facade import SessionsFacade


def test_sessions_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    store = SessionsStore()
    store.state.session_mappings = {"U1": {"claude": {"base": {"/tmp": "session-1"}}}}
    store.state.active_slack_threads = {"U1": {"C1": {"123.456": 1.0}}}
    store.state.last_activity = "2026-01-18T12:00:00Z"
    store.save()

    reloaded = SessionsStore()
    reloaded.load()
    assert reloaded.state.session_mappings["U1"]["claude"]["base"]["/tmp"] == "session-1"
    assert reloaded.state.active_slack_threads["U1"]["C1"]["123.456"] == 1.0
    assert reloaded.state.last_activity == "2026-01-18T12:00:00Z"


def test_sessions_store_namespaces(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    store = SessionsStore()
    agent_map = store.get_agent_map("U2", "opencode")
    thread_map = store.get_thread_map("U2", "C2")
    assert agent_map == {}
    assert thread_map == {}
    assert "U2" in store.state.session_mappings
    assert "U2" in store.state.active_slack_threads


def test_active_thread_is_shared_across_users(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    store = SessionsStore()
    sessions = SessionsFacade(store)

    sessions.mark_thread_active("U1", "C1", "123.456")

    assert sessions.is_thread_active("U2", "C1", "123.456")
    assert not sessions.is_thread_active_for_user("U2", "C1", "123.456")
    assert sessions.is_thread_active_for_user("U1", "C1", "123.456")


def test_shared_active_thread_ignores_expired_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    store = SessionsStore()
    store.state.active_slack_threads = {"U1": {"C1": {"123.456": 1.0}}}
    sessions = SessionsFacade(store)

    assert not sessions.is_thread_active("U2", "C1", "123.456")
    assert "U1" not in store.state.active_slack_threads


def test_migrate_active_polls_backfills_platform_and_scoped_key(tmp_path, monkeypatch):
    """Legacy active_polls lacking platform are migrated; unscoped keys stay raw."""
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    store = SessionsStore()
    # Simulate a legacy poll saved before the multi-platform PR
    store.state.active_polls = {
        "oc-session-1": {
            "opencode_session_id": "oc-session-1",
            "base_session_id": "C123:msg1",
            "channel_id": "C123",
            "thread_id": "t1",
            "settings_key": "C123",  # already raw key
            "working_path": "/tmp/work",
            "platform": "",  # missing platform
            "user_id": "U1",
        }
    }
    store.save()

    reloaded = SessionsStore()
    reloaded.load()
    reloaded.migrate_active_polls("slack")

    poll = reloaded.state.active_polls["oc-session-1"]
    assert poll["platform"] == "slack"
    assert poll["settings_key"] == "C123"


def test_migrate_active_polls_strips_scoped_key(tmp_path, monkeypatch):
    """Scoped settings_key is stripped back to raw ID."""
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    store = SessionsStore()
    store.state.active_polls = {
        "oc-session-2": {
            "opencode_session_id": "oc-session-2",
            "base_session_id": "C456:msg2",
            "channel_id": "C456",
            "thread_id": "t2",
            "settings_key": "discord::C456",
            "working_path": "/tmp/work",
            "platform": "discord",
            "user_id": "U2",
        }
    }
    store.save()

    reloaded = SessionsStore()
    reloaded.load()
    reloaded.migrate_active_polls("slack")

    poll = reloaded.state.active_polls["oc-session-2"]
    assert poll["platform"] == "discord"
    assert poll["settings_key"] == "C456"


def test_migrate_active_polls_extracts_platform_from_scoped_key(tmp_path, monkeypatch):
    """When platform is empty but settings_key is scoped, extract platform from prefix."""
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    store = SessionsStore()
    store.state.active_polls = {
        "oc-session-3": {
            "opencode_session_id": "oc-session-3",
            "base_session_id": "C789:msg3",
            "channel_id": "C789",
            "thread_id": "t3",
            "settings_key": "discord::C789",
            "working_path": "/tmp/work",
            "platform": "",  # missing platform, but scoped key has it
            "user_id": "U3",
        }
    }
    store.save()

    reloaded = SessionsStore()
    reloaded.load()
    reloaded.migrate_active_polls("slack")

    poll = reloaded.state.active_polls["oc-session-3"]
    assert poll["platform"] == "discord", "Should extract platform from scoped key prefix, not default"
    assert poll["settings_key"] == "C789"


def test_active_poll_persists_typing_cleanup_context(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    store = SessionsStore()
    sessions = SessionsFacade(store)

    sessions.add_active_poll(
        opencode_session_id="oc-session-4",
        base_session_id="base-4",
        channel_id="wx-chat",
        thread_id="",
        settings_key="wx-chat",
        working_path="/tmp/work",
        baseline_message_ids=[],
        typing_indicator_active=True,
        context_token="ctx-4",
        processing_indicator={
            "platform": "wechat",
            "user_id": "wx-user",
            "channel_id": "wx-chat",
            "context_token": "ctx-4",
            "typing_indicator_active": True,
        },
        user_id="wx-user",
        platform="wechat",
        prompt_started_at=1234.5,
        model_dict={"providerID": "glm", "modelID": "glm-5.2"},
        reasoning_effort="high",
    )

    reloaded = SessionsStore()
    reloaded.load()
    poll = reloaded.get_active_poll("oc-session-4")

    assert poll is not None
    assert poll.typing_indicator_active is True
    assert poll.context_token == "ctx-4"
    assert poll.processing_indicator["context_token"] == "ctx-4"
    assert poll.prompt_started_at == 1234.5
    assert poll.model_dict == {"providerID": "glm", "modelID": "glm-5.2"}
    assert poll.reasoning_effort == "high"


# --- session_mappings migration tests ---


def test_migrate_session_mappings_moves_old_key_to_prefixed(tmp_path, monkeypatch):
    """Old raw key entries are merged into the platform-prefixed key."""
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    store = SessionsStore()
    store.state.session_mappings = {
        "C0A6U2GH6P5": {
            "opencode": {
                "slack_123.456:/tmp/work": "ses_old_abc",
                "slack_789.012:/tmp/work": "ses_old_def",
            }
        }
    }
    store.save()

    reloaded = SessionsStore()
    reloaded.load()
    reloaded.migrate_session_mappings("slack")
    reloaded.load()

    # Old raw key should be gone
    assert "C0A6U2GH6P5" not in reloaded.state.session_mappings
    # New prefixed key should have the entries. The ``:/tmp/work`` cwd suffix in the
    # seed is stripped on save (#368: anchors no longer carry the working path; it
    # lives on the ``workdir`` column), so the round-tripped thread keys are bare.
    prefixed = reloaded.state.session_mappings.get("slack::C0A6U2GH6P5", {})
    assert prefixed["opencode"]["slack_123.456"] == "ses_old_abc"
    assert prefixed["opencode"]["slack_789.012"] == "ses_old_def"


def test_migrate_session_mappings_merges_without_overwriting(tmp_path, monkeypatch):
    """Old entries don't overwrite newer entries already under the prefixed key."""
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    store = SessionsStore()
    store.state.session_mappings = {
        # Old key with a stale session for thread A, and a session for thread B
        "C123": {
            "opencode": {
                "slack_threadA:/work": "ses_stale",
                "slack_threadB:/work": "ses_old_B",
            }
        },
        # New key already has a fresh session for thread A
        "slack::C123": {
            "opencode": {
                "slack_threadA:/work": "ses_fresh",
            }
        },
    }
    store.save()

    reloaded = SessionsStore()
    reloaded.load()
    reloaded.migrate_session_mappings("slack")
    reloaded.load()

    assert "C123" not in reloaded.state.session_mappings
    oc = reloaded.state.session_mappings["slack::C123"]["opencode"]
    # Thread A keeps the fresh value (not overwritten by stale). Keys are bare:
    # the ``:/work`` cwd suffix is stripped on save (#368).
    assert oc["slack_threadA"] == "ses_fresh"
    # Thread B is carried over from old key
    assert oc["slack_threadB"] == "ses_old_B"


def test_migrate_session_mappings_cleans_empty_keys(tmp_path, monkeypatch):
    """Empty orphan keys are removed even when no real migration is needed."""
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    store = SessionsStore()
    store.state.session_mappings = {
        "U0E0FM3QT": {},  # empty orphan
        "749794605024936027": {},  # empty orphan
        "slack::C123": {"opencode": {"t1": "s1"}},
    }
    store.save()

    reloaded = SessionsStore()
    reloaded.load()
    reloaded.migrate_session_mappings("slack")
    reloaded.load()

    assert "U0E0FM3QT" not in reloaded.state.session_mappings
    assert "749794605024936027" not in reloaded.state.session_mappings
    assert reloaded.state.session_mappings["slack::C123"]["opencode"]["t1"] == "s1"


def test_migrate_session_mappings_noop_when_already_prefixed(tmp_path, monkeypatch):
    """No changes when all keys are already platform-prefixed."""
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    store = SessionsStore()
    store.state.session_mappings = {
        "slack::C123": {"opencode": {"t1": "s1"}},
        "discord::D456": {"opencode": {"t2": "s2"}},
    }
    store.save()

    reloaded = SessionsStore()
    reloaded.load()
    reloaded.migrate_session_mappings("slack")

    # Nothing should change
    assert set(reloaded.state.session_mappings.keys()) == {"slack::C123", "discord::D456"}


def test_migrate_session_mappings_infers_platform_from_thread_ids(tmp_path, monkeypatch):
    """Platform is inferred from thread ID prefixes, not blindly using default_platform."""
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    store = SessionsStore()
    store.state.session_mappings = {
        # Legacy key with discord thread IDs, but default_platform will be "slack"
        "D456": {
            "opencode": {
                "discord_1485641561998889093:/work": "ses_discord_1",
                "discord_1485641756535165051:/work": "ses_discord_2",
            }
        }
    }
    store.save()

    reloaded = SessionsStore()
    reloaded.load()
    reloaded.migrate_session_mappings("slack")  # default is slack, but data is discord
    reloaded.load()

    # Should migrate to discord:: not slack::
    assert "D456" not in reloaded.state.session_mappings
    assert "slack::D456" not in reloaded.state.session_mappings
    prefixed = reloaded.state.session_mappings.get("discord::D456", {})
    assert prefixed["opencode"]["discord_1485641561998889093"] == "ses_discord_1"


def test_migrate_session_mappings_is_idempotent(tmp_path, monkeypatch):
    """Running migration twice does not duplicate or corrupt data."""
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    store = SessionsStore()
    store.state.session_mappings = {
        "C123": {"opencode": {"slack_123.456:/work": "ses_abc"}},
    }
    store.save()

    # First migration
    store1 = SessionsStore()
    store1.load()
    store1.migrate_session_mappings("slack")

    # Second migration (reload from disk)
    store2 = SessionsStore()
    store2.load()
    store2.migrate_session_mappings("slack")
    store2.load()

    assert set(store2.state.session_mappings.keys()) == {"slack::C123"}
    assert store2.state.session_mappings["slack::C123"]["opencode"]["slack_123.456"] == "ses_abc"
