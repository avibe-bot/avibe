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
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from config.v2_compat import to_app_config
from config.v2_config import AntigravityConfig, V2Config
from core import agent_auth_service as auth_mod
from core.agent_auth_service import (
    ANTIGRAVITY_ADC_LOGIN_COMMAND,
    AgentAuthService,
    antigravity_acp_command,
    build_antigravity_auth_env,
    classify_auth_error,
    detect_antigravity_adc,
    logout_antigravity_via_acp,
    probe_antigravity_passive_status,
)
from modules.agents import acp
from modules.agents.antigravity import AntigravityAgent
from modules.agents.antigravity import agent as agy_agent_module
from modules.agents.antigravity.auth import (
    extract_antigravity_oauth_url,
    run_antigravity_connection_probe,
)
from modules.agents.native_sessions.antigravity import AntigravityNativeSessionProvider
from modules.agents.native_sessions.service import AgentNativeSessionService
from modules.im import MessageContext
from tests.helpers.antigravity_harness import (
    SENTINEL_SESSION_TITLE,
    FakeAgyAuthFixture,
    FakeController,
    FakeIMClient,
    antigravity_config,
    app_config,
    create_fake_agy_shim,
    make_acp_capabilities,
    make_request,
    read_jsonl,
)
from vibe import api, desktop_backends, ui_server

