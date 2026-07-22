from __future__ import annotations

import json
import os
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import Any

from .constants import CRITERIA_IDS
from .environment import ProviderSettings, lock_id
from .errors import HarnessError, ReportValidationError
from .identifiers import validate_run_id
from .paths import read_private_text, workspace_root, write_private_text

_URI_PATTERN = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]{0,31}:(?://)?", re.IGNORECASE)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_OUTCOME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. ()/-]{0,255}$")
_TOP_LEVEL_KEYS = {
    "run_id",
    "harness_commit",
    "corpus_revision",
    "environment",
    "criteria",
    "quality",
    "latency",
    "resources",
    "egress",
    "duplicates",
    "recommendation",
}
_ENVIRONMENT_KEYS = {
    "os",
    "machine_class",
    "python",
    "lock_id",
    "llm_model",
    "embedding_model",
    "endpoint_locality",
    "timezone",
}
_CRITERION_KEYS = {"id", "pass", "value", "threshold"}
_QUALITY_KEYS = {"query_id", "pass", "rank", "latency_ms"}
_LATENCY_KEYS = {"add_ms", "flush_ms", "searchable_ms", "query_ms"}
_RESOURCE_KEYS = {
    "env_size_bytes",
    "idle_rss_p95_bytes",
    "peak_rss_bytes",
    "root_growth_bytes",
    "llm_calls",
    "embedding_calls",
}
_DUPLICATE_KEYS = {"observed", "count"}


def pending_criteria() -> list[dict[str, Any]]:
    return [{"id": criterion_id, "pass": False, "value": None, "threshold": None} for criterion_id in CRITERIA_IDS]


def set_criterion(criteria: list[dict[str, Any]], criterion_id: str, *, passed: bool, value: Any, threshold: Any) -> None:
    for item in criteria:
        if item["id"] == criterion_id:
            item.update({"pass": passed, "value": value, "threshold": threshold})
            return
    raise ReportValidationError("unknown_criterion")


def build_report(*, run_id: str, settings: ProviderSettings) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "harness_commit": _git_commit(),
        "corpus_revision": "stage1-mini",
        "environment": {
            "os": os.uname().sysname,
            "machine_class": os.uname().machine,
            "python": "3.12",
            "lock_id": lock_id(),
            "llm_model": settings.llm_model,
            "embedding_model": settings.embedding_model,
            "endpoint_locality": settings.endpoint_locality(),
            "timezone": local_timezone_name(),
        },
        "criteria": pending_criteria(),
        "quality": [],
        "latency": {"add_ms": {}, "flush_ms": {}, "searchable_ms": {}, "query_ms": {}},
        "resources": {
            "env_size_bytes": 0,
            "idle_rss_p95_bytes": 0,
            "peak_rss_bytes": 0,
            "root_growth_bytes": 0,
            "llm_calls": 0,
            "embedding_calls": 0,
        },
        "egress": [],
        "duplicates": {"observed": "not_run", "count": 0},
        "recommendation": "stop",
    }


def validate_report(report: dict[str, Any], *, fixture_texts: tuple[str, ...] = ()) -> None:
    if not isinstance(report, dict) or set(report) != _TOP_LEVEL_KEYS:
        raise ReportValidationError("report_top_level_schema_invalid")
    try:
        validate_run_id(report.get("run_id"))
    except HarnessError:
        raise ReportValidationError("report_run_id_invalid")

    if not _safe_identifier(report["harness_commit"], allow_unknown=True):
        raise ReportValidationError("report_harness_commit_invalid")
    if not _safe_identifier(report["corpus_revision"]):
        raise ReportValidationError("report_corpus_revision_invalid")
    _validate_environment(report["environment"])

    criteria = report.get("criteria")
    if not isinstance(criteria, list) or len(criteria) != len(CRITERIA_IDS):
        raise ReportValidationError("report_criteria_schema_invalid")
    for expected_id, item in zip(CRITERIA_IDS, criteria, strict=True):
        if not isinstance(item, dict) or set(item) != _CRITERION_KEYS or item.get("id") != expected_id:
            raise ReportValidationError("report_criteria_schema_invalid")
        if type(item.get("pass")) is not bool or not _numeric_or_none(item.get("value")):
            raise ReportValidationError("report_criteria_value_invalid")
        if not _numeric_or_none(item.get("threshold")):
            raise ReportValidationError("report_criteria_threshold_invalid")

    _validate_quality(report["quality"])
    _validate_latency(report["latency"])
    _validate_resources(report["resources"])
    _validate_egress(report["egress"])
    _validate_duplicates(report["duplicates"])
    if report.get("recommendation") not in {"official", "fork", "stop"}:
        raise ReportValidationError("report_recommendation_invalid")

    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if _URI_PATTERN.search(rendered):
        raise ReportValidationError("report_contains_uri")
    for fixture_text in fixture_texts:
        normalized = _normalize_text(fixture_text)
        if normalized and normalized in _normalize_text(rendered):
            raise ReportValidationError("report_contains_fixture_text")


