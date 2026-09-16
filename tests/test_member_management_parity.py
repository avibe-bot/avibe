"""Instance management preserves admission policy through real remote HTTP writes.

Scenario: PERMISSIONS-013. All persistent state belongs to pytest's home;
only provider/host/runtime effects are stubbed, after the authorization seam.
"""

from __future__ import annotations

import copy
import contextlib
import json
import re
from pathlib import Path

import pytest

from config import paths
from config.v2_config import V2Config
from config.v2_settings import BindCode, ChannelSettings, GuildSettings, SettingsStore, UserSettings
from storage.importer import ensure_sqlite_state
from tests.ui_server_test_helpers import _save_config, csrf_headers, remote_peer, remote_session_cookie
from vibe import api, internal_client, remote_access, ui_server
from vibe.authorization import AuthorizationContext, InstanceAuthorizationError, http_authorization_policy

BASE_URL = "https://alex.avibe.bot"
MEMBER = AuthorizationContext(
    instance_role="member",
    subject="manager",
    instance_access_source="organization_group",
    instance_kind="organization",
    organization_id="org-1",
    organization_member_id="membership-manager",
    organization_role="member",
    group_ids=frozenset(),
    is_remote=True,
)


@pytest.fixture
def management_http(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path, paired=True, instance_kind="organization")
    ensure_sqlite_state()
    SettingsStore.reset_instance()

    async def reconciled(*args, **kwargs):
        return {"status_code": 200, "body": {"ok": True}}

    monkeypatch.setattr(internal_client, "reconcile_platforms", reconciled)
    monkeypatch.setattr(internal_client, "reconcile_agent_backends", reconciled)
    monkeypatch.setattr(ui_server, "_ensure_remote_access_monitoring", lambda *args: None)

    def connect(role="member", *, organization_role="member", authenticated=True):
        client = ui_server.app.test_client()
        if authenticated:
            client.set_cookie(
                remote_access.SESSION_COOKIE_NAME,
                remote_session_cookie(
                    config,
                    f"{role}@example.com",
                    role,
                    role=role,
                    access_source="owner" if role == "owner" else "organization_group",
                    organization_id="org-1",
                    organization_member_id=f"membership-{role}",
                    organization_role=organization_role,
                    group_ids=[],
                ),
                domain="alex.avibe.bot",
            )
        headers = csrf_headers(client, base_url=BASE_URL)

        def request(method, route, *, payload=None, csrf=True, origin=None):
            request_headers = dict(headers) if csrf else {}
            if origin is not None:
                request_headers["Origin"] = origin
            return client.request(
                method,
                route,
                json=payload,
                headers=request_headers,
                base_url=BASE_URL,
                environ_base=remote_peer(),
            )

        return request

    yield connect
    SettingsStore.reset_instance()


# This inventory describes retained effects independently of the policy rules.
# A newly registered Owner-only management route must be consciously classified.
_RETAINED_OWNER_ROUTES = {
    ("POST", "/api/model-service/refresh"),  # Internal signed CLI notification, not a settings API.
    ("PUT", "/api/permissions/authorized-users"),
    ("PUT", "/api/permissions/projects/{project_id}/access"),
    ("PUT", "/api/permissions/resources/{resource_kind}/{resource_id}/access"),
    ("GET", "/api/agent-onboarding"),
    ("POST", "/api/agent-onboarding"),
    ("POST", "/api/remote-access/vibe-cloud/pair"),
    ("POST", "/api/wechat/qr_login/start"),
    ("POST", "/api/wechat/qr_login/poll"),
    ("POST", "/api/users/{user_id}/admin"),
    ("DELETE", "/api/users/{user_id}"),
    ("GET", "/api/bind-codes"),
    ("POST", "/api/bind-codes"),
    ("DELETE", "/api/bind-codes/{code}"),
    ("GET", "/api/setup/first-bind-code"),
}


