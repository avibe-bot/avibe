import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import jwt
import pytest
import yaml
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from config.v2_config import (
    AgentsConfig,
    ModelHubModelConfig,
    ModelHubRouteConfig,
    ModelHubRouteHopConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
    PlatformsConfig,
    RemoteAccessConfig,
    RuntimeConfig,
    SlackConfig,
    UiConfig,
    V2Config,
)
from core.agent_auth_service import AgentAuthService
from core.handlers.model_hub.adapter import SOURCE_PROTOCOLS
from core.handlers.model_hub.service import ModelHubError
from core.show_pages import ShowPageStore
from modules.agents.codex.agent import CodexAgent
from tests.scenario_harness.auth_setup import AuthSetupScenarioHarness, FakeProcess
from tests.scenario_harness.core import ScenarioExpect, ScenarioRunner, ScenarioStep
from tests.ui_server_test_helpers import _save_config, csrf_headers, remote_session_cookie
from storage import remote_access_authorization_service
from tests.scenario_harness.model_hub_native_oauth import (
    HubOAuthScenarioHarness,
    NativeOAuthScenarioHarness,
)
from vibe.api import (
    get_claude_auth,
    save_claude_auth,
    test_backend_auth_async as probe_backend_auth_async,
)
from vibe.claude_config import (
    build_claude_subprocess_env,
    materialize_claude_subprocess_env,
    read_claude_settings_env,
)
from vibe import remote_access, show_identity, ui_server
from vibe.ui_server import app
from vibe.model_hub_runtime.api_key_vendors import api_key_vendor_catalog


def test_auth_setup_catalog_priorities_reference_live_scenarios():
    catalog = yaml.safe_load((ROOT / "tests/scenarios/auth_setup/catalog.yaml").read_text())
    live_ids = {scenario["id"] for scenario in catalog["scenarios"]}

    assert set(catalog.get("next_priority", [])) <= live_ids


def test_limited_show_identity_closed_loop_installs_guest_lease(monkeypatch, tmp_path):
    """Scenario: AUTH-SETUP-404"""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    cloud = config.remote_access.vibe_cloud
    cloud.backend_url = "https://backend.test"
    cloud.issuer = "https://backend.test"
    cloud.jwks_uri = "https://backend.test/oauth/jwks.json"
    config.save()
    monkeypatch.setattr(
        ShowPageStore,
        "_resolve_instance_ownership",
        staticmethod(lambda: {"mode": "organization", "organization_id": "组织-甲"}),
    )

    store = ShowPageStore()
    try:
        page = store.ensure("limited-identity-scenario")
        access = store.get_access(page.session_id)
        assert access is not None
        applied = store.apply_access(
            page.session_id,
            expected_revision=access.revision,
            target_access_mode="limited",
            target_share_id=page.share_id,
            target_entries=[
                {
                    "kind": "group",
                    "value": "研发组",
                    "organization_id": "组织-甲",
                }
            ],
        )
        assert applied.status == "applied"
    finally:
        store.close()

    client = app.test_client()
    remote_peer = {"REMOTE_ADDR": "203.0.113.44"}
    navigation = client.get(
        f"/p/{page.share_id}/reports/daily?tab=1",
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer,
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert navigation.status_code == 302
    authorize_url = urllib.parse.urlsplit(navigation.headers["Location"])
    assert authorize_url.path == (
        "/api/v1/instances/inst_123/show-identity/authorize"
    )
    authorize_query = urllib.parse.parse_qs(authorize_url.query)
    state = authorize_query["state"][0]
    nonce = authorize_query["nonce"][0]

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issued_at = int(time.time())
    assertion = jwt.encode(
        {
            "iss": cloud.issuer,
            "aud": f"avibe-show-identity:{cloud.client_id}",
            "sub": "访客-甲",
            "iat": issued_at,
            "exp": issued_at + 300,
            "jti": f"scenario-{time.time_ns()}",
            "nonce": nonce,
            "instance_id": cloud.instance_id,
            "verified_email": "viewer@example.com",
            "organization_id": "组织-甲",
            "organization_member_id": "成员-甲",
            "organization_role": "member",
            "group_ids": ["研发组"],
        },
        private_key,
        algorithm="RS256",
        headers={"typ": "JWT", "kid": "scenario"},
    )

    class ScenarioJwkClient:
        def __init__(self, uri, *, timeout):
            assert uri == cloud.jwks_uri
            assert timeout == 5

        def get_signing_key_from_jwt(self, token):
            assert token == assertion
            return SimpleNamespace(key=private_key.public_key())

    monkeypatch.setattr(show_identity, "PyJWKClient", ScenarioJwkClient)
    form = {"state": state, "assertion": assertion}
    callback = client.post(
        show_identity.CALLBACK_PATH,
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer,
        data=form,
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["Location"] == f"/p/{page.share_id}/reports/daily?tab=1"
    assert show_identity.show_guest_cookie_name(page.share_id) in callback.headers[
        "Set-Cookie"
    ]

    admitted = client.get(
        f"/p/{page.share_id}/__show/me",
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer,
    )
    assert admitted.status_code == 200
    assert admitted.get_json() == {"authenticated": False, "canAnnotate": False}

    replay = app.test_client().post(
        show_identity.CALLBACK_PATH,
        base_url="https://alex.avibe.bot",
        environ_base={"REMOTE_ADDR": "203.0.113.45"},
        data=form,
    )
    assert replay.status_code == 400
    assert replay.get_json()["error"] == "replayed_assertion"

    store = ShowPageStore()
    try:
        access = store.get_access(page.session_id)
        assert access is not None
        revoked = store.apply_access(
            page.session_id,
            expected_revision=access.revision,
            target_access_mode="limited",
            target_share_id=page.share_id,
            target_emails=["someone-else@example.com"],
        )
        assert revoked.status == "applied"
    finally:
        store.close()

    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "viewer@example.com",
            "访客-甲",
            role="viewer",
        ),
        domain="alex.avibe.bot",
    )
    revoked_navigation = client.get(
        f"/p/{page.share_id}/",
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer,
        headers={
            "Accept": "text/html",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        },
        follow_redirects=False,
    )
    assert revoked_navigation.status_code == 403
    assert "Location" not in revoked_navigation.headers
    assert "You do not have access to this page" in revoked_navigation.text

    revoked_subresource = client.get(
        f"/p/{page.share_id}/app.js",
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer,
    )
    assert revoked_subresource.status_code == 404


class _FakeNextTurnRuntime:
    def __init__(self):
        self.refreshed = False
        self.cleared_settings_keys = []

    async def refresh(self):
        self.refreshed = True

    async def clear_sessions(self, settings_key: str):
        self.cleared_settings_keys.append(settings_key)

    def run_turn(self, settings_key: str) -> str:
        assert self.refreshed, "Expected runtime to be refreshed before the next turn"
        assert settings_key in self.cleared_settings_keys, "Expected stale sessions to be cleared before the next turn"
        return "turn-ok"


class _FakeCodexNextTurnRuntime:
    def __init__(self):
        self.refreshed = False

    async def refresh(self):
        self.refreshed = True

    def run_turn(self) -> str:
        assert self.refreshed, "Expected Codex runtime to be refreshed before the next turn"
        return "turn-ok"


class _CodexProviderBindingSessions:
    def get_agent_session_id(self, *_args, **_kwargs):
        return "thread-existing"

    def ensure_agent_session_id(self, *_args, **_kwargs):
        return "ses-provider"

    def bind_agent_session(self, *_args, **_kwargs):
        return "ses-provider"


class _ReloadingV2ConfigController:
    @property
    def config(self):
        return V2Config.load()


def _save_remote_web_auth_config() -> V2Config:
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
    cloud.public_url = "https://alex.avibe.bot"
    cloud.client_id = "vr_client_123"
    cloud.instance_id = "inst_123"
    cloud.session_secret = "session-secret"
    cloud.authorization_endpoint = "https://backend.test/oauth/authorize"
    cloud.redirect_uri = "https://alex.avibe.bot/auth/callback"
    config.save()
    return config


