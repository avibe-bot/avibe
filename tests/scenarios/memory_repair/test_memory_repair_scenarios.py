"""Hermetic UI/service-boundary evidence for the Memory Repair capability.

The backend route and ownership tests belong to the backend lane. These checks
keep the frontend pinned to the public contract while remaining safe to run
without a live sidecar, provider, artifact, or user state.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
API_CONTEXT = (ROOT / "ui/src/context/ApiContext.tsx").read_text(encoding="utf-8")
MEMORY_PAGE = (ROOT / "ui/src/components/settings/SettingsMemoryPage.tsx").read_text(encoding="utf-8")


def test_memory_repair_001_uses_exact_post_contract() -> None:
    """Scenario: MEMORY-REPAIR-001"""
    assert "repairMemoryIndex: () =>" in API_CONTEXT
    assert "postJson('/api/memory/runtime/repair', { confirm: true }, { handleError: false })" in API_CONTEXT
    assert "result: 'completed' | 'completed_with_warnings'" in API_CONTEXT
    assert "health: MemoryCascadeHealth" in API_CONTEXT


def test_memory_repair_004_has_no_automatic_retry_or_polling() -> None:
    """Scenario: MEMORY-REPAIR-004"""
    repair_start = MEMORY_PAGE.index("const repairIndex = async () =>")
    repair_end = MEMORY_PAGE.index("  const tabs = useMemo", repair_start)
    repair_block = MEMORY_PAGE[repair_start:repair_end]
    assert "setRepairBusy(true)" in repair_block
    assert "setTimeout" not in repair_block
    assert "setInterval" not in repair_block
    assert "repairMemoryIndex()" in repair_block
    assert "repairIndex()" not in repair_block.replace("repairMemoryIndex()", "")


def test_memory_repair_002_renders_warning_health() -> None:
    """Scenario: MEMORY-REPAIR-002"""
    panel = (ROOT / "ui/src/components/settings/memory/MemoryStatusPanel.tsx").read_text(encoding="utf-8")
    assert "completedWithWarnings" in panel
    assert "repairHealth.healthy" in panel


def test_memory_repair_003_has_running_guard() -> None:
    """Scenario: MEMORY-REPAIR-003"""
    panel = (ROOT / "ui/src/components/settings/memory/MemoryStatusPanel.tsx").read_text(encoding="utf-8")
    assert "disabled={repairBusy}" in panel
    assert "repairBusy ?" in panel


def test_memory_repair_005_fails_closed_on_capability() -> None:
    """Scenario: MEMORY-REPAIR-005"""
    assert "settings.repair_available === true" in MEMORY_PAGE
    panel = (ROOT / "ui/src/components/settings/memory/MemoryStatusPanel.tsx").read_text(encoding="utf-8")
    assert "{repairSupported ? (" in panel


def test_memory_repair_006_does_not_stop_sidecar() -> None:
    """Scenario: MEMORY-REPAIR-006"""
    repair_start = MEMORY_PAGE.index("const repairIndex = async () =>")
    repair_end = MEMORY_PAGE.index("  const tabs = useMemo", repair_start)
    repair_block = MEMORY_PAGE[repair_start:repair_end]
    assert "restartMemoryRuntime" not in repair_block
    assert "stopMemory" not in repair_block
