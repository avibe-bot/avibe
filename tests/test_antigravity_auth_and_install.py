"""Hermetic unit and integration tests for Antigravity auth, probes, API routes, and desktop archive install."""

from __future__ import annotations

from tests.helpers.acp_probe_stub import make_probe_client

from tests.helpers.antigravity_harness import make_native_provider

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
from modules.agents.antigravity.agent import AntigravityAgent
from modules.agents.antigravity import agent as agy_agent_module
from modules.agents.antigravity.auth import (
    extract_antigravity_oauth_url,
    is_antigravity_verification_required_reply,
    run_antigravity_connection_probe,
)
from modules.agents.native_sessions.antigravity import AntigravityNativeSessionProvider
from modules.agents.native_sessions.service import AgentNativeSessionService
from modules.im import MessageContext
from tests.helpers.antigravity_harness import SENTINEL_SESSION_TITLE, FakeAgyAuthFixture, FakeController, FakeIMClient, antigravity_config, app_config, create_fake_agy_shim, make_request, read_jsonl
from tests.helpers.fake_acp_server import make_acp_capabilities
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


def test_http_antigravity_models_route(fake_agy: FakeAgyAuthFixture) -> None:
    fake_agy.set_state(authenticated=True, include_sentinel_session=True)
    fake_agy.clear_logs()
    client = ui_server.app.test_client()

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
async def test_pipe_paste_code_oauth_personal_flow_and_acp_logout(fake_agy: FakeAgyAuthFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_agy.set_state(authenticated=False)
    fake_agy.set_config({"expected_code": "agy-code-7788"})
    launch = asyncio.create_subprocess_exec
    launches = []

    async def capture(*args, **kwargs):
        if "--login" in args:
            launches.append(kwargs)
        return await launch(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    monkeypatch.setattr(os, "openpty", lambda: pytest.fail("Antigravity login must not allocate a PTY"))
    fake_agy.clear_logs()
    service = _make_auth_service()

    flow = await service.start_web_setup("antigravity", force_reset=True, auth_method="oauth-personal")
    assert flow.backend == "antigravity"
    assert not hasattr(flow, "pty_master_fd")
    assert launches and launches[0]["stdin"] == asyncio.subprocess.PIPE
    assert launches[0]["stdout"] == asyncio.subprocess.PIPE
    assert launches[0]["stderr"] == asyncio.subprocess.STDOUT
    assert flow.process.stdin is not None and not flow.process.stdin.is_closing()
    status = await _poll_flow(service, flow.flow_id, {"awaiting_code"}, require_url=True)
    assert status["callback_kind"] == "code" and status["url"].startswith("https://accounts.google.com/o/oauth2/auth")
    assert await service.submit_web_code(flow.flow_id, "line1\nline2") == {"ok": False, "error": "invalid_format"}
    assert await service.submit_web_code(flow.flow_id, "  https://antigravity.google/oauth-callback?code=agy-code-7788&state=fixture  ") == {"ok": True}

    status = await _poll_flow(service, flow.flow_id, {"success", "failed"})
    assert status["state"] == "success" and status["authorization_completed"] is True
    assert (V2Config.load().agents.antigravity.auth_mode, V2Config.load().agents.antigravity.api_key) == ("oauth-personal", None)
    assert any(inv["argv"] == ["acp", "--login", "--disable-telemetry"] for inv in fake_agy.invocations())
    _assert_passive_invocations(fake_agy)

    fake_agy.clear_logs()
    # An explicit Web re-login must obtain fresh consent even with valid credentials.
    assert (await probe_antigravity_passive_status(str(fake_agy.binary), config=V2Config.load().agents.antigravity))["authenticated"] is True
    relogin = await service.start_web_setup("antigravity", force_reset=False, auth_method="oauth-personal")
    try:
        assert relogin.state != "success" and relogin.authorization_completed is False
        status = await _poll_flow(service, relogin.flow_id, {"awaiting_code"}, require_url=True)
        assert status["url"].startswith("https://accounts.google.com/o/oauth2/auth")
        assert [inv["argv"] for inv in fake_agy.invocations() if "--login" in inv["argv"]] == [["acp", "--login", "--disable-telemetry"]]
        assert await service.submit_web_code(relogin.flow_id, "agy-code-7788") == {"ok": True}
        status = await _poll_flow(service, relogin.flow_id, {"success", "failed"})
        assert status["state"] == "success" and status["authorization_completed"] is True
        assert "logout" not in fake_agy.rpc_methods()
    finally:
        await service.cancel_web_flow(relogin.flow_id)

    fake_agy.clear_logs()
    assert await service.remove_web_auth("antigravity") == {"ok": True} and "logout" in fake_agy.rpc_methods()
    _assert_passive_invocations(fake_agy)
    assert (await probe_antigravity_passive_status(str(fake_agy.binary), config=V2Config.load().agents.antigravity))["authenticated"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["login", "logout"])
async def test_oauth_credential_cutover_discards_previous_account_model_menu(
    fake_agy: FakeAgyAuthFixture, transition: str,
) -> None:
    """OAuth accounts can change while every auth-config field remains identical.

    Existing cache tests changed an API key/mode, so they missed this cutover.
    """
    from modules.agents.antigravity.models import (
        auth_config_fingerprint, read_antigravity_live_models, record_antigravity_live_models,
    )
    from vibe.backend_model_catalog import backend_model_snapshot

    cfg = V2Config.load().agents.antigravity
    before_fingerprint = auth_config_fingerprint(cfg)
    record_antigravity_live_models(cfg, {"configOptions": [{
        "id": "model", "category": "model", "currentValue": "account-a-only",
        "options": [{"value": "account-a-only"}],
    }]})
    assert read_antigravity_live_models(cfg) is None  # OAuth mode cannot name the active account.
    service = _make_auth_service()

    if transition == "login":
        fake_agy.set_state(authenticated=False)
        fake_agy.set_config({"expected_code": "new-account-code"})
        flow = await service.start_web_setup("antigravity", force_reset=True, auth_method="oauth-personal")
        await _poll_flow(service, flow.flow_id, {"awaiting_code"}, require_url=True)
        assert await service.submit_web_code(flow.flow_id, "new-account-code") == {"ok": True}
        assert (await _poll_flow(service, flow.flow_id, {"success", "failed"}))["state"] == "success"
    else:
        fake_agy.set_state(authenticated=True)
        assert await service.remove_web_auth("antigravity") == {"ok": True}

    current = V2Config.load().agents.antigravity
    assert auth_config_fingerprint(current) == before_fingerprint
    assert read_antigravity_live_models(current) is None
    assert backend_model_snapshot("antigravity", schedule_refresh=False)["authoritative"] is False


def test_oauth_url_buffers_split_chunks() -> None:
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


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize(("payload", "english", "chinese"), [
    ([], "Send the Antigravity settings as an object.", "请以对象格式提交 Antigravity 设置。"),
    ({"auth_mode": "unknown"}, "Choose a valid Antigravity sign-in method.", "请选择有效的 Antigravity 登录方式。"),
    ({"auth_mode": "gemini-api-key", "api_key": 123}, "Enter text for api_key.", "请为 api_key 输入文本。"),
    ({"auth_mode": "oauth-personal", "base_url": []}, "Enter text for base_url.", "请为 base_url 输入文本。"),
    ({"auth_mode": "agent-platform", "gcp_project": []}, "Enter text for gcp_project.", "请为 gcp_project 输入文本。"),
    ({"auth_mode": "agent-platform", "gcp_location": []}, "Enter text for gcp_location.", "请为 gcp_location 输入文本。"),
    ({"auth_mode": "gemini-api-key"}, "Enter a Gemini API key before saving.", "请先填写 Gemini API Key，再保存。"),
])
def test_antigravity_auth_save_validation_uses_configured_language(
    fake_agy: FakeAgyAuthFixture, monkeypatch, language, payload, english, chinese,
):
    """Rejected form saves render localized guidance, including a key removed since readback.

    Successful-save tests never checked the display text of rejected inputs.
    """
    cfg = V2Config.load()
    cfg.language = language
    cfg.agents.antigravity.api_key = None
    cfg.save()
    restart = []
    monkeypatch.setattr(api, "restart_backend", lambda *_args, **_kwargs: restart.append(True))

    result = api.save_antigravity_auth(payload)

    assert result["ok"] is False
    assert result["message"] == (chinese if language == "zh" else english)
    assert restart == [] and fake_agy.invocations() == []


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
            "config_options_required_even_if_legacy_set_model_would_succeed",
            [{"id": "model", "category": "model", "currentValue": "gemini-4-neon", "options": [{"value": "gemini-4-neon"}, {"value": "gemini-4-carbon"}]}],
            "method_not_found",
            {"currentModelId": "gemini-4-neon"},
            {"stopReason": "end_turn"},
            "model_rejected",
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
        ("other_session", [{"id": "model", "category": "model", "currentValue": "gemini-4-neon", "options": [{"value": "gemini-4-neon"}]}], {"configOptions": [{"id": "model", "category": "model", "currentValue": "gemini-4-neon", "options": [{"value": "gemini-4-neon"}]}]}, {}, {"stopReason": "end_turn"}, "empty_response", True),
        ("verification", [{"id": "model", "category": "model", "currentValue": "gemini-4-neon", "options": [{"value": "gemini-4-neon"}]}], {"configOptions": [{"id": "model", "category": "model", "currentValue": "gemini-4-neon", "options": [{"value": "gemini-4-neon"}]}]}, {}, {"stopReason": "end_turn"}, "not_logged_in", True),
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
    chunks = [("other-session", "foreign reply")] if case_id == "other_session" else [("sess-probe-1", "\n⚠ Verification Required\n")] if case_id == "verification" else None
    Client, observed = make_probe_client(options=config_opts, selection=set_opt_behavior, result=prompt_result, chunks=chunks, legacy_model=set_model_resp)
    monkeypatch.setattr(acp, "AcpClient", Client)
    res = await run_antigravity_connection_probe(
        binary="/fake/agy",
        cwd=str(tmp_path),
        model="gemini-4-neon",
        config=AntigravityConfig(auth_mode="oauth-personal"),
    )
    assert (res["ok"], res["authenticated"], res["catalog_available"], res["inference_verified"], res["error"]) == (
        False, case_id != "verification", True, False, expected_error
    )
    assert (observed["prompt_called"], observed["closed"]) == (expect_prompt, True)
    if case_id == "verification":
        assert "agy" in res["detail"] and "Verification Required" in res["detail"]
        assert "Verification Required" in res["excerpt"]


def test_classify_auth_error_and_env_command_guards() -> None:
    for msg, exp in (
        ("ACP RPC error -32000: Authentication required", True),
        ("Error -32000", True),
        ("HTTP 401 Unauthorized", True),
        ("Could not find default credentials", True),
        ("API key not valid", True),
        ("Antigravity authentication required (ACP -32000, verification required): ⚠ Verification Required", True),
        ("Verification required for this account", True),
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

    for auth_mode, err_msg, expected in (("gemini-api-key", "API key not valid", "API key"), ("agent-platform", "Could not find default credentials", ANTIGRAVITY_ADC_LOGIN_COMMAND)):
        cfg.agents.antigravity.auth_mode = auth_mode
        assert await service.maybe_emit_auth_recovery_message(ctx, "antigravity", err_msg) is True and expected in im_client.sent_plain[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["oauth-personal", "oauth-business"])
@pytest.mark.parametrize("terminator", ["\r\n", " ", "\x1b[0m"])
async def test_im_setup_link_sends_instructions_once_url_completes(fake_agy: FakeAgyAuthFixture, method: str, terminator: str) -> None:
    service, ctx = _make_auth_service(), MessageContext(user_id="u1", channel_id="c1", platform="slack")
    service._send_message = AsyncMock()
    service.controller._get_settings_key = lambda context: context.channel_id
    flow = auth_mod.AgentAuthFlow(
        flow_id="f1", backend="antigravity", settings_key="c1", initiator_user_id="u1",
        context=ctx, process=None, reader_task=None, waiter_task=None, auth_method=method,
    )
    service._flow_registry.put(flow, flow_key=service._make_flow_key(ctx, "antigravity"))

    url = "https://accounts.google.com/o/oauth2/auth?client_id=agy&state=s1"
    await service._handle_process_text(ctx, "antigravity", "Open " + url)
    assert flow.url is None and service._send_message.await_count == 0
    await service._handle_process_text(ctx, "antigravity", terminator)
    assert flow.url == url and flow.awaiting_code
    await service._handle_process_text(ctx, "antigravity", "Enter code: ")
    assert flow.url == url and service._send_message.await_count == 1

    sent = service._send_message.await_args.args[1]
    assert sent == service._t("command.setup.antigravityInstructions", url=url)


@pytest.mark.asyncio
async def test_im_callback_url_is_relayed_as_code(fake_agy: FakeAgyAuthFixture) -> None:
    """IM owns its submit branch; Web coverage cannot detect a missed IM parser call."""
    service, ctx = _make_auth_service(), MessageContext(user_id="u1", channel_id="c1", platform="slack")
    service._send_message = AsyncMock()
    writer = SimpleNamespace(is_closing=lambda: False, write=lambda payload: writes.append(payload), drain=AsyncMock())
    writes = []
    flow = auth_mod.AgentAuthFlow(
        flow_id="im-code", backend="antigravity", settings_key="c1", initiator_user_id="u1",
        context=ctx, process=SimpleNamespace(stdin=writer), reader_task=None, waiter_task=None,
        awaiting_code=True,
    )
    await service._submit_code_owned(flow, ctx, "https://antigravity.google/oauth-callback?state=s")
    assert writes == [] and flow.awaiting_code
    await service._submit_code_owned(flow, ctx, " https://antigravity.google/oauth-callback?code=im%2Fcode&state=s ")
    assert writes == [b"im/code\n"] and not flow.awaiting_code


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

    state_file.write_text(json.dumps({"authenticated": True, "next_session_num": 1, "sessions": {}}), encoding="utf-8")
    assert (await probe_antigravity_passive_status("~/.local/bin/agy", config=v2.agents.antigravity))["authenticated"] is True


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
    provider = make_native_provider(cli_path="agy", config=bare_cfg)
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


def test_install_agent_antigravity_disables_auto_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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



@pytest.mark.parametrize("reply_text,expected", [
    ("", False), (" \n\t", False),
    ("\n⚠ Verification Required\n", True), ("⚠️ Verification Required", True),
    ("**⚠ Verification Required**", True), ("⚠ verification required", True),
    ("Verification Required", False), ("**Verification Required**", False),
    ("Verification required: run the tests before merging the google branch", False),
    ("⚠ Verification Required\nHere is the explanation.", False),
    ("Here is how verification works in Google OAuth:\n⚠ Verification Required is an example banner.", False),
    ("Hi! How can I help you today?", False),
])
def test_is_antigravity_verification_required_reply(reply_text: str, expected: bool) -> None:
    assert is_antigravity_verification_required_reply(reply_text) is expected


@pytest.mark.parametrize("updated", ["1.5.17", "1.5.18", "broken"])
def test_script_auto_update_verifies_settled_version_and_restores_binary(tmp_path, monkeypatch, updated):
    binary = tmp_path / "agy"
    binary.write_text("1.5.16")
    binary.chmod(0o755)
    cfg = V2Config.default()
    cfg.agents.antigravity.cli_path = str(binary)
    cfg.save()
    monkeypatch.setattr(api, "is_desktop_backend_path", lambda _p: False)
    monkeypatch.setattr(api, "is_desktop_managed_runtime", lambda: False)
    monkeypatch.setattr(api, "resolve_cli_path", lambda _p: str(binary))
    def health(path, _name):
        version = Path(path).read_text()
        return {"found": True, "path": str(path), "status": "broken" if version == "broken" else "ok", "version": None if version == "broken" else version, "reason": None}
    monkeypatch.setattr(api, "detect_cli", health)
    def update(name, cmd, truncate, **kwargs):
        assert cmd == [str(binary), "update"] and kwargs["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
        binary.write_text(updated)
        valid = kwargs["readback_validator"](str(binary))
        return {"ok": valid, "code": None if valid else "cli_readback_failed", "path": str(binary)}
    monkeypatch.setattr(api, "_run_install_command", update)
    result = api.install_agent("antigravity", version="1.5.17")
    assert result["ok"] is (updated == "1.5.17"), result
    assert binary.read_text() == ("1.5.17" if updated == "1.5.17" else "1.5.16")
    if updated != "1.5.17":
        assert result["restore"] == "restored"

@pytest.mark.parametrize("mode", ["existing", "fresh", "broken"])
def test_non_desktop_antigravity_refuses_pinned_version(monkeypatch, tmp_path, mode):
    """Non-desktop agy update and script installers must refuse pinned versions without executing."""
    from config.v2_config import V2Config
    from vibe import api
    from vibe.cli_repair import RepairSource

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    cfg = V2Config.default()
    cfg.agents.antigravity.cli_path = "agy"
    cfg.save()

    executed: list[list[str]] = []
    monkeypatch.setattr(
        api,
        "_run_install_command",
        lambda _n, cmd, *_a, **_kw: executed.append(list(cmd)) or {"ok": True},
    )
    monkeypatch.setattr(api, "is_desktop_managed_runtime", lambda: False)
    monkeypatch.setattr(api, "is_desktop_backend_path", lambda _p: False)

    existing = str(tmp_path / "agy")
    if mode == "existing":
        monkeypatch.setattr(api, "resolve_cli_path", lambda name: existing if name == "agy" else f"/usr/bin/{name}")
        monkeypatch.setattr(
            api,
            "detect_cli",
            lambda _p, _b: {"found": True, "path": existing, "status": "ok", "version": "0.5.0", "reason": None},
        )
    elif mode == "broken":
        monkeypatch.setattr(api, "resolve_cli_path", lambda name: existing if name == "agy" else f"/usr/bin/{name}")
        monkeypatch.setattr(
            api,
            "detect_cli",
            lambda _p, _b: {"found": True, "path": existing, "status": "broken", "version": None, "reason": "broken"},
        )
        monkeypatch.setattr(
            api,
            "identify_repair_source",
            lambda _name, _sel: RepairSource(kind="native", destination=existing),
        )
    else:
        monkeypatch.setattr(
            api,
            "resolve_cli_path",
            lambda name: f"/usr/bin/{name}" if name in {"curl", "bash", "powershell"} else None,
        )
        monkeypatch.setattr(
            api,
            "detect_cli",
            lambda _p, _b: {"found": False, "path": None, "status": "missing", "version": None, "reason": "not_found"},
        )

    result = api.install_agent("antigravity", version="0.4.0")

    assert result["ok"] is False
    assert result["code"] == ("cli_readback_failed" if mode == "existing" else "pinned_version_unsupported")
    assert executed == []


def test_plain_text_oauth_url_is_published_only_after_the_full_line() -> None:
    """Pipe output has no ANSI terminator; earlier coverage used only ANSI chunks."""
    url = "https://accounts.google.com/o/oauth2/auth?client_id=agy&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback&state=complete"
    flow = SimpleNamespace(pty_output_buffer="", last_status_text="", url=None, awaiting_code=False)
    AgentAuthService._consume_antigravity_text(flow, "Open this link in a browser to log in with Google:\n\n  " + url[:90])
    assert flow.url is None
    AgentAuthService._consume_antigravity_text(flow, url[90:] + "\n\nThen paste the authorization code, or the full URL of the page you land on, and press Enter:")
    assert flow.url == url and flow.awaiting_code
    assert extract_antigravity_oauth_url("  " + url + "\n") == url


@pytest.mark.parametrize("raw, expected", [
    ("https://antigravity.google/oauth-callback?code=4%2Fabc&state=s", "4/abc"),
    ("Returned: https://antigravity.google/oauth-callback?state=s&code=abc", "abc"),
    ("bare-code", "bare-code"),
    ("  bare-code \n", "bare-code"),
    (" https://antigravity.google/oauth-callback?state=s ", None),
    ("https://antigravity.google/oauth-callback?code=", None),
    ("https://antigravity.google/oauth-callback?code=a%0Ab", None),
])
def test_antigravity_pasted_callback_code(raw: str, expected: str | None) -> None:
    """Both submit surfaces must relay only a code; previous coverage used bare codes."""
    assert AgentAuthService._parse_antigravity_code(raw) == expected


@pytest.mark.skipif(os.name == "nt", reason="POSIX login opener shims; Windows remains vendor-controlled")
@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["success", "failed", "cancel", "supersede", "launch_failed", "removed"])
async def test_login_opener_shims_are_private_and_retired_with_flow(
    fake_agy: FakeAgyAuthFixture, monkeypatch: pytest.MonkeyPatch, ending: str,
) -> None:
    """agy opens even with pipes; previous launch tests could not observe opener isolation or lifetime."""
    original_path = os.environ.get("PATH", os.defpath)
    launches = []
    real_launch = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        launches.append((args, kwargs.get("env", {})))
        if "--login" in args and ending == "launch_failed":
            raise OSError("fixture launch failure")
        return await real_launch(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    service = _make_auth_service()
    fake_agy.set_state(authenticated=False)
    if ending == "launch_failed":
        failed = await service.start_web_setup("antigravity", auth_method="oauth-personal")
        assert failed.state == "failed" and failed.error == "fixture launch failure"
        directory = Path(launches[0][1]["PATH"].split(os.pathsep)[0])
        assert directory.name.startswith("avibe-agy-login-") and not directory.exists()
        return

    flow = await service.start_web_setup("antigravity", auth_method="oauth-personal")
    replacement = None
    try:
        await _poll_flow(service, flow.flow_id, {"awaiting_code"}, require_url=True)
        env = next(env for args, env in launches if "--login" in args)
        directory = Path(env["PATH"].split(os.pathsep)[0])
        assert directory.name.startswith("avibe-agy-login-")
        assert env["PATH"] == str(directory) + os.pathsep + original_path
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        for name in ("open", "xdg-open"):
            shim = directory / name
            assert shim.is_file() and stat.S_IMODE(shim.stat().st_mode) == 0o755
            # Invoke via PATH, exactly as the vendor does; both commands must no-op.
            result = subprocess.run([name, "https://example.test/consent"], env=env, capture_output=True)
            assert result.returncode == 0 and result.stdout == result.stderr == b""
        assert os.environ.get("PATH", os.defpath) == original_path
        assert build_antigravity_auth_env()["PATH"] == original_path

        if ending in {"success", "failed"}:
            code = "valid-agy-code" if ending == "success" else "wrong-code"
            assert await service.submit_web_code(flow.flow_id, code) == {"ok": True}
            await flow.waiter_task
            assert flow.state == ending
        elif ending in {"cancel", "removed"}:
            if ending == "removed":
                # A temp reaper must not turn disposable-file cleanup into a
                # native credential lockout; prior tests kept the directory.
                for shim in directory.iterdir():
                    shim.unlink()
                directory.rmdir()
            await service.cancel_web_flow(flow.flow_id)
            assert flow.native_lease is None
        else:
            replacement = await service.start_web_setup("antigravity", auth_method="oauth-personal")
            assert replacement.flow_id != flow.flow_id
            assert replacement.antigravity_opener_shim_dir != str(directory)
            assert Path(replacement.antigravity_opener_shim_dir).exists()
        assert not directory.exists() and flow.antigravity_opener_shim_dir is None
        # The real passive-status/logout call paths must receive the original PATH.
        if replacement is None:
            await service.remove_web_auth("antigravity")
        assert all(env["PATH"] == original_path for args, env in launches if "--login" not in args)
    finally:
        await service.cancel_web_flow(flow.flow_id)
        if replacement is not None:
            replacement_dir = Path(replacement.antigravity_opener_shim_dir)
            await service.cancel_web_flow(replacement.flow_id)
            assert not replacement_dir.exists()


@pytest.mark.asyncio
async def test_antigravity_concurrent_probes_coalesce_inflight_and_isolate_returned_dicts(
    fake_agy: FakeAgyAuthFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent runtime/connection/auth polls share one --version and one acp probe and deep-copy results."""
    from vibe import internal_client

    monkeypatch.setattr(
        internal_client,
        "backend_application",
        AsyncMock(return_value={"status_code": 200, "body": {"ok": True, "state": "applied", "controller_pid": 42}}),
    )
    monkeypatch.setattr(api, "_cached_latest", lambda _name: None)
    fake_agy.set_state(authenticated=True)
    fake_agy.clear_logs()

    conn, auth, runtime = await asyncio.gather(
        api.get_backend_connection("antigravity"),
        api.get_antigravity_auth_async(),
        api.get_backend_runtime_async("antigravity"),
    )

    assert (conn["ok"], conn["auth"], conn["ready"], conn["entry_eligible"]) == (True, "subscription", True, True)
    assert (auth["ok"], auth["authenticated"], auth["active_auth_mode"]) == (True, True, "oauth-personal")
    assert (runtime["ok"], runtime["installed"], runtime["status"]) == (True, True, "ok")

    version_launches = [inv for inv in fake_agy.invocations() if inv["argv"] == ["--version"]]
    acp_launches = [inv for inv in fake_agy.invocations() if inv["argv"][:1] == ["acp"]]
    assert len(version_launches) == 1
    assert len(acp_launches) == 1
    assert fake_agy.rpc_methods() == ["initialize", "session/list"]

    # Coalesced callers receive independent deep copies so dict/list mutations never leak.
    fake_agy.clear_logs()
    auth_a, auth_b = await asyncio.gather(
        api.get_antigravity_auth_async(),
        api.get_antigravity_auth_async(),
    )
    assert len([inv for inv in fake_agy.invocations() if inv["argv"][:1] == ["acp"]]) == 1
    auth_a["authenticated"] = False
    auth_a["auth_methods"].append("mutated-method")
    assert auth_b["authenticated"] is True
    assert "mutated-method" not in auth_b["auth_methods"]

    cli_a, cli_b = await asyncio.gather(
        api.detect_cli_async(str(fake_agy.binary), "antigravity"),
        api.detect_cli_async(str(fake_agy.binary), "antigravity"),
    )
    cli_a["status"] = "broken"
    assert cli_b["status"] == "ok"


@pytest.mark.asyncio
async def test_antigravity_inflight_cancellation_preserves_survivor_and_cancels_when_all_leave(
    fake_agy: FakeAgyAuthFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling one waiter keeps the shared probe alive; cancelling every waiter cancels the probe."""
    probe_started = asyncio.Event()
    allow_finish = asyncio.Event()
    probe_calls = 0
    probe_cancelled = False

    async def _slow_probe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal probe_calls, probe_cancelled
        probe_calls += 1
        probe_started.set()
        try:
            await allow_finish.wait()
        except asyncio.CancelledError:
            probe_cancelled = True
            raise
        return {
            "ok": True,
            "authenticated": True,
            "credentials_present": True,
            "uncertain": False,
            "auth_methods": ["oauth-personal"],
            "supports_logout": True,
            "error": None,
        }

    monkeypatch.setattr(auth_mod, "probe_antigravity_passive_status", _slow_probe)

    async def _wait_for_waiters(expected: int) -> None:
        await probe_started.wait()
        for _ in range(100):
            if any(entry.waiter_count == expected for entry in api._ANTIGRAVITY_AUTH_INFLIGHT.values()):
                return
            await asyncio.sleep(0.01)
        pytest.fail(f"Expected {expected} waiters in _ANTIGRAVITY_AUTH_INFLIGHT")

    # Part 1: cancelling one caller lets the surviving caller finish with the shared probe.
    t1 = asyncio.create_task(api.get_antigravity_auth_async())
    t2 = asyncio.create_task(api.get_antigravity_auth_async())
    await _wait_for_waiters(2)
    t1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t1
    assert probe_cancelled is False
    allow_finish.set()
    survivor = await t2
    assert (survivor["ok"], survivor["authenticated"], survivor["active_auth_mode"]) == (True, True, "oauth-personal")
    assert probe_calls == 1
    assert not api._ANTIGRAVITY_AUTH_INFLIGHT

    # Part 2: cancelling all concurrent callers cancels the shared probe cleanly.
    probe_started.clear()
    allow_finish.clear()
    probe_cancelled = False
    t3 = asyncio.create_task(api.get_antigravity_auth_async())
    t4 = asyncio.create_task(api.get_antigravity_auth_async())
    await _wait_for_waiters(2)
    t3.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t3
    assert probe_cancelled is False
    t4.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t4
    assert probe_cancelled is True
    assert not api._ANTIGRAVITY_AUTH_INFLIGHT


@pytest.mark.asyncio
async def test_antigravity_no_post_completion_cache_and_epoch_bump_forces_fresh_probe(
    fake_agy: FakeAgyAuthFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sequential reads never serve stale cached state, and epoch bumps split in-flight probes."""
    # Part 1: sequential calls observe external auth transitions immediately (no TTL cache).
    fake_agy.set_state(authenticated=False)
    before = await api.get_antigravity_auth_async()
    assert (before["authenticated"], before["active_auth_mode"]) == (False, "none")

    fake_agy.set_state(authenticated=True)
    after = await api.get_antigravity_auth_async()
    assert (after["authenticated"], after["active_auth_mode"]) == (True, "oauth-personal")

    # Auth mutations bump the in-flight epoch.
    monkeypatch.setattr(api, "restart_backend", lambda *_a, **_kw: {"ok": True})
    epoch_0 = api._ANTIGRAVITY_AUTH_INFLIGHT_EPOCH
    assert api.save_antigravity_auth({"auth_mode": "gemini-api-key", "api_key": "AIza-epoch-test"})["ok"] is True
    epoch_1 = api._ANTIGRAVITY_AUTH_INFLIGHT_EPOCH
    assert epoch_1 > epoch_0
    assert api.remove_backend_api_key("antigravity")["ok"] is True
    epoch_2 = api._ANTIGRAVITY_AUTH_INFLIGHT_EPOCH
    assert epoch_2 > epoch_1
    service = _make_auth_service()
    await service._persist_backend_auth_mode("antigravity", "oauth-personal")
    epoch_3 = api._ANTIGRAVITY_AUTH_INFLIGHT_EPOCH
    assert epoch_3 > epoch_2
    assert (await service.remove_web_auth("antigravity"))["ok"] is True
    epoch_4 = api._ANTIGRAVITY_AUTH_INFLIGHT_EPOCH
    assert epoch_4 > epoch_3

    # Part 2: bumping epoch while a slow probe is in flight forces a concurrent caller to run fresh.
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    probe_calls = 0

    async def _probe_with_cutover(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal probe_calls
        probe_calls += 1
        call_num = probe_calls
        if call_num == 1:
            first_started.set()
            await release_first.wait()
            return {
                "ok": True,
                "authenticated": False,
                "credentials_present": False,
                "uncertain": False,
                "auth_methods": ["oauth-personal"],
                "supports_logout": True,
                "error": "not_logged_in",
            }
        return {
            "ok": True,
            "authenticated": True,
            "credentials_present": True,
            "uncertain": False,
            "auth_methods": ["oauth-personal"],
            "supports_logout": True,
            "error": None,
        }

    monkeypatch.setattr(auth_mod, "probe_antigravity_passive_status", _probe_with_cutover)

    slow_task = asyncio.create_task(api.get_antigravity_auth_async())
    await first_started.wait()
    api.bump_antigravity_auth_epoch()

    fresh = await api.get_antigravity_auth_async()
    assert (fresh["authenticated"], fresh["active_auth_mode"]) == (True, "oauth-personal")
    assert probe_calls == 2

    release_first.set()
    stale = await slow_task
    assert (stale["authenticated"], stale["active_auth_mode"]) == (False, "none")
    assert not api._ANTIGRAVITY_AUTH_INFLIGHT