def test_remote_web_oauth_cold_launch_retry_is_single_owner(monkeypatch, tmp_path):
    """Scenario: AUTH-SETUP-209"""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_remote_web_auth_config()
    ui_dist = tmp_path / "ui-dist"
    ui_dist.mkdir()
    (ui_dist / "index.html").write_text("<html><body>Avibe shell</body></html>", encoding="utf-8")
    monkeypatch.setattr(ui_server, "get_ui_dist_path", lambda: ui_dist)
    with ui_server._auth_ratelimit_lock:
        ui_server._auth_ratelimit.clear()

    harness = SimpleNamespace(
        primary=app.test_client(),
        retry=app.test_client(),
        base_url="https://alex.avibe.bot",
        peer={"REMOTE_ADDR": "203.0.113.10"},
        config=config,
    )
    runner = ScenarioRunner(harness)

    def cold_launch(current):
        response = current.primary.get(
            "/old-route",
            base_url=current.base_url,
            environ_base=current.peer,
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert response.text == "<html><body>Avibe shell</body></html>"
        assert response.headers.get("Location") is None
        assert ui_server.REMOTE_OAUTH_COOKIE_NAME not in response.headers.get("Set-Cookie", "")

    def start_background_providers(current):
        session = current.primary.get(
            "/api/session",
            base_url=current.base_url,
            environ_base=current.peer,
        )
        assert session.get_json() == {"remote": True, "authenticated": False}

        def request(path):
            return app.test_client().get(
                path,
                base_url=current.base_url,
                environ_base=current.peer,
                follow_redirects=False,
            )

        with ThreadPoolExecutor(max_workers=3) as pool:
            responses = list(pool.map(request, ("/status", "/api/events", "/api/config")))
        for response in responses:
            assert response.status_code == 401
            assert response.get_json()["error"] == "remote_access_login_required"
            assert response.headers.get("Location") is None
            assert ui_server.REMOTE_OAUTH_COOKIE_NAME not in response.headers.get("Set-Cookie", "")

    def begin_explicit_login(current):
        response = current.primary.get(
            "/auth/login?next=/old-route",
            base_url=current.base_url,
            environ_base=current.peer,
            follow_redirects=False,
        )
        authorize = httpx.URL(response.headers["Location"])
        current.first_state = authorize.params["state"]
        state_payload = ui_server._read_oauth_state(
            current.config.remote_access.vibe_cloud.session_secret,
            current.first_state,
        )
        assert response.status_code == 302
        assert authorize.host == "backend.test"
        assert state_payload is not None
        assert state_payload["next"] == "/old-route"
        assert state_payload["retry"] is False

    def lose_browser_context_and_retry(current):
        response = current.retry.get(
            f"/auth/callback?code=lost-context&state={current.first_state}",
            base_url=current.base_url,
            environ_base=current.peer,
            follow_redirects=False,
        )
        retry_target = response.headers["Location"]
        assert response.status_code == 302
        assert retry_target == f"/old-route?{ui_server.REMOTE_OAUTH_RETRY_PARAM}=1"

        retry_shell = current.retry.get(
            retry_target,
            base_url=current.base_url,
            environ_base=current.peer,
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert retry_shell.status_code == 200
        assert retry_shell.text == "<html><body>Avibe shell</body></html>"

        login = current.retry.get(
            f"/auth/login?{httpx.QueryParams({'next': retry_target})}",
            base_url=current.base_url,
            environ_base=current.peer,
            follow_redirects=False,
        )
        authorize = httpx.URL(login.headers["Location"])
        current.retry_state = authorize.params["state"]
        current.retry_nonce = authorize.params["nonce"]
        state_payload = ui_server._read_oauth_state(
            current.config.remote_access.vibe_cloud.session_secret,
            current.retry_state,
        )
        assert login.status_code == 302
        assert authorize.host == "backend.test"
        assert state_payload is not None
        assert state_payload["next"] == "/old-route"
        assert state_payload["retry"] is True

    def complete_retry(current):
        def exchange(_config, code, _verifier, redirect_uri=None):
            return {
                "claims": {
                    "email": "alex@example.com",
                    "sub": "user-1",
                    "nonce": current.retry_nonce,
                    "code": code,
                },
                "session_claims": {
                    "vibe_instance_id": current.config.remote_access.vibe_cloud.instance_id,
                    "vibe_instance_role": "owner",
                    "vibe_instance_access_source": "owner",
                },
            }

        monkeypatch.setattr(
            remote_access,
            "exchange_oauth_code",
            exchange,
        )
        callback = current.retry.get(
            f"/auth/callback?code=accepted&state={current.retry_state}",
            base_url=current.base_url,
            environ_base=current.peer,
            follow_redirects=False,
        )
        assert callback.status_code == 302
        assert callback.headers["Location"] == "/old-route"

        session = current.retry.get(
            "/api/session",
            base_url=current.base_url,
            environ_base=current.peer,
        )
        assert session.get_json()["authenticated"] is True
        shell = current.retry.get(
            callback.headers["Location"],
            base_url=current.base_url,
            environ_base=current.peer,
            follow_redirects=False,
        )
        assert shell.status_code == 200
        assert shell.text == "<html><body>Avibe shell</body></html>"

    asyncio.run(
        runner.run(
            ScenarioStep("cold_launch_unknown_route", cold_launch),
            ScenarioStep("start_background_providers", start_background_providers),
            ScenarioStep("begin_explicit_login", begin_explicit_login),
            ScenarioStep("retry_after_browser_context_loss", lose_browser_context_and_retry),
            ScenarioStep("complete_retry", complete_retry),
        )
    )
    ScenarioExpect.step_history(
        runner,
        [
            "cold_launch_unknown_route",
            "start_background_providers",
            "begin_explicit_login",
            "retry_after_browser_context_loss",
            "complete_retry",
        ],
    )


def _save_remote_session_authorization_config(instance_kind: str) -> V2Config:
    config = _save_remote_web_auth_config()
    cloud = config.remote_access.vibe_cloud
    cloud.backend_url = "https://backend.test"
    cloud.instance_secret = "device-secret"
    cloud.instance_kind = instance_kind
    config.save()
    remote_access._clear_authorization_revision_cache()
    remote_access._replace_authorization_revision(config, 1)
    return config


def test_personal_remote_session_slides_without_interactive_reauthorization(
    monkeypatch,
    tmp_path,
):
    """Scenario: AUTH-SETUP-402."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_remote_session_authorization_config("personal")
    base = int(time.time())
    harness = SimpleNamespace(config=config, cookie=None, payload=None)
    runner = ScenarioRunner(harness)

    def sign_in_once(current):
        monkeypatch.setattr(remote_access.time, "time", lambda: base)
        current.cookie = remote_access.make_session_cookie(
            current.config,
            "owner@example.com",
            "owner-1",
            session_claims={
                "vibe_instance_id": "inst_123",
                "vibe_instance_role": "owner",
                "vibe_instance_access_source": "owner",
                "vibe_instance_authorization_revision": 1,
            },
        )

    def slide_after_half_life(current):
        monkeypatch.setattr(
            remote_access.time,
            "time",
            lambda: base + remote_access.PERSONAL_SESSION_RENEW_AFTER_SECONDS + 1,
        )
        identity = remote_access.parse_session_identity(current.config, current.cookie)
        assert identity is not None
        resolution = remote_access.resolve_current_authorization(current.config, identity)
        assert resolution.current is True
        current.cookie = remote_access.renew_session_cookie(current.config, resolution.payload)

    def continue_after_original_expiry(current):
        monkeypatch.setattr(
            remote_access.time,
            "time",
            lambda: base + remote_access.PERSONAL_SESSION_TTL_SECONDS + 60,
        )
        identity = remote_access.parse_session_identity(current.config, current.cookie)
        assert identity is not None
        resolution = remote_access.resolve_current_authorization(current.config, identity)
        assert resolution.current is True
        assert resolution.policy == "personal"
        assert resolution.payload["claims_issued_at"] == base

    asyncio.run(
        runner.run(
            ScenarioStep("sign_in_once", sign_in_once),
            ScenarioStep("slide_after_half_life", slide_after_half_life),
            ScenarioStep("continue_after_original_expiry", continue_after_original_expiry),
        )
    )
    ScenarioExpect.step_history(
        runner,
        ["sign_in_once", "slide_after_half_life", "continue_after_original_expiry"],
    )


def test_organization_remote_session_recovers_and_revokes_without_oauth(
    monkeypatch,
    tmp_path,
):
    """Scenario: AUTH-SETUP-403."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_remote_session_authorization_config("organization")
    mode = {"value": "unavailable", "revision": 2}

    def device_request(_config, method, suffix, payload=None, *, timeout=8.0):
        assert (method, suffix, timeout) == ("POST", "authorization-context", 8.0)
        if mode["value"] == "unavailable":
            raise remote_access.BackendRequestError(503, {"error": "unavailable"})
        if mode["value"] == "revoked":
            raise remote_access.BackendRequestError(403, {"error": "access_denied"})
        return {
            "sub": payload["sub"],
            "email": payload["email"],
            "instance_kind": "organization",
            "vibe_instance_id": "inst_123",
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "organization_group",
            "vibe_instance_authorization_revision": mode["revision"],
            "vibe_organization_id": "org-1",
            "vibe_organization_member_id": "member-1",
            "vibe_organization_role": "member",
            "vibe_group_ids": ["group-1"],
            "vibe_membership_version": f"v{mode['revision']}",
        }

    monkeypatch.setattr(remote_access, "_device_json_request", device_request)
    cookie = remote_access.make_session_cookie(
        config,
        "member@example.com",
        "member-1",
        session_claims={
            "vibe_instance_id": "inst_123",
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "organization_group",
            "vibe_instance_authorization_revision": 1,
            "vibe_organization_id": "org-1",
            "vibe_organization_member_id": "member-1",
            "vibe_organization_role": "member",
            "vibe_group_ids": ["group-1"],
            "vibe_membership_version": "v1",
        },
    )
    client = app.test_client()
    client.set_cookie(remote_access.SESSION_COOKIE_NAME, cookie, domain="alex.avibe.bot")
    harness = SimpleNamespace(config=config, client=client)
    runner = ScenarioRunner(harness)

    def enter_control_plane_grace(current):
        now = int(time.time())
        assert remote_access_authorization_service.mark_matching_revision_checked(
            instance_id="inst_123",
            authorization_revision=1,
            checked_at=now,
        ) == 1
        remote_access._replace_authorization_revision(current.config, 2)
        session = current.client.get(
            "/api/session",
            base_url="https://alex.avibe.bot",
        ).get_json()
        assert session["authenticated"] is True
        assert session["authorization_state"] == "current"

    def recover_silently(current):
        remote_access._AUTHORIZATION_REFRESH_FAILURES.clear()
        mode["value"] = "current"
        session = current.client.get(
            "/api/session",
            base_url="https://alex.avibe.bot",
        ).get_json()
        assert session["authenticated"] is True
        assert session["authorization_state"] == "current"
        assert session["instance_role"] == "editor"

    def enforce_confirmed_revocation(current):
        mode.update(value="revoked", revision=3)
        remote_access._replace_authorization_revision(current.config, 3)
        protected = current.client.get(
            "/api/config",
            base_url="https://alex.avibe.bot",
        )
        session = current.client.get(
            "/api/session",
            base_url="https://alex.avibe.bot",
        ).get_json()
        assert protected.status_code == 403
        assert protected.get_json()["error"] == "remote_access_revoked"
        assert session["authenticated"] is True
        assert session["authorization_state"] == "revoked"

    asyncio.run(
        runner.run(
            ScenarioStep("enter_control_plane_grace", enter_control_plane_grace),
            ScenarioStep("recover_silently", recover_silently),
            ScenarioStep("enforce_confirmed_revocation", enforce_confirmed_revocation),
        )
    )
    ScenarioExpect.step_history(
        runner,
        ["enter_control_plane_grace", "recover_silently", "enforce_confirmed_revocation"],
    )


class AgentAuthSetupScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_oauth_to_explicit_auth_token_reaches_next_turn(self):
        """Scenario: AUTH-SETUP-904"""
        state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(state_dir.cleanup)
        home = Path(state_dir.name)
        claude_home = home / ".claude"
        claude_home.mkdir()
        credentials_path = claude_home / ".credentials.json"
        credentials_path.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "oauth-access",
                        "refreshToken": "oauth-refresh",
                    }
                }
            ),
            encoding="utf-8",
        )
        harness = AuthSetupScenarioHarness()
        runner = ScenarioRunner(harness)
        cleanup_calls = []
        restart_calls = []

        def clear_oauth(service=None):
            cleanup_calls.append(service)
            credentials_path.unlink()
            return {"ok": True}

        def restart_backend(name, *, metadata=None):
            restart_calls.append((name, metadata))
            return {"ok": True, "message": "refreshed"}

        def capture_oauth_state(current):
            current.before_switch = get_claude_auth()

        def save_auth_token(current):
            current.save_result = save_claude_auth(
                {
                    "auth_mode": "api_key",
                    "api_key": "relay-secret",
                    "credential_type": "auth_token",
                    "base_url": "https://relay.example/v1",
                }
            )

        def capture_next_turn_env(current):
            claude_config = V2Config.load().agents.claude
            current.next_turn_env = materialize_claude_subprocess_env(
                build_claude_subprocess_env(claude_config),
                base_env={"PATH": "/usr/bin"},
            )

        with (
            patch.dict(
                os.environ,
                {
                    "AVIBE_HOME": str(home / ".avibe"),
                    "CLAUDE_CONFIG_DIR": str(claude_home),
                },
            ),
            patch("vibe.api._get_oauth_service", return_value=harness.service),
            patch(
                "vibe.api._clear_claude_oauth_credentials_after_api_key_save",
                side_effect=clear_oauth,
            ),
            patch("vibe.api.restart_backend", side_effect=restart_backend),
            patch("vibe.api._read_claude_cli_oauth_signed_in", return_value=None),
        ):
            config = V2Config(
                mode="self_host",
                version="v2",
                slack=SlackConfig(bot_token=""),
                runtime=RuntimeConfig(default_cwd="."),
                agents=AgentsConfig(),
            )
            config.agents.claude.auth_mode = "oauth"
            config.agents.claude.auth_mode_set = True
            config.save()

            await runner.run(
                ScenarioStep("confirm_oauth_is_active", capture_oauth_state),
                ScenarioStep("save_auth_token", save_auth_token),
                ScenarioStep("launch_next_turn", capture_next_turn_env),
            )

        self.assertEqual(harness.before_switch["active_auth_mode"], "oauth")
        self.assertEqual(harness.save_result["active_auth_mode"], "api_key")
        self.assertEqual(harness.save_result["credential_type"], "auth_token")
        self.assertEqual(
            harness.save_result["settings_env_key_var"],
            "ANTHROPIC_AUTH_TOKEN",
        )
        self.assertNotIn("relay-secret", json.dumps(harness.save_result))
        self.assertEqual(
            harness.next_turn_env["ANTHROPIC_AUTH_TOKEN"],
            "relay-secret",
        )
        self.assertEqual(
            harness.next_turn_env["ANTHROPIC_BASE_URL"],
            "https://relay.example/v1",
        )
        self.assertNotIn("ANTHROPIC_API_KEY", harness.next_turn_env)
        self.assertEqual(cleanup_calls, [harness.service])
        self.assertEqual(
            restart_calls,
            [
                (
                    "claude",
                    {"reason": "save_claude_auth", "source": "ui_api"},
                )
            ],
        )
        ScenarioExpect.step_history(
            runner,
            ["confirm_oauth_is_active", "save_auth_token", "launch_next_turn"],
        )

    async def test_claude_oauth_to_api_key_probe_drops_stale_auth_token(self):
        """Scenario: AUTH-SETUP-905"""
        state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(state_dir.cleanup)
        home = Path(state_dir.name)
        claude_home = home / ".claude"
        claude_home.mkdir()
        credentials_path = claude_home / ".credentials.json"
        credentials_path.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "oauth-access",
                        "refreshToken": "oauth-refresh",
                    }
                }
            ),
            encoding="utf-8",
        )

        harness = AuthSetupScenarioHarness()
        runner = ScenarioRunner(harness)
        service = AgentAuthService(_ReloadingV2ConfigController())
        cleanup_calls = []
        restart_calls = []

        def clear_oauth(cleanup_service=None):
            cleanup_calls.append(cleanup_service)
            credentials_path.unlink()
            return {"ok": True}

        def restart_backend(name, *, metadata=None):
            restart_calls.append((name, metadata))
            return {"ok": True, "message": "refreshed"}

        def capture_oauth_state(current):
            current.before_switch = get_claude_auth()

        def save_api_key(current):
            current.save_result = save_claude_auth(
                {
                    "auth_mode": "api_key",
                    "api_key": "relay-api-key",
                    "credential_type": "api_key",
                    "base_url": "https://ai.coinsummer.com",
                }
            )

        async def run_connection_probe(current):
            current.test_result = await probe_backend_auth_async("claude")

        class FakeClaudeSDKClient:
            def __init__(self, *, options):
                harness.probe_options = options

            async def connect(self):
                return None

            async def query(self, text):
                harness.probe_query = text

            async def receive_response(self):
                yield SimpleNamespace(
                    is_error=False,
                    result="relay-probe-ok",
                    content=[],
                    error=None,
                )

            async def disconnect(self):
                return None

        with (
            patch.dict(
                os.environ,
                {
                    "AVIBE_HOME": str(home / ".avibe"),
                    "CLAUDE_CONFIG_DIR": str(claude_home),
                    "ANTHROPIC_API_KEY": "stale-parent-api-key",
                    "ANTHROPIC_AUTH_TOKEN": "stale-parent-auth-token",
                    "ANTHROPIC_BASE_URL": "https://stale-parent.example",
                },
            ),
            patch("vibe.api._get_oauth_service", return_value=service),
            patch(
                "vibe.api._clear_claude_oauth_credentials_after_api_key_save",
                side_effect=clear_oauth,
            ),
            patch("vibe.api.restart_backend", side_effect=restart_backend),
            patch("vibe.api._read_claude_cli_oauth_signed_in", return_value=None),
            patch("core.agent_auth_service.ClaudeSDKClient", FakeClaudeSDKClient),
        ):
            config = V2Config(
                mode="self_host",
                version="v2",
                slack=SlackConfig(bot_token=""),
                runtime=RuntimeConfig(default_cwd=str(home)),
                agents=AgentsConfig(),
            )
            config.agents.claude.auth_mode = "oauth"
            config.agents.claude.auth_mode_set = True
            config.agents.claude.cli_path = "claude-probe"
            config.save()

            await runner.run(
                ScenarioStep("confirm_oauth_is_active", capture_oauth_state),
                ScenarioStep("save_api_key", save_api_key),
                ScenarioStep("test_connection", run_connection_probe),
            )
            harness.saved_settings_env = read_claude_settings_env()

        self.assertEqual(harness.before_switch["active_auth_mode"], "oauth")
        self.assertEqual(harness.save_result["active_auth_mode"], "api_key")
        self.assertEqual(harness.save_result["credential_type"], "api_key")
        self.assertEqual(
            harness.save_result["settings_env_key_var"],
            "ANTHROPIC_API_KEY",
        )
        self.assertEqual(
            harness.saved_settings_env,
            {
                "ANTHROPIC_API_KEY": "relay-api-key",
                "ANTHROPIC_BASE_URL": "https://ai.coinsummer.com",
            },
        )
        self.assertTrue(harness.test_result["ok"])
        self.assertEqual(harness.test_result["excerpt"], "relay-probe-ok")
        self.assertEqual(harness.probe_query, "Hi")
        self.assertEqual(harness.probe_options.cli_path, "claude-probe")
        self.assertEqual(
            harness.probe_options.env["ANTHROPIC_API_KEY"],
            "relay-api-key",
        )
        self.assertEqual(harness.probe_options.env["ANTHROPIC_AUTH_TOKEN"], "")
        self.assertEqual(
            harness.probe_options.env["ANTHROPIC_BASE_URL"],
            "https://ai.coinsummer.com",
        )
        self.assertEqual(cleanup_calls, [service])
        self.assertEqual(
            restart_calls,
            [
                (
                    "claude",
                    {"reason": "save_claude_auth", "source": "ui_api"},
                )
            ],
        )
        ScenarioExpect.step_history(
            runner,
            ["confirm_oauth_is_active", "save_api_key", "test_connection"],
        )

    async def test_codex_connection_probe_reuses_persistent_app_server(self):
        """Scenario: AUTH-SETUP-906"""
        state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(state_dir.cleanup)
        home = Path(state_dir.name)
        harness = AuthSetupScenarioHarness()
        runner = ScenarioRunner(harness)
        requests = []

        class FakeCodexTransport:
            is_initialized = True

            async def send_request(self, method, params):
                requests.append((method, params))
                if method == "thread/start":
                    return {"thread": {"id": "thread-probe"}}
                await agent._on_notification(
                    "item/completed",
                    {
                        "threadId": "thread-probe",
                        "turnId": "turn-probe",
                        "item": {"type": "agentMessage", "text": "codex-probe-ok"},
                    },
                )
                await agent._on_notification(
                    "turn/completed",
                    {
                        "threadId": "thread-probe",
                        "turn": {"id": "turn-probe", "status": "completed"},
                    },
                )
                return {"turn": {"id": "turn-probe"}}

            async def wait_closed(self):
                await asyncio.Event().wait()

        controller = _ReloadingV2ConfigController()
        agent = object.__new__(CodexAgent)
        agent.controller = controller
        agent._transports = {str(home): FakeCodexTransport()}
        agent._transport_last_activity = {}
        agent._connection_probes = {}
        agent._connection_probe_turns = {}
        agent._connection_probe_cwds = {}
        agent._get_or_create_transport = AsyncMock(
            return_value=agent._transports[str(home)]
        )
        controller.agent_service = SimpleNamespace(agents={"codex": agent})
        service = AgentAuthService(controller)

        async def run_connection_probe(current):
            current.test_result = await probe_backend_auth_async(
                "codex",
                model="gpt-5.4-mini",
            )

        with (
            patch.dict(os.environ, {"AVIBE_HOME": str(home / ".avibe")}),
            patch("vibe.api._get_oauth_service", return_value=service),
        ):
            config = V2Config(
                mode="self_host",
                version="v2",
                slack=SlackConfig(bot_token=""),
                runtime=RuntimeConfig(default_cwd=str(home)),
                agents=AgentsConfig(),
            )
            config.agents.codex.cli_path = "codex-probe"
            config.save()
            await runner.run(ScenarioStep("test_connection", run_connection_probe))

        self.assertTrue(harness.test_result["ok"])
        self.assertEqual(harness.test_result["excerpt"], "codex-probe-ok")
        agent._get_or_create_transport.assert_awaited_once_with(
            str(home),
            allow_runtime_replacement=False,
        )
        self.assertEqual(
            requests,
            [
                (
                    "thread/start",
                    {
                        "cwd": str(
                            (home / ".avibe" / "runtime" / "codex-connection-probe").resolve()
                        ),
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                        "ephemeral": True,
                        "developerInstructions": (
                            "This is a connection probe. Do not use tools. "
                            "Reply with a short greeting."
                        ),
                    },
                ),
                (
                    "turn/start",
                    {
                        "threadId": "thread-probe",
                        "input": [{"type": "text", "text": "Hi"}],
                        "approvalPolicy": "never",
                        "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                        "effort": "low",
                        "model": "gpt-5.4-mini",
                    },
                ),
            ],
        )
        ScenarioExpect.step_history(runner, ["test_connection"])

    async def test_legacy_codex_thread_rebinds_once_after_api_key_endpoint_switch(self):
        """Scenario: AUTH-SETUP-903"""
        agent = object.__new__(CodexAgent)
        agent.controller = SimpleNamespace(
            config=SimpleNamespace(platform="avibe", reply_enhancements=True)
        )
        agent.codex_config = SimpleNamespace(
            default_model=None,
            auth_mode="api_key",
            base_url="https://relay.example/v1",
        )
        agent.sessions = _CodexProviderBindingSessions()
        agent._session_mgr = SimpleNamespace(set_thread_id=lambda *_args: None)
        async def build_thread_developer_instructions(_request):
            return None

        agent._build_thread_developer_instructions = build_thread_developer_instructions
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="avibe",
                platform_specific={},
                user_id="U1",
                channel_id="C1",
                thread_id=None,
            ),
            base_session_id="base-provider",
            session_key="avibe::project::provider",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            vibe_agent_id=None,
            vibe_agent_name=None,
        )
        provider_config = {
            "name": "OpenAI",
            "base_url": "https://relay.example/v1",
            "wire_api": "responses",
            "requires_openai_auth": True,
            "supports_websockets": False,
        }

        calls = []

        async def send_request(method, params):
            calls.append((method, params))
            if method == "config/read":
                return {
                    "config": {
                        "model_provider": "openai-managed",
                        "model_providers": {"openai-managed": provider_config},
                    }
                }
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "thread-existing",
                        "modelProvider": "openai-managed",
                    }
                }
            if method == "thread/resume":
                return {"thread": {"id": "thread-existing"}}
            raise AssertionError(f"Unexpected Codex request: {method}")

        thread_id = await agent._start_or_resume_thread(
            SimpleNamespace(send_request=send_request),
            request,
        )

        self.assertEqual(thread_id, "thread-existing")
        resume = next(params for method, params in calls if method == "thread/resume")
        self.assertEqual(resume["modelProvider"], "openai-managed")

    async def test_native_model_hub_reauth_confirms_before_honest_failure(self):
        """Scenario: AUTH-SETUP-106"""
        state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(state_dir.cleanup)
        harness = NativeOAuthScenarioHarness(
            Path(state_dir.name),
        )
        source = ModelHubSourceConfig(
            id="src_native0001",
            kind="subscription",
            vendor="anthropic",
            display_name="Claude subscription",
            protocol="anthropic",
            supply_channel="native_cli",
            billing="monthly",
            state=ModelHubSourceStateConfig(status="standby"),
            models=[
                ModelHubModelConfig(
                    id="claude-opus-4-6",
                    provenance="discovered",
                )
            ],
        )
        harness.store.config.sources.append(source)
        harness.store.config.agents["claude"].sources.order.append(source.id)
        harness.store.config.agents["claude"].routes["claude-opus-4-6"] = (
            ModelHubRouteConfig(
                hops=(
                    ModelHubRouteHopConfig(source.id, "claude-opus-4-6"),
                )
            )
        )

        with self.assertRaises(ModelHubError) as refused:
            await harness.service.reauth_source(source.id, {})

        self.assertEqual(
            refused.exception.code,
            "reauth_confirmation_required",
        )
        self.assertEqual(harness.agent_auth.flows, {})
        self.assertEqual(harness.store.config.sources[0].state.status, "standby")
        self.assertEqual(
            [model.id for model in harness.store.config.sources[0].models],
            ["claude-opus-4-6"],
        )

        started = await harness.service.reauth_source(
            source.id,
            {"acknowledge_irreversible": True},
        )
        flow_id = started["flow"]["flow_id"]
        self.assertEqual(started["flow"]["intent"], "reauth")
        self.assertEqual(harness.agent_auth.start_calls, [("claude", True)])
        self.assertEqual(
            harness.store.config.sources[0].state.status,
            "needs_action",
        )
        self.assertEqual(
            harness.store.config.sources[0].state.detail_key,
            "models.source.needs_action.oauth_expired",
        )
        self.assertEqual(harness.store.config.sources[0].models, [])

        harness.agent_auth.timeout(flow_id)
        failed = await harness.service.oauth_status(flow_id)

        self.assertEqual(failed["flow"]["state"], "failed")
        self.assertNotIn("source", failed)
        self.assertEqual(
            harness.store.config.sources[0].state.status,
            "needs_action",
        )
        self.assertEqual(
            harness.service.get_agent_sources("claude")["supply_status"],
            "interrupted",
        )

    async def test_native_reauth_reuses_a_pending_flow_and_cancel_commits_a_finished_one(self):
        """Scenario: AUTH-SETUP-107

        Pins the three server-side rules the 模型 page's re-auth dialog is built
        on, in the order a single abandoned journey meets them: a start REUSES a
        live pending flow, cancelling a pending flow destroys the very flow a
        successor would have reused, and the same cancel against a FINISHED flow
        materializes the re-auth instead of cancelling it.

        This is also the closed-loop evidence for the client-side ownership rules
        in `ui/src/components/settings/models/asyncLifetime.ts` (`releaseFlow`):
        the dialog only skips a teardown cancel when it no longer owns the source's
        flow, and never skips the refetch, and both of those are conclusions about
        the sequence asserted here. The dialog itself cannot be driven from this
        harness — it is React, and this is an asyncio TestCase with no DOM — so the
        UI-side interleavings are exercised in `asyncLifetime.test.ts` against the
        same rules.
        """
        state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(state_dir.cleanup)
        harness = NativeOAuthScenarioHarness(Path(state_dir.name))
        source = ModelHubSourceConfig(
            id="src_native0001",
            kind="subscription",
            vendor="anthropic",
            display_name="Claude subscription",
            protocol="anthropic",
            supply_channel="native_cli",
            billing="monthly",
            state=ModelHubSourceStateConfig(status="standby"),
            models=[ModelHubModelConfig(id="claude-opus-4-6", provenance="discovered")],
        )
        harness.store.config.sources.append(source)
        harness.store.config.agents["claude"].sources.order.append(source.id)
        harness.store.config.agents["claude"].routes["claude-opus-4-6"] = (
            ModelHubRouteConfig(
                hops=(
                    ModelHubRouteHopConfig(source.id, "claude-opus-4-6"),
                )
            )
        )
        ack = {"acknowledge_irreversible": True}

        # 1. A second start for the same source is handed the SAME flow, and the
        #    irreversible half is not paid twice: one spawn, one marking.
        first = await harness.service.reauth_source(source.id, ack)
        flow_id = first["flow"]["flow_id"]
        self.assertEqual(harness.agent_auth.start_calls, [("claude", True)])
        self.assertEqual(harness.store.config.sources[0].state.status, "needs_action")
        self.assertEqual(harness.store.config.sources[0].models, [])

        reused = await harness.service.reauth_source(source.id, ack)
        self.assertEqual(reused["flow"]["flow_id"], flow_id)
        self.assertEqual(harness.agent_auth.start_calls, [("claude", True)])
        # A start answers with the flow ALONE — no repair tail — which is why the
        # page has to re-read a start that comes back already finished.
        self.assertEqual(set(reused), {"flow"})

        # 2. Cancelling a still-pending flow destroys it, so the next start has to
        #    open a new one — and pay the irreversible half again.
        await harness.service.oauth_cancel(flow_id)
        self.assertEqual(harness.agent_auth.cancelled, [flow_id])

        restarted = await harness.service.reauth_source(source.id, ack)
        successor_flow_id = restarted["flow"]["flow_id"]
        self.assertNotEqual(successor_flow_id, flow_id)
        self.assertEqual(
            harness.agent_auth.start_calls,
            [("claude", True), ("claude", True)],
        )
        self.assertEqual(harness.store.config.sources[0].state.status, "needs_action")
        self.assertEqual(harness.store.config.sources[0].models, [])

        # 3. The same call against a FINISHED flow is not a cancel at all: it
        #    materializes the re-auth. Nothing polls the status first, so this call
        #    is the only thing that could have written the row.
        harness.agent_auth.complete(successor_flow_id)
        await harness.service.oauth_cancel(successor_flow_id)

        self.assertEqual(harness.agent_auth.cancelled, [flow_id])
        repaired = harness.store.config.sources[0]
        self.assertEqual(repaired.state.status, "standby")
        self.assertIn("claude-opus-4-6", [model.id for model in repaired.models])
        self.assertEqual(
            harness.service.get_agent_sources("claude")["supply_status"],
            "ok",
        )

    async def test_duplicate_native_source_is_rejected_before_login_starts(self):
        """Scenario: AUTH-SETUP-108"""
        state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(state_dir.cleanup)
        harness = NativeOAuthScenarioHarness(Path(state_dir.name))
        source = ModelHubSourceConfig.from_payload(
            {
                "id": "src_native0001",
                "created_at": "2026-07-25T00:00:00+00:00",
                "last_discovered_at": "2026-07-25T00:00:00+00:00",
                "kind": "subscription",
                "vendor": "anthropic",
                "display_name": "Claude subscription",
                "protocol": "anthropic",
                "base_url": None,
                "supply_channel": "native_cli",
                "billing": "monthly",
                "state": {
                    "status": "standby",
                    "retry_at": None,
                    "detail_key": None,
                },
                "usage": {
                    "cycle_used_pct": None,
                    "month_spend_cents": None,
                    "currency": None,
                    "projected_exhaust_at": None,
                },
                "models": [
                    {
                        "id": "claude-opus-4-6",
                        "display_name": None,
                        "origin": "discovered",
                        "reasoning_efforts": [],
                        "discovered_at": "2026-07-25T00:00:00+00:00",
                    }
                ],
                "credential_ref": None,
                "account_label": None,
                "masked_credential": None,
            }
        )
        harness.store.config.sources.append(source)

        with self.assertRaises(ModelHubError) as refused:
            await harness.service.oauth_start(
                {"vendor": "anthropic", "channel": "native_cli"}
            )

        self.assertEqual(refused.exception.code, "native_source_already_exists")
        self.assertEqual(
            refused.exception.data,
            {"existing_source_id": source.id},
        )
        self.assertEqual(harness.agent_auth.start_calls, [])

        started = await harness.service.oauth_start(
            {"vendor": "openai", "channel": "native_cli"}
        )
        self.assertEqual(started["flow"]["channel"], "native_cli")
        self.assertEqual(harness.agent_auth.start_calls, [("codex", False)])

    async def test_hub_reauth_requires_acknowledgement_and_reaches_consistent_terminal(self):
        """Scenario: AUTH-SETUP-109.

        Prewritten against merged #1326 head ea26ee6a0; recheck at implementation head.
        """
        state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(state_dir.cleanup)
        harness = HubOAuthScenarioHarness(Path(state_dir.name))
        source = ModelHubSourceConfig.from_payload(
            {
                "id": "src_hubreauth01",
                "created_at": "2026-07-25T00:00:00+00:00",
                "last_discovered_at": "2026-07-25T00:00:00+00:00",
                "kind": "subscription",
                "vendor": "anthropic",
                "display_name": "Claude Hub subscription",
                "protocol": "anthropic",
                "base_url": None,
                "supply_channel": "hub",
                "billing": "monthly",
                "state": {
                    "status": "needs_action",
                    "retry_at": None,
                    "detail_key": "models.source.needs_action.oauth_expired",
                },
                "usage": {
                    "cycle_used_pct": None,
                    "month_spend_cents": None,
                    "currency": None,
                    "projected_exhaust_at": None,
                },
                "models": [
                    {
                        "id": "claude-opus-4-6",
                        "display_name": None,
                        "origin": "discovered",
                        "reasoning_efforts": [],
                        "discovered_at": "2026-07-25T00:00:00+00:00",
                    }
                ],
                "credential_ref": "cred_hubold01",
                "account_label": None,
                "masked_credential": None,
            }
        )
        harness.store.config.sources.append(source)
        harness.store.config.agents["claude"].sources.order.append(source.id)
        harness.store.config.agents["claude"].routes["claude-opus-4-6"] = (
            ModelHubRouteConfig(
                hops=(
                    ModelHubRouteHopConfig(source.id, "claude-opus-4-6"),
                )
            )
        )

        for acknowledgement in ({}, {"acknowledge_irreversible": False}):
            with self.assertRaises(ModelHubError) as refused:
                await harness.service.reauth_source(source.id, acknowledgement)
            self.assertEqual(refused.exception.code, "reauth_confirmation_required")
            self.assertEqual(harness.adapter.flows, {})
            self.assertEqual(harness.store.config.sources[0].state.status, "needs_action")

        started = await harness.service.reauth_source(
            source.id,
            {"acknowledge_irreversible": True},
        )
        self.assertEqual(started["flow"]["channel"], "hub")
        self.assertEqual(started["flow"]["intent"], "reauth")
        self.assertEqual(len(harness.adapter.flows), 1)

        flow_id = started["flow"]["flow_id"]
        harness.adapter.complete(flow_id)
        terminal = await harness.service.oauth_status(flow_id)

        self.assertEqual(terminal["flow"]["state"], "success")
        self.assertEqual(terminal["flow"]["intent"], "reauth")
        self.assertEqual(terminal["source"], harness.service.list_sources()[0])
        self.assertEqual(terminal["source"]["id"], source.id)
        self.assertEqual(terminal["source"]["credential_ref"], "cred_consent01")
        self.assertEqual(terminal["source"]["state"]["status"], "standby")
        self.assertIn(
            "claude-opus-4-6",
            [model["id"] for model in terminal["source"]["models"]],
        )
        agent = harness.service.get_agent_sources("claude")
        self.assertEqual(agent["sources"]["order"], [source.id])
        self.assertEqual(agent["supply_status"], "ok")

    async def test_lost_model_hub_oauth_start_response_reuses_nonce_flow(self):
        """Scenario: AUTH-SETUP-210.

        Prewritten against merged #1326 head ea26ee6a0; recheck at implementation head.
        """
        state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(state_dir.cleanup)
        harness = NativeOAuthScenarioHarness(Path(state_dir.name))
        start_request = {
            "vendor": "anthropic",
            "channel": "native_cli",
            "client_nonce": "ofn_01j5w8z7p4n6q2rt",
        }

        provider_started = asyncio.Event()
        retry_started = asyncio.Event()
        release_provider = asyncio.Event()
        provider_calls = 0
        original_start = harness.agent_auth.start_web_setup

        async def blocked_provider_start(*args, **kwargs):
            nonlocal provider_calls
            provider_calls += 1
            provider_started.set()
            await release_provider.wait()
            return await original_start(*args, **kwargs)

        harness.agent_auth.start_web_setup = blocked_provider_start
        first_task = asyncio.create_task(harness.service.oauth_start(start_request))
        await asyncio.wait_for(provider_started.wait(), timeout=1)

        async def retry_after_lost_response():
            retry_started.set()
            return await harness.service.oauth_start(dict(start_request))

        retry_task = asyncio.create_task(retry_after_lost_response())
        await asyncio.wait_for(retry_started.wait(), timeout=1)
        await asyncio.sleep(0)
        self.assertFalse(retry_task.done())

        release_provider.set()
        lost_response, recovered_response = await asyncio.gather(
            first_task,
            retry_task,
        )

        lost_flow = lost_response["flow"]
        recovered_flow = recovered_response["flow"]
        self.assertEqual(lost_flow["client_nonce"], start_request["client_nonce"])
        self.assertEqual(recovered_flow["client_nonce"], start_request["client_nonce"])
        self.assertEqual(recovered_flow, lost_flow)
        self.assertEqual(provider_calls, 1)

    async def test_same_service_instance_native_login_conflict_is_localized(self):
        """Scenario: AUTH-SETUP-211; both callers share one service instance."""
        harness = AuthSetupScenarioHarness()
        harness.controller.config.language = "zh"
        process = FakeProcess()
        harness.service._start_codex_process = AsyncMock(return_value=process)
        harness.service._read_codex_output_web = AsyncMock()
        harness.service._wait_for_codex_completion_web = AsyncMock()

        web_flow = await harness.service.start_web_setup(
            "codex",
            force_reset=False,
        )
        await harness.service.start_setup(
            harness.context,
            backend="codex",
            force_reset=False,
        )

        self.assertEqual(harness.service._start_codex_process.await_count, 1)
        ScenarioExpect.text_contains(harness, "登录正在进行中")
        await harness.service.cancel_web_flow(web_flow.flow_id)

    async def test_codex_failure_scenario_emits_reset_path(self):
        """Scenario: AUTH-SETUP-202"""
        harness = AuthSetupScenarioHarness()
        fake_process = FakeProcess()
        runner = ScenarioRunner(harness)
        harness.service._start_codex_process = AsyncMock(return_value=fake_process)
        harness.service._read_codex_output = AsyncMock(return_value=None)
        harness.service._verify_login = AsyncMock(return_value=(False, "not logged in"))
        harness.service._refresh_backend_runtime = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(h.context, backend="codex", force_reset=True),
            ),
            ScenarioStep(
                "emit_device_url",
                lambda h: h.service._handle_process_text(
                    h.context,
                    "codex",
                    "Open this URL to authenticate: https://auth.openai.com/codex/device",
                ),
            ),
        )

        fake_process.finish(0)
        await harness.flow("codex").waiter_task

        harness.service._refresh_backend_runtime.assert_not_awaited()
        ScenarioExpect.step_history(runner, ["start_setup", "emit_device_url"])
        ScenarioExpect.text_contains(harness, "failed")
        ScenarioExpect.text_contains(harness, "not logged in")
        ScenarioExpect.button_callback_contains(harness, "auth_setup:codex")
        ScenarioExpect.flow_missing(harness, "C1:codex")

    async def test_claude_startup_cleanup_failure_emits_reset_path(self):
        """Scenario: AUTH-SETUP-208"""
        harness = AuthSetupScenarioHarness()
        runner = ScenarioRunner(harness)
        harness.service._start_claude_control_flow = AsyncMock(
            side_effect=RuntimeError("Failed to clear Claude Code settings env")
        )

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(
                    h.context,
                    backend="claude",
                    force_reset=True,
                    claude_login_method="console",
                ),
            ),
        )

        ScenarioExpect.step_history(runner, ["start_setup"])
        ScenarioExpect.text_contains(harness, "starting claude", index=0)
        ScenarioExpect.text_contains(harness, "failed", index=1)
        ScenarioExpect.text_contains(harness, "Failed to clear Claude Code settings env", index=1)
        ScenarioExpect.button_callback_contains(harness, "auth_setup:claude")
        ScenarioExpect.flow_missing(harness, "C1:claude")

    async def test_codex_reentry_scenario_replaces_existing_flow(self):
        """Scenario: AUTH-SETUP-201"""
        harness = AuthSetupScenarioHarness()
        first_process = FakeProcess()
        second_process = FakeProcess()
        runner = ScenarioRunner(harness)
        harness.service._start_codex_process = AsyncMock(side_effect=[first_process, second_process])
        harness.service._read_codex_output = AsyncMock(return_value=None)

        await runner.run(
            ScenarioStep(
                "start_first_setup",
                lambda h: h.service.start_setup(h.context, backend="codex", force_reset=True),
            ),
        )
        first_flow = harness.flow("codex")
        self.assertFalse(first_flow.waiter_task.done())

        await runner.run(
            ScenarioStep(
                "start_second_setup",
                lambda h: h.service.start_setup(h.context, backend="codex", force_reset=True),
            ),
        )

        second_flow = harness.flow("codex")
        self.assertIsNot(first_flow, second_flow)
        self.assertTrue(first_flow.waiter_task.cancelled())
        self.assertGreaterEqual(first_process.terminate_calls, 1)
        ScenarioExpect.step_history(runner, ["start_first_setup", "start_second_setup"])
        ScenarioExpect.text_contains(harness, "starting codex", index=0)
        ScenarioExpect.text_contains(harness, "starting codex", index=1)

    async def test_codex_device_auth_scenario_reaches_terminal_success(self):
        """Scenario: AUTH-SETUP-001"""
        harness = AuthSetupScenarioHarness()
        fake_process = FakeProcess()
        runner = ScenarioRunner(harness)
        harness.service._start_codex_process = AsyncMock(return_value=fake_process)
        harness.service._read_codex_output = AsyncMock(return_value=None)
        harness.service._verify_login = AsyncMock(return_value=(True, "Logged in using ChatGPT"))
        harness.service._refresh_backend_runtime = AsyncMock()
        harness.service._persist_backend_auth_mode = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(h.context, backend="codex", force_reset=True),
            ),
            ScenarioStep(
                "emit_device_url",
                lambda h: h.service._handle_process_text(
                    h.context,
                    "codex",
                    "Open this URL to authenticate: https://auth.openai.com/codex/device",
                ),
            ),
            ScenarioStep(
                "emit_device_code",
                lambda h: h.service._handle_process_text(
                    h.context,
                    "codex",
                    "Then enter this code: T74L-XU61D",
                ),
            ),
        )

        flow = harness.flow("codex")
        self.assertFalse(flow.waiter_task.done())
        fake_process.finish(0)
        await flow.waiter_task

        harness.service._persist_backend_auth_mode.assert_awaited_once_with("codex", "oauth")
        harness.service._refresh_backend_runtime.assert_awaited_once_with("codex")
        ScenarioExpect.step_history(runner, ["start_setup", "emit_device_url", "emit_device_code"])
        ScenarioExpect.text_contains(harness, "starting codex", index=0)
        ScenarioExpect.text_contains(harness, "https://auth.openai.com/codex/device", index=1)
        ScenarioExpect.text_contains(harness, "T74L-XU61D", index=1)
        ScenarioExpect.text_contains(harness, "codex login is active again")
        ScenarioExpect.flow_missing(harness, "C1:codex")

    async def test_codex_successful_setup_refreshes_runtime_before_the_next_turn(self):
        """Scenario: AUTH-SETUP-901"""
        harness = AuthSetupScenarioHarness()
        fake_process = FakeProcess()
        runtime = _FakeCodexNextTurnRuntime()
        runner = ScenarioRunner(harness)
        harness.controller.agent_service.agents["codex"] = SimpleNamespace(
            refresh_auth_state=AsyncMock(side_effect=runtime.refresh)
        )
        harness.service._start_codex_process = AsyncMock(return_value=fake_process)
        harness.service._read_codex_output = AsyncMock(return_value=None)
        harness.service._verify_login = AsyncMock(return_value=(True, "Logged in using ChatGPT"))

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(h.context, backend="codex", force_reset=True),
            ),
            ScenarioStep(
                "emit_device_url",
                lambda h: h.service._handle_process_text(
                    h.context,
                    "codex",
                    "Open this URL to authenticate: https://auth.openai.com/codex/device",
                ),
            ),
        )

        flow = harness.flow("codex")
        fake_process.finish(0)
        await flow.waiter_task

        await runner.run(
            ScenarioStep(
                "next_turn_after_success",
                lambda h: runtime.run_turn(),
            ),
        )

        harness.controller.agent_service.agents["codex"].refresh_auth_state.assert_awaited_once()
        ScenarioExpect.step_history(runner, ["start_setup", "emit_device_url", "next_turn_after_success"])
        ScenarioExpect.text_contains(harness, "codex login is active again")
        ScenarioExpect.flow_missing(harness, "C1:codex")

    async def test_claude_wrong_user_cannot_submit_callback_into_active_flow(self):
        """Scenario: AUTH-SETUP-103"""
        harness = AuthSetupScenarioHarness()
        fake_client = object()
        runner = ScenarioRunner(harness)
        callback_payloads = []
        harness.service._start_claude_control_flow = AsyncMock(
            return_value=(fake_client, "https://platform.claude.com/oauth/code/callback", None)
        )
        harness.service._wait_for_claude_completion = AsyncMock(return_value=None)
        harness.service._send_claude_callback = AsyncMock(
            side_effect=lambda client, authorization_code, state: callback_payloads.append((client, authorization_code, state))
        )

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(
                    h.context,
                    backend="claude",
                    force_reset=True,
                    claude_login_method="console",
                ),
            ),
        )

        intruder_context = harness.make_context(user_id="U2")
        consumed = await harness.service.maybe_consume_setup_reply(intruder_context, "auth-code#oauth-state")
        self.assertFalse(consumed)
        self.assertEqual(callback_payloads, [])

        await runner.run(
            ScenarioStep(
                "intruder_submit_callback",
                lambda h: h.service.submit_code(intruder_context, "auth-code#oauth-state", backend_hint="claude"),
            ),
        )

        ScenarioExpect.step_history(runner, ["start_setup", "intruder_submit_callback"])
        ScenarioExpect.text_contains(harness, "starting claude", index=0)
        ScenarioExpect.text_contains(harness, "https://platform.claude.com/oauth/code/callback", index=1)
        ScenarioExpect.text_contains(harness, "only the user who started this setup flow")
        self.assertIn("C1:claude", harness.service._flows)
        self.assertEqual(callback_payloads, [])

    async def test_callback_submission_and_fallback_command_do_not_double_consume_claude_flow(self):
        """Scenario: AUTH-SETUP-105"""
        harness = AuthSetupScenarioHarness()
        fake_client = object()
        completion_released = asyncio.Event()
        callback_payloads = []
        runner = ScenarioRunner(harness)

        harness.service._start_claude_control_flow = AsyncMock(
            return_value=(fake_client, "https://platform.claude.com/oauth/code/callback", None)
        )

        async def fake_control_request(client, request, timeout=900.0):
            self.assertIs(client, fake_client)
            if request["subtype"] == "claude_oauth_wait_for_completion":
                await completion_released.wait()
                return {}
            raise AssertionError(f"unexpected control request: {request}")

        async def fake_send_callback(client, authorization_code, state):
            self.assertIs(client, fake_client)
            callback_payloads.append((authorization_code, state))
            completion_released.set()

        harness.service._send_claude_control_request = AsyncMock(side_effect=fake_control_request)
        harness.service._send_claude_callback = AsyncMock(side_effect=fake_send_callback)
        harness.service._verify_login = AsyncMock(return_value=(True, '{"loggedIn": true}'))
        harness.service._refresh_backend_runtime = AsyncMock()
        harness.service._disconnect_claude_client = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(
                    h.context,
                    backend="claude",
                    force_reset=True,
                    claude_login_method="console",
                ),
            ),
            ScenarioStep(
                "submit_plain_callback",
                lambda h: h.service.maybe_consume_setup_reply(h.context, "auth-code#oauth-state"),
            ),
        )

        flow = harness.flow("claude")
        await flow.waiter_task
        self.assertEqual(callback_payloads, [("auth-code", "oauth-state")])

        await runner.run(
            ScenarioStep(
                "submit_fallback_after_completion",
                lambda h: h.service.handle_setup_command(h.context, "code auth-code#oauth-state"),
            ),
        )

        ScenarioExpect.step_history(runner, ["start_setup", "submit_plain_callback", "submit_fallback_after_completion"])
        ScenarioExpect.text_contains(harness, "submitted")
        ScenarioExpect.text_contains(harness, "there is no active setup flow")
        self.assertEqual(callback_payloads, [("auth-code", "oauth-state")])
        ScenarioExpect.flow_missing(harness, "C1:claude")

    async def test_claude_manual_callback_scenario_accepts_plain_reply_and_completes(self):
        """Scenario: AUTH-SETUP-002"""
        harness = AuthSetupScenarioHarness()
        fake_client = object()
        completion_released = asyncio.Event()
        callback_payloads = []
        runner = ScenarioRunner(harness)

        harness.service._start_claude_control_flow = AsyncMock(
            return_value=(fake_client, "https://platform.claude.com/oauth/code/callback", None)
        )

        async def fake_control_request(client, request, timeout=900.0):
            self.assertIs(client, fake_client)
            if request["subtype"] == "claude_oauth_wait_for_completion":
                await completion_released.wait()
                return {}
            raise AssertionError(f"unexpected control request: {request}")

        async def fake_send_callback(client, authorization_code, state):
            self.assertIs(client, fake_client)
            callback_payloads.append((authorization_code, state))
            completion_released.set()

        harness.service._send_claude_control_request = AsyncMock(side_effect=fake_control_request)
        harness.service._send_claude_callback = AsyncMock(side_effect=fake_send_callback)
        harness.service._verify_login = AsyncMock(return_value=(True, '{"loggedIn": true}'))
        harness.service._refresh_backend_runtime = AsyncMock()
        harness.service._disconnect_claude_client = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(
                    h.context,
                    backend="claude",
                    force_reset=True,
                    claude_login_method="console",
                ),
            ),
            ScenarioStep(
                "submit_callback_reply",
                lambda h: h.service.maybe_consume_setup_reply(h.context, "auth-code#oauth-state"),
            ),
        )

        flow = harness.flow("claude")
        self.assertFalse(flow.waiter_task.done())
        await flow.waiter_task

        self.assertEqual(callback_payloads, [("auth-code", "oauth-state")])
        harness.service._refresh_backend_runtime.assert_awaited_once_with("claude")
        harness.service._disconnect_claude_client.assert_awaited_once_with(fake_client)
        ScenarioExpect.step_history(runner, ["start_setup", "submit_callback_reply"])
        ScenarioExpect.text_contains(harness, "starting claude", index=0)
        ScenarioExpect.text_contains(harness, "https://platform.claude.com/oauth/code/callback", index=1)
        ScenarioExpect.text_contains(harness, "submitted", index=2)
        ScenarioExpect.text_contains(harness, "claude login is active again")
        ScenarioExpect.flow_missing(harness, "C1:claude")

    async def test_claude_malformed_callback_keeps_flow_active_and_instructs_retry(self):
        """Scenario: AUTH-SETUP-102"""
        harness = AuthSetupScenarioHarness()
        fake_client = object()
        runner = ScenarioRunner(harness)
        harness.service._start_claude_control_flow = AsyncMock(
            return_value=(fake_client, "https://platform.claude.com/oauth/code/callback", None)
        )
        harness.service._wait_for_claude_completion = AsyncMock(return_value=None)
        harness.service._send_claude_callback = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(
                    h.context,
                    backend="claude",
                    force_reset=True,
                    claude_login_method="console",
                ),
            ),
            ScenarioStep(
                "submit_malformed_callback",
                lambda h: h.service.submit_code(h.context, "not-a-valid-callback", backend_hint="claude"),
            ),
        )

        harness.service._send_claude_callback.assert_not_awaited()
        ScenarioExpect.step_history(runner, ["start_setup", "submit_malformed_callback"])
        ScenarioExpect.text_contains(harness, "authorizationCode#state")
        self.assertIn("C1:claude", harness.service._flows)

    async def test_concurrent_setup_flows_route_replies_to_the_matching_backend(self):
        """Scenario: AUTH-SETUP-205"""
        harness = AuthSetupScenarioHarness()
        fake_client = object()
        completion_released = asyncio.Event()
        callback_payloads = []
        runner = ScenarioRunner(harness)

        harness.service._start_claude_control_flow = AsyncMock(
            return_value=(fake_client, "https://platform.claude.com/oauth/code/callback", None)
        )
        harness.service._resolve_opencode_provider = AsyncMock(return_value="opencode")
        harness.service._install_opencode_api_key = AsyncMock()
        harness.service._refresh_backend_runtime = AsyncMock()
        harness.service._clear_backend_sessions_for_context = AsyncMock()

        async def fake_control_request(client, request, timeout=900.0):
            self.assertIs(client, fake_client)
            if request["subtype"] == "claude_oauth_wait_for_completion":
                await completion_released.wait()
                return {}
            raise AssertionError(f"unexpected control request: {request}")

        async def fake_send_callback(client, authorization_code, state):
            self.assertIs(client, fake_client)
            callback_payloads.append((authorization_code, state))
            completion_released.set()

        harness.service._send_claude_control_request = AsyncMock(side_effect=fake_control_request)
        harness.service._send_claude_callback = AsyncMock(side_effect=fake_send_callback)
        harness.service._verify_login = AsyncMock(return_value=(True, '{"loggedIn": true}'))

        await runner.run(
            ScenarioStep(
                "start_claude_setup",
                lambda h: h.service.start_setup(
                    h.context,
                    backend="claude",
                    force_reset=True,
                    claude_login_method="console",
                ),
            ),
            ScenarioStep(
                "start_opencode_setup",
                lambda h: h.service.start_setup(h.context, backend="opencode", force_reset=True),
            ),
        )

        consumed_callback = await harness.service.maybe_consume_setup_reply(harness.context, "auth-code#oauth-state")
        self.assertTrue(consumed_callback)
        self.assertEqual(callback_payloads, [("auth-code", "oauth-state")])
        self.assertTrue(harness.flow("opencode").awaiting_code)
        self.assertIn("C1:claude", harness.service._flows)

        consumed_credential = await harness.service.maybe_consume_setup_reply(
            harness.context,
            "oc_live_Abcdef1234567890",
        )
        self.assertTrue(consumed_credential)

        await harness.flow("claude").waiter_task

        harness.service._install_opencode_api_key.assert_awaited_once_with("opencode", "oc_live_Abcdef1234567890")
        harness.service._clear_backend_sessions_for_context.assert_any_await("opencode", harness.context)
        ScenarioExpect.step_history(runner, ["start_claude_setup", "start_opencode_setup"])
        ScenarioExpect.text_contains(harness, "starting claude", index=0)
        ScenarioExpect.text_contains(harness, "starting opencode", index=2)
        ScenarioExpect.text_contains(harness, "submitted")
        ScenarioExpect.text_contains(harness, "opencode login is active again")
        ScenarioExpect.text_contains(harness, "claude login is active again")
        ScenarioExpect.flow_missing(harness, "C1:claude")
        ScenarioExpect.flow_missing(harness, "C1:opencode")

    async def test_claude_timeout_emits_recoverable_terminal_state(self):
        """Scenario: AUTH-SETUP-203"""
        harness = AuthSetupScenarioHarness()
        fake_client = object()
        runner = ScenarioRunner(harness)
        completion_started = asyncio.Event()
        release_completion = asyncio.Event()

        harness.service.setup_timeout_seconds = 0.01
        harness.service._start_claude_control_flow = AsyncMock(
            return_value=(fake_client, "https://platform.claude.com/oauth/code/callback", None)
        )

        async def fake_control_request(client, request, timeout=900.0):
            self.assertIs(client, fake_client)
            if request["subtype"] == "claude_oauth_wait_for_completion":
                completion_started.set()
                await release_completion.wait()
                return {}
            raise AssertionError(f"unexpected control request: {request}")

        harness.service._send_claude_control_request = AsyncMock(side_effect=fake_control_request)
        harness.service._disconnect_claude_client = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(
                    h.context,
                    backend="claude",
                    force_reset=True,
                    claude_login_method="console",
                ),
            ),
        )

        flow = harness.flow("claude")
        await completion_started.wait()
        await flow.waiter_task

        ScenarioExpect.step_history(runner, ["start_setup"])
        ScenarioExpect.text_contains(harness, "timed out")
        ScenarioExpect.button_callback_contains(harness, "auth_setup:claude")
        ScenarioExpect.flow_missing(harness, "C1:claude")
        harness.service._disconnect_claude_client.assert_awaited_once_with(fake_client)

    async def test_opencode_direct_key_scenario_installs_key_and_refreshes_runtime(self):
        """Scenario: AUTH-SETUP-003"""
        harness = AuthSetupScenarioHarness()
        runner = ScenarioRunner(harness)
        harness.service._resolve_opencode_provider = AsyncMock(return_value="opencode")
        harness.service._install_opencode_api_key = AsyncMock()
        harness.service._refresh_backend_runtime = AsyncMock()
        harness.service._clear_backend_sessions_for_context = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(h.context, backend="opencode", force_reset=True),
            ),
        )
        flow = harness.flow("opencode")
        self.assertTrue(flow.awaiting_code)
        self.assertEqual(flow.url, "https://opencode.ai/auth")

        await runner.run(
            ScenarioStep(
                "submit_direct_credential",
                lambda h: h.service.maybe_consume_setup_reply(
                    h.context,
                    "oc_live_Abcdef1234567890",
                ),
            ),
        )

        harness.service._install_opencode_api_key.assert_awaited_once_with("opencode", "oc_live_Abcdef1234567890")
        harness.service._refresh_backend_runtime.assert_awaited_once_with("opencode")
        harness.service._clear_backend_sessions_for_context.assert_awaited_once_with("opencode", harness.context)
        ScenarioExpect.step_history(runner, ["start_setup", "submit_direct_credential"])
        ScenarioExpect.text_contains(harness, "starting opencode", index=0)
        ScenarioExpect.text_contains(harness, "https://opencode.ai/auth", index=1)
        ScenarioExpect.text_contains(harness, "opencode login is active again")
        ScenarioExpect.flow_missing(harness, "C1:opencode")

    async def test_opencode_invalid_reply_keeps_flow_recoverable_until_valid_retry(self):
        """Scenario: AUTH-SETUP-104"""
        harness = AuthSetupScenarioHarness()
        runner = ScenarioRunner(harness)
        harness.service._resolve_opencode_provider = AsyncMock(return_value="opencode")
        harness.service._install_opencode_api_key = AsyncMock()
        harness.service._refresh_backend_runtime = AsyncMock()
        harness.service._clear_backend_sessions_for_context = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(h.context, backend="opencode", force_reset=True),
            ),
        )

        flow = harness.flow("opencode")
        before_count = len(harness.rendered_texts())
        consumed_invalid = await harness.service.maybe_consume_setup_reply(harness.context, "--------------------")
        self.assertFalse(consumed_invalid)
        self.assertTrue(flow.awaiting_code)
        self.assertEqual(len(harness.rendered_texts()), before_count)
        harness.service._install_opencode_api_key.assert_not_awaited()

        await runner.run(
            ScenarioStep(
                "submit_valid_retry",
                lambda h: h.service.maybe_consume_setup_reply(
                    h.context,
                    "oc_live_Abcdef1234567890",
                ),
            ),
        )

        harness.service._install_opencode_api_key.assert_awaited_once_with("opencode", "oc_live_Abcdef1234567890")
        harness.service._refresh_backend_runtime.assert_awaited_once_with("opencode")
        harness.service._clear_backend_sessions_for_context.assert_awaited_once_with("opencode", harness.context)
        ScenarioExpect.step_history(runner, ["start_setup", "submit_valid_retry"])
        ScenarioExpect.text_contains(harness, "opencode login is active again")
        ScenarioExpect.flow_missing(harness, "C1:opencode")

    async def test_successful_setup_refreshes_runtime_before_the_next_turn(self):
        """Scenario: AUTH-SETUP-204"""
        harness = AuthSetupScenarioHarness()
        runtime = _FakeNextTurnRuntime()
        runner = ScenarioRunner(harness)

        harness.controller.agent_service.agents["opencode"] = SimpleNamespace(
            clear_sessions=AsyncMock(side_effect=runtime.clear_sessions)
        )
        harness.service._resolve_opencode_provider = AsyncMock(return_value="opencode")
        harness.service._install_opencode_api_key = AsyncMock()
        harness.service._refresh_opencode_server = AsyncMock(side_effect=runtime.refresh)

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(h.context, backend="opencode", force_reset=True),
            ),
            ScenarioStep(
                "submit_direct_credential",
                lambda h: h.service.maybe_consume_setup_reply(
                    h.context,
                    "oc_live_Abcdef1234567890",
                ),
            ),
            ScenarioStep(
                "next_turn_after_success",
                lambda h: runtime.run_turn(h.context.channel_id),
            ),
        )

        harness.service._install_opencode_api_key.assert_awaited_once_with("opencode", "oc_live_Abcdef1234567890")
        harness.service._refresh_opencode_server.assert_awaited_once()
        ScenarioExpect.step_history(runner, ["start_setup", "submit_direct_credential", "next_turn_after_success"])
        ScenarioExpect.text_contains(harness, "opencode login is active again")
        ScenarioExpect.flow_missing(harness, "C1:opencode")

    async def test_timed_out_flow_allows_clean_restart_without_stale_state(self):
        """Scenario: AUTH-SETUP-206"""
        harness = AuthSetupScenarioHarness()
        first_client = object()
        second_client = object()
        first_started = asyncio.Event()
        first_release = asyncio.Event()
        second_started = asyncio.Event()
        second_release = asyncio.Event()
        runner = ScenarioRunner(harness)

        harness.service.setup_timeout_seconds = 0.01
        harness.service._start_claude_control_flow = AsyncMock(
            side_effect=[
                (first_client, "https://platform.claude.com/oauth/code/callback?attempt=1", None),
                (second_client, "https://platform.claude.com/oauth/code/callback?attempt=2", None),
            ]
        )

        async def fake_control_request(client, request, timeout=900.0):
            if request["subtype"] != "claude_oauth_wait_for_completion":
                raise AssertionError(f"unexpected control request: {request}")
            if client is first_client:
                first_started.set()
                await first_release.wait()
                return {}
            if client is second_client:
                second_started.set()
                await second_release.wait()
                return {}
            raise AssertionError(f"unexpected client: {client!r}")

        harness.service._send_claude_control_request = AsyncMock(side_effect=fake_control_request)
        harness.service._disconnect_claude_client = AsyncMock()
        harness.service._verify_login = AsyncMock(return_value=(True, '{"loggedIn": true}'))
        harness.service._refresh_backend_runtime = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_first_setup",
                lambda h: h.service.start_setup(
                    h.context,
                    backend="claude",
                    force_reset=True,
                    claude_login_method="console",
                ),
            ),
        )

        first_flow = harness.flow("claude")
        await first_started.wait()
        await first_flow.waiter_task
        ScenarioExpect.flow_missing(harness, "C1:claude")

        await runner.run(
            ScenarioStep(
                "start_second_setup",
                lambda h: h.service.start_setup(
                    h.context,
                    backend="claude",
                    force_reset=True,
                    claude_login_method="console",
                ),
            ),
        )

        second_flow = harness.flow("claude")
        await second_started.wait()
        self.assertIsNot(first_flow, second_flow)
        self.assertIs(second_flow.claude_client, second_client)
        self.assertTrue(second_flow.login_prompt_sent)
        self.assertEqual(harness.service._start_claude_control_flow.await_count, 2)
        ScenarioExpect.step_history(runner, ["start_first_setup", "start_second_setup"])
        ScenarioExpect.text_contains(harness, "attempt=1")
        ScenarioExpect.text_contains(harness, "timed out")
        ScenarioExpect.text_contains(harness, "attempt=2")
        await harness.service._terminate_flow(second_flow)

    async def test_failed_codex_setup_does_not_leave_stale_runtime_for_next_attempt(self):
        """Scenario: AUTH-SETUP-207"""
        harness = AuthSetupScenarioHarness()
        first_process = FakeProcess()
        second_process = FakeProcess()
        runner = ScenarioRunner(harness)
        harness.service._start_codex_process = AsyncMock(side_effect=[first_process, second_process])
        harness.service._read_codex_output = AsyncMock(return_value=None)
        harness.service._verify_login = AsyncMock(side_effect=[(False, "not logged in"), (True, "Logged in using ChatGPT")])
        harness.service._refresh_backend_runtime = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_first_setup",
                lambda h: h.service.start_setup(h.context, backend="codex", force_reset=True),
            ),
            ScenarioStep(
                "emit_first_device_url",
                lambda h: h.service._handle_process_text(
                    h.context,
                    "codex",
                    "Open this URL to authenticate: https://auth.openai.com/codex/device?attempt=1",
                ),
            ),
        )

        first_flow = harness.flow("codex")
        first_process.finish(0)
        await first_flow.waiter_task
        ScenarioExpect.flow_missing(harness, "C1:codex")
        harness.service._refresh_backend_runtime.assert_not_awaited()

        await runner.run(
            ScenarioStep(
                "start_second_setup",
                lambda h: h.service.start_setup(h.context, backend="codex", force_reset=True),
            ),
            ScenarioStep(
                "emit_second_device_url",
                lambda h: h.service._handle_process_text(
                    h.context,
                    "codex",
                    "Open this URL to authenticate: https://auth.openai.com/codex/device?attempt=2",
                ),
            ),
        )

        second_flow = harness.flow("codex")
        second_process.finish(0)
        await second_flow.waiter_task

        harness.service._refresh_backend_runtime.assert_awaited_once_with("codex")
        ScenarioExpect.step_history(
            runner,
            ["start_first_setup", "emit_first_device_url", "start_second_setup", "emit_second_device_url"],
        )
        ScenarioExpect.text_contains(harness, "not logged in")
        ScenarioExpect.text_contains(harness, "codex login is active again")
        ScenarioExpect.flow_missing(harness, "C1:codex")

    async def test_opencode_waiting_key_scenario_ignores_plain_chat(self):
        """Scenario: AUTH-SETUP-101"""
        harness = AuthSetupScenarioHarness()
        runner = ScenarioRunner(harness)
        harness.service._resolve_opencode_provider = AsyncMock(return_value="opencode")
        harness.service._install_opencode_api_key = AsyncMock()
        harness.service._refresh_backend_runtime = AsyncMock()
        harness.service._clear_backend_sessions_for_context = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(h.context, backend="opencode", force_reset=True),
            ),
        )
        flow = harness.flow("opencode")
        self.assertTrue(flow.awaiting_code)
        before_count = len(harness.rendered_texts())

        consumed = await harness.service.maybe_consume_setup_reply(harness.context, "hello world")

        self.assertFalse(consumed)
        self.assertTrue(flow.awaiting_code)
        self.assertEqual(len(harness.rendered_texts()), before_count)
        harness.service._install_opencode_api_key.assert_not_awaited()
        ScenarioExpect.step_history(runner, ["start_setup"])


