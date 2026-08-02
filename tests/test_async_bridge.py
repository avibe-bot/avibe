import pytest

from vibe.async_bridge import run_coroutine_blocking


async def _value():
    return 42


def test_run_coroutine_blocking_allows_sync_callers():
    assert run_coroutine_blocking(_value()) == 42


async def test_run_coroutine_blocking_rejects_active_event_loop():
    with pytest.raises(RuntimeError, match="active event loop"):
        run_coroutine_blocking(_value())
