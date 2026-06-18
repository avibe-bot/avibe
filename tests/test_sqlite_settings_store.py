from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from config import paths
from config.v2_settings import ChannelSettings, RoutingSettings, SettingsState, SettingsStore, UserSettings
from storage import projects_service
from storage.db import create_sqlite_engine
from storage.migrations import run_migrations
from storage.models import scope_settings, scopes
from storage.sessions_service import SQLiteSessionsService
from storage.settings_service import SQLiteSettingsService, upsert_scope
from modules.settings_manager import SettingsManager


def test_settings_store_uses_sqlite_without_rewriting_legacy_json(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    original = json.dumps(
        {
            "channels": {
                "C123": {
                    "enabled": True,
                    "show_message_types": ["assistant"],
                }
            }
        },
        indent=2,
    )
    settings_path.write_text(original, encoding="utf-8")

    store = SettingsStore(settings_path)
    store.update_channel("C999", ChannelSettings(enabled=True), platform="slack")
    store.close()

    reloaded = SettingsStore(settings_path)
    try:
        assert reloaded.find_channel("C123", platform="slack") is not None
        assert reloaded.find_channel("C999", platform="slack") is not None
        assert settings_path.read_text(encoding="utf-8") == original
    finally:
        reloaded.close()


def test_channel_require_bind_persists(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    store.update_channel("C-bind", ChannelSettings(enabled=True, require_bind=True), platform="slack")
    store.update_channel("C-open", ChannelSettings(enabled=True), platform="slack")
    store.close()

    reloaded = SettingsStore(settings_path)
    try:
        assert reloaded.find_channel("C-bind", platform="slack").require_bind is True
        assert reloaded.find_channel("C-open", platform="slack").require_bind in (None, False)
    finally:
        reloaded.close()


def test_bound_and_enabled_user_checks_are_separate(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    store.set_users_for_platform(
        "slack",
        {
            "U-enabled": UserSettings(display_name="Enabled", enabled=True),
            "U-disabled": UserSettings(display_name="Disabled", enabled=False),
        },
    )

    assert store.is_bound_user("U-enabled", platform="slack") is True
    assert store.is_enabled_user("U-enabled", platform="slack") is True
    assert store.is_bound_user("U-disabled", platform="slack") is True
    assert store.is_enabled_user("U-disabled", platform="slack") is False

    store.close()


def test_admin_helpers_require_enabled_user(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    try:
        store.set_users_for_platform(
            "slack",
            {
                "U-enabled-admin": UserSettings(display_name="Enabled Admin", is_admin=True, enabled=True),
                "U-disabled-admin": UserSettings(display_name="Disabled Admin", is_admin=True, enabled=False),
            },
        )

        assert store.is_admin("U-enabled-admin", platform="slack") is True
        assert store.is_admin("U-disabled-admin", platform="slack") is False
        assert store.has_any_admin(platform="slack") is True
        assert store.has_enabled_admin(platform="slack") is True
        assert set(store.get_admins(platform="slack")) == {"slack::U-enabled-admin"}

        store.update_user(
            "U-enabled-admin",
            UserSettings(display_name="Enabled Admin", is_admin=True, enabled=False),
            platform="slack",
        )

        assert store.has_any_admin(platform="slack") is True
        assert store.has_enabled_admin(platform="slack") is False
        assert store.get_admins(platform="slack") == {}
    finally:
        store.close()


def test_bind_user_promotes_when_only_admin_is_disabled(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    try:
        store.set_users_for_platform(
            "slack",
            {
                "U-disabled-admin": UserSettings(display_name="Disabled Admin", is_admin=True, enabled=False),
            },
        )
        code = store.create_bind_code()

        success, is_admin = store.bind_user_with_code("U-new", "New Admin", code.code, platform="slack")

        assert success is True
        assert is_admin is True
        assert store.get_user("U-new", platform="slack").is_admin is True
    finally:
        store.close()


def test_disabled_user_cannot_rebind_with_active_code(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    try:
        store.set_users_for_platform(
            "slack",
            {
                "U-disabled": UserSettings(display_name="Disabled", enabled=False),
            },
        )
        code = store.create_bind_code()

        success, is_admin = store.bind_user_with_code("U-disabled", "Rebound", code.code, platform="slack")

        assert success is False
        assert is_admin is False
        assert store.get_user("U-disabled", platform="slack").enabled is False
    finally:
        store.close()


def test_settings_manager_runtime_save_preserves_require_bind(tmp_path: Path, monkeypatch) -> None:
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(paths, "ensure_data_dirs", lambda: None)

    manager = SettingsManager(settings_file=str(settings_path), platform="slack")
    try:
        manager.store.update_channel(
            "C-bind",
            ChannelSettings(enabled=True, require_mention=True, require_bind=True, custom_cwd="/old"),
            platform="slack",
        )
        settings = manager.get_user_settings("C-bind")
        settings.custom_cwd = "/new"
        manager.update_user_settings("C-bind", settings)

        reloaded = manager.store.find_channel("C-bind", platform="slack")
        assert reloaded is not None
        assert reloaded.custom_cwd == "/new"
        assert reloaded.require_mention is True
        assert reloaded.require_bind is True
    finally:
        manager.store.close()


def test_settings_store_reloads_external_sqlite_writes(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    external = SQLiteSettingsService(tmp_path / "vibe.sqlite")
    try:
        assert store.get_user("U1", platform="slack") is None

        external.save_state(
            SettingsState(
                users={
                    "slack::U1": UserSettings(display_name="Alex", is_admin=True),
                }
            )
        )

        store.maybe_reload()

        user = store.get_user("U1", platform="slack")
        assert user is not None
        assert user.display_name == "Alex"
        assert user.is_admin is True
    finally:
        external.close()
        store.close()


def test_settings_store_preserves_user_pending_bind_menu_hint(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    service = SQLiteSettingsService(db_path)
    try:
        service.save_state(
            SettingsState(
                users={
                    "wechat::wx-user": UserSettings(
                        display_name="WeChat User",
                        pending_bind_menu_hint=True,
                    ),
                }
            )
        )

        state = service.load_state()
    finally:
        service.close()

    user = state.users["wechat::wx-user"]
    assert user.pending_bind_menu_hint is True


def test_save_state_upserts_and_deletes_only_removed_channels(tmp_path: Path) -> None:
    """save_state updates existing rows in place and drops only the rows that
    left the state — it must never wipe and rebuild the whole table."""
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    service = SQLiteSettingsService(db_path)
    try:
        service.save_state(
            SettingsState(
                channels={
                    "slack::A": ChannelSettings(enabled=True, custom_cwd="/a"),
                    "slack::B": ChannelSettings(enabled=True, custom_cwd="/b"),
                }
            )
        )
        assert set(service.load_state().channels) == {"slack::A", "slack::B"}

        # Remove B; change A in place.
        service.save_state(
            SettingsState(
                channels={
                    "slack::A": ChannelSettings(enabled=False, custom_cwd="/a2"),
                }
            )
        )
        reloaded = service.load_state()
    finally:
        service.close()

    assert set(reloaded.channels) == {"slack::A"}  # B was removed
    assert reloaded.channels["slack::A"].custom_cwd == "/a2"  # A updated in place
    assert reloaded.channels["slack::A"].enabled is False


def test_save_state_preserves_project_scope_settings(tmp_path: Path) -> None:
    """Regression: an avibe project's settings (its workdir) live in the same
    scope_settings table but are owned by projects_service. A settings save must
    NOT delete them — the old full-table clear did, which lost project folders."""
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    engine = create_sqlite_engine(db_path)
    folder = tmp_path / "project-dir"
    folder.mkdir()

    with engine.begin() as conn:
        project = projects_service.create_project(conn, str(folder), display_name="Proj")

    service = SQLiteSettingsService(db_path)
    try:
        service.save_state(
            SettingsState(
                channels={"slack::C1": ChannelSettings(enabled=True, custom_cwd="/c1")},
                users={"slack::U1": UserSettings(display_name="Alex", is_admin=True)},
            )
        )
    finally:
        service.close()

    with engine.begin() as conn:
        row = conn.execute(
            select(scope_settings.c.workdir).where(scope_settings.c.scope_id == project["scope_id"])
        ).first()

    assert row is not None, "project scope_settings was deleted by a settings save"
    assert row[0] == str(folder.resolve())


def test_settings_save_preserves_observed_scope_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            upsert_scope(
                conn,
                "telegram",
                "channel",
                "123",
                display_name="General",
                native_type="supergroup",
                is_private=True,
                supports_threads=True,
                metadata={"username": "general"},
                now="2026-05-01T00:00:00+00:00",
            )
    finally:
        engine.dispose()

    service = SQLiteSettingsService(db_path)
    try:
        service.save_state(
            SettingsState(
                channels={
                    "telegram::123": ChannelSettings(enabled=True),
                }
            )
        )
    finally:
        service.close()

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                scopes.select().where(scopes.c.id == "telegram::channel::123"),
            ).mappings().one()
    finally:
        engine.dispose()

    assert row["native_type"] == "supergroup"
    assert row["is_private"] == 1
    assert row["supports_threads"] == 1
    assert json.loads(row["metadata_json"]) == {"username": "general"}


def test_settings_save_does_not_migrate_legacy_model_fields_without_backend(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    service = SQLiteSettingsService(db_path)
    try:
        service.save_state(
            SettingsState(
                channels={
                    "slack::C123": ChannelSettings(
                        enabled=True,
                        routing=RoutingSettings(
                            agent_name=None,
                            codex_model="gpt-stale-codex",
                            claude_model="claude-stale",
                            opencode_model="openai/stale",
                            codex_reasoning_effort="high",
                            claude_reasoning_effort="medium",
                            opencode_reasoning_effort="low",
                        ),
                    ),
                }
            )
        )

        state = service.load_state()
    finally:
        service.close()

    routing = state.channels["slack::C123"].routing
    assert routing.model is None
    assert routing.reasoning_effort is None
    assert routing.codex_model == "gpt-stale-codex"
    assert routing.claude_model == "claude-stale"
    assert routing.opencode_model == "openai/stale"


def test_settings_save_lifts_backend_aliases_for_builtin_agent_name(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    service = SQLiteSettingsService(db_path)
    try:
        service.save_state(
            SettingsState(
                channels={
                    "slack::C123": ChannelSettings(
                        enabled=True,
                        routing=RoutingSettings(
                            agent_name="claude",
                            claude_model="claude-opus-4-8",
                            claude_reasoning_effort="max",
                        ),
                    ),
                }
            )
        )

        state = service.load_state()
    finally:
        service.close()

    routing = state.channels["slack::C123"].routing
    assert routing.model == "claude-opus-4-8"
    assert routing.reasoning_effort == "max"


def test_settings_load_ignores_row_model_without_agent_name(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    engine = create_sqlite_engine(db_path)
    service = None
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(
                conn,
                "slack",
                "channel",
                "C123",
                display_name=None,
                native_type=None,
                is_private=False,
                supports_threads=True,
                metadata={},
                now="now",
            )
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=1,
                    role=None,
                    workdir=None,
                    agent_name=None,
                    agent_backend="codex",
                    agent_variant=None,
                    model="gpt-stale-codex",
                    reasoning_effort="high",
                    require_mention=None,
                    settings_version=1,
                    settings_json=json.dumps({"routing": {"agent_backend": "codex"}, "require_bind": None}),
                    created_at="now",
                    updated_at="now",
                )
            )
        service = SQLiteSettingsService(db_path)
        state = service.load_state()
    finally:
        if service is not None:
            service.close()
        engine.dispose()

    routing = state.channels["slack::C123"].routing
    assert routing.agent_name is None
    assert routing.model is None
    assert routing.reasoning_effort is None


def test_settings_store_bootstrap_uses_config_primary_platform(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_config_path().write_text(
        json.dumps({"platform": "discord", "platforms": {"enabled": ["discord"], "primary": "discord"}}),
        encoding="utf-8",
    )
    sessions_path = paths.get_sessions_path()
    sessions_path.write_text(
        json.dumps(
            {
                "session_mappings": {"G123": {"codex": {"1774074591.762089:/repo": "session-1"}}},
                "active_polls": {
                    "oc-1": {
                        "opencode_session_id": "oc-1",
                        "base_session_id": "base-1",
                        "channel_id": "G123",
                        "thread_id": "1774074591.762089",
                        "settings_key": "G123",
                        "working_path": "/repo",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    store = SettingsStore(paths.get_settings_path())
    sessions = SQLiteSessionsService(paths.get_sqlite_state_path())
    try:
        state = sessions.load_state()
        assert "discord::G123" in state.session_mappings
        assert state.active_polls["oc-1"]["platform"] == "discord"
    finally:
        sessions.close()
        store.close()


def test_settings_store_custom_path_uses_sibling_config_primary_platform(tmp_path: Path) -> None:
    root = tmp_path / "custom-home"
    state_dir = root / "state"
    config_dir = root / "config"
    state_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"platform": "discord", "platforms": {"enabled": ["discord"], "primary": "discord"}}),
        encoding="utf-8",
    )
    (state_dir / "sessions.json").write_text(
        json.dumps(
            {
                "session_mappings": {"G456": {"codex": {"1774074591.762089:/repo": "session-1"}}},
                "active_polls": {
                    "oc-2": {
                        "opencode_session_id": "oc-2",
                        "base_session_id": "base-2",
                        "channel_id": "G456",
                        "thread_id": "1774074591.762089",
                        "settings_key": "G456",
                        "working_path": "/repo",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    store = SettingsStore(state_dir / "settings.json")
    sessions = SQLiteSessionsService(state_dir / "vibe.sqlite")
    try:
        state = sessions.load_state()
        assert "discord::G456" in state.session_mappings
        assert state.active_polls["oc-2"]["platform"] == "discord"
    finally:
        sessions.close()
        store.close()
