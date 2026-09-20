"""Focused tests for the native Processing Record adapter."""

from pathlib import Path

import pytest

from avibe_memory.everos_insight.reader import MemoryInsightPaths, MemoryInsightReader


def test_insight_reader_exposes_only_native_processing_record_operations(
    tmp_path: Path,
) -> None:
    reader = MemoryInsightReader(MemoryInsightPaths(tmp_path / "everos"))

    observation = reader.source_observation()

    assert observation.memcells.status == "unavailable"
    assert observation.runs.status == "unavailable"
    assert observation.semantic.status == "unavailable"
    assert not hasattr(reader, "list_entries")
    assert not hasattr(reader, "list_unlinked_calls")
    assert not hasattr(reader, "installation_preflight_calls")


@pytest.mark.parametrize("field", ["provider_base_urls", "exact_redaction_values"])
def test_insight_reader_rejects_scalar_secret_inputs(
    tmp_path: Path,
    field: str,
) -> None:
    with pytest.raises(TypeError, match=f"{field} must be a sequence of strings"):
        MemoryInsightReader(
            MemoryInsightPaths(tmp_path / "everos"),
            **{field: "secret"},
        )
