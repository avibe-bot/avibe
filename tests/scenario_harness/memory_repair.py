from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import AsyncIterator

import httpx

from config.v2_config import (
    AgentsConfig,
    MemoryConfig,
    MemoryEndpointConfig,
    MemoryProcessingConfig,
    RuntimeConfig,
    SlackConfig,
    V2Config,
)
from vibe import internal_client
from vibe.ui_server import app


LOCAL_BASE_URL = "http://127.0.0.1:15131"
REPAIR_PATH = "/api/memory/runtime/repair"


def repair_health(*, healthy: bool = True) -> dict[str, object]:
    """Return the frozen public cascade projection used by Repair."""

    return {
        "healthy": healthy,
        "reasons": [] if healthy else ["drain_failures"],
        "pending": 0,
        "failed_permanent": 0 if healthy else 1,
        "failed_retryable": 0,
        "drain_consecutive_failures": 0,
        "unrecoverable_total": 0 if healthy else 1,
        "optimize_failure_streak": 0,
        "prune_stale_seconds": 0.0,
    }


def completed_repair(*, healthy: bool = True) -> tuple[int, dict[str, object]]:
    return (
        200,
        {
            "ok": True,
            "result": "completed" if healthy else "completed_with_warnings",
            "health": repair_health(healthy=healthy),
        },
    )


class FakeMemoryRepairRuntime:
    """Hermetic fake for the UI server's internal Memory transport boundary."""

    def __init__(
        self,
        *responses: tuple[int, dict[str, object]],
        repair_available: bool = True,
        hold_repair: bool = False,
    ) -> None:
        self.repair_available = repair_available
        self.responses = deque(responses or (completed_repair(),))
        self.hold_repair = hold_repair
        self.repair_started = asyncio.Event()
        self.release_repair = asyncio.Event()
        self.repair_users: list[str] = []
        self.repair_snapshots: list[dict[str, object]] = []
        self.status_calls = 0
        self.clear_calls = 0
        self.repair_state = "idle"
        self.sidecar_running = True
        self.worker_available = True
        self.claims_paused = False
        self.sidecar_stop_calls = 0
        self.health = repair_health()

    def sync_capability(self) -> bool:
        return self.repair_available

    async def repair(self, *, user_key: str) -> dict[str, object]:
        self.repair_users.append(user_key)
        self.repair_state = "running"
        self.repair_snapshots.append(
            {
                "sidecar_running": self.sidecar_running,
                "worker_available": self.worker_available,
                "claims_paused": self.claims_paused,
                "sidecar_stop_calls": self.sidecar_stop_calls,
            }
        )
        self.repair_started.set()
        if self.hold_repair:
            await self.release_repair.wait()

        status_code, body = self.responses.popleft()
        result = body.get("result")
        self.repair_state = result if isinstance(result, str) else "failed"
        if body.get("ok") is True and isinstance(body.get("health"), dict):
            self.health = deepcopy(body["health"])
        return {"status_code": status_code, "body": deepcopy(body)}

    async def status(self) -> dict[str, object]:
        self.status_calls += 1
        source_available = self.sidecar_running and self.worker_available
        return {
            "status_code": 200,
            "body": {
                "status": "ok",
                "source": {
                    "status": "available" if source_available else "unavailable",
                    "observed_at": "2026-08-11T12:00:00Z",
                    "reason": None if source_available else "memory_sidecar_unavailable",
                },
                "health": {
                    "status": "ok" if self.health["healthy"] else "degraded",
                    "version": "scenario-fake",
                    "capabilities": {"embed": True},
                    "disabled_features": [],
                    "cascade": deepcopy(self.health),
                    "recorder": {"state": "active", "reason": None},
                },
            },
        }

    async def clear(self, *, user_key: str) -> dict[str, object]:
        self.clear_calls += 1
        return {
            "status_code": 200,
            "body": {"status": "completed", "user_key": user_key},
        }


class MemoryRepairScenarioHarness:
    """Drive Memory Repair through the real public ASGI route."""

    def __init__(self, runtime: FakeMemoryRepairRuntime) -> None:
        self.runtime = runtime
        self.headers = {
            "Origin": LOCAL_BASE_URL,
            "X-Vibe-CSRF-Token": "memory-repair-scenario-csrf",
        }

    def install(self, monkeypatch) -> None:
        config = V2Config(
            mode="self_host",
            version="v2",
            slack=SlackConfig(bot_token=""),
            runtime=RuntimeConfig(default_cwd="."),
            agents=AgentsConfig(),
        )
        config.memory = MemoryConfig(
            enabled=True,
            processing=MemoryProcessingConfig(
                llm=MemoryEndpointConfig(
                    "https://llm.scenario.test/v1",
                    "scenario-chat",
                    "scenario-llm-key",
                ),
                embedding=MemoryEndpointConfig(
                    "https://embedding.scenario.test/v1",
                    "scenario-embedding",
                    "scenario-embedding-key",
                ),
            ),
        )
        config.save()
        monkeypatch.setattr(internal_client, "memory_repair", self.runtime.repair)
        monkeypatch.setattr(internal_client, "memory_status", self.runtime.status)
        monkeypatch.setattr(internal_client, "memory_clear", self.runtime.clear)
        monkeypatch.setattr(
            "core.memory.artifact.get_memory_artifact_manager",
            lambda: self.runtime,
        )

    @asynccontextmanager
    async def client(self) -> AsyncIterator[httpx.AsyncClient]:
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 15131))
        async with httpx.AsyncClient(
            transport=transport,
            base_url=LOCAL_BASE_URL,
            cookies={"vibe_csrf_token": "memory-repair-scenario-csrf"},
        ) as client:
            yield client
