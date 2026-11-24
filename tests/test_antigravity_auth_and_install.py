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
from unittest.mock import AsyncMock

import httpx
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
from tests.helpers.antigravity_harness import (
    SENTINEL_SESSION_TITLE,
    FakeAgyAuthFixture,
    FakeIMClient,
)
from vibe import api, desktop_backends, ui_server


@pytest.fixture
def fake_agy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeAgyAuthFixture:
    Path.home().mkdir(parents=True, exist_ok=True)
    for var in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GEMINI_API_KEY",
        "GOOGLE_GEMINI_BASE_URL",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
    ):
        monkeypatch.delenv(var, raising=False)

    fixture = FakeAgyAuthFixture(tmp_path)
    config = V2Config.default()
    config.agents.antigravity.enabled = True
    config.agents.antigravity.cli_path = str(fixture.binary)
    config.agents.antigravity.auth_mode = "oauth-personal"
    config.save()
    return fixture


def _make_auth_service(config: V2Config | None = None, im_client: FakeIMClient | None = None) -> AgentAuthService:
    controller = SimpleNamespace(
        config=config or V2Config.load(),
        agent_service=None,
        session_handler=None,
        im_client=im_client,
        emit_agent_message=AsyncMock(),
    )
    return AgentAuthService(controller)