class CodexRelayRoundTripScenarioTests(unittest.IsolatedAsyncioTestCase):
    """Scenario: AUTH-SETUP-110.

    Closed loop for the reported regression: a relay user completes the
    OAuth transition (Settings web flow success hook), reloads Settings,
    saves API-key auth exactly the way the React form does (explicit
    ``base_url`` from the reloaded state), and the on-disk launch config
    the next ``codex app-server`` reads must point back at the relay —
    not at ``api.openai.com`` with a relay key (the 401 trap).

    Runs against BOTH relay shapes a real install can be in: a
    hand-rolled ``[model_providers.OpenAI]`` section, and the
    ``openai-managed`` provider the Settings API-key save itself creates
    (the OAuth cleanup deletes the managed section outright, which is
    why recovery rides the explicit ``oauth_relay_marker``).
    """

    def _seed_hand_rolled_relay(self) -> None:
        # The file credential store pin comes from the API-key save
        # that configured the relay (or codex's own login) and survives
        # the OAuth transition — the marker gate requires it.
        (self.home / ".codex" / "config.toml").write_text(
            "\n".join(
                [
                    'model_provider = "OpenAI"',
                    'cli_auth_credentials_store = "file"',
                    "",
                    "[model_providers.OpenAI]",
                    'name = "OpenAI"',
                    'base_url = "https://relay.example/v1"',
                    'wire_api = "responses"',
                ]
            ),
            encoding="utf-8",
        )

    def _seed_managed_relay(self) -> None:
        # The shape ``apply_codex_auth(api_key, base_url=...)`` writes:
        # pointer at our managed section. The OAuth pass later deletes
        # the whole section, leaving no on-disk relay evidence at all.
        (self.home / ".codex" / "config.toml").write_text(
            "\n".join(
                [
                    'model_provider = "openai-managed"',
                    'cli_auth_credentials_store = "file"',
                    "",
                    "[model_providers.openai-managed]",
                    'name = "OpenAI"',
                    'wire_api = "responses"',
                    'requires_openai_auth = true',
                    'base_url = "https://relay.example/v1"',
                    "supports_websockets = false",
                ]
            ),
            encoding="utf-8",
        )

    def setUp(self) -> None:
        state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(state_dir.cleanup)
        self.home = Path(state_dir.name)
        codex_home = self.home / ".codex"
        codex_home.mkdir(parents=True)
        # AVIBE_HOME isolation: the V2Config writes in this scenario go
        # through the cross-process config transaction, which resolves
        # config.json from AVIBE_HOME — keep them on the test's temp dir.
        self._codex_home_env = patch.dict(
            os.environ,
            {"CODEX_HOME": str(codex_home), "AVIBE_HOME": str(self.home)},
        )
        self._codex_home_env.start()
        self.addCleanup(self._codex_home_env.stop)

        # Seed the pre-OAuth state: API-key auth against a relay. The
        # token blob makes the post-OAuth state carry live OAuth
        # credentials — the marker gate requires them.
        (codex_home / "auth.json").write_text(
            json.dumps(
                {
                    "auth_mode": "apikey",
                    "OPENAI_API_KEY": "sk-relay",
                    "tokens": {"id_token": "seed"},
                }
            ),
            encoding="utf-8",
        )
        self._seed_hand_rolled_relay()

        self.harness = AuthSetupScenarioHarness()
        self.codex_cfg = SimpleNamespace(
            auth_mode="api_key",
            api_key="sk-relay",
            base_url=None,
            oauth_relay_marker=None,
        )
        self.harness.controller.config.agents.codex = self.codex_cfg
        self.harness.controller.config.save = lambda: None
        # Seed the real (isolated) config with the api_key pre-state: the
        # transaction computes its needs/marker decisions from the
        # lock-fresh file, so the transition must be warranted on disk.
        from config.v2_config import V2Config

        real_cfg = V2Config.default()
        real_cfg.agents.codex.auth_mode = "api_key"
        real_cfg.agents.codex.api_key = "sk-relay"
        real_cfg.save()

    def _api_module(self):
        from vibe import api as vibe_api

        return vibe_api

    def _reload_settings(self, api) -> None:
        with patch.object(api, "load_config", lambda: self.harness.controller.config):
            self._settings_state = api.get_codex_auth()

    def _save_api_key(self, api, base_url: str) -> dict:
        with (
            patch.object(api, "load_config", lambda: self.harness.controller.config),
            patch.object(api, "restart_backend", lambda name, **kwargs: {"ok": True}),
        ):
            return api.save_codex_auth(
                {
                    "auth_mode": "api_key",
                    "api_key": "sk-relay-2",
                    "base_url": base_url,
                }
            )

    async def _run_round_trip(self, runner, api) -> None:
        # Step 1 — OAuth transition: the web flow's success hook runs the
        # real persistence path (relay identity capture → pointer clear /
        # managed-section drop → V2Config marker write), exactly as after
        # a Settings "Sign in" completes.
        await runner.run(
            ScenarioStep(
                "oauth_transition",
                lambda h: h.service._invoke_post_web_success_hook("codex"),
            ),
        )

        toml = (self.home / ".codex" / "config.toml").read_text(encoding="utf-8")
        top_level_pointer = [
            line for line in toml.splitlines() if line.startswith("model_provider")
        ]
        self.assertEqual(top_level_pointer, [])
        self.assertEqual(self.codex_cfg.auth_mode, "oauth")
        self.assertEqual(
            self.codex_cfg.oauth_relay_marker,
            {"provider_id": self._expected_provider_id, "base_url": "https://relay.example/v1"},
        )

        # Step 2 — Settings reload: the Settings page refetches auth
        # state to pre-populate the form (marker-backed while the disk
        # chain is empty).
        await runner.run(
            ScenarioStep(
                "settings_reload",
                lambda h: self._reload_settings(api),
            )
        )
        state = self._settings_state
        self.assertTrue(state["ok"])
        self.assertEqual(state["base_url"], "https://relay.example/v1")

        # Step 3 — API-key save the way the React form sends it: the
        # Base URL input carries the reloaded value, so the payload
        # includes it explicitly (null here would mean "clear").
        await runner.run(
            ScenarioStep(
                "api_key_save",
                lambda h: self._save_api_key(api, state["base_url"]),
            )
        )

        # Step 4 — launch config: the next ``codex app-server`` process
        # reads these files. The captured provider identity is restored:
        # the hand-rolled section keeps its own settings and the pointer;
        # the managed shape rebuilds the managed provider. Either way
        # the relay URL survives, and the one-shot marker is consumed.
        toml = (self.home / ".codex" / "config.toml").read_text(encoding="utf-8")
        auth = json.loads((self.home / ".codex" / "auth.json").read_text(encoding="utf-8"))
        self.assertEqual(auth["OPENAI_API_KEY"], "sk-relay-2")
        self.assertEqual(auth["auth_mode"], "apikey")
        if self._expected_provider_id == "openai-managed":
            self.assertIn('model_provider = "openai-managed"', toml)
            self.assertIn('base_url = "https://relay.example/v1"', toml)
            self.assertIn("supports_websockets = false", toml)
        else:
            pointer = [line for line in toml.splitlines() if line.startswith("model_provider")]
            self.assertEqual(pointer, ['model_provider = "OpenAI"'])
            self.assertIn('base_url = "https://relay.example/v1"', toml)
            # The user's provider settings survive the round trip.
            self.assertIn('wire_api = "responses"', toml)
            self.assertNotIn("[model_providers.openai-managed]", toml)
        from config.v2_config import V2Config

        self.assertIsNone(V2Config.load().agents.codex.oauth_relay_marker)

        ScenarioExpect.step_history(
            runner, ["oauth_transition", "settings_reload", "api_key_save"]
        )

    _expected_provider_id = "OpenAI"

    async def test_codex_oauth_api_key_relay_round_trip_scenario(self) -> None:
        runner = ScenarioRunner(self.harness)
        api = self._api_module()
        await self._run_round_trip(runner, api)

    async def test_codex_oauth_api_key_round_trip_managed_provider_shape(self) -> None:
        """Same loop starting from the Settings-created managed relay —
        the shape whose on-disk evidence the OAuth cleanup deletes."""
        self._seed_managed_relay()
        self._expected_provider_id = "openai-managed"
        self.codex_cfg.base_url = "https://relay.example/v1"
        runner = ScenarioRunner(self.harness)
        api = self._api_module()
        await self._run_round_trip(runner, api)


