"""A picker may use an observed inventory only for an unambiguous native authority."""

import json

import pytest

from config.v2_config import V2Config
from core.handlers.direct_inventory_identity import inventory_identity
from core.handlers.direct_model_menu import PickerModel, publish_inventory, read_menu


@pytest.fixture
def codex(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    for key in ("CODEX_ACCESS_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "config.toml").write_text('cli_auth_credentials_store="file"\n')
    (tmp_path / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "fixture"}))
    cfg = V2Config.default()
    cfg.agents.codex.enabled = True
    cfg.model_hub.agents["codex"].mode = "direct"
    cfg.save()
    key = inventory_identity("codex", cfg)
    assert key
    publish_inventory("codex", key, [PickerModel("fixture-private", reasoning_efforts=("high",))])
    return cfg


@pytest.mark.parametrize("change", ["openai_base_url", "chatgpt_base_url", "access-token", "mixed-mode"])
def test_unknown_codex_authority_never_selects_previous_inventory(codex, tmp_path, monkeypatch, change):
    if change == "access-token":
        monkeypatch.setenv("CODEX_ACCESS_TOKEN", "other-fixture")
    elif change == "mixed-mode":
        (tmp_path / "auth.json").write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "OPENAI_API_KEY": "fixture",
                    "tokens": {"account_id": "other-fixture", "access_token": "opaque"},
                }
            )
        )
    else:
        with (tmp_path / "config.toml").open("a") as f:
            print(f'{change}="https://example.invalid"', file=f)
    identity = inventory_identity("codex", codex)
    assert read_menu("codex", identity, fallback=(PickerModel("public"),)).models == (PickerModel("public"),)
    assert identity is None


@pytest.mark.parametrize("field", ["chatgpt_user_id", "user_id", "chatgpt_account_user_id"])
def test_codex_oauth_is_scoped_to_person_and_workspace(codex, tmp_path, field):
    import base64
    from vibe import api

    def login(workspace, claims, revision):
        payload = json.dumps({"https://api.openai.com/auth": claims}).encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        (tmp_path / "auth.json").write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "tokens": {
                        "account_id": workspace,
                        "access_token": revision,
                        "id_token": f"e30.{encoded}.fixture",
                    },
                }
            )
        )
        return inventory_identity("codex", codex)

    shared = {"chatgpt_account_id": "shared-workspace"}
    first = login("shared-workspace", {**shared, field: "fixture-person-a"}, "r1")
    assert first and len(first) == 64
    publish_inventory("codex", first, [PickerModel("first-private")])
    assert login("shared-workspace", {**shared, field: "fixture-person-a"}, "r2") == first
    second = login("shared-workspace", {**shared, field: "fixture-person-b"}, "r3")
    assert second and second != first
    assert "first-private" not in api.codex_models(schedule_refresh=False)["models"]
    publish_inventory("codex", second, [PickerModel("second-private")])
    assert api.codex_models(schedule_refresh=False)["models"] == ["second-private"]
    assert login("shared-workspace", {**shared, field: "fixture-person-a"}, "r4") == first
    assert api.codex_models(schedule_refresh=False)["models"] == ["first-private"]
    for claims in (shared, {field: "fixture-person-a"}, {"chatgpt_account_id": "wrong", field: "fixture-person-a"}):
        assert login("shared-workspace", claims, "r5") is None
        assert "first-private" not in api.codex_models(schedule_refresh=False)["models"]


def test_existing_codex_picker_reads_scoped_ids_and_efforts(codex):
    from vibe import api
    from core.handlers.direct_model_menu import set_removed

    snapshot = api.codex_models(schedule_refresh=False)
    assert "fixture-private" in snapshot["models"]
    assert "high" in [row["value"] for row in snapshot["reasoning_options"]["fixture-private"]]
    assert "fixture-private" in [row["value"] for row in api.agent_model_options("codex", cached_only=True)["models"]]
    set_removed("codex", "fixture-private", True)
    assert "fixture-private" not in api.codex_models(schedule_refresh=False)["models"]


def test_unreadable_config_keeps_public_codex_removals(codex, monkeypatch):
    from core.handlers.direct_model_menu import set_removed
    from vibe import api

    def unavailable(*args, **kwargs):
        raise FileNotFoundError("fixture config unavailable")

    set_removed("codex", "gpt-6.2-sol", True)
    monkeypatch.setattr(V2Config, "load", unavailable)
    snapshot = api.codex_models(schedule_refresh=False)
    assert "gpt-6.2-sol" not in snapshot["models"]
    assert "fixture-private" not in snapshot["models"]


