"""Execution-boundary authorization for Agent-initiated Memory CLI reads."""

from __future__ import annotations

from core.memory.cli_access import MemoryCliAccessRegistry


def test_memory_cli_capability_is_bound_to_one_session_and_revocable() -> None:
    registry = MemoryCliAccessRegistry()
    capability = registry.grant("ses-admin")

    assert registry.grant("ses-admin") == capability
    assert registry.validate("ses-admin", capability) is True
    assert registry.validate("ses-other", capability) is False
    assert registry.validate("ses-admin", "forged") is False

    registry.revoke("ses-admin")

    assert registry.validate("ses-admin", capability) is False
