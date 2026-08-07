from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from config.v2_config import (
    AgentsConfig,
    PlatformsConfig,
    RemoteAccessConfig,
    RuntimeConfig,
    SlackConfig,
    UiConfig,
    V2Config,
)
from tests.ui_server_test_helpers import csrf_headers
from vibe import remote_access, ui_server
from vibe.authorization import context_from_session_payload
from vibe.ui_compat import g
from vibe.ui_server import app


def _paired_config(tmp_path, *, revision: int = 41) -> V2Config:
    config = V2Config(
        mode="self_host",
        version="v2",
        platform="slack",
        platforms=PlatformsConfig(enabled=["slack"], primary="slack"),
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        ui=UiConfig(),
        remote_access=RemoteAccessConfig(),
    )
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.backend_url = "https://backend.test"
    cloud.public_url = "https://alex.avibe.bot"
    cloud.client_id = "vr_client_123"
    cloud.instance_id = "inst_123"
    cloud.instance_secret = "instance-secret"
    cloud.session_secret = "session-secret"
    cloud.authorization_endpoint = "https://backend.test/oauth/authorize"
    cloud.redirect_uri = "https://alex.avibe.bot/auth/callback"
    config.save()
    remote_access._clear_authorization_revision_cache()
    remote_access._replace_authorization_revision(config, revision)
    return config


def _organization_claims(
    config: V2Config,
    *,
    revision: int = 41,
    role: str = "editor",
) -> dict[str, Any]:
    return {
        "vibe_instance_id": config.remote_access.vibe_cloud.instance_id,
        "vibe_instance_role": role,
        "vibe_instance_access_source": "organization_group",
        "vibe_instance_authorization_revision": revision,
        "vibe_organization_id": "org_123",
        "vibe_organization_member_id": "member_123",
        "vibe_organization_role": "member",
        "vibe_group_ids": ["group_research"],
        "vibe_membership_version": "membership-v1",
    }


def _organization_cookie(
    config: V2Config,
    *,
    revision: int = 41,
    subject: str = "user-1",
) -> str:
    return remote_access.make_session_cookie(
        config,
        f"{subject}@example.com",
        subject,
        session_claims=_organization_claims(config, revision=revision),
    )


def test_authorization_revision_device_contract_is_monotonic(monkeypatch, tmp_path):
    """I1057-AC2/AC3: one paired-device watermark drives every hostname."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    calls = []

    def request(cfg, method, suffix, payload=None, *, timeout=8.0):
        calls.append((cfg, method, suffix, payload, timeout))
        return {"authorization_revision": 42}

    monkeypatch.setattr(remote_access, "_device_json_request", request)

    assert remote_access.sync_authorization_revision_once(config) == {
        "ok": True,
        "authorization_revision": 42,
    }
    assert calls == [(config, "GET", "authorization-revision", None, 8.0)]
    assert remote_access.current_authorization_revision(config) == 42

    monkeypatch.setattr(
        remote_access,
        "_device_json_request",
        lambda *args, **kwargs: {"authorization_revision": 41},
    )
    assert remote_access.sync_authorization_revision_once(config) == {
        "ok": False,
        "error": "authorization_revision_regressed",
    }
    assert remote_access.current_authorization_revision(config) == 42


def test_paired_session_requires_signed_current_revision(monkeypatch, tmp_path):
    """I1057-AC4: unsigned, missing, and stale authorization versions fail closed."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    missing = _organization_claims(config)
    missing.pop("vibe_instance_authorization_revision")

    with pytest.raises(
        remote_access.OAuthCodeExchangeError,
        match="invalid_authorization_revision",
    ):
        remote_access.make_session_cookie(
            config,
            "member@example.com",
            "user-1",
            session_claims=missing,
        )

    with pytest.raises(
        remote_access.OAuthCodeExchangeError,
        match="stale_authorization_revision",
    ):
        _organization_cookie(config, revision=40)