def test_complete_registered_api_management_inventory():
    endpoints = {
        (method, route.path)
        for route in ui_server.app.routes
        if getattr(route, "path", "").startswith("/api/")
        for method in (getattr(route, "methods", None) or ())
        if method not in {"HEAD", "OPTIONS"}
    }
    assert len(endpoints) == 291
    # Frozen from 1b191200c: independently protect all existing lower-tier grants.
    baseline_read_roles = json.loads(
        (Path(__file__).parent / "fixtures/member_management_existing_read_roles.json").read_text()
    )
    actual_read_roles = {}
    owners = {
        (method, raw)
        for method, raw in endpoints
        if http_authorization_policy(method, re.sub(r"\{[^}]+\}", "example", raw)).minimum_role == "owner"
    }
    assert owners == _RETAINED_OWNER_ROUTES
    for method, raw in endpoints:
        path = re.sub(r"\{[^}]+\}", "example", raw)
        minimum = http_authorization_policy(method, path).minimum_role
        if minimum in {"viewer", "editor"}:
            actual_read_roles[f"{method} {path}"] = minimum
        for kind in ("personal", "organization"):
            for role in ("owner", "member", "editor", "viewer", None):
                context = AuthorizationContext(
                    instance_role=role,
                    instance_kind=kind,
                    organization_role="owner",
                    is_remote=True,
                )
                if role in {"owner", "member"}:
                    assert context.has_role(minimum) == (role == "owner" or (method, raw) not in _RETAINED_OWNER_ROUTES)
                if role in {"editor", "viewer"}:
                    old_minimum = baseline_read_roles.get(f"{method} {path}")
                    expected = old_minimum == "viewer" or (old_minimum == "editor" and role == "editor")
                    assert context.has_role(minimum) is expected, (role, method, path)
                if role is None:
                    assert not context.has_role(minimum), (method, raw)
    assert actual_read_roles == baseline_read_roles


@pytest.mark.parametrize("role", ["member", "owner"])
def test_manager_saves_config_credentials_and_reads_masked_result(management_http, role):
    request = management_http(role)
    before = V2Config.load().remote_access
    response = request(
        "POST",
        "/api/config",
        payload={
            "slack": {"bot_token": "xoxb-test-member-token", "app_token": "xapp-test-member-token"},
            "runtime": {"log_level": "DEBUG"},
        },
    )
    assert response.status_code == 200, response.get_json()
    saved = V2Config.load()
    assert saved.slack.bot_token == "xoxb-test-member-token"
    assert saved.runtime.log_level == "DEBUG"
    assert saved.remote_access == before
    response = request("GET", "/api/config")
    assert response.status_code == 200
    assert "xoxb-test-member-token" not in json.dumps(response.get_json())
    assert response.get_json()["runtime"]["log_level"] == "DEBUG"


@pytest.mark.parametrize(
    "payload",
    [
        {"slack": {"require_bind": True}},
        {"telegram": {"allowed_user_ids": ["attacker"]}},
        {"telegram": {"allowed_chat_ids": ["attacker"]}},
        {"ui": {"trusted_public_origins": ["https://attacker.example"]}},
        {"remote_access": {"vibe_cloud": {"instance_id": "other"}}},
        {"__avibe_list_ops": {"telegram.allowed_user_ids": {"add": ["attacker"]}}},
        {"discord": {"guild_allowlist": ["attacker"]}},
    ],
)
def test_member_generic_config_protects_access_fields(management_http, payload):
    request = management_http()
    before = paths.get_config_path().read_bytes()
    response = request("POST", "/api/config", payload=payload)
    assert response.status_code in {400, 403}, response.get_json()
    assert paths.get_config_path().read_bytes() == before


def test_legacy_discord_admission_survives_member_credential_migration(management_http):
    request = management_http()
    config_payload = json.loads(paths.get_config_path().read_text())
    config_payload["discord"] = {"bot_token": "test-token", "guild_denylist": ["blocked"]}
    paths.get_config_path().write_text(json.dumps(config_payload))
    response = request("POST", "/api/config", payload={"discord": {"bot_token": "new-test-token"}})
    assert response.status_code == 200, response.get_json()
    store = SettingsStore.get_instance()
    assert not store.is_guild_enabled("discord", "blocked")
    assert store.is_guild_enabled("discord", "other")
    response = request("POST", "/api/config", payload={"discord": {"guild_denylist": []}})
    assert response.status_code == 403, response.get_json()
    assert not store.is_guild_enabled("discord", "blocked")


