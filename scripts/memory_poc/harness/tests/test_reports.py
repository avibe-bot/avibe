from __future__ import annotations

from pathlib import Path

import pytest

from memory_poc.environment import ProviderSettings
from memory_poc.errors import ReportValidationError
from memory_poc.metrics import CallMetrics
from memory_poc.readiness import SearchReadiness
from memory_poc.reports import build_report, load_report, validate_report, write_report, write_stage2_summary, write_summary


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
    write_report(
        tmp_path / "report.json",
        report,
        fixture_texts=(),
        secret_values=("not-a-real-key",),
    )

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


def test_report_and_summary_redact_model_metadata_containing_an_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("memory_poc.reports.lock_id", lambda: "lock")
    settings = _settings(tmp_path)
    secret = "test-api-key-0123456789"
    settings = ProviderSettings(
        llm_base_url=settings.llm_base_url,
        llm_model=f"alias-{secret}",
        llm_api_key=secret,
        embedding_base_url=settings.embedding_base_url,
        embedding_model=settings.embedding_model,
        embedding_api_key=settings.embedding_api_key,
        source=settings.source,
    )
    report = build_report(run_id="r1", settings=settings)

    assert report["environment"]["llm_model"] == "configured-model-redacted"
    write_report(
        tmp_path / "report.json",
        report,
        fixture_texts=(),
        secret_values=(settings.llm_api_key, settings.embedding_api_key),
    )
    write_summary(
        tmp_path / "summary.md",
        settings=settings,
        metrics=CallMetrics(),
        message_count=1,
        http_shapes=(),
        readiness=SearchReadiness(profile_ms=None, episode_ms=1, atomic_fact_ms=1, timeout_ms=600000),
    )

    report_text = (tmp_path / "report.json").read_text(encoding="utf-8")
    summary_text = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert secret not in report_text
    assert secret not in summary_text
    assert "configured-model-redacted" in summary_text

    report["environment"]["llm_model"] = f"alias-{secret}"
    with pytest.raises(ReportValidationError, match="report_contains_secret"):
        validate_report(report, fixture_texts=(), secret_values=(secret, settings.embedding_api_key))


def test_short_key_does_not_block_report_or_summary_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memory_poc.reports.lock_id", lambda: "lock")
    settings = ProviderSettings(
        llm_base_url="https://example.invalid/v1",
        llm_model="qwen",
        llm_api_key="1",
        embedding_base_url="https://example.invalid/v1",
        embedding_model="embedding",
        embedding_api_key="2",
        source=tmp_path / ".env.poc",
    )
    report = build_report(run_id="r1", settings=settings)

    write_report(
        tmp_path / "report.json",
        report,
        fixture_texts=(),
        secret_values=(settings.llm_api_key, settings.embedding_api_key),
    )
    write_summary(
        tmp_path / "summary.md",
        settings=settings,
        metrics=CallMetrics(),
        message_count=1,
        http_shapes=(),
        readiness=SearchReadiness(profile_ms=None, episode_ms=1, atomic_fact_ms=1, timeout_ms=600000),
    )

    report_text = (tmp_path / "report.json").read_text(encoding="utf-8")
    summary_text = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert report["environment"]["llm_model"] == "qwen"
    assert "stage1-mini" in report_text
    assert "EverOS POC Stage 1 Sanity" in summary_text
    assert "qwen" in summary_text


def test_stage2_summary_keeps_evidence_redacted_and_uses_the_final_recommendation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("memory_poc.reports.lock_id", lambda: "lock")
    settings = _settings(tmp_path)
    report = build_report(run_id="r1", settings=settings, corpus_revision="2026-07-22.2")
    report["recommendation"] = "fork"

    write_stage2_summary(
        tmp_path / "summary.md",
        settings=settings,
        report=report,
        completed_stages=("quality", "pool"),
        evidence_lines=("quality trials 3 positive top8 rates 0.91 0.94 0.97", "egress hosts dashscope.aliyuncs.com"),
        fixture_texts=("synthetic fixture body",),
        anchor=tmp_path,
    )

    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "EverOS POC Stage 2" in summary
    assert "Recommendation - fork" in summary
    assert "dashscope.aliyuncs.com" in summary
    assert "synthetic fixture body" not in summary


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
        readiness=SearchReadiness(profile_ms=5000, episode_ms=None, atomic_fact_ms=15000, timeout_ms=600000),
    )

    summary = path.read_text(encoding="utf-8")
    assert "Profile content via search: first observed 5000 ms after flush completion." in summary
    assert "Episode content via search: not observed within 600000 ms after flush completion." in summary
    assert "Atomic fact via search: first observed 15000 ms after flush completion." in summary
    assert "Max observed cascade lag via search: 15000 ms." in summary


def test_summary_marks_search_readiness_unmeasured_before_flush(tmp_path: Path) -> None:
    path = tmp_path / "summary.md"

    write_summary(
        path,
        settings=_settings(tmp_path),
        metrics=CallMetrics(),
        message_count=1,
        http_shapes=(),
        readiness=SearchReadiness.not_measured(timeout_ms=600000),
    )

    summary = path.read_text(encoding="utf-8")
    assert "Search readiness timing: not measured because flush did not complete." in summary
    assert "Profile content via search: not measured." in summary
    assert "Max observed cascade lag via search: not measured." in summary


def test_summary_allows_a_slash_in_the_configured_model_name(tmp_path: Path) -> None:
    path = tmp_path / "summary.md"
    settings = _settings(tmp_path)
    settings = ProviderSettings(
        llm_base_url=settings.llm_base_url,
        llm_model="provider/model",
        llm_api_key=settings.llm_api_key,
        embedding_base_url=settings.embedding_base_url,
        embedding_model=settings.embedding_model,
        embedding_api_key=settings.embedding_api_key,
        source=settings.source,
    )

    write_summary(
        path,
        settings=settings,
        metrics=CallMetrics(),
        message_count=1,
        http_shapes=(),
        readiness=SearchReadiness(profile_ms=None, episode_ms=1, atomic_fact_ms=2, timeout_ms=600000),
    )

    summary = path.read_text(encoding="utf-8")
    assert "profile not published/readable via public search in 1.1.3 + provider/model;" in summary


def test_load_report_rejects_stale_secret_in_any_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reviewer r4 blocking: a report persisted by an older head (before the
    write-time whole-text scan) that contains the configured 35-char key in a
    non-model field must be rejected on load, not printed by `memory_poc report`."""
    import json

    monkeypatch.setattr("memory_poc.reports.lock_id", lambda: "lock")
    secret = "sk-" + "a" * 32  # 35-char key, above the 16-char match floor
    settings = ProviderSettings(
        llm_base_url="https://example.invalid/v1",
        llm_model="qwen3.7-plus",
        llm_api_key=secret,
        embedding_base_url="https://example.invalid/v1",
        embedding_model="qwen3.7-text-embedding",
        embedding_api_key=secret,
        source=tmp_path / ".env.poc",
    )
    # Simulate a stale report written without the whole-text scan: build a clean
    # report, then inject the key into duplicates.observed and write it raw.
    report = build_report(run_id="stale-run", settings=settings)
    report["duplicates"]["observed"] = "echoed " + secret
    (tmp_path / "report.json").write_text(
        json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8"
    )
    with pytest.raises(ReportValidationError):
        load_report(
            tmp_path / "report.json",
            fixture_texts=(),
            secret_values=(settings.llm_api_key, settings.embedding_api_key),
        )
