import asyncio
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from config.v2_config import (
    AgentsConfig,
    ModelHubModelConfig,
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
from core.handlers.model_hub.service import ModelHubError
from modules.agents.codex.agent import CodexAgent
from tests.scenario_harness.auth_setup import AuthSetupScenarioHarness, FakeProcess
from tests.scenario_harness.core import ScenarioExpect, ScenarioRunner, ScenarioStep
from tests.scenario_harness.model_hub_native_oauth import NativeOAuthScenarioHarness
from tests.scenario_harness.organization_management import (
    REMOTE_ORIGIN,
    OrganizationManagementScenarioHarness,
)
from tests.scenario_harness.show_page_email_access import ShowPageEmailAccessScenarioHarness
from vibe import cloud_management
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
from vibe import remote_access, ui_server
from vibe.ui_server import app


class ShowPageEmailAccessScenarioTests(unittest.TestCase):
    def setUp(self):
        self.harness = ShowPageEmailAccessScenarioHarness()
        self.addCleanup(self.harness.close)

    def test_exact_email_login_is_confined_to_its_signed_show_page(self):
        """Scenario: AUTH-SETUP-401"""
        handshake = self.harness.begin_login("session-one")
        self.assertEqual(handshake["show_page_id"], "session-one")

        callback = self.harness.complete_login(handshake)
        self.assertEqual(callback.status_code, 302)
        self.assertEqual(callback.headers["Location"], handshake["next_path"])

        exact = self.harness.get(handshake["next_path"])
        other = self.harness.get("/show/session-two/__show/me")
        api = self.harness.get("/api/show-pages")
        self.assertEqual(exact.status_code, 200)
        self.assertEqual(exact.get_json(), {"authenticated": False, "canAnnotate": False})
        self.assertEqual(other.status_code, 403)
        self.assertEqual(other.get_json()["error"], "show_page_access_forbidden")
        self.assertEqual(api.status_code, 403)

        self.harness.seed_broader_session()
        existing_session_handshake = self.harness.begin_login("session-one")
        existing_session_callback = self.harness.complete_login(
            existing_session_handshake,
            instance_role="editor",
            access_source="email",
        )
        self.assertEqual(existing_session_callback.status_code, 302)
        self.assertEqual(
            existing_session_callback.headers["Location"],
            existing_session_handshake["next_path"],
        )
        self.assertEqual(
            self.harness.get(existing_session_handshake["next_path"]).status_code,
            200,
        )


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
class OrganizationManagementAuthScenarioTests(unittest.TestCase):
    def setUp(self):
        self.harness = OrganizationManagementScenarioHarness()
        self.addCleanup(self.harness.close)

    @staticmethod
    def _authorize_state(authorize_url: str) -> str:
        """The `state` the browser was actually handed, read back off its own URL."""
        query = urllib.parse.urlparse(authorize_url).query
        return urllib.parse.parse_qs(query)["state"][0]

    def test_explicit_management_sign_in_starts_interactive_handoff(self):
        """Scenario: AUTH-SETUP-301"""
        client = self.harness.remote_client()
        with patch.object(
            cloud_management,
            "begin_authorization",
            return_value=("https://avibe.bot/oauth/management/authorize?state=state-1", "state-1"),
        ) as begin:
            response = client.post(
                "/api/cloud-management/session/start",
                json={"mode": "interactive", "next": "/admin/organization/members"},
                headers=self.harness.csrf(client),
                base_url=REMOTE_ORIGIN,
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["mode"], "interactive")
        begin.assert_called_once()
        self.assertFalse(begin.call_args.kwargs["silent"])
        self.assertEqual(begin.call_args.kwargs["remote_subject"], "user-1")

    def test_silent_reauthorization_stops_after_login_required(self):
        """Scenarios: AUTH-SETUP-302, AUTH-SETUP-306"""
        client = self.harness.remote_client()
        # The real handshake store, not a stub: the callback only tears a browser's
        # session down when it proves it belongs to that browser's own live
        # handshake, so a stubbed `begin_authorization` that registers nothing would
        # exercise a forged callback rather than this browser's failed silent retry.
        with patch.object(
            cloud_management,
            "begin_authorization",
            wraps=cloud_management.begin_authorization,
        ) as begin:
            first = client.post(
                "/api/cloud-management/session/start",
                json={"mode": "silent", "next": "/admin/organization/overview"},
                headers=self.harness.csrf(client),
                base_url=REMOTE_ORIGIN,
            )
        self.assertEqual(first.status_code, 202)
        self.assertTrue(begin.call_args.kwargs["silent"])
        state = self._authorize_state(first.get_json()["authorize_url"])

        callback = client.get(
            f"/auth/organization/callback?error=login_required&state={state}",
            base_url=REMOTE_ORIGIN,
        )
        self.assertEqual(callback.status_code, 302)
        self.assertIn("cloud_management_error=login_required", callback.headers["location"])

        second = client.post(
            "/api/cloud-management/session/start",
            json={"mode": "silent", "next": "/admin/organization/overview"},
            headers=self.harness.csrf(client),
            base_url=REMOTE_ORIGIN,
        )
        self.assertEqual(second.status_code, 401)

    def test_logout_suppresses_silent_reauthorization(self):
        """Scenario: AUTH-SETUP-303"""
        client = self.harness.remote_client()
        client.set_cookie(cloud_management.HANDLE_COOKIE_NAME, "grant-1", domain="alex.avibe.bot")
        client.set_cookie(cloud_management.BROWSER_COOKIE_NAME, "browser-1", domain="alex.avibe.bot")
        logout = client.delete(
            "/api/cloud-management/session",
            headers=self.harness.csrf(client),
            base_url=REMOTE_ORIGIN,
        )
        self.assertEqual(logout.status_code, 200)
        session = client.get("/api/cloud-management/session", base_url=REMOTE_ORIGIN)
        self.assertEqual(session.status_code, 200)
        self.assertFalse(session.get_json()["can_silent_reauthorize"])

    def test_subject_mismatch_is_terminal(self):
        """Scenario: AUTH-SETUP-304"""
        client = self.harness.remote_client()
        client.set_cookie(cloud_management.HANDLE_COOKIE_NAME, "grant-1", domain="alex.avibe.bot")
        client.set_cookie(cloud_management.BROWSER_COOKIE_NAME, "browser-1", domain="alex.avibe.bot")
        with patch.object(
            cloud_management,
            "resolve_grant",
            return_value=(None, "cloud_management_subject_mismatch"),
        ):
            response = client.get("/api/cloud-management/session", base_url=REMOTE_ORIGIN)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["state"], "subject_mismatch")

    def test_invalid_callback_token_requires_manual_sign_in(self):
        """Scenario: AUTH-SETUP-305"""
        client = self.harness.remote_client()
        client.set_cookie(cloud_management.BROWSER_COOKIE_NAME, "browser-1", domain="alex.avibe.bot")
        # A real handshake for this browser: the token exchange has to fail on a
        # flow the browser actually started, otherwise this asserts what a forged
        # cross-site callback does rather than what a broken sign-in does.
        _, state = cloud_management.begin_authorization(
            self.harness.config,
            browser_id="browser-1",
            remote_subject="user-1",
            callback_origin=REMOTE_ORIGIN,
            next_path="/admin/organization/overview",
            silent=False,
        )
        with patch.object(
            cloud_management,
            "complete_authorization",
            side_effect=cloud_management.CloudManagementError(
                "invalid_cloud_management_token",
                status=400,
            ),
        ):
            response = client.get(
                f"/auth/organization/callback?code=bad-code&state={state}",
                base_url=REMOTE_ORIGIN,
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn("cloud_management_error=invalid_cloud_management_token", response.headers["location"])
        session = client.get("/api/cloud-management/session", base_url=REMOTE_ORIGIN)
        self.assertFalse(session.get_json()["can_silent_reauthorize"])

    def test_remote_callback_keeps_the_bound_subject(self):
        """Scenario: AUTH-SETUP-307"""
        backend = SimpleNamespace(base_url="https://avibe.bot")
        next_path = "/chat/session-1?tab=show-page"
        with patch.object(cloud_management, "_validated_backend", return_value=backend):
            _, state = cloud_management.begin_authorization(
                self.harness.config,
                browser_id="browser-1",
                remote_subject="user-1",
                callback_origin=REMOTE_ORIGIN,
                next_path=next_path,
                silent=False,
            )
        client = self.harness.remote_client(subject="user-2")
        client.set_cookie(cloud_management.BROWSER_COOKIE_NAME, "browser-1", domain="alex.avibe.bot")
        token_payload = {
            "access_token": "not-exposed",
            "token_type": "Bearer",
            "subject": "user-2",
            "vibe_instance_id": "inst_123",
        }
        token_claims = {
            "sub": "user-2",
            "email": "other@example.com",
            "vibe_instance_id": "inst_123",
            "exp": 4_102_444_800,
        }
        with (
            patch.object(cloud_management, "_backend_request", return_value=(200, token_payload)),
            patch.object(cloud_management, "_validate_management_token", return_value=token_claims),
        ):
            response = client.get(
                f"/auth/organization/callback?code=code-1&state={state}",
                base_url=REMOTE_ORIGIN,
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["location"],
            f"{next_path}&cloud_management_error=cloud_management_subject_mismatch",
        )
        self.assertNotIn("not-exposed", response.text)

    def test_trusted_loopback_flow_can_establish_the_first_subject(self):
        """Scenario: AUTH-SETUP-308"""
        client = self.harness.unbound_remote_client()
        client.set_cookie(cloud_management.BROWSER_COOKIE_NAME, "browser-1", domain="alex.avibe.bot")
        grant = cloud_management.ManagementGrant(
            handle="grant-1",
            browser_id="browser-1",
            subject="first-user",
            email="first@example.com",
            token="not-exposed",
            expires_at=4_102_444_800,
        )
        with patch.object(
            cloud_management,
            "complete_authorization",
            return_value=(grant, "/admin/organization/overview"),
        ) as complete:
            response = client.get(
                "/auth/organization/callback?code=code-1&state=state-1",
                base_url=REMOTE_ORIGIN,
            )
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(complete.call_args.kwargs["remote_subject"])

    def test_unbound_loopback_requires_interactive_sign_in(self):
        """Scenario: AUTH-SETUP-309"""
        client = self.harness.unbound_remote_client()
        loopback_origin = "http://127.0.0.1:15131"

        session = client.get("/api/cloud-management/session", base_url=loopback_origin)
        silent = client.post(
            "/api/cloud-management/session/start",
            json={"mode": "silent", "next": "/admin/organization/overview"},
            headers=self.harness.csrf(client, loopback_origin),
            base_url=loopback_origin,
        )

        self.assertEqual(session.status_code, 200)
        self.assertFalse(session.get_json()["can_silent_reauthorize"])
        self.assertEqual(silent.status_code, 401)
        self.assertEqual(
            silent.get_json(),
            {
                "error": "cloud_management_authorization_required",
                "retryable": False,
            },
        )

    def test_subject_mismatch_can_reenter_with_workbench_return_path(self):
        """Scenario: AUTH-SETUP-310"""
        client = self.harness.remote_client()
        client.set_cookie(cloud_management.BROWSER_COOKIE_NAME, "browser-1", domain="alex.avibe.bot")
        with patch.object(
            cloud_management,
            "complete_authorization",
            side_effect=cloud_management.CloudManagementError(
                "cloud_management_subject_mismatch",
                status=409,
            ),
        ):
            mismatch = client.get(
                "/auth/organization/callback?code=wrong-user&state=state-1",
                base_url=REMOTE_ORIGIN,
            )

        self.assertEqual(mismatch.status_code, 302)
        self.assertIn(
            "cloud_management_error=cloud_management_subject_mismatch",
            mismatch.headers["location"],
        )

        clean_next = "/chat/session-1?tab=show-page"
        with patch.object(
            cloud_management,
            "begin_authorization",
            return_value=("https://avibe.bot/oauth/management/authorize?state=state-2", "state-2"),
        ) as begin:
            reentry = client.post(
                "/api/cloud-management/session/start",
                json={"mode": "interactive", "next": clean_next},
                headers=self.harness.csrf(client),
                base_url=REMOTE_ORIGIN,
            )

        self.assertEqual(reentry.status_code, 202)
        self.assertEqual(begin.call_args.kwargs["next_path"], clean_next)

        grant = cloud_management.ManagementGrant(
            handle="grant-2",
            browser_id="browser-1",
            subject="user-1",
            email="alex@example.com",
            token="not-exposed",
            expires_at=4_102_444_800,
        )
        with patch.object(
            cloud_management,
            "complete_authorization",
            return_value=(grant, clean_next),
        ):
            success = client.get(
                "/auth/organization/callback?code=right-user&state=state-2",
                base_url=REMOTE_ORIGIN,
            )

        self.assertEqual(success.status_code, 302)
        self.assertEqual(success.headers["location"], clean_next)
        self.assertNotIn("cloud_management_error", success.headers["location"])


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
        agent._build_thread_developer_instructions = lambda _request: None
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
        harness.store.config.refresh_follow_orders()

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
        harness.store.config.refresh_follow_orders()
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


if __name__ == "__main__":
    unittest.main()