def test_member_config_equal_protected_echo_is_valid_but_omission_cannot_reset(management_http):
    request = management_http()
    config = V2Config.load()
    config.slack.require_bind = True
    config.save()
    assert (
        request("POST", "/api/config", payload={"slack": {"require_bind": True}, "language": "zh"}).status_code == 200
    )
    assert request("POST", "/api/config", payload={"runtime": {"log_level": "DEBUG"}}).status_code == 200
    assert V2Config.load().slack.require_bind
    assert request("POST", "/api/config", payload={"slack": None}).status_code == 403
    assert V2Config.load().slack.require_bind


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_organization_owner_does_not_supply_instance_management(management_http, role):
    request = management_http(role, organization_role="owner")
    before = paths.get_config_path().read_bytes()
    response = request("POST", "/api/config", payload={"runtime": {"log_level": "DEBUG"}})
    assert response.status_code in {400, 403}
    assert request("PUT", "/api/workbench/prefs", payload={"background_work_banner_enabled": False}).status_code == 403
    assert paths.get_config_path().read_bytes() == before


@pytest.mark.parametrize("csrf,origin", [(False, None), (True, "https://attacker.example")])
def test_member_csrf_still_precedes_management_effects(management_http, csrf, origin):
    request = management_http()
    before = paths.get_config_path().read_bytes()
    response = request("POST", "/api/config", payload={"language": "zh"}, csrf=csrf, origin=origin)
    assert response.status_code == 403
    assert paths.get_config_path().read_bytes() == before


def test_unauthenticated_remote_management_is_denied(management_http):
    request = management_http(authenticated=False)
    response = request("POST", "/api/config", payload={"language": "zh"})
    assert response.status_code in {401, 403}


def test_member_channel_and_dm_preferences_preserve_access_state(management_http):
    store = SettingsStore.get_instance()
    store.set_channels_for_platform("telegram", {"chat": ChannelSettings(require_bind=True)})
    store.set_users_for_platform("telegram", {"bound": UserSettings(is_admin=True, enabled=False, bound_at="old")})
    store.save()
    request = management_http()
    response = request(
        "POST",
        "/api/settings",
        payload={
            "platform": "telegram",
            "channels": {"chat": {"enabled": False, "require_mention": False, "custom_cwd": "/tmp/ordinary"}},
        },
    )
    assert response.status_code == 200, response.get_json()
    assert not store.find_channel("chat", platform="telegram").enabled
    assert store.find_channel("chat", platform="telegram").require_bind
    response = request(
        "POST",
        "/api/users",
        payload={"platform": "telegram", "users": {"bound": {"display_name": "成员", "custom_cwd": "/tmp/dm"}}},
    )
    assert response.status_code == 200, response.get_json()
    user = store.get_user("bound", platform="telegram")
    assert (user.is_admin, user.enabled, user.bound_at, user.custom_cwd) == (True, False, "old", "/tmp/dm")
    readback = request("GET", "/api/users?platform=telegram").get_json()
    assert readback["users"]["bound"]["display_name"] == "成员"
    for patch in ({"new": {}}, {"bound": {"enabled": True}}, {"bound": {"is_admin": False}}):
        assert request("POST", "/api/users", payload={"platform": "telegram", "users": patch}).status_code == 403
    assert set(store.get_users_for_platform("telegram")) == {"bound"}
    assert not store.get_user("bound", platform="telegram").enabled


@pytest.mark.parametrize(
    "channels", [{}, {"chat": {}}, {"chat": {"require_bind": None}}, {"chat": {"require_bind": False}}]
)
def test_member_channel_replacement_preserves_binding_effect(management_http, channels):
    store = SettingsStore.get_instance()
    store.set_channels_for_platform("telegram", {"chat": ChannelSettings(require_bind=True)})
    store.save()
    response = management_http()("POST", "/api/settings", payload={"platform": "telegram", "channels": channels})
    assert response.status_code == (200 if channels == {"chat": {}} else 403), response.get_json()
    assert store.find_channel("chat", platform="telegram").require_bind


@pytest.mark.parametrize("parent,override", [(True, False), (False, True), (True, True), (False, None)])
def test_thread_deletion_compares_actual_parent_fallback(management_http, parent, override):
    store = SettingsStore.get_instance()
    store.set_channels_for_platform("telegram", {"chat": ChannelSettings(require_bind=parent)})
    store.update_thread("chat", "7", ChannelSettings(require_bind=override), platform="telegram")
    response = management_http()("DELETE", "/api/settings/thread?platform=telegram&channel_id=chat&thread_id=7")
    assert response.status_code == (200 if parent == bool(override) else 403), response.get_json()


