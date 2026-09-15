"""Real EverOS storage/processing with a loopback external-provider double."""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import json
import os
import sys

import psutil
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from avibe_memory.artifact import EVEROS_VERSION, MemoryArtifactManager
from avibe_memory.types import CaptureAccepted, CaptureRequest, RecallPolicy
from config.v2_config import MemoryConfig, MemoryEndpointConfig, MemoryProcessingConfig


@pytest.fixture
def lifecycle_provider():
    # Same stdlib HTTP boundary used by the Model Hub test upstream. This fixture
    # replaces only external model answers; Avibe/EverOS storage is never mocked.
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            if self.path.endswith("/embeddings"):
                inputs = body["input"]
                if isinstance(inputs, str):
                    inputs = [inputs]
                result = {
                    "object": "list",
                    "model": "fixture",
                    "data": [
                        {"object": "embedding", "index": i, "embedding": [1.0] + [0.0] * 1023}
                        for i, _ in enumerate(inputs)
                    ],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                }
            else:
                prompt = json.dumps(body.get("messages", []), ensure_ascii=False)
                token = next(
                    (value for value in ("lifecycle-new-1990", "lifecycle-during-1990") if value in prompt),
                    "lifecycle-old-1990",
                )
                content = json.dumps(
                    {
                        "title": token,
                        "content": f"The user prefers {token} tea. 测试记忆。",
                        "atomic_facts": {"time": "2026-09-15", "atomic_fact": [f"The user prefers {token} tea."]},
                        "foresights": [],
                        "should_split": False,
                    }
                )
                if "should_end" in prompt:
                    content = json.dumps({"should_end": False, "reason": "fixture", "topic_summary": "tea"})
                elif "should_wait" in prompt:
                    content = json.dumps({"boundaries": [], "should_wait": True})
                result = {
                    "id": "fixture",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "fixture",
                    "choices": [
                        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            raw = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_real_wake_preserves_old_and_processes_new_input(monkeypatch, memory_runtime_factory, lifecycle_provider):
    """MEMORY-WAKE-205: real claims/writer -> EverOS -> recall across Wake."""
    runtime_python = Path.cwd() / "scripts/memory_runtime/.venv/bin/python"
    if importlib.util.find_spec("everos") is None or not runtime_python.is_file():
        if os.environ.get("AVIBE_REQUIRE_MEMORY_RUNTIME_CONTRACT") == "1":
            pytest.fail("pinned EverOS 1.2.3 runtime environment is required")
        pytest.skip("pinned EverOS 1.2.3 runtime environment is not installed")
    assert importlib.metadata.version("everos") == EVEROS_VERSION
    monkeypatch.setenv("AVIBE_MEMORY_DEV_RUNTIME", str(runtime_python))
    url, requests = lifecycle_provider
    config = MemoryConfig(
        enabled=True,
        profile_enabled=False,
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig(url, "fixture", "fixture-key"),
            embedding=MemoryEndpointConfig(url, "fixture", "fixture-key"),
        ),
    )
    with tempfile.TemporaryDirectory(prefix="ml1990-", dir="/tmp") as temporary:
        home = Path(temporary).resolve()
        artifact = MemoryArtifactManager(
            runtime_dir=home / "runtime", offline=True, provider_root=home / "memory/everos-root"
        )
        runtime = memory_runtime_factory(config, artifact_manager=artifact, effective_home=home)
        principal = "u-" + "1" * 32

        async def capture_and_recall(token):
            assert (
                await runtime.module.capture(
                    CaptureRequest(
                        source_message_id=token,
                        session_id=token,
                        principal_id=principal,
                        project_id="default",
                        provenance="user_input",
                        text=f"I prefer {token} tea. 测试记忆。",
                        occurred_at_ms=1789488000000,
                    )
                )
                == CaptureAccepted()
            )
            await runtime.module.wait_writer_idle_for_tests(timeout_seconds=30)
            assert runtime.module.offer_barrier(token) == "queued"
            await runtime.module.wait_writer_idle_for_tests(timeout_seconds=30)
            return await recall(token)

        async def recall(token):
            for _ in range(100):
                result = await runtime.module.recall(
                    token,
                    policy=RecallPolicy(mode="keyword", include_profile=False),
                    principal_id=principal,
                    project_id="default",
                )
                if token in str(result):
                    return result
                await asyncio.sleep(0.2)
            pytest.fail(
                f"real EverOS did not recall {token}; result={result}; provider routes={[p for p, _ in requests]}"
            )

        try:
            assert await asyncio.wait_for(runtime.wake(), 60) == {"ok": True, "state": "running"}
            old_process = runtime._supervisor._child
            await capture_and_recall("lifecycle-old-1990")
            native_child = old_process._process
            if sys.platform == "darwin":
                from psutil import _psosx

                stamp = psutil.Process(native_child.pid).create_time()
                monkeypatch.setattr(_psosx, "boot_time", lambda: _psosx.INIT_BOOT_TIME + 1)
                assert psutil.Process(native_child.pid).create_time() == stamp + 1
            await asyncio.sleep(3.2)
            await capture_and_recall("lifecycle-during-1990")
            assert runtime._supervisor._child is old_process
            assert native_child.returncode is None
            assert runtime._supervisor._restart_attempts == 0
            assert await asyncio.wait_for(runtime.wake(), 60) == {"ok": True, "state": "running"}
            assert native_child.returncode is not None
            assert not old_process.running
            assert not old_process.retains_active_config
            await recall("lifecycle-old-1990")
            await capture_and_recall("lifecycle-new-1990")
            await recall("lifecycle-old-1990")
            assert requests
        finally:
            await memory_runtime_factory.close(runtime)
