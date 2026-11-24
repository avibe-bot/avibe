"""Hermetic unit and integration tests for Antigravity auth, probes, API routes, and desktop archive install."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import stat
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config.v2_config import AntigravityConfig, V2Config
from core.agent_auth_service import (
    ANTIGRAVITY_ADC_LOGIN_COMMAND,
    AgentAuthService,
    antigravity_acp_command,
    build_antigravity_auth_env,
    classify_auth_error,
    detect_antigravity_adc,
    probe_antigravity_passive_status,
)
from modules.im import MessageContext
from tests.helpers.fake_acp_server import FAKE_ACP_SERVER_PATH
from vibe import api, desktop_backends, ui_server


SENTINEL_SESSION_TITLE = "TOP-SECRET-EXISTING-SESSION-TITLE-999"

_FAKE_AGY_SCRIPT = textwrap.dedent(
    """\
    import asyncio
    import json
    import os
    import sys
    from pathlib import Path

    # Make repository root importable so we reuse the canonical FakeAcpServer.
    REPO_ROOT = {repo_root!r}
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    AUDIT_LOG = Path({audit_log!r})
    RPC_LOG = Path({rpc_log!r})
    STATE_FILE = Path({state_file!r})
    CONFIG_FILE = Path({config_file!r})


    def _append_jsonl(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\\n")


    def _load_cfg() -> dict:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return {{}}


    def _run_login(cfg: dict) -> int:
        if cfg.get("business_unsupported"):
            sys.stdout.write("Interactive browser login required for Gemini Enterprise.\\n")
            sys.stdout.flush()
            return 1
        sys.stdout.write("Open this URL to sign in:\\n")
        sys.stdout.write("https://accounts.google.com/o/oauth2/auth?client_id=agy&state=fixture-state\\n")
        sys.stdout.write("Enter authorization code: ")
        sys.stdout.flush()
        submitted = sys.stdin.readline().strip()
        expected = cfg.get("expected_code", "valid-agy-code")
        if submitted != expected:
            sys.stdout.write("Error: invalid authorization code\\n")
            sys.stdout.flush()
            return 1
        state = (
            json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if STATE_FILE.exists()
            else {{"authenticated": False, "next_session_num": 1, "sessions": {{}}}}
        )
        state["authenticated"] = True
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        sys.stdout.write("Login successful.\\n")
        sys.stdout.flush()
        return 0


    def _run_acp(cfg: dict) -> int:
        from tests.helpers.fake_acp_server import FakeAcpServer

        class AuditingFakeAcpServer(FakeAcpServer):
            def _handle_sync_request(self, req_id, method, params):
                _append_jsonl(RPC_LOG, {{"method": method, "params": params}})
                if method == "authenticate":
                    method_id = params.get("methodId")
                    if method_id == "gemini-api-key":
                        key = os.environ.get("GEMINI_API_KEY", "")
                        if key != cfg.get("expected_api_key", "AIza-fixture-key-1234"):
                            self._send_error(req_id, -32000, "API key not valid")
                            return
                    if method_id == "agent-platform":
                        if not os.environ.get("GOOGLE_CLOUD_PROJECT") and cfg.get("require_gcp_project"):
                            self._send_error(req_id, -32000, "Authentication required: missing project")
                            return
                super()._handle_sync_request(req_id, method, params)

            async def _handle_prompt(self, req_id, params):
                _append_jsonl(RPC_LOG, {{"method": "session/prompt", "params": params}})
                await super()._handle_prompt(req_id, params)

        server_cfg = dict(cfg.get("acp_config") or {{}})
        server_cfg.setdefault(
            "authMethods",
            [
                {{"id": "oauth-personal", "name": "Log in with Google"}},
                {{"id": "oauth-business", "name": "Log in with Gemini Enterprise"}},
                {{"id": "gemini-api-key", "name": "Gemini API key"}},
                {{"id": "agent-platform", "name": "Gemini Enterprise Agent Platform"}},
            ],
        )
        server_cfg.setdefault(
            "configOptions",
            [
                {{
                    "id": "model",
                    "name": "Model",
                    "category": "model",
                    "type": "select",
                    "currentValue": "gemini-4-neon",
                    "options": [
                        {{"value": "gemini-4-neon", "name": "Gemini 4 Neon"}},
                        {{"value": "gemini-4-carbon", "name": "Gemini 4 Carbon"}},
                    ],
                }},
            ],
        )
        server = AuditingFakeAcpServer(state_file=STATE_FILE, config=server_cfg)
        asyncio.run(server.run())
        return 0


    def main() -> int:
        argv = sys.argv[1:]
        _append_jsonl(
            AUDIT_LOG,
            {{
                "argv": argv,
                "env": {{
                    "AGY_CLI_DISABLE_AUTO_UPDATE": os.environ.get("AGY_CLI_DISABLE_AUTO_UPDATE"),
                    "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY"),
                    "GOOGLE_GEMINI_BASE_URL": os.environ.get("GOOGLE_GEMINI_BASE_URL"),
                    "GOOGLE_CLOUD_PROJECT": os.environ.get("GOOGLE_CLOUD_PROJECT"),
                    "GOOGLE_CLOUD_LOCATION": os.environ.get("GOOGLE_CLOUD_LOCATION"),
                }},
            }},
        )
        if argv == ["--version"]:
            sys.stdout.write("1.5.16\\n")
            return 0
        cfg = _load_cfg()
        if argv and argv[0] == "acp":
            if "--login" in argv:
                return _run_login(cfg)
            return _run_acp(cfg)
        sys.stderr.write(f"Unexpected argv: {{argv}}\\n")
        return 2


    if __name__ == "__main__":
        raise SystemExit(main())
    """
)


class _FakeAgyFixture:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "fake-agy"
        self.root.mkdir(parents=True, exist_ok=True)
        self.audit_log = self.root / "audit.jsonl"
        self.rpc_log = self.root / "rpc.jsonl"
        self.state_file = self.root / "state.json"
        self.config_file = self.root / "config.json"
        self.script = self.root / "fake_agy.py"
        self.binary = self.root / "agy"

        repo_root = str(FAKE_ACP_SERVER_PATH.parents[2])
        self.script.write_text(
            _FAKE_AGY_SCRIPT.format(
                repo_root=repo_root,
                audit_log=str(self.audit_log),
                rpc_log=str(self.rpc_log),
                state_file=str(self.state_file),
                config_file=str(self.config_file),
            ),
            encoding="utf-8",
        )
        self.binary.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" -u "{self.script}" "$@"\n',
            encoding="utf-8",
        )
        self.binary.chmod(0o755)
        self.set_state(authenticated=True)
        self.set_config({})

    def set_state(self, *, authenticated: bool, include_sentinel_session: bool = True) -> None:
        sessions = {}
        if include_sentinel_session:
            sessions["sess-existing"] = {
                "sessionId": "sess-existing",
                "cwd": str(self.root),
                "mcpServers": [],
                "additionalDirectories": [],
                "configOptions": [],
                "mode": "code",
                "model": "gemini-4-neon",
                "title": SENTINEL_SESSION_TITLE,
                "history": [],
            }
        self.state_file.write_text(
            json.dumps(
                {
                    "authenticated": authenticated,
                    "next_session_num": 2,
                    "sessions": sessions,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def set_config(self, payload: dict) -> None:
        self.config_file.write_text(json.dumps(payload), encoding="utf-8")

    def clear_logs(self) -> None:
        self.audit_log.unlink(missing_ok=True)
        self.rpc_log.unlink(missing_ok=True)

    def invocations(self) -> list[dict]:
        if not self.audit_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.audit_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def rpc_methods(self) -> list[str]:
        if not self.rpc_log.exists():
            return []
        return [
            json.loads(line)["method"]
            for line in self.rpc_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


@pytest.fixture
def fake_agy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeAgyFixture:
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GEMINI_BASE_URL", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)

    fixture = _FakeAgyFixture(tmp_path)
    config = V2Config.default()
    config.agents.antigravity.enabled = True
    config.agents.antigravity.cli_path = str(fixture.binary)
    config.agents.antigravity.auth_mode = "oauth-personal"
    config.save()
    return fixture


def _make_auth_service(config: V2Config | None = None) -> AgentAuthService:
    controller = SimpleNamespace(
        config=config or V2Config.load(),
        agent_service=None,
        session_handler=None,
        im_client=None,
    )
    return AgentAuthService(controller)


# ---------------------------------------------------------------------------
# 1. Passive status checks via session/list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passive_status_uses_session_list_never_infers_or_exposes_sessions(
    fake_agy: _FakeAgyFixture,
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.DEBUG)
    fake_agy.set_state(authenticated=True, include_sentinel_session=True)
    fake_agy.clear_logs()

    cfg = V2Config.load().agents.antigravity
    result = await probe_antigravity_passive_status(str(fake_agy.binary), config=cfg)

    assert result["ok"] is True
    assert result["authenticated"] is True
    assert result["credentials_present"] is True
    assert result["catalog_available"] is False
    assert result["inference_verified"] is False
    assert result["supports_logout"] is True

    invocations = fake_agy.invocations()
    assert len(invocations) == 1
    assert "--dangerously-skip-permissions" not in invocations[0]["argv"]
    assert invocations[0]["argv"] == ["acp", "--disable-telemetry"]
    assert invocations[0]["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"

    methods = fake_agy.rpc_methods()
    assert methods == ["initialize", "session/list"]
    assert "session/new" not in methods
    assert "session/prompt" not in methods

    # Existing session listings must never leak into API output or logs.
    assert SENTINEL_SESSION_TITLE not in json.dumps(result)
    assert SENTINEL_SESSION_TITLE not in caplog.text


@pytest.mark.asyncio
async def test_passive_status_reports_unauthenticated_on_minus_32000(fake_agy: _FakeAgyFixture):
    fake_agy.set_state(authenticated=False)
    fake_agy.clear_logs()

    auth = await api.get_antigravity_auth_async()

    assert auth["ok"] is True
    assert auth["authenticated"] is False
    assert auth["credentials_present"] is False
    assert auth["active_auth_mode"] == "none"
    assert auth["catalog_available"] is False
    assert auth["inference_verified"] is False
    assert auth["error"] == "not_logged_in"
    assert SENTINEL_SESSION_TITLE not in json.dumps(auth)
    assert fake_agy.rpc_methods() == ["initialize", "session/list"]


def test_http_antigravity_auth_get_and_models_routes(fake_agy: _FakeAgyFixture):
    fake_agy.set_state(authenticated=True, include_sentinel_session=True)
    fake_agy.clear_logs()
    client = ui_server.app.test_client()

    auth_resp = client.get("/api/backend/antigravity/auth")
    assert auth_resp.status_code == 200
    body = auth_resp.get_json()
    assert body["ok"] is True
    assert body["authenticated"] is True
    assert body["active_auth_mode"] == "oauth-personal"
    assert body["cli_installed"] is True
    assert body["cli_version"] == "1.5.16"
    assert body["adc_command"] == ANTIGRAVITY_ADC_LOGIN_COMMAND
    assert body["catalog_available"] is False
    assert body["inference_verified"] is False
    assert SENTINEL_SESSION_TITLE not in json.dumps(body)

    models_resp = client.get("/api/antigravity/models")
    assert models_resp.status_code == 200
    models_body = models_resp.get_json()
    assert models_body["ok"] is True
    assert "gemini-4-neon" in models_body["models"]


@pytest.mark.asyncio
async def test_get_backend_connection_uses_passive_antigravity_status(
    fake_agy: _FakeAgyFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    from vibe import internal_client

    monkeypatch.setattr(
        internal_client,
        "backend_application",
        AsyncMock(return_value={"status_code": 200, "body": {"ok": True, "state": "applied", "controller_pid": 42}}),
    )
    fake_agy.set_state(authenticated=True)
    fake_agy.clear_logs()

    conn = await api.get_backend_connection("antigravity")
    assert conn["ok"] is True
    assert conn["auth"] == "subscription"
    assert conn["ready"] is True
    assert conn["entry_eligible"] is True
    assert fake_agy.rpc_methods() == ["initialize", "session/list"]

    fake_agy.set_state(authenticated=False)
    conn_signed_out = await api.get_backend_connection("antigravity")
    assert conn_signed_out["auth"] == "none"
    assert conn_signed_out["ready"] is False


# ---------------------------------------------------------------------------
# 2. Explicit connection test distinguishing auth, catalog, and inference
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_connection_test_distinguishes_auth_catalog_and_inference(
    fake_agy: _FakeAgyFixture,
):
    service = _make_auth_service()
    fake_agy.set_state(authenticated=True)
    fake_agy.clear_logs()

    ok_result = await service.test_web_auth("antigravity", model="gemini-4-neon")
    assert ok_result["ok"] is True
    assert ok_result["authenticated"] is True
    assert ok_result["credentials_present"] is True
    assert ok_result["catalog_available"] is True
    assert ok_result["inference_verified"] is True
    assert ok_result["model"] == "gemini-4-neon"
    assert "gemini-4-neon" in ok_result["models"]
    assert "echo:Hi" in ok_result["excerpt"]

    assert fake_agy.rpc_methods() == [
        "initialize",
        "session/new",
        "session/set_config_option",
        "session/prompt",
        "session/close",
    ]
    for inv in fake_agy.invocations():
        assert "--dangerously-skip-permissions" not in inv["argv"]
        assert inv["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"

    # Auth + catalog succeed, but an unsupported Avibe-selected model fails inference
    # (never silently falls back to CLI default).
    fake_agy.clear_logs()
    bad_model = await service.test_web_auth("antigravity", model="nonexistent-model-xyz")
    assert bad_model["ok"] is False
    assert bad_model["authenticated"] is True
    assert bad_model["catalog_available"] is True
    assert bad_model["inference_verified"] is False
    assert "session/prompt" not in fake_agy.rpc_methods()
    assert "session/close" in fake_agy.rpc_methods()

    # Unauthenticated fails before catalog and inference.
    fake_agy.set_state(authenticated=False)
    fake_agy.clear_logs()
    no_auth = await service.test_web_auth("antigravity", model="gemini-4-neon")
    assert no_auth["ok"] is False
    assert no_auth["error"] == "not_logged_in"
    assert no_auth["authenticated"] is False
    assert no_auth["catalog_available"] is False
    assert no_auth["inference_verified"] is False


# ---------------------------------------------------------------------------
# 3. PTY paste-code OAuth flow, login reuse, business fallback, and ACP logout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pty_paste_code_oauth_personal_flow_and_acp_logout(fake_agy: _FakeAgyFixture):
    fake_agy.set_state(authenticated=False)
    fake_agy.set_config({"expected_code": "agy-code-7788"})
    fake_agy.clear_logs()

    service = _make_auth_service()
    service._post_web_success_hook = lambda _backend: None

    flow = await service.start_web_setup("antigravity", force_reset=True, auth_method="oauth-personal")
    assert flow.backend == "antigravity"

    # Wait for PTY reader to capture URL and enter awaiting_code.
    for _ in range(80):
        status = service.get_web_flow_status(flow.flow_id)
        if status["state"] == "awaiting_code" and status["url"]:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail(f"Antigravity flow did not reach awaiting_code: {service.get_web_flow_status(flow.flow_id)}")

    assert status["callback_kind"] == "code"
    assert status["url"].startswith("https://accounts.google.com/o/oauth2/auth")

    # Malformed code (empty or newline) is rejected cleanly while keeping flow recoverable.
    bad = await service.submit_web_code(flow.flow_id, "line1\nline2")
    assert bad == {"ok": False, "error": "invalid_format"}

    submitted = await service.submit_web_code(flow.flow_id, "agy-code-7788")
    assert submitted == {"ok": True}

    for _ in range(100):
        status = service.get_web_flow_status(flow.flow_id)
        if status["state"] in {"success", "failed"}:
            break
        await asyncio.sleep(0.05)
    assert status["state"] == "success", status
    assert status["authorization_completed"] is True
    assert flow.pty_master_fd is None

    # No Google credentials are stored in Avibe config, and no --dangerously-skip-permissions was passed.
    persisted = V2Config.load().agents.antigravity
    assert persisted.auth_mode == "oauth-personal"
    assert persisted.api_key is None
    assert any(inv["argv"] == ["acp", "--login", "--disable-telemetry"] for inv in fake_agy.invocations())
    for inv in fake_agy.invocations():
        assert "--dangerously-skip-permissions" not in inv["argv"]
        assert inv["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"

    # Reusing existing login when force_reset=False does not re-spawn --login.
    fake_agy.clear_logs()
    reused_flow = await service.start_web_setup("antigravity", force_reset=False, auth_method="oauth-personal")
    assert reused_flow.state == "success"
    assert reused_flow.authorization_completed is True
    assert all("--login" not in inv["argv"] for inv in fake_agy.invocations())

    # Sign out invokes ACP logout and clears Avibe-stored fields.
    fake_agy.clear_logs()
    removed = await service.remove_web_auth("antigravity")
    assert removed == {"ok": True}
    assert "logout" in fake_agy.rpc_methods()
    for inv in fake_agy.invocations():
        assert "--dangerously-skip-permissions" not in inv["argv"]

    after_logout = await probe_antigravity_passive_status(str(fake_agy.binary), config=V2Config.load().agents.antigravity)
    assert after_logout["authenticated"] is False


@pytest.mark.asyncio
async def test_oauth_business_surfaces_terminal_fallback_when_paste_code_unsupported(
    fake_agy: _FakeAgyFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    import httpx

    fake_agy.set_state(authenticated=False)
    fake_agy.set_config({"business_unsupported": True})
    service = _make_auth_service()
    service._post_web_success_hook = lambda _backend: None

    # Verify HTTP POST /api/backend/antigravity/auth/oauth/start forwards UI's auth_mode field
    # on the live ASGI event loop, and GET .../status/<flow_id> observes terminal fallback.
    monkeypatch.setattr(api, "_get_oauth_service", lambda: service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ui_server.app),
        base_url="http://localhost",
    ) as client:
        csrf_resp = await client.get("/api/csrf-token")
        assert csrf_resp.status_code == 200
        token = csrf_resp.json()["csrf_token"]
        client.cookies.set("vibe_csrf_token", token, domain="localhost")
        started = await client.post(
            "/api/backend/antigravity/auth/oauth/start",
            json={"force_reset": True, "auth_mode": "oauth-business"},
            headers={"Origin": "http://localhost", "X-Vibe-CSRF-Token": token},
        )
        assert started.status_code == 200
        start_body = started.json()
        assert start_body["ok"] is True
        assert start_body["auth_method"] == "oauth-business"
        assert start_body["terminal_command"] == "agy"
        assert "agy" in (start_body.get("instructions") or "")

        flow_id = start_body["flow_id"]
        status: dict = {}
        for _ in range(80):
            poll = await client.get(f"/api/backend/antigravity/auth/oauth/status/{flow_id}")
            status = poll.json()
            if status["state"] in {"failed", "success"}:
                break
            await asyncio.sleep(0.05)

    assert status["state"] == "failed"
    assert status["auth_method"] == "oauth-business"
    assert status["terminal_command"] == "agy"
    assert "agy" in (status.get("instructions") or "")
    assert "agy" in (status.get("error") or "")


# ---------------------------------------------------------------------------
# 4. API key + base URL save/remove, Agent Platform ADC, and error classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_key_save_authenticate_and_remove_never_touches_gemini_dir(
    fake_agy: _FakeAgyFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(api, "restart_backend", lambda *_args, **_kwargs: {"ok": True})
    fake_agy.set_state(authenticated=False)
    fake_agy.set_config({"expected_api_key": "AIza-fixture-key-9999"})
    fake_agy.clear_logs()

    saved = api.save_antigravity_auth(
        {
            "auth_mode": "gemini-api-key",
            "api_key": "AIza-fixture-key-9999",
            "base_url": "https://relay.example.test/gemini",
        }
    )
    assert saved["ok"] is True
    assert saved["auth_mode"] == "gemini-api-key"
    assert saved["has_api_key"] is True
    assert saved["base_url"] == "https://relay.example.test/gemini"
    assert "AIza-fixture-key-9999" not in json.dumps(saved)
    # Saving settings must NEVER invoke the CLI or trigger inference.
    assert fake_agy.invocations() == []
    # Never writes to ~/.gemini or shell profiles.
    home = Path(os.environ["HOME"])
    assert not (home / ".gemini").exists()
    assert not (home / ".bashrc").exists()
    assert not (home / ".zshrc").exists()

    # Passive probe injects GEMINI_API_KEY + GOOGLE_GEMINI_BASE_URL and authenticates.
    passive = await api.get_antigravity_auth_async()
    assert passive["authenticated"] is True
    assert passive["credentials_present"] is True
    assert passive["active_auth_mode"] == "gemini-api-key"
    assert passive["catalog_available"] is False
    assert passive["inference_verified"] is False
    acp_invs = [inv for inv in fake_agy.invocations() if inv["argv"][:1] == ["acp"]]
    assert acp_invs[-1]["env"]["GEMINI_API_KEY"] == "AIza-fixture-key-9999"
    assert acp_invs[-1]["env"]["GOOGLE_GEMINI_BASE_URL"] == "https://relay.example.test/gemini"
    assert fake_agy.rpc_methods() == ["initialize", "authenticate", "session/list"]

    # Remove API key resets V2Config without touching external files.
    removed = api.remove_backend_api_key("antigravity")
    assert removed["ok"] is True
    reloaded = V2Config.load().agents.antigravity
    assert reloaded.api_key is None
    assert reloaded.base_url is None
    assert reloaded.auth_mode == "oauth-personal"


@pytest.mark.asyncio
async def test_agent_platform_adc_detection_and_gcp_settings(
    fake_agy: _FakeAgyFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(api, "restart_backend", lambda *_args, **_kwargs: {"ok": True})
    fake_agy.set_state(authenticated=False)
    fake_agy.set_config({"require_gcp_project": True})
    fake_agy.clear_logs()

    saved = api.save_antigravity_auth(
        {
            "auth_mode": "agent-platform",
            "gcp_project": "my-gcp-project",
            "gcp_location": "us-central1",
        }
    )
    assert saved["ok"] is True
    assert saved["adc_present"] is False
    assert saved["adc_command"] == ANTIGRAVITY_ADC_LOGIN_COMMAND

    # Without ADC file, passive status reports missing ADC and never spawns inference.
    no_adc = await api.get_antigravity_auth_async()
    assert no_adc["authenticated"] is False
    assert no_adc["credentials_present"] is False
    assert no_adc["adc_present"] is False
    assert no_adc["adc_command"] == ANTIGRAVITY_ADC_LOGIN_COMMAND

    # With default gcloud ADC file present, detection succeeds read-only and authenticates.
    adc_file = Path(os.environ["HOME"]) / ".config" / "gcloud" / "application_default_credentials.json"
    adc_file.parent.mkdir(parents=True, exist_ok=True)
    adc_file.write_text('{"type":"authorized_user"}', encoding="utf-8")
    adc_info = detect_antigravity_adc()
    assert adc_info["present"] is True
    assert adc_info["source"] == "gcloud_default"
    assert adc_info["login_command"] is None

    # Explicit GOOGLE_APPLICATION_CREDENTIALS takes precedence.
    custom_adc = tmp_path / "custom-adc.json"
    custom_adc.write_text('{"type":"service_account"}', encoding="utf-8")
    env_adc = detect_antigravity_adc({"GOOGLE_APPLICATION_CREDENTIALS": str(custom_adc)})
    assert env_adc["present"] is True
    assert env_adc["source"] == "env"

    with_adc = await api.get_antigravity_auth_async()
    assert with_adc["authenticated"] is True
    assert with_adc["credentials_present"] is True
    assert with_adc["active_auth_mode"] == "agent-platform"
    acp_invs = [inv for inv in fake_agy.invocations() if inv["argv"][:1] == ["acp"]]
    assert acp_invs[-1]["env"]["GOOGLE_CLOUD_PROJECT"] == "my-gcp-project"
    assert acp_invs[-1]["env"]["GOOGLE_CLOUD_LOCATION"] == "us-central1"


def test_classify_auth_error_and_env_command_guards():
    assert classify_auth_error("antigravity", "ACP RPC error -32000: Authentication required") is True
    assert classify_auth_error("antigravity", "Error -32000") is True
    assert classify_auth_error("antigravity", "HTTP 401 Unauthorized") is True
    assert classify_auth_error("antigravity", "Could not find default credentials") is True
    assert classify_auth_error("antigravity", "API key not valid") is True
    assert classify_auth_error("antigravity", "SyntaxError: unexpected token on port 32000") is False
    assert classify_auth_error("antigravity", "") is False

    assert "--dangerously-skip-permissions" not in antigravity_acp_command("agy", login=False)
    assert "--dangerously-skip-permissions" not in antigravity_acp_command("agy", login=True)
    assert "--disable-telemetry" in antigravity_acp_command("agy", login=False)
    assert "--disable-telemetry" in antigravity_acp_command("agy", login=True)

    env = build_antigravity_auth_env(
        AntigravityConfig(auth_mode="oauth-personal", api_key="should-not-leak"),
        base_env={"GEMINI_API_KEY": "stale-shell-key", "GOOGLE_GEMINI_BASE_URL": "https://stale.example"},
    )
    assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
    assert "GEMINI_API_KEY" not in env
    assert "GOOGLE_GEMINI_BASE_URL" not in env


@pytest.mark.asyncio
async def test_maybe_emit_auth_recovery_message_branches_for_antigravity(fake_agy: _FakeAgyFixture):
    sent_plain: list[str] = []
    sent_buttons: list[tuple[str, str]] = []

    class _FakeImClient:
        async def send_message(self, _ctx, text):
            sent_plain.append(text)
            return "m1"

        async def send_message_with_buttons(self, _ctx, text, keyboard):
            sent_buttons.append((text, keyboard.buttons[0][0].callback_data))
            return "m2"

    cfg = V2Config.load()
    controller = SimpleNamespace(
        config=cfg,
        im_client=_FakeImClient(),
        emit_agent_message=AsyncMock(),
    )
    service = AgentAuthService(controller)
    ctx = MessageContext(user_id="u1", channel_id="c1", platform="slack")

    # OAuth mode -> emits Reset OAuth button.
    cfg.agents.antigravity.auth_mode = "oauth-personal"
    assert await service.maybe_emit_auth_recovery_message(ctx, "antigravity", "ACP RPC error -32000") is True
    assert sent_buttons and sent_buttons[-1][1] == "auth_setup:antigravity"

    # API-key mode -> emits Settings API key prompt (no OAuth button).
    cfg.agents.antigravity.auth_mode = "gemini-api-key"
    assert await service.maybe_emit_auth_recovery_message(ctx, "antigravity", "API key not valid") is True
    assert "API key" in sent_plain[-1]

    # Agent Platform ADC mode -> emits gcloud ADC login command.
    cfg.agents.antigravity.auth_mode = "agent-platform"
    assert await service.maybe_emit_auth_recovery_message(ctx, "antigravity", "Could not find default credentials") is True
    assert ANTIGRAVITY_ADC_LOGIN_COMMAND in sent_plain[-1]


def test_install_agent_antigravity_install_and_upgrade_disable_auto_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    recorded: list[tuple[list[str], dict | None, str]] = []

    def fake_run_install_command(name, cmd, _trunc, *, mode, env=None, **_kwargs):
        recorded.append((list(cmd), dict(env) if env else None, mode))
        return {"ok": True, "message": "ok", "output": "", "path": str(tmp_path / "agy")}

    monkeypatch.setattr(api, "_run_install_command", fake_run_install_command)
    monkeypatch.setattr(api, "is_desktop_managed_runtime", lambda: False)
    monkeypatch.setattr(api, "is_desktop_backend_path", lambda _p: False)

    # Fresh install (CLI missing) -> install.sh / install.ps1 with AGY_CLI_DISABLE_AUTO_UPDATE=true.
    cfg = V2Config.default()
    cfg.agents.antigravity.cli_path = "agy"
    cfg.save()
    monkeypatch.setattr(api, "resolve_cli_path", lambda name: f"/usr/bin/{name}" if name in {"curl", "bash", "powershell"} else None)
    monkeypatch.setattr(api, "detect_cli", lambda _p, _b: {"found": False, "path": None, "status": "missing", "version": None, "reason": "not_found"})

    fresh = api._install_agent("antigravity")
    assert fresh["ok"] is True
    assert recorded[-1][2] == "install"
    assert recorded[-1][1]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
    assert "https://antigravity.google/cli/install." in " ".join(recorded[-1][0])

    # Upgrade (CLI exists) -> [existing_path, "update"] with AGY_CLI_DISABLE_AUTO_UPDATE=true.
    existing = str(tmp_path / "agy")
    monkeypatch.setattr(api, "resolve_cli_path", lambda name: existing if name == "agy" else f"/usr/bin/{name}")
    monkeypatch.setattr(api, "detect_cli", lambda _p, _b: {"found": True, "path": existing, "status": "ok", "version": "1.5.15", "reason": None})

    upgraded = api._install_agent("antigravity")
    assert upgraded["ok"] is True
    assert recorded[-1][0] == [existing, "update"]
    assert recorded[-1][2] == "upgrade"
    assert recorded[-1][1]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"


# ---------------------------------------------------------------------------
# 5. Desktop release-archive + SHA-256 installation
# ---------------------------------------------------------------------------


def _desktop_archive_env(tmp_path: Path) -> dict[str, str]:
    """Desktop env WITHOUT Node/npm to prove archive installs do not require them."""
    runtime = tmp_path / "runtime" / "1.0.0" / "digest"
    runtime.mkdir(parents=True)
    return {
        "AVIBE_DESKTOP_RUNTIME_ROOT": str(runtime),
        "AVIBE_DESKTOP_BACKENDS_ROOT": str(tmp_path / "backends"),
        "PATH": str(tmp_path / "external-bin"),
    }


def _native_binary_bytes() -> bytes:
    if os.name == "nt":
        return b"MZ\x00\x00" + b"0" * 64
    return b"\x7fELF" + b"0" * 64


def _build_tar_gz(archive_path: Path, members: dict[str, bytes]) -> str:
    with tarfile.open(archive_path, "w:gz") as tf:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(content))
    return hashlib.sha256(archive_path.read_bytes()).hexdigest()


def _build_zip(archive_path: Path, members: dict[str, bytes]) -> str:
    with zipfile.ZipFile(archive_path, "w") as zf:
        for name, content in members.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFREG | 0o755) << 16
            zf.writestr(info, content)
    return hashlib.sha256(archive_path.read_bytes()).hexdigest()


@pytest.mark.parametrize("binary_name", ["agy", "antigravity"])
@pytest.mark.parametrize("archive_format", ["tar.gz", "zip"])
def test_desktop_archive_install_verifies_sha256_and_publishes_without_node(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    binary_name: str,
    archive_format: str,
):
    env = _desktop_archive_env(tmp_path)
    # Without Node/npm, npm toolchain returns None, while antigravity toolchain succeeds.
    assert desktop_backends.desktop_backend_toolchain(env) is None
    assert desktop_backends.desktop_backend_toolchain(env, backend="antigravity") is not None

    platform_tag = desktop_backends._release_archive_platform_tag("antigravity")
    asset_name = f"agy_cli_{platform_tag}.{archive_format}"
    source_archive = tmp_path / asset_name
    exe_name = f"{binary_name}.exe" if os.name == "nt" else binary_name
    if archive_format == "tar.gz":
        digest = _build_tar_gz(source_archive, {f"bin/{exe_name}": _native_binary_bytes()})
    else:
        digest = _build_zip(source_archive, {f"bin/{exe_name}": _native_binary_bytes()})

    release_payload = {
        "tag_name": "v1.5.16",
        "assets": [
            {
                "name": asset_name,
                "browser_download_url": f"https://github.com/google-antigravity/antigravity-cli/releases/download/v1.5.16/{asset_name}",
                "digest": f"sha256:{digest}",
            }
        ],
    }
    probe_envs: list[dict[str, str]] = []

    monkeypatch.setattr(desktop_backends, "_fetch_github_release", lambda _repo, **_kw: release_payload)
    monkeypatch.setattr(
        desktop_backends,
        "_download_release_asset",
        lambda _url, dest, **_kw: dest.write_bytes(source_archive.read_bytes()),
    )

    def fake_probe(command, *, cwd, env, timeout_seconds):
        probe_envs.append(dict(env))
        return subprocess.CompletedProcess(command, 0, "1.5.16\n", "")

    monkeypatch.setattr(desktop_backends, "_run_command", fake_probe)

    activated: list[str] = []
    result = desktop_backends.install_desktop_backend(
        "antigravity",
        base_env=env,
        activate=lambda p: activated.append(p),
    )

    assert result.backend == "antigravity"
    assert result.package == "google-antigravity/antigravity-cli"
    assert result.version == "1.5.16"
    assert result.path == activated[0]
    assert Path(result.path).name == exe_name
    assert desktop_backends.resolve_published_desktop_backend("antigravity", env) == result.path
    assert probe_envs and probe_envs[0]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"

    sub_env = desktop_backends.desktop_backend_subprocess_environment("antigravity", result.path, env)
    assert sub_env is not None
    assert sub_env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"


def test_desktop_archive_install_rejects_sha256_mismatch_before_extract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    env = _desktop_archive_env(tmp_path)
    platform_tag = desktop_backends._release_archive_platform_tag("antigravity")
    asset_name = f"agy_cli_{platform_tag}.tar.gz"
    source_archive = tmp_path / asset_name
    _build_tar_gz(source_archive, {"agy": _native_binary_bytes()})

    release_payload = {
        "tag_name": "v1.5.16",
        "assets": [
            {
                "name": asset_name,
                "browser_download_url": f"https://github.com/google-antigravity/antigravity-cli/releases/download/v1.5.16/{asset_name}",
                "digest": "sha256:" + ("0" * 64),
            }
        ],
    }

    extract_called = False

    def forbidden_extract(*_args, **_kwargs):
        nonlocal extract_called
        extract_called = True

    monkeypatch.setattr(desktop_backends, "_fetch_github_release", lambda _repo, **_kw: release_payload)
    monkeypatch.setattr(
        desktop_backends,
        "_download_release_asset",
        lambda _url, dest, **_kw: dest.write_bytes(source_archive.read_bytes()),
    )
    monkeypatch.setattr(desktop_backends, "_extract_release_archive", forbidden_extract)

    with pytest.raises(desktop_backends.DesktopBackendError) as raised:
        desktop_backends.install_desktop_backend("antigravity", base_env=env)

    assert raised.value.code == "checksum_mismatch"
    assert extract_called is False
    backend_root = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "antigravity"
    assert not (backend_root / "current.json").exists()
    assert list(backend_root.glob(".staging-*")) == []
    assert list(backend_root.glob("releases/*")) == []


def test_desktop_archive_install_rejects_missing_checksum_and_traversal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    env = _desktop_archive_env(tmp_path)
    platform_tag = desktop_backends._release_archive_platform_tag("antigravity")
    asset_name = f"agy_cli_{platform_tag}.tar.gz"

    # Missing published digest -> fail closed.
    monkeypatch.setattr(
        desktop_backends,
        "_fetch_github_release",
        lambda _repo, **_kw: {
            "tag_name": "v1.5.16",
            "assets": [
                {
                    "name": asset_name,
                    "browser_download_url": f"https://github.com/google-antigravity/antigravity-cli/releases/download/v1.5.16/{asset_name}",
                }
            ],
        },
    )
    with pytest.raises(desktop_backends.DesktopBackendError) as raised:
        desktop_backends.install_desktop_backend("antigravity", base_env=env)
    assert raised.value.code == "missing_checksum"

    # Archive with path traversal -> fail closed even if digest matches.
    evil_archive = tmp_path / "evil.tar.gz"
    evil_digest = _build_tar_gz(evil_archive, {"../outside-agy": _native_binary_bytes()})
    monkeypatch.setattr(
        desktop_backends,
        "_fetch_github_release",
        lambda _repo, **_kw: {
            "tag_name": "v1.5.16",
            "assets": [
                {
                    "name": asset_name,
                    "browser_download_url": f"https://github.com/google-antigravity/antigravity-cli/releases/download/v1.5.16/{asset_name}",
                    "digest": f"sha256:{evil_digest}",
                }
            ],
        },
    )
    monkeypatch.setattr(
        desktop_backends,
        "_download_release_asset",
        lambda _url, dest, **_kw: dest.write_bytes(evil_archive.read_bytes()),
    )
    with pytest.raises(desktop_backends.DesktopBackendError) as raised_evil:
        desktop_backends.install_desktop_backend("antigravity", base_env=env)
    assert raised_evil.value.code == "invalid_archive"
    assert not (tmp_path / "outside-agy").exists()