def _assert_passive_invocations(fixture: FakeAgyAuthFixture) -> None:
    for inv in fixture.invocations():
        assert "--dangerously-skip-permissions" not in inv["argv"]
        assert inv["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"


# ---------------------------------------------------------------------------
# 1. Passive status checks via session/list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passive_status_uses_session_list_never_infers_or_exposes_sessions(
    fake_agy: FakeAgyAuthFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake_agy.set_state(authenticated=True, include_sentinel_session=True)
    fake_agy.clear_logs()

    cfg = V2Config.load().agents.antigravity
    result = await probe_antigravity_passive_status(str(fake_agy.binary), config=cfg)

    assert (result["ok"], result["authenticated"], result["credentials_present"]) == (True, True, True)
    assert (result["catalog_available"], result["inference_verified"], result["supports_logout"]) == (False, False, True)

    invocations = fake_agy.invocations()
    assert len(invocations) == 1 and invocations[0]["argv"] == ["acp", "--disable-telemetry"]
    _assert_passive_invocations(fake_agy)

    methods = fake_agy.rpc_methods()
    assert methods == ["initialize", "session/list"]
    assert "session/new" not in methods and "session/prompt" not in methods
    assert SENTINEL_SESSION_TITLE not in json.dumps(result)
    assert SENTINEL_SESSION_TITLE not in caplog.text


@pytest.mark.asyncio
async def test_passive_status_reports_unauthenticated_on_minus_32000(fake_agy: FakeAgyAuthFixture) -> None:
    fake_agy.set_state(authenticated=False)
    fake_agy.clear_logs()

    auth = await api.get_antigravity_auth_async()
    assert (auth["ok"], auth["authenticated"], auth["credentials_present"]) == (True, False, False)
    assert (auth["active_auth_mode"], auth["error"]) == ("none", "not_logged_in")
    assert (auth["catalog_available"], auth["inference_verified"]) == (False, False)
    assert SENTINEL_SESSION_TITLE not in json.dumps(auth)
    assert fake_agy.rpc_methods() == ["initialize", "session/list"]


def test_http_antigravity_auth_get_and_models_routes(fake_agy: FakeAgyAuthFixture) -> None:
    fake_agy.set_state(authenticated=True, include_sentinel_session=True)
    fake_agy.clear_logs()
    client = ui_server.app.test_client()

    auth_resp = client.get("/api/backend/antigravity/auth")
    assert auth_resp.status_code == 200
    body = auth_resp.get_json()
    assert (body["ok"], body["authenticated"], body["active_auth_mode"]) == (True, True, "oauth-personal")
    assert (body["cli_installed"], body["cli_version"], body["adc_command"]) == (
        True,
        "1.5.16",
        ANTIGRAVITY_ADC_LOGIN_COMMAND,
    )
    assert (body["catalog_available"], body["inference_verified"]) == (False, False)
    assert SENTINEL_SESSION_TITLE not in json.dumps(body)

    models_resp = client.get("/api/antigravity/models")
    assert models_resp.status_code == 200
    models_body = models_resp.get_json()
    assert models_body["ok"] is True and "gemini-4-neon" in models_body["models"]


@pytest.mark.asyncio
async def test_get_backend_connection_uses_passive_antigravity_status(
    fake_agy: FakeAgyAuthFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vibe import internal_client

    monkeypatch.setattr(
        internal_client,
        "backend_application",
        AsyncMock(return_value={"status_code": 200, "body": {"ok": True, "state": "applied", "controller_pid": 42}}),
    )
    fake_agy.set_state(authenticated=True)
    fake_agy.clear_logs()

    conn = await api.get_backend_connection("antigravity")
    assert (conn["ok"], conn["auth"], conn["ready"], conn["entry_eligible"]) == (True, "subscription", True, True)
    assert fake_agy.rpc_methods() == ["initialize", "session/list"]

    fake_agy.set_state(authenticated=False)
    conn_signed_out = await api.get_backend_connection("antigravity")
    assert (conn_signed_out["auth"], conn_signed_out["ready"]) == ("none", False)


# ---------------------------------------------------------------------------
# 2. Explicit connection test distinguishing auth, catalog, and inference
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_connection_test_distinguishes_auth_catalog_and_inference(
    fake_agy: FakeAgyAuthFixture,
) -> None:
    service = _make_auth_service()
    fake_agy.set_state(authenticated=True)
    fake_agy.clear_logs()

    ok = await service.test_web_auth("antigravity", model="gemini-4-neon")
    assert (ok["ok"], ok["authenticated"], ok["credentials_present"]) == (True, True, True)
    assert (ok["catalog_available"], ok["inference_verified"], ok["model"]) == (True, True, "gemini-4-neon")
    assert "gemini-4-neon" in ok["models"] and "echo:Hi" in ok["excerpt"]
    assert fake_agy.rpc_methods() == [
        "initialize",
        "session/new",
        "session/set_config_option",
        "session/prompt",
        "session/close",
    ]
    _assert_passive_invocations(fake_agy)

    # Auth + catalog succeed, unsupported model fails inference without sending session/prompt.
    fake_agy.clear_logs()
    bad_model = await service.test_web_auth("antigravity", model="nonexistent-model-xyz")
    assert (bad_model["ok"], bad_model["authenticated"], bad_model["catalog_available"], bad_model["inference_verified"]) == (
        False,
        True,
        True,
        False,
    )
    assert "session/prompt" not in fake_agy.rpc_methods() and "session/close" in fake_agy.rpc_methods()

    # Unauthenticated fails before catalog and inference.
    fake_agy.set_state(authenticated=False)
    fake_agy.clear_logs()
    no_auth = await service.test_web_auth("antigravity", model="gemini-4-neon")
    assert (no_auth["ok"], no_auth["error"], no_auth["authenticated"]) == (False, "not_logged_in", False)
    assert (no_auth["catalog_available"], no_auth["inference_verified"]) == (False, False)


# ---------------------------------------------------------------------------
# 3. PTY paste-code OAuth flow, login reuse, business fallback, and ACP logout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pty_paste_code_oauth_personal_flow_and_acp_logout(fake_agy: FakeAgyAuthFixture) -> None:
    fake_agy.set_state(authenticated=False)
    fake_agy.set_config({"expected_code": "agy-code-7788"})
    fake_agy.clear_logs()

    service = _make_auth_service()
    service._post_web_success_hook = lambda _backend: None

    flow = await service.start_web_setup("antigravity", force_reset=True, auth_method="oauth-personal")
    assert flow.backend == "antigravity"

    for _ in range(80):
        status = service.get_web_flow_status(flow.flow_id)
        if status["state"] == "awaiting_code" and status["url"]:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail(f"Antigravity flow did not reach awaiting_code: {service.get_web_flow_status(flow.flow_id)}")

    assert status["callback_kind"] == "code"
    assert status["url"].startswith("https://accounts.google.com/o/oauth2/auth")
    assert await service.submit_web_code(flow.flow_id, "line1\nline2") == {"ok": False, "error": "invalid_format"}
    assert await service.submit_web_code(flow.flow_id, "agy-code-7788") == {"ok": True}

    for _ in range(100):
        status = service.get_web_flow_status(flow.flow_id)
        if status["state"] in {"success", "failed"}:
            break
        await asyncio.sleep(0.05)
    assert status["state"] == "success", status
    assert status["authorization_completed"] is True and flow.pty_master_fd is None

    persisted = V2Config.load().agents.antigravity
    assert persisted.auth_mode == "oauth-personal" and persisted.api_key is None
    assert any(inv["argv"] == ["acp", "--login", "--disable-telemetry"] for inv in fake_agy.invocations())
    _assert_passive_invocations(fake_agy)

    # Reusing existing login when force_reset=False skips --login.
    fake_agy.clear_logs()
    reused = await service.start_web_setup("antigravity", force_reset=False, auth_method="oauth-personal")
    assert reused.state == "success" and reused.authorization_completed is True
    assert all("--login" not in inv["argv"] for inv in fake_agy.invocations())

    # Sign out invokes ACP logout and clears auth state.
    fake_agy.clear_logs()
    assert await service.remove_web_auth("antigravity") == {"ok": True}
    assert "logout" in fake_agy.rpc_methods()
    _assert_passive_invocations(fake_agy)

    after_logout = await probe_antigravity_passive_status(str(fake_agy.binary), config=V2Config.load().agents.antigravity)
    assert after_logout["authenticated"] is False


@pytest.mark.asyncio
async def test_oauth_business_surfaces_terminal_fallback_when_paste_code_unsupported(
    fake_agy: FakeAgyAuthFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_agy.set_state(authenticated=False)
    fake_agy.set_config({"business_unsupported": True})
    service = _make_auth_service()
    service._post_web_success_hook = lambda _backend: None
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
        assert (start_body["ok"], start_body["auth_method"], start_body["terminal_command"]) == (
            True,
            "oauth-business",
            "agy",
        )
        assert "agy" in (start_body.get("instructions") or "")

        status: dict = {}
        for _ in range(80):
            status = (await client.get(f"/api/backend/antigravity/auth/oauth/status/{start_body['flow_id']}")).json()
            if status["state"] in {"failed", "success"}:
                break
            await asyncio.sleep(0.05)

    assert (status["state"], status["auth_method"], status["terminal_command"]) == ("failed", "oauth-business", "agy")
    assert "agy" in (status.get("instructions") or "") and "agy" in (status.get("error") or "")


# ---------------------------------------------------------------------------
# 4. API key + base URL save/remove, Agent Platform ADC, and error classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_key_save_authenticate_and_remove_never_touches_gemini_dir(
    fake_agy: FakeAgyAuthFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "restart_backend", lambda *_a, **_kw: {"ok": True})
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
    assert (saved["ok"], saved["auth_mode"], saved["has_api_key"], saved["base_url"]) == (
        True,
        "gemini-api-key",
        True,
        "https://relay.example.test/gemini",
    )
    assert "AIza-fixture-key-9999" not in json.dumps(saved)
    assert fake_agy.invocations() == []
    home = Path.home()
    assert not any((home / name).exists() for name in (".gemini", ".bashrc", ".zshrc"))

    passive = await api.get_antigravity_auth_async()
    assert (passive["authenticated"], passive["credentials_present"], passive["active_auth_mode"]) == (
        True,
        True,
        "gemini-api-key",
    )
    assert (passive["catalog_available"], passive["inference_verified"]) == (False, False)
    acp_invs = [inv for inv in fake_agy.invocations() if inv["argv"][:1] == ["acp"]]
    assert acp_invs[-1]["env"]["GEMINI_API_KEY"] == "AIza-fixture-key-9999"
    assert acp_invs[-1]["env"]["GOOGLE_GEMINI_BASE_URL"] == "https://relay.example.test/gemini"
    assert fake_agy.rpc_methods() == ["initialize", "authenticate", "session/list"]

    assert api.remove_backend_api_key("antigravity")["ok"] is True
    reloaded = V2Config.load().agents.antigravity
    assert (reloaded.api_key, reloaded.base_url, reloaded.auth_mode) == (None, None, "oauth-personal")


@pytest.mark.asyncio
async def test_agent_platform_adc_detection_and_gcp_settings(
    fake_agy: FakeAgyAuthFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "restart_backend", lambda *_a, **_kw: {"ok": True})
    fake_agy.set_state(authenticated=False)
    fake_agy.set_config({"require_gcp_project": True})
    fake_agy.clear_logs()

    saved = api.save_antigravity_auth(
        {"auth_mode": "agent-platform", "gcp_project": "my-gcp-project", "gcp_location": "us-central1"}
    )
    assert (saved["ok"], saved["adc_present"], saved["adc_command"]) == (True, False, ANTIGRAVITY_ADC_LOGIN_COMMAND)

    no_adc = await api.get_antigravity_auth_async()
    assert (no_adc["authenticated"], no_adc["credentials_present"], no_adc["adc_present"]) == (False, False, False)
    assert no_adc["adc_command"] == ANTIGRAVITY_ADC_LOGIN_COMMAND

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
    assert (with_adc["authenticated"], with_adc["credentials_present"], with_adc["active_auth_mode"]) == (
        True,
        True,
        "agent-platform",
    )
    acp_invs = [inv for inv in fake_agy.invocations() if inv["argv"][:1] == ["acp"]]
    assert acp_invs[-1]["env"]["GOOGLE_CLOUD_PROJECT"] == "my-gcp-project"
    assert acp_invs[-1]["env"]["GOOGLE_CLOUD_LOCATION"] == "us-central1"


def test_classify_auth_error_and_env_command_guards() -> None:
    cases = [
        ("ACP RPC error -32000: Authentication required", True),
        ("Error -32000", True),
        ("HTTP 401 Unauthorized", True),
        ("Could not find default credentials", True),
        ("API key not valid", True),
        ("SyntaxError: unexpected token on port 32000", False),
        ("", False),
    ]
    for message, expected in cases:
        assert classify_auth_error("antigravity", message) is expected

    for login in (False, True):
        cmd = antigravity_acp_command("agy", login=login)
        assert "--dangerously-skip-permissions" not in cmd and "--disable-telemetry" in cmd

    env = build_antigravity_auth_env(
        AntigravityConfig(auth_mode="oauth-personal", api_key="should-not-leak"),
        base_env={"GEMINI_API_KEY": "stale-shell-key", "GOOGLE_GEMINI_BASE_URL": "https://stale.example"},
    )
    assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
    assert "GEMINI_API_KEY" not in env and "GOOGLE_GEMINI_BASE_URL" not in env


@pytest.mark.asyncio
async def test_maybe_emit_auth_recovery_message_branches_for_antigravity(fake_agy: FakeAgyAuthFixture) -> None:
    im_client = FakeIMClient()
    cfg = V2Config.load()
    service = _make_auth_service(cfg, im_client=im_client)
    ctx = MessageContext(user_id="u1", channel_id="c1", platform="slack")

    cfg.agents.antigravity.auth_mode = "oauth-personal"
    assert await service.maybe_emit_auth_recovery_message(ctx, "antigravity", "ACP RPC error -32000") is True
    assert im_client.sent_buttons and im_client.sent_buttons[-1][1] == "auth_setup:antigravity"

    for auth_mode, err_msg, expected_text in (
        ("gemini-api-key", "API key not valid", "API key"),
        ("agent-platform", "Could not find default credentials", ANTIGRAVITY_ADC_LOGIN_COMMAND),
    ):
        cfg.agents.antigravity.auth_mode = auth_mode
        assert await service.maybe_emit_auth_recovery_message(ctx, "antigravity", err_msg) is True
        assert expected_text in im_client.sent_plain[-1]


def test_install_agent_antigravity_install_and_upgrade_disable_auto_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[tuple[list[str], dict | None, str]] = []

    def fake_run_install(_name, cmd, _trunc, *, mode, env=None, **_kw):
        recorded.append((list(cmd), dict(env) if env else None, mode))
        return {"ok": True, "message": "ok", "output": "", "path": str(tmp_path / "agy")}

    monkeypatch.setattr(api, "_run_install_command", fake_run_install)
    monkeypatch.setattr(api, "is_desktop_managed_runtime", lambda: False)
    monkeypatch.setattr(api, "is_desktop_backend_path", lambda _p: False)

    cfg = V2Config.default()
    cfg.agents.antigravity.cli_path = "agy"
    cfg.save()
    monkeypatch.setattr(
        api, "resolve_cli_path", lambda name: f"/usr/bin/{name}" if name in {"curl", "bash", "powershell"} else None
    )
    monkeypatch.setattr(
        api, "detect_cli", lambda _p, _b: {"found": False, "path": None, "status": "missing", "version": None, "reason": "not_found"}
    )
    assert api._install_agent("antigravity")["ok"] is True
    assert recorded[-1][2] == "install" and recorded[-1][1]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
    assert "https://antigravity.google/cli/install." in " ".join(recorded[-1][0])

    existing = str(tmp_path / "agy")
    monkeypatch.setattr(api, "resolve_cli_path", lambda name: existing if name == "agy" else f"/usr/bin/{name}")
    monkeypatch.setattr(
        api, "detect_cli", lambda _p, _b: {"found": True, "path": existing, "status": "ok", "version": "1.5.15", "reason": None}
    )
    assert api._install_agent("antigravity")["ok"] is True
    assert recorded[-1][0] == [existing, "update"] and recorded[-1][2] == "upgrade"
    assert recorded[-1][1]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"


# ---------------------------------------------------------------------------
# 5. Desktop release-archive + SHA-256 installation & extraction guards
# ---------------------------------------------------------------------------


def _desktop_archive_env(tmp_path: Path) -> dict[str, str]:
    runtime = tmp_path / "runtime" / "1.0.0" / "digest"
    runtime.mkdir(parents=True)
    return {
        "AVIBE_DESKTOP_RUNTIME_ROOT": str(runtime),
        "AVIBE_DESKTOP_BACKENDS_ROOT": str(tmp_path / "backends"),
        "PATH": str(tmp_path / "external-bin"),
    }


def _native_binary_bytes() -> bytes:
    return (b"MZ\x00\x00" if os.name == "nt" else b"\x7fELF") + b"0" * 64


def _build_archive(archive_path: Path, fmt: str, members: dict[str, bytes], *, symlink: str | None = None) -> str:
    if fmt == "tar.gz":
        with tarfile.open(archive_path, "w:gz") as tf:
            for name, content in members.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                info.mode = 0o755
                tf.addfile(info, io.BytesIO(content))
            if symlink is not None:
                link = tarfile.TarInfo(name=symlink)
                link.type = tarfile.SYMTYPE
                link.linkname = "/etc/passwd"
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


def _release_payload(asset_name: str, digest: str | None = None) -> dict:
    asset: dict[str, str] = {
        "name": asset_name,
        "browser_download_url": f"https://github.com/google-antigravity/antigravity-cli/releases/download/v1.5.16/{asset_name}",
    }
    if digest is not None:
        asset["digest"] = f"sha256:{digest}"
    return {"tag_name": "v1.5.16", "assets": [asset]}


@pytest.mark.parametrize("binary_name", ["agy", "antigravity"])
@pytest.mark.parametrize("archive_format", ["tar.gz", "zip"])
def test_desktop_archive_install_verifies_sha256_and_publishes_without_node(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    binary_name: str,
    archive_format: str,
) -> None:
    env = _desktop_archive_env(tmp_path)
    assert desktop_backends.desktop_backend_toolchain(env) is None
    assert desktop_backends.desktop_backend_toolchain(env, backend="antigravity") is not None

    platform_tag = desktop_backends._release_archive_platform_tag("antigravity")
    asset_name = f"agy_cli_{platform_tag}.{archive_format}"
    source_archive = tmp_path / asset_name
    exe_name = f"{binary_name}.exe" if os.name == "nt" else binary_name
    digest = _build_archive(source_archive, archive_format, {f"bin/{exe_name}": _native_binary_bytes()})

    probe_envs: list[dict[str, str]] = []
    monkeypatch.setattr(desktop_backends, "_fetch_github_release", lambda _r, **_kw: _release_payload(asset_name, digest))
    monkeypatch.setattr(
        desktop_backends, "_download_release_asset", lambda _u, dest, **_kw: dest.write_bytes(source_archive.read_bytes())
    )
    monkeypatch.setattr(
        desktop_backends,
        "_run_command",
        lambda cmd, *, cwd, env, timeout_seconds: (
            probe_envs.append(dict(env)) or subprocess.CompletedProcess(cmd, 0, "1.5.16\n", "")
        ),
    )

    activated: list[str] = []
    result = desktop_backends.install_desktop_backend("antigravity", base_env=env, activate=activated.append)
    assert (result.backend, result.package, result.version) == (
        "antigravity",
        "google-antigravity/antigravity-cli",
        "1.5.16",
    )
    assert result.path == activated[0] and Path(result.path).name == exe_name
    assert desktop_backends.resolve_published_desktop_backend("antigravity", env) == result.path
    assert probe_envs and probe_envs[0]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"

    sub_env = desktop_backends.desktop_backend_subprocess_environment("antigravity", result.path, env)
    assert sub_env is not None and sub_env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"


def test_desktop_archive_install_rejects_sha256_mismatch_before_extract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _desktop_archive_env(tmp_path)
    asset_name = f"agy_cli_{desktop_backends._release_archive_platform_tag('antigravity')}.tar.gz"
    source_archive = tmp_path / asset_name
    _build_archive(source_archive, "tar.gz", {"agy": _native_binary_bytes()})

    extract_called = False

    def forbidden_extract(*_a, **_kw):
        nonlocal extract_called
        extract_called = True

    monkeypatch.setattr(desktop_backends, "_fetch_github_release", lambda _r, **_kw: _release_payload(asset_name, "0" * 64))
    monkeypatch.setattr(
        desktop_backends, "_download_release_asset", lambda _u, dest, **_kw: dest.write_bytes(source_archive.read_bytes())
    )
    monkeypatch.setattr(desktop_backends, "_extract_release_archive", forbidden_extract)

    with pytest.raises(desktop_backends.DesktopBackendError) as raised:
        desktop_backends.install_desktop_backend("antigravity", base_env=env)
    assert raised.value.code == "checksum_mismatch" and extract_called is False
    backend_root = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "antigravity"
    assert not (backend_root / "current.json").exists()
    assert list(backend_root.glob(".staging-*")) == [] and list(backend_root.glob("releases/*")) == []


@pytest.mark.parametrize(
    ("case_id", "fmt", "members", "symlink", "omit_digest", "expected_code"),
    [
        ("missing_checksum", "tar.gz", {"agy": b"x"}, None, True, "missing_checksum"),
        ("traversal_tar", "tar.gz", {"../outside-agy": _native_binary_bytes()}, None, False, "invalid_archive"),
        ("symlink_tar", "tar.gz", {"bin/agy": _native_binary_bytes()}, "bin/link", False, "invalid_archive"),
        ("symlink_zip", "zip", {"bin/agy": _native_binary_bytes()}, "bin/link", False, "invalid_archive"),
    ],
)
def test_desktop_archive_install_rejects_missing_checksum_traversal_and_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case_id: str,
    fmt: str,
    members: dict[str, bytes],
    symlink: str | None,
    omit_digest: bool,
    expected_code: str,
) -> None:
    env = _desktop_archive_env(tmp_path)
    asset_name = f"agy_cli_{desktop_backends._release_archive_platform_tag('antigravity')}.{fmt}"
    archive = tmp_path / f"{case_id}.{fmt}"
    digest = _build_archive(archive, fmt, members, symlink=symlink)
    payload = _release_payload(asset_name, None if omit_digest else digest)

    monkeypatch.setattr(desktop_backends, "_fetch_github_release", lambda _r, **_kw: payload)
    monkeypatch.setattr(
        desktop_backends, "_download_release_asset", lambda _u, dest, **_kw: dest.write_bytes(archive.read_bytes())
    )
    with pytest.raises(desktop_backends.DesktopBackendError) as raised:
        desktop_backends.install_desktop_backend("antigravity", base_env=env)
    assert raised.value.code == expected_code
    assert not (tmp_path / "outside-agy").exists()
