from __future__ import annotations

from core.memory.processing_record import (
    AnomalyProjection,
    MaintenanceProjection,
    ProcessingRecordSummary,
    ProcessingSourceObservations,
    RuntimeHealthProjection,
    SourceObservation,
)


def test_processing_summary_has_no_recovery_stage_fields() -> None:
    available = SourceObservation("available", observed_at="now")
    summary = ProcessingRecordSummary(
        runtime=RuntimeHealthProjection(source=available, health=None),
        sources=ProcessingSourceObservations(available, available, available),
        anomalies=AnomalyProjection(source=available, items=()),
        maintenance=MaintenanceProjection(
            source=available,
            data_exists=False,
            can_delete_data=True,
        ),
    )

    assert summary.maintenance.can_delete_data is True
    assert not hasattr(summary.maintenance, "clear_in_progress")
    assert not hasattr(summary, "provider_checks")
