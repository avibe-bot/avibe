from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from config import paths
from config import v2_settings
from config.v2_sessions import SessionsStore
from config.v2_settings import ChannelSettings, RoutingSettings, SettingsState, SettingsStore, UserSettings
from core import chat_discovery
from core.vibe_agents import VibeAgentStore
from storage import agent_events_service, messages_service, projects_service
from storage.background import SQLiteBackgroundTaskStore
from storage.db import create_sqlite_engine
from storage.migrations import run_migrations
from storage.models import scope_settings, scopes
from storage.sessions_service import SQLiteSessionsService
from storage.settings_service import (
    SQLiteSettingsService,
    ScopeAgentUnavailableError,
    StaleScopeAgentBindingError,
    upsert_scope,
)
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


def test_telegram_thread_settings_round_trip_and_parent_fallback(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    agent_store = VibeAgentStore(tmp_path / "vibe.sqlite")
    agent_store.create(name="reviewer", backend="codex")
    store = SettingsStore(settings_path)
    parent = ChannelSettings(enabled=True, require_mention=True, require_bind=False)
    topic = ChannelSettings(
        enabled=True,
        show_message_types=["assistant", "toolcall"],
        custom_cwd="/topics/release",
        routing=RoutingSettings(agent_name="reviewer", model="gpt-5.4"),
        require_mention=False,
        require_bind=True,
    )
    store.update_channel("-1001", parent, platform="telegram")
    store.update_thread("-1001", "42", topic, platform="telegram")
    store.close()

    reloaded = SettingsStore(settings_path)
    try:
        effective = reloaded.find_effective_channel("-1001", thread_id="42", platform="telegram")
        inherited = reloaded.find_effective_channel("-1001", thread_id="99", platform="telegram")
        assert effective is not None
        assert effective.require_mention is False
        assert effective.require_bind is True
        assert effective.show_message_types == ["assistant", "toolcall"]
        assert effective.routing.agent_name == "reviewer"
        assert effective.custom_cwd == "/topics/release"
        assert inherited is not None
        assert inherited.require_mention is True
        assert reloaded.get_threads_for_platform("telegram")["-1001"]["42"] == effective

        assert reloaded.delete_thread("-1001", "42", platform="telegram") is True
        assert reloaded.find_effective_channel("-1001", thread_id="42", platform="telegram").require_mention is True
    finally:
        reloaded.close()
        agent_store.close()


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


def test_create_bind_code_uses_high_entropy_format(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    try:
        code = store.create_bind_code().code

        assert code.startswith("vr-")
        random_part = code.removeprefix("vr-")
        assert len(random_part) >= 8
        assert set(random_part) <= set(v2_settings._BIND_CODE_ALPHABET)
    finally:
        store.close()


def test_validate_bind_code_uses_constant_time_compare(tmp_path: Path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    try:
        first_code = store.create_bind_code()
        bind_code = store.create_bind_code()
        comparisons: list[tuple[str, str]] = []

        def compare_digest(left: str, right: str) -> bool:
            comparisons.append((left, right))
            return left == right

        monkeypatch.setattr(v2_settings.hmac, "compare_digest", compare_digest)

        assert store.validate_bind_code(bind_code.code) == bind_code
        assert comparisons == [(first_code.code, bind_code.code), (bind_code.code, bind_code.code)]
    finally:
        store.close()


def test_create_bind_code_limits_active_codes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(v2_settings, "_MAX_ACTIVE_BIND_CODES", 2)
    store = SettingsStore(tmp_path / "settings.json")
    try:
        first = store.create_bind_code()
        store.create_bind_code()

        with pytest.raises(ValueError, match="active bind code limit reached"):
            store.create_bind_code()

        assert store.deactivate_bind_code(first.code) is True
        assert store.create_bind_code().is_active is True
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


def test_settings_manager_topic_override_materializes_inherited_mention_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(paths, "ensure_data_dirs", lambda: None)

    manager = SettingsManager(settings_file=str(settings_path), platform="telegram")
    manager.require_mention_default = lambda: False
    try:
        manager.store.update_channel(
            "-1001",
            ChannelSettings(enabled=True, require_mention=None),
            platform="telegram",
        )
        settings_key = v2_settings.make_thread_settings_key("-1001", "42")
        settings = manager.get_user_settings(settings_key)
        settings.custom_cwd = "/topic"

        manager.update_user_settings(settings_key, settings)

        topic = manager.store.find_thread("-1001", "42", platform="telegram")
        assert topic is not None
        assert topic.custom_cwd == "/topic"
        assert topic.require_mention is False
    finally:
        manager.store.close()


def test_settings_manager_topic_mention_inherit_materializes_live_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # Scenario: TELEGRAM-TOPIC-001
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(paths, "ensure_data_dirs", lambda: None)

    manager = SettingsManager(settings_file=str(settings_path), platform="telegram")
    current_default = {"value": False}
    manager.require_mention_default = lambda: current_default["value"]
    try:
        manager.store.update_channel(
            "-1001",
            ChannelSettings(enabled=True, require_mention=None),
            platform="telegram",
        )
        manager.set_require_mention(v2_settings.make_thread_settings_key("-1001", "42"), None)
        current_default["value"] = True
        manager.set_require_mention(v2_settings.make_thread_settings_key("-1001", "43"), None)

        first = manager.store.find_thread("-1001", "42", platform="telegram")
        second = manager.store.find_thread("-1001", "43", platform="telegram")
        assert first is not None and first.require_mention is False
        assert second is not None and second.require_mention is True
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


def test_runtime_settings_ignore_agent_harness_and_project_writes(
    tmp_path: Path, monkeypatch
) -> None:
    settings_path = tmp_path / "settings.json"
    db_path = tmp_path / "vibe.sqlite"
    monkeypatch.setattr(paths, "ensure_data_dirs", lambda: None)
    SettingsStore.reset_instance()
    run_migrations(db_path)
    sessions = SessionsStore(tmp_path / "sessions.json")
    seed = SQLiteSettingsService(db_path)
    seed.save_state(
        SettingsState(channels={"slack::C1": ChannelSettings(enabled=True)})
    )
    seed.close()
    manager = SettingsManager(
        settings_file=str(settings_path),
        platform="slack",
        sessions_store=sessions,
    )
    engine = create_sqlite_engine(db_path)
    harness = SQLiteBackgroundTaskStore(db_path)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    try:
        initial = manager.runtime_settings_diagnostics()
        with engine.begin() as conn:
            agent_events_service.append(
                conn,
                scope_id=None,
                session_id=None,
                platform="slack",
                event_type="tool_call",
                text="bounded test event",
            )
            messages_service.append(
                conn,
                scope_id=None,
                session_id=None,
                platform="slack",
                author="agent",
                message_type="assistant",
                text="bounded test message",
                source="agent",
            )
            projects_service.create_project(conn, str(project_dir), display_name="Project")
        assert harness.upsert_watch(
            {
                "id": "watch-settings-invalidation",
                "name": "settings invalidation",
                "shell_command": "true",
                "enabled": True,
                "created_at": "2026-08-29T00:00:00Z",
                "updated_at": "2026-08-29T00:00:00Z",
            }
        )

        assert manager.get_user_settings("C1").enabled is True
        assert manager.runtime_settings_diagnostics() == initial
    finally:
        harness.close()
        engine.dispose()
        sessions.close()
        SettingsStore.reset_instance()


def test_legitimate_settings_changes_coalesce_under_concurrent_reads(
    tmp_path: Path, monkeypatch
) -> None:
    settings_path = tmp_path / "settings.json"
    db_path = tmp_path / "vibe.sqlite"
    monkeypatch.setattr(paths, "ensure_data_dirs", lambda: None)
    SettingsStore.reset_instance()
    sessions = SessionsStore(tmp_path / "sessions.json")
    external = SQLiteSettingsService(db_path)
    external.save_state(
        SettingsState(
            channels={
                "slack::C1": ChannelSettings(enabled=True, custom_cwd="/initial"),
                "slack::C2": ChannelSettings(enabled=True, custom_cwd="/steady"),
            },
            users={
                "slack::U1": UserSettings(
                    display_name="Alex", enabled=True, custom_cwd="/dm-initial"
                )
            },
            guild_scope_platforms={"slack"},
            guild_default_enabled={"slack": False},
        )
    )
    manager = SettingsManager(
        settings_file=str(settings_path),
        platform="slack",
        sessions_store=sessions,
    )
    try:
        unchanged_channel = manager.channel_settings["C2"]
        for custom_cwd in ("/first", "/second", "/final"):
            external.save_state(
                SettingsState(
                    channels={
                        "slack::C1": ChannelSettings(enabled=True, custom_cwd=custom_cwd),
                        "slack::C2": ChannelSettings(enabled=True, custom_cwd="/steady"),
                    },
                    users={
                        "slack::U1": UserSettings(
                            display_name="Alex", enabled=True, custom_cwd="/dm"
                        )
                    },
                    guild_scope_platforms={"slack"},
                    guild_default_enabled={"slack": True},
                )
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: manager.get_user_settings("C1"), range(16)))

        assert {settings.custom_cwd for settings in results} == {"/final"}
        assert manager.get_user_settings("U1").custom_cwd == "/dm"
        assert manager.store.get_guild_default_enabled_for_platform("slack") is True
        assert manager.channel_settings["C2"] is unchanged_channel
        assert manager.runtime_settings_diagnostics() == {
            "store_reload_count": 1,
            "rebuild_count": 2,
            "changed_channels": 1,
            "changed_dm_users": 1,
            "channels": 2,
            "dm_users": 1,
        }
    finally:
        external.close()
        sessions.close()
        SettingsStore.reset_instance()


def test_settings_save_does_not_absorb_a_newer_external_revision(
    tmp_path: Path, monkeypatch
) -> None:
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    external = SQLiteSettingsService(tmp_path / "vibe.sqlite")
    original_save = store._service.save_state

    def save_then_race(state: SettingsState) -> str:
        committed_revision = original_save(state)
        external.save_state(
            SettingsState(
                channels={
                    "slack::C1": ChannelSettings(enabled=True, custom_cwd="/external")
                }
            )
        )
        return committed_revision

    monkeypatch.setattr(store._service, "save_state", save_then_race)
    try:
        store.settings.channels = {
            "slack::C1": ChannelSettings(enabled=True, custom_cwd="/local")
        }
        store.save()

        assert store.maybe_reload() is True
        assert store.find_channel("C1", platform="slack").custom_cwd == "/external"
        assert store.reload_count == 1
    finally:
        external.close()
        store.close()


def test_scope_delete_and_agent_rename_publish_settings_revisions(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    agents_store = VibeAgentStore(tmp_path / "vibe.sqlite")
    try:
        agents_store.create(name="reviewer", backend="codex")
        store.update_channel(
            "C1",
            ChannelSettings(
                enabled=True,
                routing=RoutingSettings(agent_name="reviewer"),
            ),
            platform="slack",
        )

        agents_store.rename("reviewer", "renamed")
        assert store.maybe_reload() is True
        assert store.find_channel("C1", platform="slack").routing.agent_name == "renamed"

        assert chat_discovery.delete_scope("slack", "C1", db_path=store.db_path) == {
            "removed": True,
            "dismissed": False,
        }
        assert store.maybe_reload() is True
        assert store.find_channel("C1", platform="slack") is None
    finally:
        agents_store.close()
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


def test_settings_save_serializes_and_reconciles_stale_agent_bindings(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    agents_store = VibeAgentStore(tmp_path / "vibe.sqlite")
    conflicting_store = None
    try:
        agents_store.create(name="pm", backend="claude")
        agents_store.create(name="zz-fallback", backend="claude")
        route = RoutingSettings(agent_name="pm")
        store.update_channel(
            "C1",
            ChannelSettings(enabled=True, routing=route),
            platform="slack",
        )
        store.update_thread(
            "C1",
            "T1",
            ChannelSettings(enabled=True, routing=RoutingSettings(agent_name="pm")),
            platform="slack",
        )
        store.update_user(
            "U1",
            UserSettings(display_name="Pat", routing=RoutingSettings(agent_name="pm")),
            platform="slack",
        )

        race: dict[str, object] = {"fired": 0, "refused": [], "committed": 0}

        @event.listens_for(agents_store.engine, "checkout")
        def _no_wait(dbapi_connection, *_args) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA busy_timeout = 0")
            cursor.close()

        @event.listens_for(store._service.engine, "after_cursor_execute")
        def _archive_on_binding_read(
            _conn, _cursor, statement, _parameters, _context, _executemany
        ) -> None:
            normalized = " ".join(statement.split())
            if race["fired"] or "SELECT scope_settings.agent_name" not in normalized:
                return
            race["fired"] = 1
            try:
                agents_store.archive("pm")
            except OperationalError as exc:
                race["refused"].append(str(exc))
            else:
                race["committed"] = 1

        store.save()
        assert race["fired"] == 1
        assert race["committed"] == 0
        assert len(race["refused"]) == 1

        conflicting_store = SettingsStore(settings_path)
        archived = agents_store.archive("pm")
        assert archived is not None
        replacement = agents_store.create(name="pm", backend="codex")

        conflicting = conflicting_store.find_channel("C1", platform="slack")
        assert conflicting is not None
        conflicting.routing.agent_name = "codex"
        with pytest.raises(StaleScopeAgentBindingError) as exc:
            conflicting_store.update_channel("C1", conflicting, platform="slack")
        assert exc.value.code == "settings_conflict"
        assert exc.value.scope_id == "slack::channel::C1"
        refreshed_conflict = conflicting_store.find_channel("C1", platform="slack")
        assert refreshed_conflict is not None
        assert refreshed_conflict.routing.agent_name == archived.archived_name

        store.settings.channels["slack::C1"].custom_cwd = "/stale-channel"
        store.settings.threads["slack::C1/T1"].custom_cwd = "/stale-thread"
        store.settings.users["slack::U1"].custom_cwd = "/stale-user"
        store.save()
        assert store.settings.channels["slack::C1"].routing.agent_name == archived.archived_name
        assert store.settings.threads["slack::C1/T1"].routing.agent_name == archived.archived_name
        assert store.settings.users["slack::U1"].routing.agent_name == archived.archived_name

        with agents_store.engine.connect() as conn:
            rows = conn.execute(
                select(
                    scopes.c.scope_type,
                    scope_settings.c.agent_name,
                    scope_settings.c.workdir,
                    scope_settings.c.settings_json,
                )
                .select_from(scopes.join(scope_settings, scope_settings.c.scope_id == scopes.c.id))
                .where(scopes.c.id.in_(("slack::channel::C1", "slack::thread::C1/T1", "slack::user::U1")))
                .order_by(scopes.c.scope_type)
            ).mappings().all()
        assert len(rows) == 3
        assert {row["agent_name"] for row in rows} == {archived.archived_name}
        assert {row["workdir"] for row in rows} == {
            "/stale-channel",
            "/stale-thread",
            "/stale-user",
        }
        assert {
            json.loads(row["settings_json"])["routing"]["agent_name"] for row in rows
        } == {archived.archived_name}

        store.close()
        fresh = SettingsStore(settings_path)
        try:
            channel = fresh.find_channel("C1", platform="slack")
            assert channel is not None
            channel.routing.agent_name = replacement.name
            fresh.update_channel("C1", channel, platform="slack")
        finally:
            fresh.close()

        with agents_store.engine.connect() as conn:
            rebound = conn.execute(
                select(scope_settings.c.agent_name).where(
                    scope_settings.c.scope_id == "slack::channel::C1"
                )
            ).scalar_one()
        assert rebound == replacement.name
    finally:
        if conflicting_store is not None:
            conflicting_store.close()
        store.close()
        agents_store.close()


def test_client_binding_expectation_survives_server_reload_before_scope_save(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    initial = SettingsStore(settings_path)
    agent_store = VibeAgentStore(tmp_path / "vibe.sqlite")
    reloaded = None
    try:
        original = agent_store.create(name="pm", backend="claude")
        agent_store.create(name="zz-fallback", backend="claude")
        initial.update_channel(
            "C1",
            ChannelSettings(enabled=True, routing=RoutingSettings(agent_name=original.name)),
            platform="slack",
        )

        archived = agent_store.archive(original.name)
        assert archived is not None
        replacement = agent_store.create(name="pm", backend="codex")
        reloaded = SettingsStore(settings_path)
        stale_form = ChannelSettings(
            enabled=True,
            custom_cwd="/saved-after-reload",
            routing=RoutingSettings(agent_name="pm"),
            _agent_name_at_load="pm",
        )

        reloaded.set_channels_for_platform("slack", {"C1": stale_form})
        reloaded.save()

        assert stale_form.routing.agent_name == archived.archived_name
        with agent_store.engine.connect() as conn:
            stored = conn.execute(
                select(scope_settings.c.agent_name, scope_settings.c.workdir).where(
                    scope_settings.c.scope_id == "slack::channel::C1"
                )
            ).one()
        assert stored == (archived.archived_name, "/saved-after-reload")
        assert stored.agent_name != replacement.name
    finally:
        if reloaded is not None:
            reloaded.close()
        initial.close()
        agent_store.close()


def test_settings_save_rejects_new_archived_binding_but_preserves_existing(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    agent_store = VibeAgentStore(tmp_path / "vibe.sqlite")
    try:
        agent_store.create(name="pm", backend="claude")
        agent_store.create(name="zz-fallback", backend="claude")
        store.update_channel(
            "C1",
            ChannelSettings(enabled=True, routing=RoutingSettings(agent_name="pm")),
            platform="slack",
        )
        archived = agent_store.archive("pm")
        assert archived is not None

        reloaded = SettingsStore(settings_path)
        try:
            existing = reloaded.find_channel("C1", platform="slack")
            assert existing is not None
            assert existing.routing.agent_name == archived.archived_name
            existing.custom_cwd = "/preserved"
            reloaded.save()

            with pytest.raises(ScopeAgentUnavailableError) as unavailable:
                reloaded.update_channel(
                    "C2",
                    ChannelSettings(
                        enabled=True,
                        routing=RoutingSettings(agent_name=archived.archived_name),
                    ),
                    platform="slack",
                )
            assert unavailable.value.code == "agent_unavailable"
            assert unavailable.value.agent_name == archived.archived_name
            assert reloaded.find_channel("C2", platform="slack") is None
        finally:
            reloaded.close()

        with agent_store.engine.connect() as conn:
            rows = conn.execute(
                select(scope_settings.c.scope_id, scope_settings.c.agent_name, scope_settings.c.workdir)
                .where(scope_settings.c.scope_id.in_(("slack::channel::C1", "slack::channel::C2")))
                .order_by(scope_settings.c.scope_id)
            ).all()
        assert rows == [("slack::channel::C1", archived.archived_name, "/preserved")]
    finally:
        agent_store.close()
        store.close()


def test_settings_save_canonicalizes_normalized_agent_binding(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    agent_store = VibeAgentStore(tmp_path / "vibe.sqlite")
    try:
        agent_store.create(name="Project Manager", backend="claude")

        settings = ChannelSettings(
            enabled=True,
            routing=RoutingSettings(agent_name="PROJECT-MANAGER"),
        )
        store.update_channel("C1", settings, platform="slack")

        assert settings.routing.agent_name == "Project Manager"
        with agent_store.engine.connect() as conn:
            stored_name = conn.execute(
                select(scope_settings.c.agent_name).where(
                    scope_settings.c.scope_id == "slack::channel::C1"
                )
            ).scalar_one()
        assert stored_name == "Project Manager"
    finally:
        agent_store.close()
        store.close()


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


def test_settings_save_stores_only_active_builtin_agent_variant(tmp_path: Path) -> None:
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
                            agent_name="opencode",
                            codex_agent="stale-codex-profile",
                            opencode_agent=None,
                        ),
                    ),
                }
            )
        )
        state = service.load_state()
    finally:
        service.close()

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(scope_settings.c.agent_variant, scope_settings.c.settings_json)
                .select_from(scope_settings)
                .join(scopes, scopes.c.id == scope_settings.c.scope_id)
                .where(scopes.c.platform == "slack", scopes.c.native_id == "C123")
            ).one()
    finally:
        engine.dispose()

    assert row.agent_variant is None
    assert state.channels["slack::C123"].routing.codex_agent == "stale-codex-profile"
    assert json.loads(row.settings_json)["routing"]["codex_agent"] == "stale-codex-profile"


def test_settings_save_uses_matching_builtin_agent_variant(tmp_path: Path) -> None:
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
                            agent_name="codex",
                            codex_agent="active-codex-profile",
                            opencode_agent="stale-opencode-profile",
                        ),
                    ),
                }
            )
        )
    finally:
        service.close()

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(scope_settings.c.agent_variant)
                .select_from(scope_settings)
                .join(scopes, scopes.c.id == scope_settings.c.scope_id)
                .where(scopes.c.platform == "slack", scopes.c.native_id == "C123")
            ).one()
    finally:
        engine.dispose()

    assert row.agent_variant == "active-codex-profile"


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
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
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