@pytest.mark.parametrize("mutation", ["remove", "role", "disable"])
def test_locked_member_save_cannot_undo_concurrent_membership_change(management_http, mutation):
    store = SettingsStore.get_instance()
    store.set_users_for_platform("telegram", {"bound": UserSettings(bound_at="old")})
    store.save()
    stale = copy.deepcopy(store.settings)
    if mutation == "remove":
        store.set_users_for_platform("telegram", {})
    else:
        user = store.get_user("bound", platform="telegram")
        if mutation == "role":
            user.is_admin = True
        else:
            user.enabled = False
    store.save()
    fresh = store._service.load_state()
    stale.users["telegram::bound"].custom_cwd = "/tmp/stale-preference"
    with pytest.raises(InstanceAuthorizationError):
        store._service.save_state(stale, user_context=MEMBER)
    assert store._service.load_state() == fresh


def test_member_guild_aliases_cannot_change_admission(management_http):
    store = SettingsStore.get_instance()
    store.set_guilds_for_platform("discord", {"blocked": GuildSettings(enabled=False)}, default_enabled=True)
    store.save()
    request = management_http()
    for payload in (
        {"guilds": {}, "guild_default_enabled": True},
        {"guild_allowlist": ["new"], "guild_default_enabled": False},
        {"guilds": {"blocked": {"enabled": True}}},
    ):
        response = request("POST", "/api/settings", payload={"platform": "discord", **payload})
        assert response.status_code == 403, response.get_json()
        assert not store.is_guild_enabled("discord", "blocked")


def test_member_workbench_preferences_round_trip(management_http):
    request = management_http()
    response = request("PUT", "/api/workbench/prefs", payload={"background_work_banner_enabled": False})
    assert response.status_code == 200, response.get_json()
    assert request("GET", "/api/workbench/prefs").get_json()["background_work_banner_enabled"] is False


@pytest.mark.parametrize("role", ["owner", "member", "editor", "viewer"])
def test_remote_management_handlers_reach_only_stubbed_host_effects(management_http, monkeypatch, role):
    from vibe import cli

    calls = []

    def effect(name):
        def run(*args, **kwargs):
            calls.append((name, args, kwargs))
            return {"ok": True, "operation": name}

        return run

    monkeypatch.setattr(api, "get_backend_runtime", effect("runtime"))
    monkeypatch.setattr(api, "start_dependency_install_job", effect("dependency"))
    monkeypatch.setattr(api, "slack_auth_test", effect("platform"))
    monkeypatch.setattr(cli, "_doctor", effect("doctor"))
    monkeypatch.setattr(remote_access, "apply_settings", effect("transport"))
    request = management_http(role)
    for method, path, payload in (
        ("GET", "/api/backend/claude/runtime", None),
        ("POST", "/api/dependencies/avault/install", {}),
        ("POST", "/api/slack/auth_test", {"bot_token": "test-token"}),
        ("POST", "/api/doctor", {"deep": True}),
        ("POST", "/api/remote-access/settings", {"connector_mode": "manual"}),
    ):
        response = request(method, path, payload=payload)
        assert response.status_code == (200 if role in {"owner", "member"} else 403), response.get_json()
        if role in {"owner", "member"}:
            assert response.get_json()["ok"]
    assert [call[0] for call in calls] == (
        ["runtime", "dependency", "platform", "doctor", "transport"] if role in {"owner", "member"} else []
    )
    if calls:
        assert calls[0][1] == ("claude",)
        assert calls[1][1] == ("avault",)
        assert calls[2][1][0] == "test-token"
        assert calls[3][2]["deep"] is True


@pytest.mark.parametrize("child_requires_bind", [True, False])
def test_channel_delete_checks_descendant_policy_in_same_transaction(management_http, child_requires_bind):
    from core import chat_discovery

    chat_discovery.remember_thread("telegram", "chat", "7")
    store = SettingsStore.get_instance()
    store.set_channels_for_platform("telegram", {"chat": ChannelSettings(require_bind=False)})
    store.update_thread("chat", "7", ChannelSettings(require_bind=child_requires_bind), platform="telegram")
    before = store._service.load_state()
    response = management_http()("POST", "/api/channels/delete", payload={"platform": "telegram", "id": "chat"})
    assert response.status_code == (403 if child_requires_bind else 200), response.get_json()
    after = store._service.load_state()
    if child_requires_bind:
        assert after == before
    else:
        assert after.channels == {} and after.threads == {}


