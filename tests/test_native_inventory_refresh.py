"""The writer's contract spans the CLI/SDK reply and PR1's durable store.

Store-only tests cannot catch importing the wrong CLI catalog, treating a failed
probe as [], restoring a deleted row on upgrade, or relabeling one account's reply.
"""

import asyncio
import json
from unittest.mock import AsyncMock
import sys
from types import SimpleNamespace

import pytest

from config.v2_config import V2Config
from core.handlers.direct_inventory_identity import NativeScope, inventory_identity
from core.handlers.direct_model_menu import read_inventory, read_menu, set_removed
from core.native_inventory_refresh import NativeInventoryRefresh
from modules.agents.direct_inventory import discover


@pytest.mark.asyncio
async def test_codex_upgrade_failure_and_account_cutover(tmp_path, monkeypatch):
    avibe = tmp_path / "avibe"
    home = tmp_path / "codex"
    home.mkdir()
    monkeypatch.setenv("AVIBE_HOME", str(avibe))
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (home / "config.toml").write_text('cli_auth_credentials_store = "file"')
    auth = home / "auth.json"
    auth.write_text(json.dumps({"OPENAI_API_KEY": "test-account-A"}))
    binary = tmp_path / "codex-cli"
    config = V2Config.default()
    config.agents.codex.enabled = True
    config.agents.codex.cli_path = str(binary)
    config.model_hub.agents["codex"].mode = "direct"
    config.save()
    worker = NativeInventoryRefresh(SimpleNamespace(agent_service=SimpleNamespace(is_backend_ready=lambda _: True)))
    calls = tmp_path / "calls"

    def release(rows, *, broken=False):
        rows = [dict({"priority": i, "supported_in_api": True}, **row) for i, row in enumerate(rows)]
        command = f"print({json.dumps({'models': rows})!r})" if not broken else 'print("{truncated")'
        binary.write_text(
            f'#!{sys.executable}\nimport sys\nassert sys.argv[1:] == ["debug", "models"]\n'
            f'with open({str(calls)!r},"a") as f: f.write("1")\n{command}\n'
        )
        binary.chmod(0o700)

    old = {"slug": "old", "visibility": "list", "priority": -1}
    # A closed native-auth lease must prevent starting a subprocess, even for
    # the boot scan of a freshly installed executable.
    from core.backend_restart import NativeCredentialLease

    release([old])
    with NativeCredentialLease(("codex",)):
        await worker._refresh(config, "codex", None)
    assert not calls.exists()
    worker = NativeInventoryRefresh(worker.controller)
    identity = inventory_identity("codex", config)
    assert identity
    await worker._refresh(config, "codex", None)
    assert [r.id for r in read_menu("codex", identity).models] == ["old"]
    set_removed("codex", "old", True)
    new = [{"slug": slug, "visibility": "list"} for slug in ("gpt-6.2-sol", "gpt-6.2-astra")]
    release(
        [
            old,
            {"slug": "hidden", "visibility": "hide"},
            {"slug": "subscription-only", "visibility": "list", "supported_in_api": False},
            *new,
        ]
    )
    await worker._refresh(config, "codex", None)
    before = read_inventory("codex", identity)
    assert [r.id for r in read_menu("codex", identity).models] == ["gpt-6.2-sol", "gpt-6.2-astra"]
    release([{"slug": "silent-fallback", "visibility": "list"}])
    await worker._refresh(config, "codex", None)
    assert read_inventory("codex", identity) == before
    release([], broken=True)
    await worker._refresh(config, "codex", None)
    await worker._refresh(config, "codex", None)
    assert calls.read_text() == "1111"  # backoff; a reader did not spawn anything
    assert read_inventory("codex", identity) == before
    auth.write_text(json.dumps({"OPENAI_API_KEY": "test-account-B"}))
    other = inventory_identity("codex", config)
    assert other and other != identity and read_inventory("codex", other).observed_at is None
    monkeypatch.setenv("CODEX_API_KEY", "test-override-C")
    assert inventory_identity("codex", config) is None  # competing file and environment credentials
    auth.unlink()
    third = inventory_identity("codex", config)
    assert third and third not in (identity, other)
    (home / "config.toml").write_text('profile="private"\n[profiles.private]\nmodel_catalog_json="projected.json"')
    assert inventory_identity("codex", config) is None


@pytest.mark.asyncio
async def test_stalled_probe_does_not_block_menu_or_relabel_its_reply(tmp_path, monkeypatch):
    from core import native_inventory_refresh as refresh
    from core.handlers.direct_model_menu import PickerModel, publish_inventory

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe"))
    config = V2Config.default()
    config.model_hub.agents["codex"].mode = "direct"
    config.save()
    key = "A"
    monkeypatch.setattr(
        refresh, "_source", lambda *_: refresh._Source(NativeScope(key, key, "api"), "codex", (1,), ".")
    )
    started, finish = asyncio.Event(), asyncio.Event()

    async def blocked(*_):
        started.set()
        await finish.wait()
        return [PickerModel("not-B")]

    monkeypatch.setattr(refresh, "discover", blocked)
    publish_inventory("codex", key, [PickerModel("last-good")])
    worker = NativeInventoryRefresh(SimpleNamespace(agent_service=SimpleNamespace(is_backend_ready=lambda _: True)))
    task = asyncio.create_task(worker._refresh(config, "codex", None))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert [r.id for r in read_menu("codex", key).models] == ["last-good"]
        key = "B"
        assert read_menu("codex", key).observed_at is None
    finally:
        finish.set()
        await task
    assert read_inventory("codex", "B").observed_at is None
    key = "A"  # A rejected commit was not a successful observation of A.
    monkeypatch.setattr(refresh, "discover", AsyncMock(return_value=[PickerModel("last-good"), PickerModel("new-A")]))
    await worker._refresh(config, "codex", None)
    assert [r.id for r in read_inventory("codex", "A").models] == ["last-good", "new-A"]


@pytest.mark.asyncio
async def test_claude_preserves_aliases_even_when_they_resolve_to_one_id(monkeypatch, tmp_path):
    import claude_agent_sdk

    monkeypatch.setenv("HOME", str(tmp_path))

    class Client:
        def __init__(self, *, options):
            assert options.cli_path == "/fixture/claude" and options.strict_mcp_config

        async def connect(self):
            pass

        async def get_server_info(self):
            return {
                "account": {"apiProvider": "firstParty", "apiKeySource": "ANTHROPIC_API_KEY"},
                "models": [
                    {
                        "value": value,
                        "displayName": value,
                        "resolvedModel": "claude-new",
                        "supportedEffortLevels": ["high"],
                    }
                    for value in ("default", "opus")
                ],
            }

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", Client)
    settings = SimpleNamespace(cli_path="/fixture/claude")
    controller = SimpleNamespace(agent_auth_service=SimpleNamespace(_disconnect_claude_probe_client=AsyncMock()))
    rows = await discover(
        "claude", settings, settings.cli_path, str(tmp_path), NativeScope("key", "key", "api"), controller
    )
    controller.agent_auth_service._disconnect_claude_probe_client.assert_awaited_once()
    assert [(r.id, r.resolved_model, r.reasoning_efforts) for r in rows] == [
        ("default", "claude-new", ("high",)),
        ("opus", "claude-new", ("high",)),
    ]
