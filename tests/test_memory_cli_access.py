"""Execution-boundary authorization for Agent-initiated Memory CLI reads."""

from __future__ import annotations

from core.memory.cli_access import MemoryCliAccessRegistry


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