def test_catalog_api_key_setup_observe_then_create_closed_loop(monkeypatch, tmp_path):
    """Scenario: AUTH-SETUP-111"""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", "1")
    _save_config(tmp_path)

    from core.handlers.model_hub.adapter import (
        ObservationDiscovery,
        ObservationOutcome,
        SourceObservation,
    )
    from tests.test_model_hub_api import _service

    class _CatalogAPIKeySetupHarness(SimpleNamespace):
        pass

    def make_harness(state_dir: Path) -> _CatalogAPIKeySetupHarness:
        service, store, adapter = _service(state_dir)
        return _CatalogAPIKeySetupHarness(
            service=service,
            store=store,
            adapter=adapter,
            client=app.test_client(),
            base_url="http://127.0.0.1:15131",
        )

    harness = make_harness(tmp_path / "catalog-api-key-flow")
    runner = ScenarioRunner(harness)
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: harness.service)

    def observe_catalog_pin(h) -> None:
        response = h.client.post(
            "/api/models/sources/observe",
            json={
                "vendor": "qwen",
                "key": "sk-test-auth-setup-qwen",
            },
            headers=csrf_headers(h.client, h.base_url),
            base_url=h.base_url,
        )

        assert response.status_code == 200
        observation = response.get_json()["observation"]
        assert observation["outcome"] == "observed"
        assert observation["protocol"] == "openai_chat"
        assert h.store.config.sources == []
        assert h.adapter.observed_protocol_orders == [("openai_chat",)]
        assert h.adapter.revoked == ["cred_test001"]

    def create_catalog_pin(h) -> None:
        response = h.client.post(
            "/api/models/sources",
            json={
                "kind": "api_key",
                "vendor": "qwen",
                "key": "sk-test-auth-setup-qwen",
            },
            headers=csrf_headers(h.client, h.base_url),
            base_url=h.base_url,
        )

        assert response.status_code == 201
        source = response.get_json()["source"]
        assert source["vendor"] == "qwen"
        assert source["display_name"] == "Qwen"
        assert source["protocol"] == "openai_chat"
        assert len(h.store.config.sources) == 1
        assert h.store.config.sources[0].vendor == "qwen"
        assert h.store.config.sources[0].protocol == "openai_chat"
        assert h.store.config.sources[0].credential_ref == "cred_test003"
        assert h.adapter.observed_protocol_orders == [
            ("openai_chat",),
            ("openai_chat",),
        ]
        assert h.adapter.revoked == ["cred_test001", "cred_test002"]

    def observe_auth_failure(h) -> None:
        h.adapter.observation = SourceObservation(
            outcome=ObservationOutcome.AUTHENTICATION_FAILED,
            reachable=True,
            authenticated=False,
            protocol=None,
            discovery=ObservationDiscovery.NOT_ATTEMPTED,
            models=(),
        )
        response = h.client.post(
            "/api/models/sources/observe",
            json={
                "vendor": "qwen",
                "key": "sk-test-auth-setup-qwen-invalid",
            },
            headers=csrf_headers(h.client, h.base_url),
            base_url=h.base_url,
        )

        assert response.status_code == 200
        observation = response.get_json()["observation"]
        assert observation["outcome"] == "authentication_failed"
        assert observation["authenticated"] == "rejected"
        assert len(h.store.config.sources) == 1
        assert h.adapter.observed_protocol_orders == [
            ("openai_chat",),
            ("openai_chat",),
            ("openai_chat",),
        ]
        assert h.store.config.sources[0].credential_ref == "cred_test003"
        assert h.adapter.revoked == ["cred_test001", "cred_test002", "cred_test004"]

    def create_auth_failure(h) -> None:
        response = h.client.post(
            "/api/models/sources",
            json={
                "kind": "api_key",
                "vendor": "qwen",
                "key": "sk-test-auth-setup-qwen-invalid",
            },
            headers=csrf_headers(h.client, h.base_url),
            base_url=h.base_url,
        )

        assert response.status_code == 422
        body = response.get_json()
        assert body["error"] == "discovery_failed"
        assert len(h.store.config.sources) == 1
        assert h.store.config.sources[0].vendor == "qwen"
        assert h.store.config.sources[0].protocol == "openai_chat"
        assert h.store.config.sources[0].credential_ref == "cred_test003"
        assert h.adapter.observed_protocol_orders == [
            ("openai_chat",),
            ("openai_chat",),
            ("openai_chat",),
            ("openai_chat",),
        ]
        assert h.adapter.revoked == [
            "cred_test001",
            "cred_test002",
            "cred_test004",
            "cred_test005",
        ]

    asyncio.run(
        runner.run(
            ScenarioStep("observe_catalog_pin", observe_catalog_pin),
            ScenarioStep("create_catalog_pin", create_catalog_pin),
            ScenarioStep("observe_auth_failure", observe_auth_failure),
            ScenarioStep("create_auth_failure", create_auth_failure),
        )
    )

    ScenarioExpect.step_history(
        runner,
        [
            "observe_catalog_pin",
            "create_catalog_pin",
            "observe_auth_failure",
            "create_auth_failure",
        ],
    )