def write_report(
    path: Path,
    report: dict[str, Any],
    *,
    anchor: Path | None = None,
    fixture_texts: tuple[str, ...] = (),
) -> None:
    validate_report(report, fixture_texts=fixture_texts)
    write_private_text(path, json.dumps(report, ensure_ascii=True, indent=2, sort_keys=False) + "\n", anchor=anchor)


def load_report(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(read_private_text(path))
    except (HarnessError, OSError, ValueError) as exc:
        raise ReportValidationError("report_unreadable") from exc
    if not isinstance(payload, dict):
        raise ReportValidationError("report_not_object")
    validate_report(payload)
    return payload


def write_summary(
    path: Path,
    *,
    llm_calls: int,
    embedding_calls: int,
    message_count: int,
    anchor: Path | None = None,
) -> None:
    divisor = max(message_count, 1)
    write_private_text(
        path,
        "\n".join(
            (
                "# EverOS POC Stage 1 Sanity",
                "",
                f"LLM calls per message: {llm_calls / divisor:.2f}",
                f"Embedding calls per message: {embedding_calls / divisor:.2f}",
                "Rough provider cost per message: unavailable without provider-authoritative pricing.",
                "",
            )
        ),
        anchor=anchor,
    )


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "-C", str(workspace_root()), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def local_timezone_name() -> str:
    try:
        from tzlocal import get_localzone_name

        return get_localzone_name()
    except Exception:  # noqa: BLE001
        return "UTC"


def _safe_identifier(value: Any, *, allow_unknown: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    if allow_unknown and value == "unknown":
        return True
    return bool(_SAFE_IDENTIFIER.fullmatch(value))


def _numeric_or_none(value: Any) -> bool:
    return value is None or (type(value) in {int, float} and value >= 0)


def _validate_environment(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _ENVIRONMENT_KEYS:
        raise ReportValidationError("report_environment_schema_invalid")
    if not all(isinstance(item, str) and item for item in value.values()):
        raise ReportValidationError("report_environment_value_invalid")
    if value["endpoint_locality"] not in {"remote", "loopback"}:
        raise ReportValidationError("report_endpoint_locality_invalid")


def _validate_quality(value: Any) -> None:
    if not isinstance(value, list):
        raise ReportValidationError("report_quality_schema_invalid")
    for item in value:
        if not isinstance(item, dict) or set(item) != _QUALITY_KEYS:
            raise ReportValidationError("report_quality_schema_invalid")
        if not _safe_identifier(item.get("query_id")) or type(item.get("pass")) is not bool:
            raise ReportValidationError("report_quality_value_invalid")
        if item.get("rank") is not None and (type(item["rank"]) is not int or item["rank"] < 1):
            raise ReportValidationError("report_quality_rank_invalid")
        if type(item.get("latency_ms")) is not int or item["latency_ms"] < 0:
            raise ReportValidationError("report_quality_latency_invalid")


def _validate_latency(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _LATENCY_KEYS:
        raise ReportValidationError("report_latency_schema_invalid")
    for measurements in value.values():
        if not isinstance(measurements, dict):
            raise ReportValidationError("report_latency_schema_invalid")
        for label, milliseconds in measurements.items():
            if not _safe_identifier(label) or type(milliseconds) is not int or milliseconds < 0:
                raise ReportValidationError("report_latency_value_invalid")


def _validate_resources(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _RESOURCE_KEYS:
        raise ReportValidationError("report_resources_schema_invalid")
    if any(type(item) is not int or item < 0 for item in value.values()):
        raise ReportValidationError("report_resources_value_invalid")


def _validate_egress(value: Any) -> None:
    if not isinstance(value, list) or not all(isinstance(host, str) and _safe_hostname(host) for host in value):
        raise ReportValidationError("report_egress_invalid")


def _validate_duplicates(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _DUPLICATE_KEYS:
        raise ReportValidationError("report_duplicates_schema_invalid")
    if not isinstance(value.get("observed"), str) or not _SAFE_OUTCOME.fullmatch(value["observed"]):
        raise ReportValidationError("report_duplicates_outcome_invalid")
    if type(value.get("count")) is not int or value["count"] < 0:
        raise ReportValidationError("report_duplicates_count_invalid")


def _safe_hostname(value: str) -> bool:
    if not value or len(value) > 253 or "/" in value or ":" in value:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", value))


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()