@pytest.mark.parametrize(
    "hosted_change",
    [
        "role_downgrade",
        "group_membership_removal",
        "group_archival",
        "member_removal",
        "access_binding_removal",
    ],
)
def test_revision_advance_revokes_active_editor_http_session(
    monkeypatch,
    tmp_path,
    hosted_change,
):
    """I1057-AC1/AC2: narrowing invalidates a remote session with chat disabled."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    cookie = _organization_cookie(config)
    payload = remote_access.parse_session_cookie(config, cookie)
    assert payload is not None
    assert context_from_session_payload(payload).can_chat is False

    client = app.test_client()
    client.set_cookie(remote_access.SESSION_COOKIE_NAME, cookie, domain="alex.avibe.bot")
    headers = csrf_headers(client, "https://alex.avibe.bot")
    before = client.post(
        "/api/sessions",
        base_url="https://alex.avibe.bot",
        headers=headers,
        json={},
    )
    assert before.status_code == 400

    assert hosted_change
    remote_access._replace_authorization_revision(config, 42)
    after = client.post(
        "/api/sessions",
        base_url="https://alex.avibe.bot",
        headers=headers,
        json={},
    )

    assert after.status_code == 401
    assert after.get_json()["error"] == "remote_access_login_required"


def test_default_and_custom_hostname_sessions_share_revision(monkeypatch, tmp_path):
    """I1057-AC3: host-scoped cookies cannot retain divergent authorization."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    remote_access._replace_active_hostnames(config, ["max.fileguard.io"])
    default_client = app.test_client()
    custom_client = app.test_client()
    default_client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(config, subject="default-user"),
        domain="alex.avibe.bot",
    )
    custom_client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(config, subject="custom-user"),
        domain="max.fileguard.io",
    )

    assert default_client.get(
        "/api/session",
        base_url="https://alex.avibe.bot",
    ).get_json()["authenticated"] is True
    assert custom_client.get(
        "/api/session",
        base_url="https://max.fileguard.io",
    ).get_json()["authenticated"] is True

    remote_access._replace_authorization_revision(config, 42)

    assert default_client.get(
        "/api/session",
        base_url="https://alex.avibe.bot",
    ).get_json()["authenticated"] is False
    assert custom_client.get(
        "/api/session",
        base_url="https://max.fileguard.io",
    ).get_json()["authenticated"] is False


def test_revision_snapshot_expiry_and_renewal_fail_closed(monkeypatch, tmp_path):
    """I1057-AC4: offline snapshots and renewal races cannot extend old claims."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    cookie = _organization_cookie(config)
    payload = remote_access.parse_session_cookie(config, cookie)
    assert payload is not None

    remote_access._replace_authorization_revision(config, 42)
    with pytest.raises(
        remote_access.OAuthCodeExchangeError,
        match="stale_authorization_revision",
    ):
        remote_access.make_session_cookie(
            config,
            "user-1@example.com",
            "user-1",
            session_claims=payload,
        )

    remote_access._replace_authorization_revision(config, 42)
    snapshot = remote_access._load_authorization_revision_snapshot(config)
    assert snapshot is not None
    _, updated_at = snapshot
    assert remote_access.session_authorization_is_current(
        config,
        {"vibe_instance_authorization_revision": 42},
        now=updated_at + remote_access.AUTHORIZATION_REVISION_MAX_AGE_SECONDS + 1,
    ) is False


def test_workbench_and_show_sse_end_after_revision_change(monkeypatch, tmp_path):
    """I1057-AC4/AC6: active and resumed SSE streams converge on the watermark."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    cookie = _organization_cookie(config)
    payload = remote_access.parse_session_cookie(config, cookie)
    assert payload is not None
    context = context_from_session_payload(payload)

    class EmptyShowEventStore:
        def list(self, *args, **kwargs):
            return {"events": [], "next_after_id": None}

        def close(self):
            return None

    monkeypatch.setattr(ui_server, "_show_session_event_store", EmptyShowEventStore)

    async def exercise() -> None:
        with app.test_request_context("/api/events"):
            g.authorization_context = context
            g.remote_session_payload = payload
            response = await ui_server.workbench_events()
            iterator = response.body_iterator.__aiter__()
            try:
                for _ in range(3):
                    await iterator.__anext__()
                remote_access._replace_authorization_revision(config, 42)
                with pytest.raises(StopAsyncIteration):
                    await asyncio.wait_for(iterator.__anext__(), timeout=1)
            finally:
                await iterator.aclose()

        remote_access._replace_authorization_revision(config, 42)
        show_response = await ui_server._show_events_stream(
            "ses123",
            after_id="show_evt_resume",
            authorization_context=context,
            remote_session_payload=payload,
            remote_config=config,
        )
        show_iterator = show_response.body_iterator.__aiter__()
        try:
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(show_iterator.__anext__(), timeout=1)
        finally:
            await show_iterator.aclose()

    asyncio.run(exercise())


