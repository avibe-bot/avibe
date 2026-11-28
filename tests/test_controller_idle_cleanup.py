from types import SimpleNamespace

import pytest

from config.v2_config import DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS
from core.controller import Controller


def test_idle_cleanup_timeouts_disable_codex_when_backend_config_absent() -> None:
    controller = object.__new__(Controller)
    controller.config = SimpleNamespace(
        claude=SimpleNamespace(idle_timeout_seconds=0),
        codex=None,
    )

    claude_timeout, codex_timeout = Controller._get_idle_cleanup_timeouts(controller)

    assert claude_timeout == 0
    assert codex_timeout == 0


def test_idle_cleanup_timeouts_preserve_explicit_codex_timeout() -> None:
    controller = object.__new__(Controller)
    controller.config = SimpleNamespace(
        claude=SimpleNamespace(idle_timeout_seconds=300),
        codex=SimpleNamespace(idle_timeout_seconds=900),
    )

    claude_timeout, codex_timeout = Controller._get_idle_cleanup_timeouts(controller)

    assert claude_timeout == 300
    assert codex_timeout == 900


def test_idle_cleanup_timeouts_fall_back_to_shared_default_when_backend_config_omits_value() -> None:
    controller = object.__new__(Controller)
    controller.config = SimpleNamespace(
        claude=SimpleNamespace(),
        codex=SimpleNamespace(),
    )

    claude_timeout, codex_timeout = Controller._get_idle_cleanup_timeouts(controller)

    assert claude_timeout == DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS
    assert codex_timeout == DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS


def test_a_saved_idle_timeout_applies_without_a_backend_restart(monkeypatch) -> None:
    """RUNTIME-GEN-005: the idle sweep reads timeouts live and always sweeps generations.

    Saving an idle timeout renews the backend in place rather than restarting
    it, so the sweep must not keep the value, or an interval derived from the
    value, it started with.
    """
    import asyncio
    from unittest.mock import AsyncMock

    controller = object.__new__(Controller)
    controller.config = SimpleNamespace(claude=SimpleNamespace(idle_timeout_seconds=86400), codex=None)
    controller.session_handler = SimpleNamespace(
        evict_idle_sessions=AsyncMock(),
        reap_orphaned_claude_sessions=AsyncMock(),
    )
    reap = AsyncMock()
    controller.agent_service = SimpleNamespace(agents={"codex": SimpleNamespace(name="codex", reap_runtime_generations=reap)})
    sweeps = []

    async def sleep(_seconds):
        sweeps.append(_seconds)
        if len(sweeps) == 2:
            controller.config.claude.idle_timeout_seconds = 600
        if len(sweeps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr("core.controller.asyncio.sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Controller.periodic_cleanup(controller))

    assert [call.args for call in controller.session_handler.evict_idle_sessions.await_args_list] == [
        (86400,),
        (600,),
    ]
    assert reap.await_count == 2
    # A one-day timeout must not stretch the interval before the new value is read.
    assert sweeps == [60, 60, 60]


def test_periodic_cleanup_evicts_idle_transports_dynamically_for_all_registered_agents(monkeypatch) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    controller = object.__new__(Controller)
    controller.config = SimpleNamespace(
        claude=SimpleNamespace(idle_timeout_seconds=0),
        codex=SimpleNamespace(idle_timeout_seconds=300),
        antigravity=SimpleNamespace(idle_timeout_seconds=450),
        future_backend=SimpleNamespace(idle_timeout_seconds=600),
    )
    controller.session_handler = SimpleNamespace(
        evict_idle_sessions=AsyncMock(),
        reap_orphaned_claude_sessions=AsyncMock(),
    )
    codex_evict = AsyncMock()
    antigravity_evict = AsyncMock()
    future_evict = AsyncMock()
    unconfigured_evict = AsyncMock()
    controller.agent_service = SimpleNamespace(
        retry_pending=AsyncMock(),
        agents={
            "codex": SimpleNamespace(name="codex", evict_idle_transports=codex_evict),
            "antigravity": SimpleNamespace(name="antigravity", evict_idle_transports=antigravity_evict),
            "future_backend": SimpleNamespace(name="future_backend", evict_idle_transports=future_evict),
            "unconfigured_backend": SimpleNamespace(name="unconfigured_backend", evict_idle_transports=unconfigured_evict),
            "opencode": SimpleNamespace(name="opencode"),
        },
    )

    ticks = 0

    async def sleep(_seconds):
        nonlocal ticks
        ticks += 1
        if ticks == 2:
            controller.config.codex.idle_timeout_seconds = 0
            controller.config.antigravity.idle_timeout_seconds = 120
            controller.config.future_backend.idle_timeout_seconds = 900
        if ticks == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr("core.controller.asyncio.sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Controller.periodic_cleanup(controller))

    assert [call.args for call in codex_evict.await_args_list] == [(300,)]
    assert [call.args for call in antigravity_evict.await_args_list] == [(450,), (120,)]
    assert [call.args for call in future_evict.await_args_list] == [(600,), (900,)]
    assert unconfigured_evict.await_count == 0


def test_periodic_cleanup_reclaims_superseded_antigravity_epoch_once_when_idle_timeout_zero(monkeypatch) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from modules.agents.antigravity.agent import AntigravityAgent

    antigravity_agent = object.__new__(AntigravityAgent)
    antigravity_agent.evict_idle_transports = AsyncMock(return_value=0)

    controller = object.__new__(Controller)
    controller.config = SimpleNamespace(
        claude=SimpleNamespace(idle_timeout_seconds=0),
        antigravity=SimpleNamespace(idle_timeout_seconds=0),
    )
    controller.session_handler = SimpleNamespace(
        evict_idle_sessions=AsyncMock(),
        reap_orphaned_claude_sessions=AsyncMock(),
    )
    controller.agent_service = SimpleNamespace(
        retry_pending=AsyncMock(),
        agents={"antigravity": antigravity_agent},
    )

    ticks = 0

    async def sleep(_seconds):
        nonlocal ticks
        ticks += 1
        if ticks == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr("core.controller.asyncio.sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Controller.periodic_cleanup(controller))

    # Each tick reclaims superseded renewal epochs once via reap_runtime_generations()
    # without calling evict_idle_transports(0) a second time from the idle sweep.
    assert [call.args for call in antigravity_agent.evict_idle_transports.await_args_list] == [
        (0.0,),
        (0.0,),
    ]
