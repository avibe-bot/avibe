"""Execution-boundary authorization for Agent-initiated Memory CLI reads."""

from __future__ import annotations

from core.memory.cli_access import CAPABILITY_TTL_SECONDS, MemoryCliAccessRegistry


def test_memory_cli_capability_is_bound_to_one_session_and_revocable() -> None:
    registry = MemoryCliAccessRegistry()
    capability = registry.grant("ses-admin", "u-11111111111111111111111111111111")

    assert registry.grant("ses-admin", "u-11111111111111111111111111111111") == capability
    assert registry.validate("ses-admin", capability) == "u-11111111111111111111111111111111"
    assert registry.validate("ses-other", capability) is None
    assert registry.validate("ses-admin", "forged") is None

    rotated = registry.grant("ses-admin", "u-22222222222222222222222222222222")
    assert rotated != capability
    assert registry.validate("ses-admin", capability) is None
    assert registry.validate("ses-admin", rotated) == "u-22222222222222222222222222222222"

    registry.revoke("ses-admin")

    assert registry.validate("ses-admin", rotated) is None


def test_memory_cli_capability_expires_without_a_further_admitted_turn() -> None:
    now = [1000.0]
    registry = MemoryCliAccessRegistry(ttl_seconds=60.0, clock=lambda: now[0])
    principal_id = "u-11111111111111111111111111111111"
    capability = registry.grant("ses-agent", principal_id)

    # Use inside the window works and never extends the window.
    now[0] += 59.0
    assert registry.validate("ses-agent", capability) == principal_id
    now[0] += 1.0
    assert registry.validate("ses-agent", capability) is None
    # The lapsed grant is evicted, not merely rejected.
    assert registry._tokens == {}

    # A background process holding a stale capability cannot revive it, but the
    # next admitted turn for the same session refreshes the same token.
    refreshed = registry.grant("ses-agent", principal_id)
    now[0] += 30.0
    assert registry.grant("ses-agent", principal_id) == refreshed
    now[0] += 45.0
    assert registry.validate("ses-agent", refreshed) == principal_id

    # Unrelated lapsed sessions do not accumulate.
    now[0] += 1000.0
    registry.grant("ses-other", principal_id)
    assert list(registry._tokens) == ["ses-other"]


def test_memory_cli_capability_ttl_default_is_bounded() -> None:
    assert 0 < CAPABILITY_TTL_SECONDS <= 12 * 60 * 60
