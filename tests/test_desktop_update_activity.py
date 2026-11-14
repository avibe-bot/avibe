"""Native auto-update uses the controller's live owners, not window idleness."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from core import internal_server, update_activity
from core.update_activity import UpdateActivity
from modules.agents.service import AgentService
from modules.agents.runtime_generations import RuntimeGenerationSet
from modules.agents.opencode.agent import OpenCodeAgent
from vibe import internal_client, ui_server


@pytest.fixture
def activity(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(update_activity, "monotonic", lambda: clock[0])
    controller = SimpleNamespace(session_turns=SimpleNamespace(in_flight={}, active_turn_sinks={}))
    service = AgentService(controller)
    service.agents = {name: SimpleNamespace(name=name) for name in ("vibey", "claude", "codex", "opencode")}
    controller.agent_service = service
    monitor = UpdateActivity(controller)
    monitor.start()
    yield monitor, controller, clock
    monitor.stop()


def test_clock_starts_cold_and_a_short_turn_between_polls_resets_it(activity):
    monitor, _, clock = activity
    assert asyncio.run(monitor.idle_seconds()) == 0
    clock[0] = 1801
    assert asyncio.run(monitor.idle_seconds()) == 1801
    # Both boundaries happen between native polls. There is no active turn left
    # to observe, but a new idle window must begin when that turn finishes.
    update_activity.bus.publish("turn.start", {"session_id": "web-or-im"})
    clock[0] += 10
    update_activity.bus.publish("turn.end", {"session_id": "web-or-im"})
    clock[0] += 15
    assert asyncio.run(monitor.idle_seconds()) == 15
    monitor.stop()
    clock[0] += 3600
    assert asyncio.run(monitor.idle_seconds()) == 0
    monitor.start()
    assert asyncio.run(monitor.idle_seconds()) == 0


@pytest.mark.parametrize("registry", ["in_flight", "active_turn_sinks"])
def test_any_surface_turn_blocks_install_even_after_a_long_quiet_period(activity, registry):
    monitor, controller, clock = activity
    clock[0] = 7200
    getattr(controller.session_turns, registry)["session"] = object()
    assert asyncio.run(monitor.idle_seconds()) == 0
    getattr(controller.session_turns, registry).clear()
    clock[0] += 60
    assert asyncio.run(monitor.idle_seconds()) == 60


@pytest.mark.parametrize("backend", ["vibey", "claude", "codex", "opencode"])
def test_backend_owned_work_and_tokens_are_not_window_activity(activity, backend):
    monitor, controller, clock = activity
    clock[0] = 7200
    controller.agent_service.runtime_turn_tokens_for_backend = lambda name: {"turn": "token"} if name == backend else {}
    assert asyncio.run(monitor.idle_seconds()) == 0
    controller.agent_service.runtime_turn_tokens_for_backend = lambda _name: {}
    clock[0] += 7200
    controller.agent_service.backend_runtime_active = AsyncMock(side_effect=lambda name: name == backend)
    assert asyncio.run(monitor.idle_seconds()) == 0


@pytest.mark.parametrize("change", ["activity", "turn", "backend", "stop", "failure"])
def test_activity_while_awaiting_the_backend_probe_invalidates_idle(activity, change):
    monitor, controller, clock = activity
    clock[0] = 7200

    async def probe(_backend):
        if change == "activity":
            update_activity.bus.publish("message.new", {})
        elif change == "turn":
            controller.session_turns.in_flight["new"] = object()
        elif change == "backend":
            controller.agent_service.agents["new"] = object()
        elif change == "stop":
            monitor.stop()
        else:
            raise RuntimeError("ownership unavailable")
        return False

    controller.agent_service.backend_runtime_active = probe
    assert asyncio.run(monitor.idle_seconds()) == 0


def test_missing_runtime_owners_fail_closed(activity):
    monitor, controller, clock = activity
    clock[0] = 7200
    del controller.agent_service
    assert asyncio.run(monitor.idle_seconds()) == 0


@pytest.mark.parametrize("outcome", ["completed", "failed", "stopped", "killed", "disconnected"])
def test_detached_activity_completion_between_polls_starts_a_new_idle_window(activity, outcome):
    monitor, controller, clock = activity
    registry = controller.agent_service.activities
    registry.start(backend="claude", runtime_key="runtime", session_id="session", activity_id="task", kind="background_task")
    clock[0] = 1
    assert asyncio.run(monitor.idle_seconds()) == 0
    clock[0] = 1805
    registry.complete(backend="claude", runtime_key="runtime", activity_id="task", status=outcome)
    clock[0] = 1806
    assert asyncio.run(monitor.idle_seconds()) == 1


def test_unregistered_backend_work_and_failed_teardown_still_block_automatic_install(activity):
    monitor, controller, clock = activity
    service = controller.agent_service
    registry = service.activities
    registry.start(backend="opencode", runtime_key="runtime", session_id="session", activity_id="task", kind="background_task")
    service.agents.pop("opencode")
    clock[0] = 1900
    assert asyncio.run(monitor.idle_seconds()) == 0
    registry.complete(backend="opencode", runtime_key="runtime", activity_id="task", status="killed")

    async def run():
        async def fail():
            assert service.has_retained_runtime_work()
            raise RuntimeError("teardown did not finish")
        assert not await service.run_until_done("disabled:opencode", fail)
        clock[0] += 7200
        assert await monitor.idle_seconds() == 0
        async def finish():
            assert service.has_retained_runtime_work()
            assert await monitor.idle_seconds() == 0
        assert await service.run_until_done("disabled:opencode", finish)
        clock[0] += 1
        assert await monitor.idle_seconds() == 1
    asyncio.run(run())


def test_unregistered_runtime_turn_token_remains_busy(activity):
    monitor, controller, clock = activity
    controller.agent_service._turn_gates["gone"] = SimpleNamespace(token="live", lock=asyncio.Lock())
    controller.agent_service.agents.clear()
    clock[0] = 1900
    assert asyncio.run(monitor.idle_seconds()) == 0


def test_real_generation_binding_release_starts_a_new_idle_window(activity):
    monitor, _, clock = activity
    async def run():
        generations = RuntimeGenerationSet(start=AsyncMock(return_value=object()), stop=AsyncMock(return_value=True))
        clock[0] = 1800
        binding = await generations.acquire(SimpleNamespace(digest="test"))
        assert await monitor.idle_seconds() == 0
        clock[0] = 3600
        await binding.release()
        clock[0] = 3601
        assert await monitor.idle_seconds() == 1
        await generations.stop_all(force=True)
    asyncio.run(run())


def test_outside_turn_admission_on_an_already_probed_backend_invalidates_the_result(activity):
    monitor, controller, clock = activity
    agent = SimpleNamespace(name="opencode", controller=controller, _runtime=SimpleNamespace(outside_turn_acquisitions=0))
    controller.agent_service.agents = {"opencode": SimpleNamespace(name="opencode"), "codex": SimpleNamespace(name="codex")}
    clock[0] = 7200
    async def run():
        admission = OpenCodeAgent._outside_turn_admission(agent)
        async def probe(name):
            if name == "codex":
                # OpenCode was already probed; its new acquisition has no
                # binding, Activity, turn or message yet.
                await admission.__aenter__()
            return False
        controller.agent_service.backend_runtime_active = probe
        assert await monitor.idle_seconds() == 0
        assert agent._runtime.outside_turn_acquisitions == 1
        clock[0] += 1800
        await admission.__aexit__(RuntimeError, RuntimeError("launch failed"), None)
        controller.agent_service.backend_runtime_active = AsyncMock(return_value=False)
        clock[0] += 1
        assert await monitor.idle_seconds() == 1
    asyncio.run(run())


def test_controller_idle_route_calls_the_live_owner_each_time(activity):
    monitor, controller, clock = activity
    controller.update_checker = SimpleNamespace(desktop_idle_seconds=monitor.idle_seconds)
    client = TestClient(internal_server.create_app(controller))
    clock[0] = 1800
    response = client.get("/internal/desktop/update/idle")
    assert response.status_code == 200
    assert response.json() == {"idle_seconds": 1800}
    assert response.headers["Cache-Control"] == "no-store"
    # Simulate work arriving during payload download, ahead of the second probe.
    controller.session_turns.in_flight["new"] = object()
    assert client.get("/internal/desktop/update/idle").json() == {"idle_seconds": 0}


@pytest.mark.parametrize("status,payload", [(404, {}), (503, {}), (200, {}), (200, {"idle_seconds": True}), (200, {"idle_seconds": -1})])
def test_old_unreachable_or_malformed_controller_never_means_idle(monkeypatch, status, payload):
    endpoint = SimpleNamespace(base_url="http://controller", headers={}, descriptor=None)
    monkeypatch.setattr(internal_client, "_resolve_endpoint_async", AsyncMock(return_value=endpoint))
    monkeypatch.setattr(internal_client, "_async_transport", lambda _endpoint: httpx.MockTransport(
        lambda request: httpx.Response(status, json=payload)
    ))
    with pytest.raises(internal_client.InternalServerUnavailable):
        asyncio.run(internal_client.desktop_update_idle())


def test_ui_idle_is_a_fresh_controller_proxy_not_ui_process_state(monkeypatch):
    probe = AsyncMock(side_effect=[{"idle_seconds": 1800}, {"idle_seconds": 0}])
    monkeypatch.setattr(internal_client, "desktop_update_idle", probe)
    client = ui_server.app.test_client()
    for seconds in (1800, 0):
        response = client.get("/api/desktop/update/idle", base_url="http://127.0.0.1:5123")
        assert response.status_code == 200
        assert response.get_json() == {"idle_seconds": seconds}
        assert response.headers["Cache-Control"] == "no-store"
    assert probe.await_count == 2


@pytest.mark.parametrize("failure", [internal_client.InternalServerUnavailable, internal_client.InternalServerTimeout])
def test_ui_idle_controller_failure_is_closed(monkeypatch, failure):
    monkeypatch.setattr(internal_client, "desktop_update_idle", AsyncMock(side_effect=failure("unavailable")))
    response = ui_server.app.test_client().get("/api/desktop/update/idle", base_url="http://127.0.0.1:5123")
    assert response.status_code == 503
    assert response.get_json() == {"idle_seconds": 0}
