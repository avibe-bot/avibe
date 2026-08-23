from __future__ import annotations

from core.memory.maintenance import ClearInProgressResult
from core.memory.processing_record import (
    AnomalyProjection,
    MaintenanceProjection,
    ProcessingRecordSummary,
    ProcessingSourceObservations,
    RuntimeHealthProjection,
    SourceObservation,
)


def test_processing_projection_exposes_clear_in_progress():
    clear = ClearInProgressResult(
        state="failed",
        operation_id="op-1",
        occurred_at="2026-08-13T00:00:00Z",
        error_code="memory_clear_marker_unreadable",
    )
    projection = MaintenanceProjection(
        source=SourceObservation("unavailable", reason="memory_clear_failed"),
        data_exists=True,
        can_clear=False,
        clear_in_progress=clear,
    )

    assert projection.clear_in_progress == clear


def test_processing_summary_has_no_legacy_recovery_field():
    available = SourceObservation("available", observed_at="now")
    summary = ProcessingRecordSummary(
        runtime=RuntimeHealthProjection(source=available, health=None),
        sources=ProcessingSourceObservations(available, available, available),
        anomalies=AnomalyProjection(source=available, items=()),
        maintenance=MaintenanceProjection(
            source=available,
            data_exists=False,
            can_clear=True,
            clear_in_progress=None,
        ),
    )

    assert not hasattr(summary.maintenance, "clear_in_progress") or summary.maintenance.clear_in_progress is None
    assert not hasattr(summary, "provider_checks")
