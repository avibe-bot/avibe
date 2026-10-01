"""RUNTIME-GEN-019: the real Codex CLI contract that app-server generations rely on.

Two app-servers share one working directory and one ``CODEX_HOME``. A thread
loaded in one cannot be resumed in the other until the first unsubscribes from
it and reports ``thread/closed``; the move appends nothing to the rollout.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path

import pytest

from modules.agents.codex.transport import CodexRPCError, CodexTransport
from tests.e2e.drivers.mock_llm_upstream import MockLLMUpstream
from tests.e2e.test_model_hub_catalog_consumer import (  # noqa: F401 -- fixture dependency
    _isolated_runtime,
    _loopback_cli,
    rejected_external_proxy,
)


pytestmark = pytest.mark.e2e_model_hub


class _Server:
    def __init__(self, binary, runtime, cwd):
        self.transport = CodexTransport(binary=binary, cwd=cwd, runtime_env=runtime.env)
        self.completed: dict[str, asyncio.Queue] = {}
        self.closed: dict[str, asyncio.Event] = {}
        self.transport.on_notification(self._notify)

    async def _notify(self, method, params):
        thread_id = params.get("threadId")
        if method == "turn/completed":
            self.completed.setdefault(thread_id, asyncio.Queue()).put_nowait(params)
        elif method == "thread/closed":
            self.closed.setdefault(thread_id, asyncio.Event()).set()

    async def turn(self, thread_id, text):
        queue = self.completed.setdefault(thread_id, asyncio.Queue())
        await self.transport.send_request(
            "turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}]}
        )
        done = await asyncio.wait_for(queue.get(), timeout=60)
        assert done["turn"]["status"] == "completed", done


def _digest(path: Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


@pytest.fixture
def codex_home_runtime(tmp_path, rejected_external_proxy):
    binary = shutil.which("codex")
    if binary is None:
        pytest.skip("Codex executable is unavailable")
    runtime = _isolated_runtime(tmp_path, rejected_external_proxy)
    binary = _loopback_cli(binary, runtime)
    codex_home = Path(runtime.env["CODEX_HOME"])
    codex_home.mkdir()
    node = shutil.which("node")
    if node:
        runtime.env["PATH"] += ":" + str(Path(node).parent)
    upstream = MockLLMUpstream().start()
    upstream.configure(protocol="openai_responses")
    runtime.env["PROBE_API_KEY"] = "fixture-only"
    (codex_home / "config.toml").write_text(
        'cli_auth_credentials_store = "file"\n'
        'model = "generation-fixture"\n'
        'model_provider = "fixture"\n'
        "[model_providers.fixture]\n"
        'name = "fixture"\n'
        f'base_url = "{upstream.url}/v1"\n'
        'wire_api = "responses"\n'
        'env_key = "PROBE_API_KEY"\n'
    )
    workdir = runtime.home / "工作 目录"
    workdir.mkdir()
    try:
        yield binary, runtime, str(workdir)
    finally:
        upstream.stop()


async def test_runtime_gen_019_a_thread_moves_between_app_servers_only_after_release(codex_home_runtime):
    binary, runtime, cwd = codex_home_runtime
    old = _Server(binary, runtime, cwd)
    new = _Server(binary, runtime, cwd)
    try:
        await old.transport.start()
        await new.transport.start()
        started = await old.transport.send_request(
            "thread/start", {"cwd": cwd, "approvalPolicy": "never", "sandbox": "danger-full-access"}
        )
        thread_id = started["thread"]["id"]
        rollout = Path(started["thread"]["path"])
        await old.turn(thread_id, "第一轮 on the old process")

        # Codex lets one process at a time write a thread.
        with pytest.raises(CodexRPCError, match="active writer"):
            await new.transport.send_request("thread/resume", {"threadId": thread_id, "excludeTurns": True})

        before = _digest(rollout)
        closed = old.closed.setdefault(thread_id, asyncio.Event())
        response = await old.transport.send_request("thread/unsubscribe", {"threadId": thread_id})
        assert response["status"] == "unsubscribed"
        # Avibe's thread_unload_delay_secs=0 unloads an idle thread at once.
        await asyncio.wait_for(closed.wait(), timeout=5)
        assert _digest(rollout) == before

        await new.transport.send_request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        await new.turn(thread_id, "second turn on the new process")
        moved = _digest(rollout)

        await old.transport.stop()
        await asyncio.sleep(0.5)
        assert _digest(rollout) == moved
        read = await new.transport.send_request("thread/read", {"threadId": thread_id, "includeTurns": True})
        assert [turn["status"] for turn in read["thread"]["turns"]] == ["completed", "completed"]
    finally:
        await old.transport.stop()
        await new.transport.stop()