def test_remote_member_vault_metadata_and_protected_use_boundary(management_http):
    from storage import vault_service
    from storage.vault_crypto import Sealed

    engine = api._vault_engine()
    try:
        with engine.begin() as conn:
            vault_service.create_secret(
                conn,
                name="MANAGED_TEST",
                protection="protected",
                sealed=Sealed(ciphertext="ct", nonce="n", wrap_meta="wm"),
            )
        request = management_http()
        response = request(
            "PATCH", "/api/vault/secrets/MANAGED_TEST", payload={"description": "managed metadata", "tags": ["test"]}
        )
        assert response.status_code == 200, response.get_json()
        with engine.connect() as conn:
            row = vault_service.get_secret_meta(conn, "MANAGED_TEST")
            assert row["description"] == "managed metadata" and row["tags"] == ["test"]
            result = vault_service.resolve_secret_access(
                conn, "MANAGED_TEST", session_id="ses-test", user_context=MEMBER
            )
            assert result["status"] == "approval_required"
            assert result.get("envelope") is None
        assert (
            management_http("editor")(
                "PATCH", "/api/vault/secrets/MANAGED_TEST", payload={"description": "forbidden"}
            ).status_code
            == 403
        )
    finally:
        engine.dispose()


def test_member_remote_transport_save_preserves_pairing_identity(management_http, monkeypatch):
    monkeypatch.setattr(remote_access, "status", lambda *args: {"running": False, "ok": True})
    monkeypatch.setattr(remote_access, "edge_binding_error", lambda *args: None)
    request = management_http()
    before = V2Config.load().remote_access.vibe_cloud
    response = request("POST", "/api/remote-access/settings", payload={"auto_recovery": not before.auto_recovery})
    assert response.status_code == 200 and response.get_json()["settings_applied"], response.get_json()
    after = V2Config.load().remote_access.vibe_cloud
    assert after.auto_recovery != before.auto_recovery
    assert (after.instance_id, after.instance_secret, after.session_secret) == (
        before.instance_id,
        before.instance_secret,
        before.session_secret,
    )
    for field in ("instance_id", "session_secret", "instance_secret", "backend_url"):
        response = request("POST", "/api/remote-access/settings", payload={field: "injected"})
        assert response.get_json()["error"] == "remote_access_settings_invalid"
    assert V2Config.load().remote_access.vibe_cloud == after