def test_public_default_efforts_and_explicit_empty_native_efforts(codex, tmp_path, monkeypatch):
    from vibe import api, backend_model_catalog as catalog

    key = inventory_identity("codex", codex)
    publish_inventory("codex", key, [PickerModel("fixture-model", reasoning_efforts=())])

    def efforts():
        snapshot = api.codex_models(schedule_refresh=False)
        return [opt["value"] for opt in snapshot["reasoning_options"]["fixture-model"]]

    assert efforts() == ["__default__"]
    (tmp_path / "auth.json").unlink()
    remote = {"backends": {"codex": {"models": [{"id": "fixture-model"}]}}}
    monkeypatch.setattr(catalog, "load_cached_remote_catalog", lambda **_: remote)
    assert efforts() == ["__default__", "minimal", "low", "medium", "high", "xhigh"]


def test_claude_custom_headers_are_not_scoped_by_an_unrelated_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "Authorization: fixture")
    cfg = V2Config.default()
    cfg.agents.claude.enabled = True
    assert inventory_identity("claude", cfg) is None


@pytest.mark.asyncio
async def test_failed_acp_session_preserves_membership(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from modules.agents import acp
    from modules.agents.antigravity.auth import run_antigravity_connection_probe
    from modules.agents.antigravity.models import read_antigravity_live_models, record_antigravity_live_models
    from tests.helpers.acp_probe_stub import make_probe_client

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe"))
    cfg = V2Config.default()
    settings = cfg.agents.antigravity
    settings.auth_mode, settings.api_key = "gemini-api-key", "fixture"
    cfg.save()

    def options(ids):
        return [{"id": "model", "options": [{"value": value} for value in ids]}]

    record_antigravity_live_models(settings, {"sessionId": "good", "configOptions": options(["A", "B"])})
    before = read_antigravity_live_models(settings)
    assert [row["id"] for row in before] == ["A", "B"]
    client, _ = make_probe_client(options=[])
    monkeypatch.setattr(client, "session_new", AsyncMock(return_value={"configOptions": options(["A"])}))
    monkeypatch.setattr(acp, "AcpClient", client)
    result = await run_antigravity_connection_probe(binary="/fake/agy", cwd=str(tmp_path), model="A", config=settings)
    assert result["error"] == "session_create_failed"
    assert read_antigravity_live_models(settings) == before


def test_oauth_email_alone_is_not_a_claude_organization(tmp_path, monkeypatch):
    from vibe import native_oauth_store as store

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = V2Config.default()
    cfg.agents.claude.enabled = True
    for org in ("one", "two"):
        bag = store.NativeOAuthSnapshot(
            "claude", org, {"claudeAiOauth": {"email": "fixture@example.invalid", "organizationUuid": org}}, True
        )
        monkeypatch.setattr(store, "read_native_oauth", lambda *args, **kwargs: bag)
        assert inventory_identity("claude", cfg) is None


def test_scope_rejects_oversize_codex_config(codex, tmp_path):
    (tmp_path / "config.toml").write_bytes(b"#" + b"x" * (3 * 1024 * 1024))
    assert inventory_identity("codex", codex) is None


@pytest.mark.asyncio
async def test_cold_opencode_is_not_a_failed_attempt(codex, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from core import native_inventory_refresh as refresh
    from modules.agents.direct_inventory import InventoryNotReady
    from core.handlers.direct_inventory_identity import NativeScope

    codex.model_hub.agents["opencode"].mode = "direct"
    codex.save()
    scope = NativeScope("fixture-scope", "fixture-revision", "opencode")
    monkeypatch.setattr(refresh, "_source", lambda *_: refresh._Source(scope, "fixture", (1,), "."))
    probe = AsyncMock(side_effect=[InventoryNotReady("cold directory"), [PickerModel("fixture/one")]])
    monkeypatch.setattr(refresh, "discover", probe)
    worker = refresh.NativeInventoryRefresh(
        SimpleNamespace(agent_service=SimpleNamespace(is_backend_ready=lambda _: True))
    )
    await worker._refresh(codex, "opencode", ".")
    assert not worker.attempts
    await worker._refresh(codex, "opencode", ".")
    assert [row.id for row in read_menu("opencode", scope.identity).models] == ["fixture/one"]
