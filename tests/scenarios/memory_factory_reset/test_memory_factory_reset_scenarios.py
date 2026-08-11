"""Executable UI contract scenarios for Memory Factory Reset (#1315).

These hermetic checks intentionally inspect only repository-owned frontend assets. The
server-side route and runtime scenarios remain owned by the backend implementation lane.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PAGE = ROOT / "ui/src/components/settings/SettingsMemoryPage.tsx"
PANEL = ROOT / "ui/src/components/settings/memory/MemorySettingsPanel.tsx"
API = ROOT / "ui/src/context/ApiContext.tsx"


def _catalog(locale: str) -> dict[str, object]:
    return json.loads((ROOT / f"ui/src/i18n/{locale}.json").read_text(encoding="utf-8"))


def test_memory_factory_001_confirmation_contract() -> None:
    """MEMORY-FACTORY-001: confirmation names both roots and retained install state."""
    source = PAGE.read_text(encoding="utf-8")
    assert "holdSeconds={5}" in source
    assert "memory.factoryReset.deletes" in source
    assert "memory.factoryReset.retains" in source
    for locale in ("en", "zh"):
        factory = _catalog(locale)["memory"]["factoryReset"]  # type: ignore[index]
        assert "memory" in " ".join(factory["deletes"])  # type: ignore[index]
        assert "state/memory" in " ".join(factory["deletes"])  # type: ignore[index]
        assert factory["retains"]  # type: ignore[index]


def test_memory_factory_002_per_root_outcome_contract() -> None:
    """MEMORY-FACTORY-002: the page renders independent root deletion outcomes."""
    source = PANEL.read_text(encoding="utf-8")
    assert "roots" in source
    assert "rootOutcome" in source


def test_memory_factory_101_invalid_artifact_repair_contract() -> None:
    """MEMORY-FACTORY-101: invalid artifacts disable reset and expose Dependencies Repair."""
    source = PANEL.read_text(encoding="utf-8")
    assert "!factoryResetArtifactValid" in source
    assert "/admin/settings/dependencies" in source
    assert "memory.factoryReset.artifactRepairRequired" in source


def test_memory_factory_201_retry_contract() -> None:
    """MEMORY-FACTORY-201: public derived marker uses Retry and no polling loop."""
    page = PAGE.read_text(encoding="utf-8")
    panel = PANEL.read_text(encoding="utf-8")
    api = API.read_text(encoding="utf-8")
    assert "factory_reset_required?: boolean" in api
    assert "recovery_intent" not in api
    assert "settings?.factory_reset_required === true" in page
    assert "recovery_intent" not in page
    assert "memory.factoryReset.retry" in panel
    assert "factoryResetMemory" in page
    assert "setInterval" not in page