@pytest.mark.parametrize("surface", ["users", "channels", "thread", "thread_delete", "guilds", "config_guilds"])
def test_uncommitted_member_candidate_cannot_escape_through_owner_request(management_http, monkeypatch, surface):
    """Two real HTTP worker threads overlap between mutation and guarded save."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    store = SettingsStore.get_instance()
    store.set_users_for_platform("telegram", {"bound": UserSettings(is_admin=False)})
    store.set_channels_for_platform("telegram", {"chat": ChannelSettings(require_bind=True)})
    store.update_thread("chat", "7", ChannelSettings(require_bind=False), platform="telegram")
    store.set_guilds_for_platform("discord", {"blocked": GuildSettings(enabled=False)}, default_enabled=True)
    store.save()
    before = store._service.load_state()
    member = management_http()
    owner = management_http("owner")
    prepared, owner_done = Event(), Event()
    save = SettingsStore.save

    def paused_save(candidate, *, user_context=None):
        if user_context is not None and user_context.instance_role == "member":
            prepared.set()
            assert owner_done.wait(10), "Owner request did not finish while Member candidate was private"
        return save(candidate, user_context=user_context)

    monkeypatch.setattr(SettingsStore, "save", paused_save)
    mutations = {
        "users": ("POST", "/api/users", {"platform": "telegram", "users": {"bound": {"is_admin": True}}}),
        "channels": ("POST", "/api/settings", {"platform": "telegram", "channels": {"chat": {"require_bind": False}}}),
        "thread": (
            "POST",
            "/api/settings/thread",
            {"platform": "telegram", "channel_id": "chat", "thread_id": "7", "settings": {"require_bind": True}},
        ),
        "thread_delete": ("DELETE", "/api/settings/thread?platform=telegram&channel_id=chat&thread_id=7", None),
        "guilds": ("POST", "/api/settings", {"platform": "discord", "guilds": {"blocked": {"enabled": True}}}),
        "config_guilds": ("POST", "/api/config", {"discord": {"guild_denylist": []}}),
    }
    method, route, payload = mutations[surface]
    with ThreadPoolExecutor(max_workers=2) as pool:
        member_job = pool.submit(member, method, route, payload=payload)
        assert prepared.wait(10)
        try:
            # Read the shared cache as a controller would, before Owner's save.
            assert SettingsStore.get_instance().settings == before
            if surface == "config_guilds":
                # Config holds its own lock, so overlap the controller's existing
                # shared-store writer, which does not need to load config.
                def controller_save():
                    store.set_channels_for_platform("slack", {"ordinary": ChannelSettings(enabled=True)})
                    store.save()

                pool.submit(controller_save).result(timeout=10)
            else:
                owner_job = pool.submit(
                    owner,
                    "POST",
                    "/api/settings",
                    payload={"platform": "slack", "channels": {"ordinary": {"enabled": True}}},
                )
                response = owner_job.result(timeout=10)
                assert response.status_code == 200, response.get_json()
        finally:
            owner_done.set()
        response = member_job.result(timeout=10)
    assert response.status_code == 403, response.get_json()
    after = store._service.load_state()
    assert after.users == before.users and after.threads == before.threads
    assert after.guilds == before.guilds
    assert after.channels["telegram::chat"].require_bind
    assert after.channels["slack::ordinary"].enabled


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize("surface", ["users", "channels", "thread", "thread_delete", "guilds", "config_guilds"])
def test_overlapping_settings_requests_keep_both_scopes(management_http, monkeypatch, role, surface):
    """Successful private writes merge into the lock-fresh state, not a stale whole snapshot."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    store = SettingsStore.get_instance()
    store.set_users_for_platform("telegram", {"bound": UserSettings()})
    store.set_channels_for_platform("telegram", {"chat": ChannelSettings(require_bind=True)})
    store.set_channels_for_platform("slack", {
        "ordinary": ChannelSettings(custom_cwd="/before"),
        "removed": ChannelSettings(),
    })
    store.update_thread("chat", "7", ChannelSettings(require_bind=True), platform="telegram")
    store.set_guilds_for_platform("discord", {"blocked": GuildSettings(enabled=False)}, default_enabled=True)
    store.save()
    first, owner = management_http(role), management_http("owner")
    prepared, committed = Event(), Event()
    save = SettingsStore.save

    def paused_save(candidate, *, user_context=None):
        if user_context is not None and not prepared.is_set():
            prepared.set()
            assert committed.wait(10)
        return save(candidate, user_context=user_context)

    monkeypatch.setattr(SettingsStore, "save", paused_save)
    mutations = {
        "users": ("POST", "/api/users", {"platform": "telegram", "users": {"bound": {"custom_cwd": "/first"}}}),
        "channels": ("POST", "/api/settings", {"platform": "telegram", "channels": {"chat": {"custom_cwd": "/first"}}}),
        "thread": ("POST", "/api/settings/thread", {
            "platform": "telegram", "channel_id": "chat", "thread_id": "7", "settings": {"custom_cwd": "/first"},
        }),
        "thread_delete": ("DELETE", "/api/settings/thread?platform=telegram&channel_id=chat&thread_id=7", None),
        "guilds": ("POST", "/api/settings", {"platform": "discord", "guilds": {"blocked": {"enabled": False}}}),
        "config_guilds": ("POST", "/api/config", {"discord": {"guild_denylist": ["blocked"]}}),
    }
    method, route, payload = mutations[surface]
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(first, method, route, payload=payload)
        assert prepared.wait(10)
        try:
            if surface == "config_guilds":
                # The config writer holds config lock; overlap the existing
                # controller writer, not a second request needing that lock.
                def controller_save():
                    store.set_channels_for_platform("slack", {
                        "ordinary": ChannelSettings(custom_cwd="/second"),
                        "added": ChannelSettings(enabled=True),
                    })
                    store.save()

                pool.submit(controller_save).result(timeout=10)
            else:
                response = pool.submit(owner, "POST", "/api/settings", payload={
                    "platform": "slack", "channels": {
                        "ordinary": {"custom_cwd": "/second"}, "added": {"enabled": True},
                    },
                }).result(timeout=10)
                assert response.status_code == 200, response.get_json()
        finally:
            committed.set()
        response = pending.result(timeout=10)
    assert response.status_code == 200, response.get_json()
    after = store._service.load_state()
    assert after.channels["slack::ordinary"].custom_cwd == "/second"
    assert after.channels["slack::added"].enabled
    assert "slack::removed" not in after.channels
    if surface in {"users", "channels", "thread"}:
        changed = {
            "users": after.users["telegram::bound"],
            "channels": after.channels["telegram::chat"],
            "thread": after.threads["telegram::chat/7"],
        }
        assert changed[surface].custom_cwd == "/first"
    elif surface == "thread_delete":
        assert not after.threads