@pytest.mark.parametrize(
    ("vendor", "protocol"),
    [(entry.id, entry.protocol) for entry in api_key_vendor_catalog()]
    + [("custom", protocol) for protocol in SOURCE_PROTOCOLS],
)
def test_api_key_setup_does_not_schedule_a_model(monkeypatch, tmp_path, vendor, protocol):
    """Scenario: AUTH-SETUP-112"""
    from tests.test_model_hub_api import _service
    from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
    from vibe.model_hub_runtime.state import EngineStateStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    state_store = EngineStateStore(tmp_path / "engine-state")
    transport = CLIProxyEngineAdapter(supervisor=Mock(), state_store=state_store)
    service, store, adapter = _service(tmp_path)
    # Keep engine lifecycle simulated; exercise real credential custody, HTTP
    # observation, inventory discovery, and Source admission together.
    for method in ("provision_credential", "provision_transient_credential", "revoke_credential", "observe_source"):
        monkeypatch.setattr(adapter, method, getattr(transport, method))

    async def scenario():
        requests = []
        valid_key = "test-model-free-key"
        paths = {
            "anthropic": "/v1/messages",
            "openai_responses": "/v1/responses",
            "openai_chat": "/v1/chat/completions",
        }

        async def upstream(request):
            body = await request.json() if request.method == "POST" else None
            requests.append((request.method, request.path, body))
            supplied_key = (
                request.headers.get("x-api-key")
                if protocol == "anthropic"
                else request.headers.get("Authorization", "").removeprefix("Bearer ")
            )
            if supplied_key != valid_key:
                return web.json_response({"code": "INVALID_API_KEY"}, status=401)
            if request.method == "GET":
                return web.json_response({"data": [{"id": "relay-model"}]})
            if "model" in body:
                return web.json_response(
                    {"error": {"type": "rate_limit_error", "message": "No available model capacity"}},
                    status=429,
                )
            return web.json_response(
                {"error": {"type": "invalid_request_error", "message": "model is required"}},
                status=400,
            )

        upstream_app = web.Application()
        upstream_app.router.add_post(paths[protocol], upstream)
        upstream_app.router.add_get("/v1/models", upstream)
        web_runner = web.AppRunner(upstream_app)
        await web_runner.setup()
        site = web.TCPSite(web_runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        draft = {
            "vendor": vendor,
            "protocol": protocol,
            "base_url": f"http://127.0.0.1:{port}",
            "key": valid_key,
        }
        harness = SimpleNamespace()
        runner = ScenarioRunner(harness)

        async def observe(h):
            result = await service.observe_source(draft)
            observation = result["observation"]
            assert observation["outcome"] == "observed"
            assert observation["protocol"] == protocol
            assert observation["authenticated"] == "authenticated"
            assert observation["models"] == ["relay-model"]
            assert not store.config.sources

        async def confirm(h):
            await service.create_source({"kind": "api_key", **draft})
            assert len(store.config.sources) == 1
            h.source = store.config.sources[0].to_payload()
            assert h.source["protocol"] == protocol
            assert h.source["models"][0]["id"] == "relay-model"
            assert state_store.read_api_key(h.source["credential_ref"]) == valid_key

        async def reject_invalid_key(h):
            invalid = {**draft, "key": "invalid-test-key"}
            result = await service.observe_source(invalid)
            assert result["observation"]["outcome"] == "authentication_failed"
            with pytest.raises(ModelHubError):
                await service.create_source({"kind": "api_key", **invalid})
            assert [source.to_payload() for source in store.config.sources] == [h.source]

        try:
            await runner.run(
                ScenarioStep("observe", observe),
                ScenarioStep("confirm", confirm),
                ScenarioStep("reject_invalid_key", reject_invalid_key),
            )
            ScenarioExpect.step_history(runner, ["observe", "confirm", "reject_invalid_key"])
            assert [(method, path) for method, path, _ in requests] == [
                ("POST", paths[protocol]), ("GET", "/v1/models"),
                ("POST", paths[protocol]), ("GET", "/v1/models"),
                ("POST", paths[protocol]), ("POST", paths[protocol]),
            ]
            assert all("model" not in body for _, _, body in requests if body is not None)
        finally:
            await web_runner.cleanup()

    asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
