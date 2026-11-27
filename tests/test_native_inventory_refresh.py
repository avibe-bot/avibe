"""The writer's contract spans the CLI/SDK reply and PR1's durable store.

Store-only tests cannot catch importing the wrong CLI catalog, treating a failed
probe as [], restoring a deleted row on upgrade, or relabeling one account's reply.
"""

import asyncio
import json
from contextlib import asynccontextmanager
import sys
from types import SimpleNamespace

import pytest

from config.v2_config import V2Config
from core.handlers.direct_inventory_identity import inventory_identity
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
    worker = NativeInventoryRefresh(None)
    calls = tmp_path / "calls"

    def release(rows, *, broken=False):
        command = f"print({json.dumps({'models': rows})!r})" if not broken else 'print("{truncated")'
        binary.write_text(
            f'#!{sys.executable}\nimport sys\nassert sys.argv[1:] == ["debug", "models"]\n'
            f'with open({str(calls)!r},"a") as f: f.write("1")\n{command}\n'
        )
        binary.chmod(0o700)

    old = {"slug": "old", "visibility": "list"}
    release([old])
    identity = inventory_identity("codex", config)
    assert identity
    await worker._refresh(config, "codex", None)
    assert [r.id for r in read_menu("codex", identity).models] == ["old"]
    set_removed("codex", "old", True)
    new = [{"slug": slug, "visibility": "list"} for slug in ("gpt-6.2-sol", "gpt-6.2-astra")]
    release([old, {"slug": "hidden", "visibility": "hide"}, *new])
    await worker._refresh(config, "codex", None)
    before = read_inventory("codex", identity)
    assert [r.id for r in read_menu("codex", identity).models] == ["gpt-6.2-sol", "gpt-6.2-astra"]
    release([], broken=True)
    await worker._refresh(config, "codex", None)
    await worker._refresh(config, "codex", None)
    assert calls.read_text() == "111"  # backoff; a reader did not spawn anything
    assert read_inventory("codex", identity) == before
    auth.write_text(json.dumps({"OPENAI_API_KEY": "test-account-B"}))
    other = inventory_identity("codex", config)
    assert other and other != identity and read_inventory("codex", other).observed_at is None


@pytest.mark.asyncio
async def test_stalled_probe_does_not_block_menu_or_relabel_its_reply(tmp_path, monkeypatch):
    from core import native_inventory_refresh as refresh
    from core.handlers.direct_model_menu import PickerModel, publish_inventory

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe"))
    config = V2Config.default()
    config.save()
    key = "A"
    monkeypatch.setattr(refresh, "_source", lambda *_: (key, "codex", (1,), "."))
    started, finish = asyncio.Event(), asyncio.Event()

    async def blocked(*_):
        started.set()
        await finish.wait()
        return [PickerModel("not-B")]

    monkeypatch.setattr(refresh, "discover", blocked)
    publish_inventory("codex", key, [PickerModel("last-good")])
    task = asyncio.create_task(NativeInventoryRefresh(None)._refresh(config, "codex", None))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert [r.id for r in read_menu("codex", key).models] == ["last-good"]
        key = "B"
        assert read_menu("codex", key).observed_at is None
    finally:
        finish.set()
        await task
    assert read_inventory("codex", "B").observed_at is None
    assert [r.id for r in read_inventory("codex", "A").models] == ["last-good"]


@pytest.mark.asyncio
async def test_claude_preserves_aliases_even_when_they_resolve_to_one_id(monkeypatch, tmp_path):
    import claude_agent_sdk

    monkeypatch.setenv("HOME", str(tmp_path))

    class Client:
        def __init__(self, *, options):
            assert options.cli_path == "/fixture/claude" and options.strict_mcp_config

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def get_server_info(self):
            return {
                "models": [
                    {
                        "value": value,
                        "displayName": value,
                        "resolvedModel": "claude-new",
                        "supportedEffortLevels": ["high"],
                    }
                    for value in ("default", "opus")
                ]
            }

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", Client)
    settings = SimpleNamespace(cli_path="/fixture/claude")
    rows = await discover("claude", settings, settings.cli_path, str(tmp_path), None)
    assert [(r.id, r.resolved_model, r.reasoning_efforts) for r in rows] == [
        ("default", "claude-new", ("high",)),
        ("opus", "claude-new", ("high",)),
    ]


@pytest.mark.asyncio
async def test_opencode_selects_connected_provider_in_the_requested_directory(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from modules.agents.opencode.server import OpenCodeServerClient
    config = V2Config.default()
    config.agents.opencode.enabled = True
    monkeypatch.setenv("HOME", str(tmp_path))
    data = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_AUTH_CONTENT", "OPENCODE_CONFIG_CONTENT"):
        monkeypatch.delenv(key, raising=False)
    auth = data / "opencode/auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text('{"one":{"type":"api","key":"fixture-A"}}')
    folder = str(tmp_path / "workspace")
    scope = inventory_identity("opencode", config, cwd=folder)
    assert scope and inventory_identity("opencode", config, cwd=str(tmp_path)) != scope
    auth.write_text('{"one":{"type":"api","key":"fixture-B"}}')
    assert inventory_identity("opencode", config, cwd=folder) != scope

    async def providers(request):
        assert request.headers["x-opencode-directory"] == folder
        return web.json_response(
            {
                "all": [
                    {"id": pid, "models": {"new": {"name": "New", "variants": {"high": {}}}}} for pid in ("one", "two")
                ],
                "connected": ["one"],
            }
        )

    app = web.Application()
    app.router.add_get("/provider", providers)
    async with TestServer(app) as http:
        server = OpenCodeServerClient(str(http.make_url("")).rstrip("/"))

        @asynccontextmanager
        async def current():
            yield server

        agent = SimpleNamespace(current_server=current)
        controller = SimpleNamespace(agent_service=SimpleNamespace(agents={"opencode": agent}))
        try:
            rows = await discover("opencode", None, "", folder, controller)
            assert [(r.id, r.reasoning_efforts) for r in rows] == [("one/new", ("high",))]
        finally:
            await server.close_http_session()
