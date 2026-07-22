from __future__ import annotations

from pathlib import Path

import pytest

from memory_poc.environment import ProviderSettings
from memory_poc.errors import ReportValidationError
from memory_poc.reports import build_report, validate_report, write_report


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

    validate_report(report)
    write_report(tmp_path / "report.json", report)

    rendered = (tmp_path / "report.json").read_text(encoding="utf-8")
    assert "https://" not in rendered
    assert "not-a-real-key" not in rendered


def test_report_rejects_url_and_extra_top_level_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memory_poc.reports.lock_id", lambda: "lock")
    report = build_report(run_id="r1", settings=_settings(tmp_path))
    report["unexpected"] = "value"

    with pytest.raises(ReportValidationError, match="report_top_level_schema_invalid"):
        validate_report(report)


def test_report_rejects_nested_uris_and_fixture_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memory_poc.reports.lock_id", lambda: "lock")
    report = build_report(run_id="r1", settings=_settings(tmp_path))
    report["environment"]["llm_model"] = "https://example.invalid"

    with pytest.raises(ReportValidationError, match="report_contains_uri"):
        validate_report(report)

    report["environment"]["llm_model"] = "llm-model"
    report["duplicates"]["observed"] = "synthetic fixture body"
    with pytest.raises(ReportValidationError, match="report_contains_fixture_text"):
        validate_report(report, fixture_texts=("synthetic fixture body",))


def test_report_rejects_unknown_nested_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memory_poc.reports.lock_id", lambda: "lock")
    report = build_report(run_id="r1", settings=_settings(tmp_path))
    report["resources"]["secret"] = 1

    with pytest.raises(ReportValidationError, match="report_resources_schema_invalid"):
        validate_report(report)