def test_websocket_reconnect_and_active_waiter_recheck_revision(monkeypatch, tmp_path):
    """I1057-AC4/AC6: active sockets close and stale reconnects are rejected."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    cookie = _organization_cookie(config)

    class Socket:
        cookies = {remote_access.SESSION_COOKIE_NAME: cookie}

    websocket = Socket()
    monkeypatch.setattr(ui_server, "_websocket_is_local_request", lambda *args: False)
    monkeypatch.setattr(ui_server, "_remote_access_host_allowed", lambda *args: True)
    monkeypatch.setattr(ui_server, "_websocket_normalized_host", lambda *args: "alex.avibe.bot")
    monkeypatch.setattr(ui_server, "_AUTHORIZATION_REVISION_RECHECK_SECONDS", 0.001)

    payload = ui_server._remote_access_websocket_session_payload(websocket, config)
    assert payload is not None

    async def exercise() -> None:
        waiter = asyncio.create_task(
            ui_server._wait_for_remote_session_authorization_loss(config, payload)
        )
        await asyncio.sleep(0)
        remote_access._replace_authorization_revision(config, 42)
        await asyncio.wait_for(waiter, timeout=1)

    asyncio.run(exercise())
    assert ui_server._remote_access_websocket_session_payload(websocket, config) is None


def test_terminal_websocket_rejects_stale_remote_session_before_accept(
    monkeypatch,
    tmp_path,
):
    """I1057-AC4: a stale remote terminal request is never accepted."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)

    class RecordingWebSocket:
        client = None
        query_params = {}

        def __init__(self):
            self.calls = []

        async def accept(self):
            self.calls.append(("accept", None))

        async def close(self, code=1000):
            self.calls.append(("close", code))

    websocket = RecordingWebSocket()
    monkeypatch.setattr(ui_server, "_terminal_enabled", lambda: True)
    monkeypatch.setattr(ui_server, "TERMINAL_SUPPORTED", True)
    monkeypatch.setattr(ui_server, "_terminal_origin_allowed", lambda socket: True)
    monkeypatch.setattr(ui_server, "_show_runtime_websocket_authorized", lambda *a, **k: True)
    monkeypatch.setattr(ui_server, "_load_remote_access_config", lambda: config)
    monkeypatch.setattr(ui_server, "_remote_access_websocket_session_claims", lambda *a: None)
    monkeypatch.setattr(ui_server, "_websocket_is_local_request", lambda *a: False)
    monkeypatch.setattr(
        ui_server,
        "get_terminal_service",
        lambda: (_ for _ in ()).throw(AssertionError("stale socket reached terminal")),
    )

    asyncio.run(ui_server.terminal_websocket(websocket, "test"))

    assert websocket.calls == [
        ("close", ui_server._AUTHORIZATION_REFRESH_WEBSOCKET_CLOSE_CODE),
    ]


def test_trusted_local_access_ignores_hosted_revision_state(monkeypatch, tmp_path):
    """I1057-AC5: trusted local access stays owner-equivalent while sync is offline."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    state_path = remote_access._authorization_revision_state_path()
    state_path.unlink()
    remote_access._clear_authorization_revision_cache()
    assert remote_access.current_authorization_revision(config) is None

    response = app.test_client().get(
        "/api/config",
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 200
    assert response.get_json()["runtime"]["default_cwd"] == "."
