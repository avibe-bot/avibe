"""Closed-loop public-route journeys for the Memory Repair capability."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.scenario_harness.memory_repair import (
    REPAIR_PATH,
    FakeMemoryRepairRuntime,
    MemoryRepairScenarioHarness,
    completed_repair,
    repair_health,
)
from vibe import ui_memory_routes


def assert_confirmed_repair_post(response) -> None:
    assert response.request.method == "POST"
    assert response.request.url.path == REPAIR_PATH
    assert json.loads(response.request.content) == {"confirm": True}


async def test_memory_repair_001_completes_through_public_route(monkeypatch) -> None:
    """Scenario: MEMORY-REPAIR-001"""

    runtime = FakeMemoryRepairRuntime(completed_repair())
    harness = MemoryRepairScenarioHarness(runtime)
    harness.install(monkeypatch)

    async with harness.client() as client:
        given = await client.get("/api/memory/settings", headers=harness.headers)
        assert given.status_code == 200
        assert given.json()["enabled"] is True
        assert given.json()["repair_available"] is True
        assert runtime.repair_state == "idle"

        completed = await client.post(
            REPAIR_PATH,
            json={"confirm": True},
            headers=harness.headers,
        )
        assert_confirmed_repair_post(completed)
        assert completed.status_code == 200
        assert completed.headers["cache-control"] == "no-store"
        assert completed.json() == {
            "ok": True,
            "result": "completed",
            "health": repair_health(),
        }
        assert runtime.repair_users == ["avibe:local"]
        assert runtime.repair_state == "completed"

        follow_up = await client.get("/api/memory/status", headers=harness.headers)
        assert follow_up.status_code == 200
        assert follow_up.json()["source"]["status"] == "available"
        assert follow_up.json()["health"]["cascade"] == repair_health()


async def test_memory_repair_002_preserves_warning_health_through_public_route(
    monkeypatch,
) -> None:
    """Scenario: MEMORY-REPAIR-002"""

    runtime = FakeMemoryRepairRuntime(completed_repair(healthy=False))
    harness = MemoryRepairScenarioHarness(runtime)
    harness.install(monkeypatch)

    async with harness.client() as client:
        given = await client.get("/api/memory/settings", headers=harness.headers)
        assert given.status_code == 200
        assert given.json()["enabled"] is True
        assert given.json()["repair_available"] is True
        assert runtime.health == repair_health()

        warned = await client.post(
            REPAIR_PATH,
            json={"confirm": True},
            headers=harness.headers,
        )
        assert_confirmed_repair_post(warned)
        assert warned.status_code == 200
        assert warned.json() == {
            "ok": True,
            "result": "completed_with_warnings",
            "health": repair_health(healthy=False),
        }
        assert runtime.repair_state == "completed_with_warnings"

        follow_up = await client.get("/api/memory/status", headers=harness.headers)
        assert follow_up.status_code == 200
        assert follow_up.json()["health"]["status"] == "degraded"
        assert follow_up.json()["health"]["cascade"] == repair_health(healthy=False)


async def test_memory_repair_003_retains_owner_and_rejects_conflicting_mutation(
    monkeypatch,
) -> None:
    """Scenario: MEMORY-REPAIR-003"""

    runtime = FakeMemoryRepairRuntime(completed_repair(), hold_repair=True)
    harness = MemoryRepairScenarioHarness(runtime)
    harness.install(monkeypatch)

    async with harness.client() as client:
        given_settings = await client.get(
            "/api/memory/settings",
            headers=harness.headers,
        )
        given_status = await client.get("/api/memory/status", headers=harness.headers)
        assert given_settings.status_code == 200
        assert given_settings.json()["enabled"] is True
        assert given_settings.json()["repair_available"] is True
        assert given_status.status_code == 200
        assert given_status.json()["source"]["status"] == "available"
        assert runtime.repair_state == "idle"

        second_request_entered = asyncio.Event()
        user_key_calls = 0

        def user_key() -> str:
            nonlocal user_key_calls
            user_key_calls += 1
            if user_key_calls == 2:
                second_request_entered.set()
            return "avibe:local"

        monkeypatch.setattr(ui_memory_routes, "_memory_ui_user_key", user_key)
        abandoned = asyncio.create_task(
            client.post(REPAIR_PATH, json={"confirm": True}, headers=harness.headers)
        )
        await runtime.repair_started.wait()
        joined = asyncio.create_task(
            client.post(REPAIR_PATH, json={"confirm": True}, headers=harness.headers)
        )
        await second_request_entered.wait()
        abandoned.cancel()
        with pytest.raises(asyncio.CancelledError):
            await abandoned

        conflict = await client.post(
            "/api/memory/clear",
            json={"confirm": True},
            headers=harness.headers,
        )
        assert conflict.status_code == 409
        assert conflict.json() == {
            "status": "failed",
            "error": "memory_operation_in_progress",
        }
        assert runtime.clear_calls == 0
        assert runtime.repair_users == ["avibe:local"]
        assert runtime.repair_state == "running"
        assert not joined.done()

        runtime.release_repair.set()
        completed = await joined
        assert_confirmed_repair_post(completed)
        assert completed.status_code == 200
        assert completed.json()["result"] == "completed"
        assert runtime.repair_users == ["avibe:local"]

        follow_up = await client.get("/api/memory/status", headers=harness.headers)
        assert follow_up.status_code == 200
        assert follow_up.json()["source"]["status"] == "available"
        assert runtime.repair_state == "completed"


async def test_memory_repair_004_failure_closes_and_service_retry_succeeds(
    monkeypatch,
) -> None:
    """Scenario: MEMORY-REPAIR-004"""

    failed = (
        503,
        {
            "ok": False,
            "error": "memory_repair_failed",
            "result": "timed_out",
        },
    )
    runtime = FakeMemoryRepairRuntime(failed, completed_repair())
    harness = MemoryRepairScenarioHarness(runtime)
    harness.install(monkeypatch)

    async with harness.client() as client:
        given = await client.get("/api/memory/settings", headers=harness.headers)
        assert given.status_code == 200
        assert given.json()["enabled"] is True
        assert given.json()["repair_available"] is True

        failure = await client.post(
            REPAIR_PATH,
            json={"confirm": True},
            headers=harness.headers,
        )
        assert_confirmed_repair_post(failure)
        assert failure.status_code == 503
        assert failure.json() == failed[1]
        assert runtime.repair_state == "timed_out"

        still_available = await client.get(
            "/api/memory/settings",
            headers=harness.headers,
        )
        assert still_available.status_code == 200
        assert still_available.json()["repair_available"] is True

        retried = await client.post(
            REPAIR_PATH,
            json={"confirm": True},
            headers=harness.headers,
        )
        assert_confirmed_repair_post(retried)
        assert retried.status_code == 200
        assert retried.json()["result"] == "completed"
        assert runtime.repair_users == ["avibe:local", "avibe:local"]
        assert runtime.repair_state == "completed"

        follow_up = await client.get("/api/memory/status", headers=harness.headers)
        assert follow_up.status_code == 200
        assert follow_up.json()["health"]["cascade"] == repair_health()


async def test_memory_repair_005_fails_closed_when_capability_is_unavailable(
    monkeypatch,
) -> None:
    """Scenario: MEMORY-REPAIR-005"""

    unsupported = (
        409,
        {
            "ok": False,
            "error": "memory_runtime_unsupported",
            "result": "failed",
        },
    )
    runtime = FakeMemoryRepairRuntime(unsupported, repair_available=False)
    harness = MemoryRepairScenarioHarness(runtime)
    harness.install(monkeypatch)

    async with harness.client() as client:
        given = await client.get("/api/memory/settings", headers=harness.headers)
        assert given.status_code == 200
        assert given.json()["enabled"] is True
        assert given.json()["repair_available"] is False

        rejected = await client.post(
            REPAIR_PATH,
            json={"confirm": True},
            headers=harness.headers,
        )
        assert_confirmed_repair_post(rejected)
        assert rejected.status_code == 409
        assert rejected.json() == unsupported[1]
        assert runtime.repair_users == ["avibe:local"]
        assert runtime.repair_state == "failed"

        follow_up_settings = await client.get(
            "/api/memory/settings",
            headers=harness.headers,
        )
        follow_up_status = await client.get(
            "/api/memory/status",
            headers=harness.headers,
        )
        assert follow_up_settings.status_code == 200
        assert follow_up_settings.json()["repair_available"] is False
        assert follow_up_status.status_code == 200
        assert follow_up_status.json()["source"]["status"] == "available"


async def test_memory_repair_006_keeps_sidecar_available_during_public_request(
    monkeypatch,
) -> None:
    """Scenario: MEMORY-REPAIR-006"""

    runtime = FakeMemoryRepairRuntime(completed_repair(), hold_repair=True)
    harness = MemoryRepairScenarioHarness(runtime)
    harness.install(monkeypatch)

    async with harness.client() as client:
        given_settings = await client.get(
            "/api/memory/settings",
            headers=harness.headers,
        )
        given_status = await client.get("/api/memory/status", headers=harness.headers)
        assert given_settings.status_code == 200
        assert given_settings.json()["enabled"] is True
        assert given_settings.json()["repair_available"] is True
        assert given_status.status_code == 200
        assert given_status.json()["source"]["status"] == "available"
        assert runtime.sidecar_stop_calls == 0

        pending = asyncio.create_task(
            client.post(REPAIR_PATH, json={"confirm": True}, headers=harness.headers)
        )
        await runtime.repair_started.wait()
        assert runtime.repair_snapshots == [
            {
                "sidecar_running": True,
                "worker_available": True,
                "claims_paused": False,
                "sidecar_stop_calls": 0,
            }
        ]

        during = await client.get("/api/memory/status", headers=harness.headers)
        assert during.status_code == 200
        assert during.json()["source"]["status"] == "available"
        assert during.json()["health"]["recorder"]["state"] == "active"
        assert runtime.sidecar_stop_calls == 0

        runtime.release_repair.set()
        completed = await pending
        assert_confirmed_repair_post(completed)
        assert completed.status_code == 200
        assert completed.json()["result"] == "completed"

        follow_up = await client.get("/api/memory/status", headers=harness.headers)
        assert follow_up.status_code == 200
        assert follow_up.json()["source"]["status"] == "available"
        assert runtime.sidecar_running is True
        assert runtime.worker_available is True
        assert runtime.claims_paused is False
        assert runtime.sidecar_stop_calls == 0
