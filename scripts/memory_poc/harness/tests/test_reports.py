from __future__ import annotations

from pathlib import Path

import pytest

from memory_poc.environment import ProviderSettings
from memory_poc.errors import ReportValidationError
from memory_poc.metrics import CallMetrics
from memory_poc.reports import build_report, validate_report, write_report, write_summary


def _settings(tmp_path: Path) -> ProviderSettings:
    return ProviderSettings(
        llm_base_url="https://example.invalid/v1",
        llm_model="llm-model",
        llm_api_key="not-a-real-key",
        embedding_base_url="https://example.invalid/v1",
        embedding_model="embedding-model",
        embedding_api_key="not-a-real-key",
        source=tmp_path / ".env.poc",
    )


def test_report_has_frozen_schema_and_excludes_urls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memory_poc.reports.lock_id", lambda: "lock")
    report = build_report(run_id="r1", settings=_settings(tmp_path))
    report["environment"]["endpoint_locality"] = "remote"

    validate_report(report, fixture_texts=())
    write_report(tmp_path / "report.json", report, fixture_texts=())

    rendered = (tmp_path / "report.json").read_text(encoding="utf-8")
    assert "https://" not in rendered
    assert "not-a-real-key" not in rendered


def test_report_rejects_url_and_extra_top_level_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memory_poc.reports.lock_id", lambda: "lock")
    report = build_report(run_id="r1", settings=_settings(tmp_path))
    report["unexpected"] = "value"

    with pytest.raises(ReportValidationError, match="report_top_level_schema_invalid"):
        validate_report(report, fixture_texts=())


def test_report_rejects_nested_uris_and_fixture_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memory_poc.reports.lock_id", lambda: "lock")
    report = build_report(run_id="r1", settings=_settings(tmp_path))
    report["environment"]["llm_model"] = "https://example.invalid"

    with pytest.raises(ReportValidationError, match="report_contains_uri"):
        validate_report(report, fixture_texts=())

    report["environment"]["llm_model"] = "llm-model"
    report["duplicates"]["observed"] = "synthetic fixture body"
    with pytest.raises(ReportValidationError, match="report_contains_fixture_text"):
        validate_report(report, fixture_texts=("synthetic fixture body",))


def test_report_rejects_unknown_nested_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memory_poc.reports.lock_id", lambda: "lock")
    report = build_report(run_id="r1", settings=_settings(tmp_path))
    report["resources"]["secret"] = 1

    with pytest.raises(ReportValidationError, match="report_resources_schema_invalid"):
        validate_report(report, fixture_texts=())


def test_report_requires_null_measurements_for_not_measured_criteria(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memory_poc.reports.lock_id", lambda: "lock")
    report = build_report(run_id="r1", settings=_settings(tmp_path))
    report["criteria"][0]["value"] = 0

    with pytest.raises(ReportValidationError, match="report_criteria_not_measured_invalid"):
        validate_report(report, fixture_texts=())


def test_report_requires_numeric_measurements_for_measured_criteria(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memory_poc.reports.lock_id", lambda: "lock")
    report = build_report(run_id="r1", settings=_settings(tmp_path))
    report["criteria"][0].update({"state": "pass", "value": None, "threshold": 1})

    with pytest.raises(ReportValidationError, match="report_criteria_measurement_invalid"):
        validate_report(report, fixture_texts=())


def test_report_rejects_zero_based_quality_rank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memory_poc.reports.lock_id", lambda: "lock")
    report = build_report(run_id="r1", settings=_settings(tmp_path))
    report["quality"] = [{"query_id": "q1", "pass": False, "rank": 0, "latency_ms": 1}]

    with pytest.raises(ReportValidationError, match="report_quality_rank_invalid"):
        validate_report(report, fixture_texts=())


def test_report_rejects_model_metadata_that_matches_an_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memory_poc.reports.lock_id", lambda: "lock")
    settings = _settings(tmp_path)
    settings = ProviderSettings(
        llm_base_url=settings.llm_base_url,
        llm_model=settings.embedding_api_key,
        llm_api_key=settings.llm_api_key,
        embedding_base_url=settings.embedding_base_url,
        embedding_model=settings.embedding_model,
        embedding_api_key=settings.embedding_api_key,
        source=settings.source,
    )

    with pytest.raises(ReportValidationError, match="report_model_matches_secret"):
        build_report(run_id="r1", settings=settings)


def test_summary_records_a_safe_failure_outcome(tmp_path: Path) -> None:
    path = tmp_path / "summary.md"

    write_summary(
        path,
        settings=_settings(tmp_path),
        metrics=CallMetrics(),
        message_count=1,
        http_shapes=(),
        outcome="sanity_memory_not_ready",
    )

    assert "Run outcome: sanity_memory_not_ready" in path.read_text(encoding="utf-8")
    with pytest.raises(ReportValidationError, match="summary_outcome_invalid"):
        write_summary(
            path,
            settings=_settings(tmp_path),
            metrics=CallMetrics(),
            message_count=1,
            http_shapes=(),
            outcome="unsafe outcome",
        )


def test_summary_records_redacted_search_readiness_timing(tmp_path: Path) -> None:
    path = tmp_path / "summary.md"

    write_summary(
        path,
        settings=_settings(tmp_path),
        metrics=CallMetrics(),
        message_count=1,
        http_shapes=(),
        readiness_timing={
            "profile_ms": 5000,
            "episode_ms": None,
            "atomic_fact_ms": 15000,
            "timeout_ms": 600000,
        },
    )

    summary = path.read_text(encoding="utf-8")
    assert "Profile content via search: first observed 5000 ms after flush completion." in summary
    assert "Episode content via search: not observed within 600000 ms after flush completion." in summary
    assert "Atomic fact via search: first observed 15000 ms after flush completion." in summary
    assert "Max observed cascade lag via search: 15000 ms." in summary
