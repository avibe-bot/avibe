"""Focused diagnostics tests for independent best-effort sources."""

from pathlib import Path

import pytest

from core.memory.everos_insight.reader import MemoryInsightPaths, MemoryInsightReader


def _reader(tmp_path: Path) -> MemoryInsightReader:
    return MemoryInsightReader(
        MemoryInsightPaths(
            everos_root=tmp_path / "everos",
            capture_db_path=tmp_path / "memory.sqlite",
            call_log_db_path=tmp_path / "calls.sqlite",
        )
    )


def test_capture_diagnostics_are_unavailable_without_delivery_history(tmp_path: Path) -> None:
    reader = _reader(tmp_path)
    result = reader.list_entries(("u-" + "a" * 32, "default"), None, 10)
    assert result["entries"] == []
    assert result["sections"]["capture"]["status"] == "unavailable"


def test_scoped_diagnostics_validate_identity_before_reading(tmp_path: Path) -> None:
    reader = _reader(tmp_path)
    with pytest.raises(ValueError):
        reader.list_unlinked_calls(("not-a-principal", "default"), 1)


def test_processing_source_observation_keeps_calls_independent(tmp_path: Path) -> None:
    observation = _reader(tmp_path).source_observation()
    assert observation.capture.status == "unavailable"
    assert observation.calls.status == "unavailable"
