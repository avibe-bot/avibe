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