@pytest.mark.parametrize("mutation", ["remove", "role", "disable"])
def test_private_member_dm_save_refuses_changed_membership(management_http, monkeypatch, mutation):
    """The real HTTP candidate must not resurrect or rewrite a concurrently changed user."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    store = SettingsStore.get_instance()
    store.set_users_for_platform("telegram", {"bound": UserSettings()})
    store.save()
    request = management_http()
    prepared, committed = Event(), Event()
    save = SettingsStore.save

    def paused_save(candidate, *, user_context=None):
        if user_context is not None:
            prepared.set()
            assert committed.wait(10)
        return save(candidate, user_context=user_context)

    monkeypatch.setattr(SettingsStore, "save", paused_save)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(request, "POST", "/api/users", payload={
            "platform": "telegram", "users": {"bound": {"custom_cwd": "/stale"}},
        })
        assert prepared.wait(10)
        try:
            if mutation == "remove":
                store.set_users_for_platform("telegram", {})
            elif mutation == "role":
                store.get_user("bound", platform="telegram").is_admin = True
            else:
                store.get_user("bound", platform="telegram").enabled = False
            pool.submit(store.save).result(timeout=10)
            fresh = store._service.load_state()
        finally:
            committed.set()
        response = pending.result(timeout=10)
    assert response.status_code == 403, response.get_json()
    assert store._service.load_state() == fresh


def test_member_stale_access_snapshot_cannot_replay_untouched_rows(management_http, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    store = SettingsStore.get_instance()
    store.set_channels_for_platform("telegram", {"chat": ChannelSettings()})
    store.set_users_for_platform("telegram", {"changed": UserSettings(), "removed": UserSettings()})
    store.set_guilds_for_platform("discord", {"before": GuildSettings()}, default_enabled=False)
    store.settings.bind_codes = [BindCode("old", "one_time", "now")]
    store.save()
    request = management_http()
    prepared, committed = Event(), Event()
    save = SettingsStore.save

    def paused_save(candidate, *, user_context=None):
        if user_context is not None:
            prepared.set()
            assert committed.wait(10)
        return save(candidate, user_context=user_context)

    monkeypatch.setattr(SettingsStore, "save", paused_save)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(request, "POST", "/api/settings", payload={
            "platform": "telegram", "channels": {"chat": {"custom_cwd": "/ordinary"}},
        })
        assert prepared.wait(10)
        try:
            store.set_users_for_platform("telegram", {
                "changed": UserSettings(is_admin=True, enabled=False), "added": UserSettings(),
            })
            store.set_guilds_for_platform("discord", {"after": GuildSettings()}, default_enabled=False)
            store.settings.bind_codes = [BindCode("new", "one_time", "now")]
            pool.submit(store.save).result(timeout=10)
            fresh = store._service.load_state()
        finally:
            committed.set()
        response = pending.result(timeout=10)
    assert response.status_code == 403, response.get_json()
    after = store._service.load_state()
    assert after.channels["telegram::chat"].custom_cwd is None
    assert after.users == fresh.users
    assert after.guilds == fresh.guilds and after.guild_default_enabled == fresh.guild_default_enabled
    assert after.bind_codes == fresh.bind_codes


@pytest.mark.parametrize("field", ["is_admin", "enabled", "require_bind"])
def test_member_explicit_unchanged_access_echo_is_checked_against_fresh_state(management_http, monkeypatch, field):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    store = SettingsStore.get_instance()
    store.set_users_for_platform("telegram", {"bound": UserSettings(is_admin=False, enabled=True)})
    store.set_channels_for_platform("telegram", {"chat": ChannelSettings(require_bind=False)})
    store.save()
    request = management_http()
    prepared, committed = Event(), Event()
    save = SettingsStore.save

    def paused_save(candidate, *, user_context=None):
        if user_context is not None:
            prepared.set()
            assert committed.wait(10)
        return save(candidate, user_context=user_context)

    monkeypatch.setattr(SettingsStore, "save", paused_save)
    if field == "require_bind":
        route = "/api/settings"
        payload = {"platform": "telegram", "channels": {"chat": {field: False}}}
    else:
        route = "/api/users"
        payload = {"platform": "telegram", "users": {"bound": {field: field == "enabled"}}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(request, "POST", route, payload=payload)
        assert prepared.wait(10)
        try:
            if field == "require_bind":
                store.find_channel("chat", platform="telegram").require_bind = True
            else:
                setattr(store.get_user("bound", platform="telegram"), field, field == "is_admin")
            pool.submit(store.save).result(timeout=10)
            fresh = store._service.load_state()
        finally:
            committed.set()
        response = pending.result(timeout=10)
    assert response.status_code == 403, response.get_json()
    assert store._service.load_state() == fresh


@pytest.mark.parametrize("raw", [
    b"{", b"[]", b"null", b"\xff", b'{"discord": []}',
    b'{"discord": {"bot_token": 12}}', b'{"discord": {"thread_auto_archive_minutes": 13}}',
])
def test_member_settings_save_uses_config_recovery_semantics(management_http, raw):
    config_path = paths.get_config_path()
    config_path.write_bytes(raw)
    # Startup accepts these released/damaged shapes with recovery defaults.
    recovered = V2Config.load()
    assert recovered.load_warnings
    result = api.save_settings({"platform": "telegram", "channels": {"safe": {"enabled": True}}}, user_context=MEMBER)
    assert "safe" in result["channels"]
    with contextlib.closing(SettingsStore()) as saved:
        assert saved.find_channel("safe", platform="telegram").enabled
    assert config_path.read_bytes() == raw  # The policy read never persists recovery.


@pytest.mark.parametrize("discord", [[], "invalid", {"bot_token": 12}])
@pytest.mark.parametrize("surface", ["channels", "thread", "users"])
def test_remote_member_can_save_with_recovered_discord_section(management_http, discord, surface):
    store = SettingsStore.get_instance()
    store.set_users_for_platform("telegram", {"bound": UserSettings()})
    store.save()
    request = management_http()
    config_path = paths.get_config_path()
    payload = json.loads(config_path.read_text())
    payload["discord"] = discord
    config_path.write_text(json.dumps(payload))
    mutations = {
        "channels": ("/api/settings", {"platform": "telegram", "channels": {"safe": {"enabled": True}}}),
        "thread": ("/api/settings/thread", {
            "platform": "telegram", "channel_id": "safe", "thread_id": "7", "settings": {"enabled": True},
        }),
        "users": ("/api/users", {"platform": "telegram", "users": {"bound": {"custom_cwd": "/recovered"}}}),
    }
    route, body = mutations[surface]
    response = request("POST", route, payload=body)
    assert response.status_code == 200, response.get_json()
    after = store._service.load_state()
    if surface == "channels":
        assert after.channels["telegram::safe"].enabled
    elif surface == "thread":
        assert after.threads["telegram::safe/7"].enabled
    else:
        assert after.users["telegram::bound"].custom_cwd == "/recovered"
    # Unrelated remote identity remains intact and the raw file is not repaired here.
    assert json.loads(config_path.read_text()) == payload


def test_recovery_keeps_valid_legacy_guild_admission_policy(management_http):
    request = management_http()
    config_path = paths.get_config_path()
    payload = json.loads(config_path.read_text())
    payload["discord"] = {"guild_allowlist": ["allowed"], "guild_denylist": ["blocked"]}
    payload["runtime"] = []
    config_path.write_text(json.dumps(payload))
    recovered = V2Config.load()
    assert recovered.load_warnings
    assert recovered.discord.guild_allowlist == ["allowed"]
    response = request("POST", "/api/settings", payload={
        "platform": "discord", "guild_allowlist": ["attacker"],
    })
    assert response.status_code == 403, response.get_json()
    response = request("POST", "/api/settings", payload={
        "platform": "discord", "guilds": {"allowed": {"enabled": True}, "blocked": {"enabled": False}},
        "guild_default_enabled": False,
    })
    assert response.status_code == 200, response.get_json()
    assert json.loads(config_path.read_text()) == payload