def test_sqlite_filters_deprecated_system_message_type(tmp_path: Path) -> None:
    """The deprecated "system" message type must not survive the SQLite settings
    round-trip, and a legacy raw row that still contains it is filtered on load.

    Regression for the Codex review on PR #638: the SQLite store load/save path
    bypassed message-type normalization, so stored "system" leaked through
    store-level APIs (e.g. /api/users) until the row was edited.
    """
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    service = SQLiteSettingsService(db_path)
    try:
        # save-normalize: "system" is stripped before it is persisted.
        service.save_state(
            SettingsState(
                channels={
                    "slack::C1": ChannelSettings(
                        enabled=True, show_message_types=["system", "assistant"]
                    )
                },
                users={
                    "slack::U1": UserSettings(
                        display_name="Alex", enabled=True, show_message_types=["system", "toolcall"]
                    )
                },
            )
        )
        saved = service.load_state()
        assert saved.channels["slack::C1"].show_message_types == ["assistant"]
        assert saved.users["slack::U1"].show_message_types == ["toolcall"]

        # Simulate legacy rows written before the deprecation by injecting raw
        # "system" back into settings_json, bypassing save-normalize.
        engine = create_sqlite_engine(db_path)
        with engine.begin() as conn:
            for row in conn.execute(
                select(scope_settings.c.scope_id, scope_settings.c.settings_json)
            ).all():
                payload = json.loads(row.settings_json)
                if "show_message_types" in payload:
                    payload["show_message_types"] = ["system", *payload["show_message_types"]]
                    conn.execute(
                        scope_settings.update()
                        .where(scope_settings.c.scope_id == row.scope_id)
                        .values(settings_json=json.dumps(payload))
                    )
        engine.dispose()
    finally:
        service.close()

    # load-normalize: a fresh reader drops the legacy "system" value.
    reader = SQLiteSettingsService(db_path)
    try:
        legacy = reader.load_state()
    finally:
        reader.close()
    assert legacy.channels["slack::C1"].show_message_types == ["assistant"]
    assert legacy.users["slack::U1"].show_message_types == ["toolcall"]
