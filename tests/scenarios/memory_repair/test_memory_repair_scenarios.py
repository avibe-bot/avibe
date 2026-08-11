"""Hermetic UI/service-boundary evidence for the Memory Repair capability.

The backend route and ownership tests belong to the backend lane. These checks
keep the frontend pinned to the public contract while remaining safe to run
without a live sidecar, provider, artifact, or user state.
"""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
UI_ROOT = ROOT / "ui"


def _run_vitest(path: str, name: str) -> None:
    result = subprocess.run(
        ["npm", "run", "test", "--", "--run", path, "-t", name],
        cwd=UI_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"Vitest failed:\n{result.stdout}\n{result.stderr}"


def test_memory_repair_001_uses_exact_post_contract() -> None:
    """Scenario: MEMORY-REPAIR-001"""
    _run_vitest("src/lib/memoryRepair.test.ts", "posts the exact confirmed Repair contract")


def test_memory_repair_002_renders_warning_health() -> None:
    """Scenario: MEMORY-REPAIR-002"""
    _run_vitest(
        "src/components/settings/SettingsMemoryPage.test.tsx",
        "renders an unhealthy Repair completion as warnings",
    )


def test_memory_repair_003_has_running_guard() -> None:
    """Scenario: MEMORY-REPAIR-003"""
    _run_vitest(
        "src/components/settings/SettingsMemoryPage.test.tsx",
        "prevents a second Repair request while the first is pending",
    )


def test_memory_repair_005_fails_closed_on_capability() -> None:
    """Scenario: MEMORY-REPAIR-005"""
    _run_vitest(
        "src/components/settings/memory/MemoryStatusPanel.test.tsx",
        "only exposes Repair index when the backend capability is explicitly available",
    )
