"""Source filter hooks calling the gateway's models (docs/plans/vibey-source-filter-hooks.md §5.5).

The owner boundary is the turn gateway: a hook run gets the gateway address and a key minted for that run. The key
serves Vibey's menu models on chat/completions only, never a hosted tool, and stops working when the run ends. The
end-to-end case runs a real hook subprocess that calls the real gateway; only the engine adapter is faked.
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from contextlib import asynccontextmanager

import aiohttp
import pytest

from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.source_filter import HookFailed
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from tests.test_model_hub_l3 import LiveInvokeHandle, _outcome, _source
from tests.test_model_hub_vibey_consumer import _vibey_service

ANSWER = json.dumps({"id": "c1", "choices": [{"index": 0, "message": {"role": "assistant", "content": "[1]"}}]}).encode()


def _gateway(tmp_path, *answers):
    source = _source("src_primary01", "Primary", vendor="openai", protocol="openai_chat")
    service = _vibey_service(tmp_path, [source], handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=source.id), (body,)) for body in answers
    ])
    return service, ModelHubTurnGateway(service)


async def _post(env, path, body, headers=None):
    async with aiohttp.ClientSession() as client:
        async with client.post(
            f'{env["AVIBE_GATEWAY_URL"]}/{path}',
            headers={"Authorization": f'Bearer {env["AVIBE_GATEWAY_KEY"]}', **(headers or {})},
            json=body,
        ) as response:
            return response.status, await response.read()


@pytest.mark.asyncio
async def test_a_hook_key_serves_chat_completions_only_while_its_run_lasts(tmp_path):
    service, gateway = _gateway(tmp_path, ANSWER)
    plain = {"model": "menu-alias", "stream": False, "messages": [{"role": "user", "content": "pick"}]}
    try:
        async with gateway.hook_gateway_env() as env:
            assert env["AVIBE_GATEWAY_URL"].endswith("/vibey/v1")
            assert await _post(env, "chat/completions", plain) == (200, ANSWER)
            # Searching or fetching on its own would bypass the hooks, or recurse into them.
            for refused, path, headers in (
                ({**plain, "tools": [{"type": "web_search"}]}, "chat/completions", None),
                ({**plain, "web_search_options": {}}, "chat/completions", None),
                (plain, "responses", None),
                (plain, "messages", None),
                (plain, "chat/completions", {"x-avibe-turn-id": "turn-1"}),
            ):
                status, _ = await _post(env, path, refused, headers)
                assert status == 403, (path, refused)
            # It owns no turn, so no hosted tool runs on it.
            status, _ = await _post(env, "hosted-tools/web_search", {"arguments": {"query": "q"}})
            assert status in (401, 409)
        # Retired with the run.
        status, _ = await _post(env, "chat/completions", plain)
        assert status == 401
        assert len(service.adapter.invocations) == 1
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_a_stopped_hook_retires_its_run_key(tmp_path, monkeypatch, stop):
    """Longer run budgets must not keep a credential alive after timeout or user cancellation."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    folder = tmp_path / "home" / "hooks" / "source_filter"
    folder.mkdir(parents=True)
    ready = folder / "ready"
    (folder / "after_search.py").write_text(
        "import pathlib, time\npathlib.Path('ready').touch()\ntime.sleep(30)\n", encoding="utf-8",
    )
    service, gateway = _gateway(tmp_path)
    hooks = service._source_filter_hooks
    env = {}

    @asynccontextmanager
    async def capture_env():
        async with gateway.hook_gateway_env() as current:
            env.update(current)
            yield current

    hooks.gateway = capture_env
    if stop == "timeout":
        hooks.timeout = 0.5
    task = asyncio.create_task(hooks.run("after_search", {"v": 1, "hook": "after_search"}))
    try:
        if stop == "cancel":
            async def wait_ready():
                while not ready.exists():
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_ready(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(HookFailed, match="timed out"):
                await task
        status, _ = await _post(env, "chat/completions", {"model": "menu-alias", "messages": []})
        assert status == 401
        assert not service.adapter.invocations
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await gateway.close()


@pytest.mark.asyncio
async def test_a_hook_script_calls_a_model_through_its_run_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    folder = tmp_path / "home" / "hooks" / "source_filter"
    folder.mkdir(parents=True)
    (folder / "after_search.py").write_text(textwrap.dedent("""
        import json, os, sys, urllib.request
        req = json.load(sys.stdin)
        call = urllib.request.Request(
            os.environ["AVIBE_GATEWAY_URL"] + "/chat/completions",
            data=json.dumps({"model": "menu-alias", "messages": [{"role": "user", "content": "pick"}]}).encode(),
            headers={"Authorization": "Bearer " + os.environ["AVIBE_GATEWAY_KEY"], "Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        keep = json.loads(json.load(opener.open(call, timeout=20))["choices"][0]["message"]["content"])
        results = [req["output"]["result"]["results"][i] for i in keep]
        json.dump({"action": "replace", "output": {"ok": True, "result": {"results": results}}}, sys.stdout)
    """), encoding="utf-8")
    service, gateway = _gateway(tmp_path, ANSWER)
    hooks = service._source_filter_hooks
    assert hooks.gateway is not None
    try:
        answer = await hooks.run("after_search", {
            "v": 1, "hook": "after_search", "args": {"query": "q"}, "origin": "gateway",
            "output": {"ok": True, "result": {"results": [{"url": "https://a.example/"}, {"url": "https://b.example/"}]}},
        })
        assert answer.response["output"]["result"]["results"] == [{"url": "https://b.example/"}]
        assert len(service.adapter.invocations) == 1
    finally:
        await gateway.close()