_AUTH_ENV_VARS = ("GOOGLE_APPLICATION_CREDENTIALS", "GEMINI_API_KEY", "GOOGLE_GEMINI_BASE_URL", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")


def _clear_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _AUTH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fake_agy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeAgyAuthFixture:
    Path.home().mkdir(parents=True, exist_ok=True)
    _clear_auth_env(monkeypatch)
    fixture = FakeAgyAuthFixture(tmp_path)
    cfg = V2Config.default()
    cfg.agents.antigravity.enabled, cfg.agents.antigravity.cli_path, cfg.agents.antigravity.auth_mode = True, str(fixture.binary), "oauth-personal"
    cfg.save()
    return fixture


def _make_auth_service(config: Any = None, im_client: FakeIMClient | None = None) -> AgentAuthService:
    ctrl = SimpleNamespace(config=config or V2Config.load(), agent_service=None, session_handler=None, im_client=im_client, emit_agent_message=AsyncMock())
    svc = AgentAuthService(ctrl)
    svc._post_web_success_hook = lambda _backend: None
    return svc


def _assert_passive_invocations(fixture: FakeAgyAuthFixture) -> None:
    for inv in fixture.invocations():
        assert "--dangerously-skip-permissions" not in inv["argv"] and inv["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"


async def _poll_flow(service: AgentAuthService, flow_id: str, states: set[str], *, require_url: bool = False) -> dict[str, Any]:
    for _ in range(100):
        status = service.get_web_flow_status(flow_id)
        if status["state"] in states and (not require_url or status.get("url")):
            return status
        await asyncio.sleep(0.05)
    pytest.fail(f"Antigravity flow did not reach {states}: {service.get_web_flow_status(flow_id)}")


@pytest.mark.asyncio
async def test_passive_status_uses_session_list_never_infers_or_exposes_sessions(
    fake_agy: FakeAgyAuthFixture, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake_agy.set_state(authenticated=True, include_sentinel_session=True)
    fake_agy.clear_logs()

    res = await probe_antigravity_passive_status(str(fake_agy.binary), config=V2Config.load().agents.antigravity)
    assert (res["ok"], res["authenticated"], res["credentials_present"], res["catalog_available"], res["inference_verified"], res["supports_logout"]) == (True, True, True, False, False, True)
    assert [inv["argv"] for inv in fake_agy.invocations()] == [["acp", "--disable-telemetry"]]
    _assert_passive_invocations(fake_agy)
    assert fake_agy.rpc_methods() == ["initialize", "session/list"]
    assert SENTINEL_SESSION_TITLE not in json.dumps(res) and SENTINEL_SESSION_TITLE not in caplog.text

    fake_agy.set_state(authenticated=False)
    fake_agy.clear_logs()
    auth = await api.get_antigravity_auth_async()
    assert (auth["ok"], auth["authenticated"], auth["credentials_present"], auth["active_auth_mode"], auth["error"], auth["catalog_available"], auth["inference_verified"]) == (
        True, False, False, "none", "not_logged_in", False, False
    )
    assert SENTINEL_SESSION_TITLE not in json.dumps(auth) and fake_agy.rpc_methods() == ["initialize", "session/list"]


def test_http_antigravity_auth_get_and_models_routes(fake_agy: FakeAgyAuthFixture) -> None:
    fake_agy.set_state(authenticated=True, include_sentinel_session=True)
    fake_agy.clear_logs()
    client = ui_server.app.test_client()

    auth_resp = client.get("/api/backend/antigravity/auth")
    assert auth_resp.status_code == 200
    body = auth_resp.get_json()
    assert (body["ok"], body["authenticated"], body["active_auth_mode"], body["cli_installed"], body["cli_version"], body["adc_command"], body["catalog_available"], body["inference_verified"]) == (
        True, True, "oauth-personal", True, "1.5.16", ANTIGRAVITY_ADC_LOGIN_COMMAND, False, False
    )
    assert SENTINEL_SESSION_TITLE not in json.dumps(body)

    models_resp = client.get("/api/antigravity/models")
    assert models_resp.status_code == 200 and models_resp.get_json()["ok"] is True and "gemini-4-neon" in models_resp.get_json()["models"]


@pytest.mark.asyncio
async def test_get_backend_connection_uses_passive_antigravity_status(fake_agy: FakeAgyAuthFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    from vibe import internal_client

    monkeypatch.setattr(internal_client, "backend_application", AsyncMock(return_value={"status_code": 200, "body": {"ok": True, "state": "applied", "controller_pid": 42}}))
    fake_agy.set_state(authenticated=True)
    fake_agy.clear_logs()
    conn = await api.get_backend_connection("antigravity")
    assert (conn["ok"], conn["auth"], conn["ready"], conn["entry_eligible"]) == (True, "subscription", True, True)
    assert fake_agy.rpc_methods() == ["initialize", "session/list"]

    fake_agy.set_state(authenticated=False)
    conn_out = await api.get_backend_connection("antigravity")
    assert (conn_out["auth"], conn_out["ready"]) == ("none", False)


@pytest.mark.asyncio
async def test_explicit_connection_test_distinguishes_auth_catalog_and_inference(fake_agy: FakeAgyAuthFixture) -> None:
    service = _make_auth_service()
    fake_agy.set_state(authenticated=True)
    fake_agy.clear_logs()

    ok = await service.test_web_auth("antigravity", model="gemini-4-neon")
    assert (ok["ok"], ok["authenticated"], ok["credentials_present"], ok["catalog_available"], ok["inference_verified"], ok["model"]) == (True, True, True, True, True, "gemini-4-neon")
    assert "gemini-4-neon" in ok["models"] and "echo:Hi" in ok["excerpt"]
    assert fake_agy.rpc_methods() == ["initialize", "session/new", "session/set_config_option", "session/prompt", "session/close"]
    _assert_passive_invocations(fake_agy)

    fake_agy.clear_logs()
    bad = await service.test_web_auth("antigravity", model="nonexistent-model-xyz")
    assert (bad["ok"], bad["authenticated"], bad["catalog_available"], bad["inference_verified"]) == (False, True, True, False)
    assert "session/prompt" not in fake_agy.rpc_methods() and "session/close" in fake_agy.rpc_methods()

    fake_agy.set_state(authenticated=False)
    no_auth = await service.test_web_auth("antigravity", model="gemini-4-neon")
    assert (no_auth["ok"], no_auth["error"], no_auth["authenticated"], no_auth["catalog_available"], no_auth["inference_verified"]) == (False, "not_logged_in", False, False, False)


@pytest.mark.asyncio
async def test_pty_paste_code_oauth_personal_flow_and_acp_logout(fake_agy: FakeAgyAuthFixture) -> None:
    fake_agy.set_state(authenticated=False)
    fake_agy.set_config({"expected_code": "agy-code-7788"})
    fake_agy.clear_logs()
    service = _make_auth_service()

    flow = await service.start_web_setup("antigravity", force_reset=True, auth_method="oauth-personal")
    assert flow.backend == "antigravity"
    status = await _poll_flow(service, flow.flow_id, {"awaiting_code"}, require_url=True)
    assert status["callback_kind"] == "code" and status["url"].startswith("https://accounts.google.com/o/oauth2/auth")
    assert await service.submit_web_code(flow.flow_id, "line1\nline2") == {"ok": False, "error": "invalid_format"}
    assert await service.submit_web_code(flow.flow_id, "agy-code-7788") == {"ok": True}

    status = await _poll_flow(service, flow.flow_id, {"success", "failed"})
    assert status["state"] == "success" and status["authorization_completed"] is True and flow.pty_master_fd is None
    assert (V2Config.load().agents.antigravity.auth_mode, V2Config.load().agents.antigravity.api_key) == ("oauth-personal", None)
    assert any(inv["argv"] == ["acp", "--login", "--disable-telemetry"] for inv in fake_agy.invocations())
    _assert_passive_invocations(fake_agy)

    fake_agy.clear_logs()
    reused = await service.start_web_setup("antigravity", force_reset=False, auth_method="oauth-personal")
    assert reused.state == "success" and reused.authorization_completed is True and all("--login" not in inv["argv"] for inv in fake_agy.invocations())

    fake_agy.clear_logs()
    assert await service.remove_web_auth("antigravity") == {"ok": True} and "logout" in fake_agy.rpc_methods()
    _assert_passive_invocations(fake_agy)
    assert (await probe_antigravity_passive_status(str(fake_agy.binary), config=V2Config.load().agents.antigravity))["authenticated"] is False


@pytest.mark.asyncio
async def test_try_reuse_rejects_cross_mode_switch_and_oauth_url_buffers_split_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    probe_calls: list[str] = []

    async def _fake_probe(_binary: str, *, auth_mode: str | None = None, **_kw: Any) -> dict[str, Any]:
        probe_calls.append(str(auth_mode))
        return {"ok": True, "authenticated": True}

    monkeypatch.setattr(auth_mod, "probe_antigravity_passive_status", _fake_probe)
    service = AgentAuthService.__new__(AgentAuthService)
    saved_mode = {"value": "oauth-personal"}
    service._resolve_backend_config = lambda _b: SimpleNamespace(auth_mode=saved_mode["value"])
    service._get_cli_binary = lambda _b: "agy"

    assert await service._try_reuse_antigravity_login(SimpleNamespace(auth_method="oauth-business")) is False
    saved_mode["value"] = "oauth-business"
    assert await service._try_reuse_antigravity_login(SimpleNamespace(auth_method="oauth-personal")) is False and probe_calls == []
    assert await service._try_reuse_antigravity_login(SimpleNamespace(auth_method="oauth-business")) is True and probe_calls == ["oauth-business"]

    p1, p2, p3 = "Open URL: \x1b[36mhttps://accounts.google.com/o/oauth2/auth?client_id=agy", "&state=split-token-123\x1b[0m", "\nPaste code: "
    expected = "https://accounts.google.com/o/oauth2/auth?client_id=agy&state=split-token-123"
    assert extract_antigravity_oauth_url(p1, eof=False) is None and extract_antigravity_oauth_url(p1 + "&state=half", eof=False) is None
    assert extract_antigravity_oauth_url(p1 + p2, eof=False) == expected and extract_antigravity_oauth_url(p1 + p2 + p3, eof=False) == expected
    assert extract_antigravity_oauth_url(p1 + "&state=eof-only", eof=True) == "https://accounts.google.com/o/oauth2/auth?client_id=agy&state=eof-only"


@pytest.mark.asyncio
async def test_oauth_business_surfaces_terminal_fallback_when_paste_code_unsupported(
    fake_agy: FakeAgyAuthFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_agy.set_state(authenticated=False)
    fake_agy.set_config({"business_unsupported": True})
    service = _make_auth_service()
    monkeypatch.setattr(api, "_get_oauth_service", lambda: service)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_server.app), base_url="http://localhost") as client:
        token = (await client.get("/api/csrf-token")).json()["csrf_token"]
        client.cookies.set("vibe_csrf_token", token, domain="localhost")
        start_body = (
            await client.post(
                "/api/backend/antigravity/auth/oauth/start",
                json={"force_reset": True, "auth_mode": "oauth-business"},
                headers={"Origin": "http://localhost", "X-Vibe-CSRF-Token": token},
            )
        ).json()
        assert (start_body["ok"], start_body["auth_method"], start_body["terminal_command"]) == (True, "oauth-business", "agy")
        assert "agy" in (start_body.get("instructions") or "")

        status: dict[str, Any] = {}
        for _ in range(80):
            status = (await client.get(f"/api/backend/antigravity/auth/oauth/status/{start_body['flow_id']}")).json()
            if status.get("state") in {"failed", "success"}:
                break
            await asyncio.sleep(0.05)

    assert (status["state"], status["auth_method"], status["terminal_command"]) == ("failed", "oauth-business", "agy")
    assert "agy" in (status.get("instructions") or "") and "agy" in (status.get("error") or "")


@pytest.mark.asyncio
async def test_antigravity_logout_without_logout_capability_distinguishes_active_vs_unauthenticated(fake_agy: FakeAgyAuthFixture) -> None:
    fake_agy.set_config({"expected_api_key": "AIza-fixture-key-9999", "acp_config": {"agentCapabilities": make_acp_capabilities(logout=False)}})

    v2 = V2Config.load()
    v2.agents.antigravity.auth_mode, v2.agents.antigravity.api_key, v2.agents.antigravity.base_url = "gemini-api-key", "AIza-fixture-key-9999", "https://relay.example.test/gemini"
    v2.agents.antigravity.gcp_project, v2.agents.antigravity.gcp_location = "proj-1", "us-central1"
    v2.save()

    fake_agy.set_state(authenticated=True)
    fake_agy.clear_logs()
    assert await logout_antigravity_via_acp(str(fake_agy.binary), config=v2.agents.antigravity) == (False, "logout_unsupported")
    assert fake_agy.rpc_methods() == ["initialize", "session/list"]
    inv_env = fake_agy.invocations()[-1]["env"]
    assert "GEMINI_API_KEY" not in inv_env and "GOOGLE_GEMINI_BASE_URL" not in inv_env

    assert await _make_auth_service().remove_web_auth("antigravity") == {"ok": True, "partial": True, "warning": "logout_failed", "detail": "logout_unsupported"}
    c = V2Config.load().agents.antigravity
    assert (c.auth_mode, c.api_key, c.base_url, c.gcp_project, c.gcp_location) == ("oauth-personal", None, None, None, None)

    v2_api = V2Config.load()
    v2_api.agents.antigravity.auth_mode, v2_api.agents.antigravity.api_key = "gemini-api-key", "AIza-fixture-key-9999"
    v2_api.save()
    fake_agy.set_state(authenticated=False)
    fake_agy.clear_logs()
    assert await logout_antigravity_via_acp(str(fake_agy.binary), config=v2_api.agents.antigravity) == (True, None)
    assert fake_agy.rpc_methods() == ["initialize", "session/list"]
    svc = _make_auth_service()
    assert await svc.remove_web_auth("antigravity") == {"ok": True} and await svc.remove_web_auth("antigravity") == {"ok": True}

    fake_agy.set_config({"acp_config": {"agentCapabilities": {"loadSession": True, "sessionCapabilities": {}}}})
    fake_agy.clear_logs()
    assert await logout_antigravity_via_acp(str(fake_agy.binary), config=V2Config.load().agents.antigravity) == (False, "logout_unsupported")
    assert fake_agy.rpc_methods() == ["initialize"]


@pytest.mark.asyncio
async def test_api_key_save_authenticate_and_remove_never_touches_gemini_dir(fake_agy: FakeAgyAuthFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "restart_backend", lambda *_a, **_kw: {"ok": True})
    fake_agy.set_state(authenticated=False)
    fake_agy.set_config({"expected_api_key": "AIza-fixture-key-9999"})
    fake_agy.clear_logs()

    saved = api.save_antigravity_auth({"auth_mode": "gemini-api-key", "api_key": "AIza-fixture-key-9999", "base_url": "https://relay.example.test/gemini"})
    assert (saved["ok"], saved["auth_mode"], saved["has_api_key"], saved["base_url"]) == (True, "gemini-api-key", True, "https://relay.example.test/gemini")
    assert "AIza-fixture-key-9999" not in json.dumps(saved) and fake_agy.invocations() == []
    assert not any((Path.home() / name).exists() for name in (".gemini", ".bashrc", ".zshrc"))

    passive = await api.get_antigravity_auth_async()
    assert (passive["authenticated"], passive["credentials_present"], passive["active_auth_mode"], passive["catalog_available"], passive["inference_verified"]) == (True, True, "gemini-api-key", False, False)
    acp_env = [inv for inv in fake_agy.invocations() if inv["argv"][:1] == ["acp"]][-1]["env"]
    assert (acp_env["GEMINI_API_KEY"], acp_env["GOOGLE_GEMINI_BASE_URL"]) == ("AIza-fixture-key-9999", "https://relay.example.test/gemini")
    assert fake_agy.rpc_methods() == ["initialize", "authenticate", "session/list"]

    assert api.remove_backend_api_key("antigravity")["ok"] is True
    r = V2Config.load().agents.antigravity
    assert (r.api_key, r.base_url, r.auth_mode) == (None, None, "oauth-personal")


@pytest.mark.asyncio
async def test_agent_platform_adc_detection_and_gcp_settings(fake_agy: FakeAgyAuthFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "restart_backend", lambda *_a, **_kw: {"ok": True})
    fake_agy.set_state(authenticated=False)
    fake_agy.set_config({"acp_config": {"requireAuthMethod": "oauth-personal"}})
    fake_agy.clear_logs()

    saved = api.save_antigravity_auth({"auth_mode": "agent-platform", "gcp_project": "my-gcp-project", "gcp_location": "us-central1"})
    assert (saved["ok"], saved["adc_present"], saved["adc_command"]) == (True, False, ANTIGRAVITY_ADC_LOGIN_COMMAND)

    cfg = V2Config.load().agents.antigravity
    rejected_passive = await probe_antigravity_passive_status(str(fake_agy.binary), config=cfg)
    assert (rejected_passive["authenticated"], rejected_passive["credentials_present"], rejected_passive["error"], rejected_passive["adc_command"]) == (
        False, False, "not_logged_in", ANTIGRAVITY_ADC_LOGIN_COMMAND
    )
    assert fake_agy.rpc_methods() == ["initialize", "authenticate"]

    fake_agy.clear_logs()
    rejected_conn = await run_antigravity_connection_probe(binary=str(fake_agy.binary), cwd=str(tmp_path), model="gemini-4-neon", config=cfg)
    assert (rejected_conn["ok"], rejected_conn["authenticated"], rejected_conn["error"], rejected_conn["login_command"]) == (
        False, False, "not_logged_in", ANTIGRAVITY_ADC_LOGIN_COMMAND
    )
    assert fake_agy.rpc_methods() == ["initialize", "authenticate"]

    no_adc = await api.get_antigravity_auth_async()
    assert (no_adc["authenticated"], no_adc["credentials_present"], no_adc["adc_present"], no_adc["adc_command"], no_adc["error"]) == (
        False, False, False, ANTIGRAVITY_ADC_LOGIN_COMMAND, "not_logged_in"
    )

    # Metadata server / attached service account: no local ADC file, but official `agy` authenticates `agent-platform`.
    fake_agy.set_state(authenticated=False)
    fake_agy.set_config({"require_gcp_project": True})
    fake_agy.clear_logs()
    meta_passive = await probe_antigravity_passive_status(str(fake_agy.binary), config=cfg)
    assert (meta_passive["ok"], meta_passive["authenticated"], meta_passive["credentials_present"], meta_passive["error"]) == (True, True, True, None)
    assert fake_agy.rpc_methods() == ["initialize", "authenticate", "session/list"]

    fake_agy.clear_logs()
    meta_conn = await run_antigravity_connection_probe(binary=str(fake_agy.binary), cwd=str(tmp_path), model="gemini-4-neon", config=cfg)
    assert (meta_conn["ok"], meta_conn["authenticated"], meta_conn["credentials_present"], meta_conn["inference_verified"]) == (True, True, True, True)
    assert fake_agy.rpc_methods()[:3] == ["initialize", "authenticate", "session/new"]

    meta_api = await api.get_antigravity_auth_async()
    assert (meta_api["authenticated"], meta_api["active_auth_mode"], meta_api["adc_present"], meta_api["error"]) == (True, "agent-platform", False, None)

    adc_file = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    adc_file.parent.mkdir(parents=True, exist_ok=True)
    adc_file.write_text('{"type":"authorized_user"}', encoding="utf-8")
    adc_info = detect_antigravity_adc()
    assert (adc_info["present"], adc_info["source"], adc_info["login_command"]) == (True, "gcloud_default", None)

    custom_adc = tmp_path / "custom-adc.json"
    custom_adc.write_text('{"type":"service_account"}', encoding="utf-8")
    env_adc = detect_antigravity_adc({"GOOGLE_APPLICATION_CREDENTIALS": str(custom_adc)})
    assert (env_adc["present"], env_adc["source"]) == (True, "env")

    with_adc = await api.get_antigravity_auth_async()
    assert (with_adc["authenticated"], with_adc["credentials_present"], with_adc["active_auth_mode"]) == (True, True, "agent-platform")
    acp_env = [inv for inv in fake_agy.invocations() if inv["argv"][:1] == ["acp"]][-1]["env"]
    assert (acp_env["GOOGLE_CLOUD_PROJECT"], acp_env["GOOGLE_CLOUD_LOCATION"]) == ("my-gcp-project", "us-central1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_id", "config_opts", "set_opt_behavior", "set_model_resp", "prompt_result", "expected_error", "expect_prompt"),
    [
        (
            "config_option_model_mismatch",
            [{"id": "model", "category": "model", "currentValue": "gemini-4-neon", "options": [{"value": "gemini-4-neon"}, {"value": "gemini-4-carbon"}]}],
            {"configOptions": [{"id": "model", "category": "model", "currentValue": "gemini-4-carbon", "options": [{"value": "gemini-4-neon"}, {"value": "gemini-4-carbon"}]}]},
            {},
            {"stopReason": "end_turn"},
            "model_mismatch",
            False,
        ),
        (
            "set_model_fallback_mismatch",
            [{"id": "model", "category": "model", "currentValue": "gemini-4-neon", "options": [{"value": "gemini-4-neon"}, {"value": "gemini-4-carbon"}]}],
            "method_not_found",
            {"currentModelId": "gemini-4-carbon"},
            {"stopReason": "end_turn"},
            "model_mismatch",
            False,
        ),
        (
            "stop_reason_max_tokens",
            [{"id": "model", "category": "model", "currentValue": "gemini-4-neon", "options": [{"value": "gemini-4-neon"}]}],
            {"configOptions": [{"id": "model", "category": "model", "currentValue": "gemini-4-neon", "options": [{"value": "gemini-4-neon"}]}]},
            {},
            {"stopReason": "max_tokens"},
            "incomplete_response",
            True,
        ),
        (
            "stop_reason_missing",
            [{"id": "model", "category": "model", "currentValue": "gemini-4-neon", "options": [{"value": "gemini-4-neon"}]}],
            {"configOptions": [{"id": "model", "category": "model", "currentValue": "gemini-4-neon", "options": [{"value": "gemini-4-neon"}]}]},
            {},
            {},
            "incomplete_response",
            True,
        ),
    ],
)
async def test_connection_probe_validates_model_receipt_and_end_turn_stop_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
    config_opts: list[dict[str, Any]],
    set_opt_behavior: Any,
    set_model_resp: dict[str, Any],
    prompt_result: dict[str, Any],
    expected_error: str,
    expect_prompt: bool,
) -> None:
    del case_id
    observed = {"prompt_called": False, "closed": False}

    class _StubAcpClient:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            self.supports_close = True
            self._notify: Any = None

        async def __aenter__(self) -> _StubAcpClient:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        def on_notification(self, cb: Any) -> None:
            self._notify = cb

        async def initialize(self, **_kw: Any) -> dict[str, Any]:
            return {"protocolVersion": 1}

        async def authenticate(self, _method_id: str) -> dict[str, Any]:
            return {}

        async def session_new(self, _cwd: str, **_kw: Any) -> dict[str, Any]:
            return {"sessionId": "sess-probe-1", "configOptions": config_opts}

        async def session_set_config_option(self, _sid: str, _cid: str, _val: Any) -> dict[str, Any]:
            if set_opt_behavior == "method_not_found":
                raise acp.AcpRpcError(acp.JSONRPC_METHOD_NOT_FOUND, "Method not found")
            return dict(set_opt_behavior)

        async def session_set_model(self, _sid: str, _model: str) -> dict[str, Any]:
            return dict(set_model_resp)

        async def session_prompt(self, _sid: str, _blocks: Any, **_kw: Any) -> dict[str, Any]:
            observed["prompt_called"] = True
            if self._notify is not None:
                await self._notify(
                    "session/update",
                    {"sessionId": "sess-probe-1", "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "partial reply"}}},
                )
            return dict(prompt_result)

        async def session_close(self, _sid: str) -> dict[str, Any]:
            observed["closed"] = True
            return {}

    monkeypatch.setattr(acp, "AcpClient", _StubAcpClient)
    res = await run_antigravity_connection_probe(
        binary="/fake/agy",
        cwd=str(tmp_path),
        model="gemini-4-neon",
        config=AntigravityConfig(auth_mode="oauth-personal"),
    )
    assert (res["ok"], res["authenticated"], res["catalog_available"], res["inference_verified"], res["error"]) == (
        False, True, True, False, expected_error
    )
    assert observed == {"prompt_called": expect_prompt, "closed": True}


def test_classify_auth_error_and_env_command_guards() -> None:
    for msg, exp in (
        ("ACP RPC error -32000: Authentication required", True),
        ("Error -32000", True),
        ("HTTP 401 Unauthorized", True),
        ("Could not find default credentials", True),
        ("API key not valid", True),
        ("SyntaxError: unexpected token on port 32000", False),
        ("", False),
    ):
        assert classify_auth_error("antigravity", msg) is exp
    for login in (False, True):
        cmd = antigravity_acp_command("agy", login=login)
        assert "--dangerously-skip-permissions" not in cmd and "--disable-telemetry" in cmd
    env = build_antigravity_auth_env(
        AntigravityConfig(auth_mode="oauth-personal", api_key="should-not-leak"),
        base_env={"GEMINI_API_KEY": "stale-shell-key", "GOOGLE_GEMINI_BASE_URL": "https://stale.example"},
    )
    assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true" and "GEMINI_API_KEY" not in env and "GOOGLE_GEMINI_BASE_URL" not in env


@pytest.mark.asyncio
async def test_maybe_emit_auth_recovery_message_branches_for_antigravity(fake_agy: FakeAgyAuthFixture) -> None:
    im_client, cfg = FakeIMClient(), V2Config.load()
    service, ctx = _make_auth_service(cfg, im_client=im_client), MessageContext(user_id="u1", channel_id="c1", platform="slack")

    cfg.agents.antigravity.auth_mode = "oauth-personal"
    assert await service.maybe_emit_auth_recovery_message(ctx, "antigravity", "ACP RPC error -32000") is True
    assert im_client.sent_buttons and im_client.sent_buttons[-1][1] == "auth_setup:antigravity"

    for auth_mode, err_msg, expected in (("gemini-api-key", "API key not valid", "API key"), ("agent-platform", "Could not find default credentials", ANTIGRAVITY_ADC_LOGIN_COMMAND)):
        cfg.agents.antigravity.auth_mode = auth_mode
        assert await service.maybe_emit_auth_recovery_message(ctx, "antigravity", err_msg) is True and expected in im_client.sent_plain[-1]


@pytest.mark.asyncio
async def test_antigravity_auth_service_expands_tilde_cli_path_non_desktop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir, empty_path = tmp_path / "repo", tmp_path / "empty-path"
    workdir.mkdir()
    empty_path.mkdir()
    monkeypatch.delenv("AVIBE_DESKTOP_MANAGED_RUNTIME", raising=False)
    monkeypatch.setenv("PATH", str(empty_path))
    _clear_auth_env(monkeypatch)

    state_file, rpc_log, cfg_file = tmp_path / "tilde-state.json", tmp_path / "tilde-rpc.jsonl", tmp_path / "tilde-cfg.json"
    state_file.write_text(json.dumps({"authenticated": False, "next_session_num": 1, "sessions": {}}), encoding="utf-8")
    cfg_file.write_text(json.dumps({"expected_code": "tilde-code-42"}), encoding="utf-8")
    tilde_shim = create_fake_agy_shim(Path.home() / ".local" / "bin", state_file=state_file, launch_log=tmp_path / "tilde-launches.jsonl", rpc_log=rpc_log, config_file=cfg_file, auth_mode=True, name="agy")

    v2 = V2Config.default()
    v2.runtime.default_cwd, v2.agents.antigravity.enabled, v2.agents.antigravity.cli_path, v2.agents.antigravity.auth_mode = str(workdir), True, "~/.local/bin/agy", "oauth-personal"
    v2.save()

    # Both V2Config and AppCompatConfig shapes expand ~/.local/bin/agy to an absolute path.
    service = _make_auth_service(V2Config.load())
    compat_service = _make_auth_service(to_app_config(V2Config.load(), resolve_agent_paths=False))
    assert service._get_cli_binary("antigravity") == str(tilde_shim.absolute()) == compat_service._get_cli_binary("antigravity")

    flow = await service.start_web_setup("antigravity", force_reset=True, auth_method="oauth-personal")
    await _poll_flow(service, flow.flow_id, {"awaiting_code"}, require_url=True)
    assert await service.submit_web_code(flow.flow_id, "tilde-code-42") == {"ok": True}
    assert (await _poll_flow(service, flow.flow_id, {"success", "failed"}))["state"] == "success"

    assert (await probe_antigravity_passive_status("~/.local/bin/agy", config=v2.agents.antigravity))["authenticated"] is True
    monkeypatch.setattr(api, "detect_cli_async", AsyncMock(return_value={"found": True, "path": "", "status": "ok", "version": "1.5.16"}))
    assert (await api.get_antigravity_auth_async())["authenticated"] is True
    conn = await service.test_web_auth("antigravity", model="gemini-4-neon")
    assert (conn["ok"], conn["authenticated"], conn["catalog_available"], conn["inference_verified"]) == (True, True, True, True)

    rpc_log.unlink(missing_ok=True)
    assert await service.remove_web_auth("antigravity") == {"ok": True}
    assert "logout" in [e["method"] for e in read_jsonl(rpc_log) if "method" in e]
    assert (await probe_antigravity_passive_status("~/.local/bin/agy", config=v2.agents.antigravity))["authenticated"] is False


@pytest.mark.asyncio
async def test_desktop_runtime_resolves_bare_agy_before_command_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir, empty_path = tmp_path / "repo", tmp_path / "empty-path"
    workdir.mkdir()
    empty_path.mkdir()
    state_file = tmp_path / "desktop-state.json"
    state_file.write_text(
        json.dumps({"authenticated": True, "next_session_num": 2, "sessions": {"sess-desktop-1": {"sessionId": "sess-desktop-1", "cwd": str(workdir), "title": "Desktop session", "history": []}}}),
        encoding="utf-8",
    )
    private_shim = create_fake_agy_shim(tmp_path / "desktop-private" / "antigravity" / "bin", state_file=state_file, launch_log=tmp_path / "desktop-launches.jsonl", name="agy")
    monkeypatch.setenv("AVIBE_DESKTOP_MANAGED_RUNTIME", "1")
    monkeypatch.setenv("PATH", str(empty_path))

    resolve_calls: list[tuple[str, bool]] = []
    desktop_env_calls: list[str] = []

    def _fake_resolve(binary: str, *, include_npm_global: bool = True, **_kw: Any) -> str | None:
        resolve_calls.append((binary, include_npm_global))
        return str(private_shim) if binary == "agy" else None

    def _fake_desktop_env(backend: str, executable: Any, base_env: Any = None) -> dict[str, str] | None:
        desktop_env_calls.append(str(executable))
        return {**dict(base_env or os.environ), "AVIBE_DESKTOP_TEST_MARKER": "derived-from-resolved-path"} if backend == "antigravity" and str(executable) == str(private_shim) else None

    monkeypatch.setattr("vibe.cli_paths.resolve_cli_path", _fake_resolve)
    monkeypatch.setattr(agy_agent_module, "desktop_backend_subprocess_environment", _fake_desktop_env)

    bare_cfg = antigravity_config("agy")
    provider = AntigravityNativeSessionProvider(cli_path="agy", config=bare_cfg)
    command, env = provider._build_launch(str(workdir), config=bare_cfg)
    assert command[0] == str(private_shim) and "--dangerously-skip-permissions" not in command and desktop_env_calls[-1] == str(private_shim)
    assert env["AVIBE_DESKTOP_TEST_MARKER"] == "derived-from-resolved-path" and ("agy", False) in resolve_calls

    discovered = await asyncio.to_thread(AgentNativeSessionService(providers=[provider]).list_recent_sessions, str(workdir))
    assert len(discovered) == 1 and discovered[0].native_session_id == "sess-desktop-1" and discovered[0].locator["title"] == "Desktop session"

    agent = AntigravityAgent(FakeController(app_config(bare_cfg)), bare_cfg, rules_base_dir=tmp_path / "rules")
    req = make_request(message="hello", workdir=workdir)
    agent.ensure_agent_session_id(req)
    spec, launch_env, _ = await agent._build_launch(req, runtime_key=req.composite_session_id, resolved_model="model-alpha")
    assert spec.command[0] == str(private_shim) and launch_env["AVIBE_DESKTOP_TEST_MARKER"] == "derived-from-resolved-path"

    auth_svc = AgentAuthService.__new__(AgentAuthService)
    for cfg in (SimpleNamespace(agents=SimpleNamespace(antigravity=SimpleNamespace(cli_path=None))), SimpleNamespace(antigravity=SimpleNamespace(binary=""))):
        auth_svc.controller = SimpleNamespace(config=cfg)
        assert auth_svc._get_cli_binary("antigravity") == str(private_shim)
    monkeypatch.setattr("vibe.cli_paths.resolve_cli_path", lambda *_a, **_kw: None)
    assert auth_svc._get_cli_binary("antigravity") == "agy"


def test_install_agent_antigravity_install_and_upgrade_disable_auto_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[tuple[list[str], dict | None, str]] = []
    monkeypatch.setattr(api, "_run_install_command", lambda _n, cmd, _t, *, mode, env=None, **_kw: recorded.append((list(cmd), dict(env) if env else None, mode)) or {"ok": True, "message": "ok", "output": "", "path": str(tmp_path / "agy")})
    monkeypatch.setattr(api, "is_desktop_managed_runtime", lambda: False)
    monkeypatch.setattr(api, "is_desktop_backend_path", lambda _p: False)

    cfg = V2Config.default()
    cfg.agents.antigravity.cli_path = "agy"
    cfg.save()
    monkeypatch.setattr(api, "resolve_cli_path", lambda name: f"/usr/bin/{name}" if name in {"curl", "bash", "powershell"} else None)
    monkeypatch.setattr(api, "detect_cli", lambda _p, _b: {"found": False, "path": None, "status": "missing", "version": None, "reason": "not_found"})
    assert api._install_agent("antigravity")["ok"] is True
    assert recorded[-1][2] == "install" and recorded[-1][1]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true" and "https://antigravity.google/cli/install." in " ".join(recorded[-1][0])

    existing = str(tmp_path / "agy")
    monkeypatch.setattr(api, "resolve_cli_path", lambda name: existing if name == "agy" else f"/usr/bin/{name}")
    monkeypatch.setattr(api, "detect_cli", lambda _p, _b: {"found": True, "path": existing, "status": "ok", "version": "1.5.15", "reason": None})
    assert api._install_agent("antigravity")["ok"] is True
    assert recorded[-1][0] == [existing, "update"] and recorded[-1][2] == "upgrade" and recorded[-1][1]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"


def _desktop_archive_env(tmp_path: Path) -> dict[str, str]:
    runtime = tmp_path / "runtime" / "1.0.0" / "digest"
    runtime.mkdir(parents=True)
    return {"AVIBE_DESKTOP_RUNTIME_ROOT": str(runtime), "AVIBE_DESKTOP_BACKENDS_ROOT": str(tmp_path / "backends"), "PATH": str(tmp_path / "external-bin")}


def _native_binary_bytes() -> bytes:
    return (b"MZ\x00\x00" if os.name == "nt" else b"\x7fELF") + b"0" * 64


def _build_archive(archive_path: Path, fmt: str, members: dict[str, bytes], *, symlink: str | None = None) -> str:
    if fmt == "tar.gz":
        with tarfile.open(archive_path, "w:gz") as tf:
            for name, content in members.items():
                info = tarfile.TarInfo(name=name)
                info.size, info.mode = len(content), 0o755
                tf.addfile(info, io.BytesIO(content))
            if symlink is not None:
                link = tarfile.TarInfo(name=symlink)
                link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
                tf.addfile(link)
    else:
        with zipfile.ZipFile(archive_path, "w") as zf:
            for name, content in members.items():
                info = zipfile.ZipInfo(name)
                info.external_attr = (stat.S_IFREG | 0o755) << 16
                zf.writestr(info, content)
            if symlink is not None:
                link = zipfile.ZipInfo(symlink)
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(link, "/etc/passwd")
    return hashlib.sha256(archive_path.read_bytes()).hexdigest()


def _release_payload(asset_name: str, digest: str | None = None) -> dict[str, Any]:
    asset: dict[str, str] = {"name": asset_name, "browser_download_url": f"https://github.com/google-antigravity/antigravity-cli/releases/download/v1.5.16/{asset_name}"}
    if digest is not None:
        asset["digest"] = f"sha256:{digest}"
    return {"tag_name": "v1.5.16", "assets": [asset]}


@pytest.mark.parametrize("binary_name", ["agy", "antigravity"])
@pytest.mark.parametrize("archive_format", ["tar.gz", "zip"])
def test_desktop_archive_install_verifies_sha256_and_publishes_without_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, binary_name: str, archive_format: str
) -> None:
    env = _desktop_archive_env(tmp_path)
    assert desktop_backends.desktop_backend_toolchain(env) is None and desktop_backends.desktop_backend_toolchain(env, backend="antigravity") is not None

    asset_name = f"agy_cli_{desktop_backends._release_archive_platform_tag('antigravity')}.{archive_format}"
    source_archive = tmp_path / asset_name
    exe_name = f"{binary_name}.exe" if os.name == "nt" else binary_name
    digest = _build_archive(source_archive, archive_format, {f"bin/{exe_name}": _native_binary_bytes()})

    probe_envs: list[dict[str, str]] = []
    monkeypatch.setattr(desktop_backends, "_fetch_github_release", lambda _r, **_kw: _release_payload(asset_name, digest))
    monkeypatch.setattr(desktop_backends, "_download_release_asset", lambda _u, dest, **_kw: dest.write_bytes(source_archive.read_bytes()))
    monkeypatch.setattr(desktop_backends, "_run_command", lambda cmd, *, cwd, env, timeout_seconds: probe_envs.append(dict(env)) or subprocess.CompletedProcess(cmd, 0, "1.5.16\n", ""))

    activated: list[str] = []
    result = desktop_backends.install_desktop_backend("antigravity", base_env=env, activate=activated.append)
    assert (result.backend, result.package, result.version, result.path, Path(result.path).name) == ("antigravity", "google-antigravity/antigravity-cli", "1.5.16", activated[0], exe_name)
    assert desktop_backends.resolve_published_desktop_backend("antigravity", env) == result.path and probe_envs[0]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
    sub_env = desktop_backends.desktop_backend_subprocess_environment("antigravity", result.path, env)
    assert sub_env is not None and sub_env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"


@pytest.mark.parametrize(
    ("case_id", "fmt", "members", "symlink", "digest_override", "expected_code"),
    [
        ("sha256_mismatch", "tar.gz", {"agy": _native_binary_bytes()}, None, "0" * 64, "checksum_mismatch"),
        ("missing_checksum", "tar.gz", {"agy": b"x"}, None, "", "missing_checksum"),
        ("traversal_tar", "tar.gz", {"../outside-agy": _native_binary_bytes()}, None, None, "invalid_archive"),
        ("symlink_tar", "tar.gz", {"bin/agy": _native_binary_bytes()}, "bin/link", None, "invalid_archive"),
        ("symlink_zip", "zip", {"bin/agy": _native_binary_bytes()}, "bin/link", None, "invalid_archive"),
    ],
)
def test_desktop_archive_install_rejects_bad_checksum_traversal_and_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case_id: str, fmt: str, members: dict[str, bytes], symlink: str | None, digest_override: str | None, expected_code: str
) -> None:
    env = _desktop_archive_env(tmp_path)
    asset_name = f"agy_cli_{desktop_backends._release_archive_platform_tag('antigravity')}.{fmt}"
    archive = tmp_path / f"{case_id}.{fmt}"
    real_digest = _build_archive(archive, fmt, members, symlink=symlink)
    digest = None if digest_override == "" else (digest_override or real_digest)

    monkeypatch.setattr(desktop_backends, "_fetch_github_release", lambda _r, **_kw: _release_payload(asset_name, digest))
    monkeypatch.setattr(desktop_backends, "_download_release_asset", lambda _u, dest, **_kw: dest.write_bytes(archive.read_bytes()))
    extract_called = False
    if case_id == "sha256_mismatch":
        def _forbid(*_a: Any, **_kw: Any) -> None:
            nonlocal extract_called
            extract_called = True
        monkeypatch.setattr(desktop_backends, "_extract_release_archive", _forbid)

    with pytest.raises(desktop_backends.DesktopBackendError) as raised:
        desktop_backends.install_desktop_backend("antigravity", base_env=env)
    assert raised.value.code == expected_code and extract_called is False and not (tmp_path / "outside-agy").exists()
    backend_root = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "antigravity"
    assert not (backend_root / "current.json").exists() and list(backend_root.glob(".staging-*")) == [] and list(backend_root.glob("releases/*")) == []
