from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import Any

from .constants import CRITERIA_IDS
from .environment import ProviderSettings, lock_id
from .errors import HarnessError, ReportValidationError
from .identifiers import validate_run_id
from .metrics import CallMetrics
from .paths import read_private_text, workspace_root, write_private_text
from .pricing import estimate_ingestion_cost
from .provider import HttpShape
from .readiness import SearchReadiness
from .research_inspection import ResearchInspection

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
_CRITERION_KEYS = {"id", "state", "value", "threshold"}
_CRITERION_STATES = {"pass", "fail", "not_measured"}
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
_REDACTED_MODEL_NAME = "configured-model-redacted"


def pending_criteria() -> list[dict[str, Any]]:
    return [
        {"id": criterion_id, "state": "not_measured", "value": None, "threshold": None}
        for criterion_id in CRITERIA_IDS
    ]


def set_criterion(criteria: list[dict[str, Any]], criterion_id: str, *, state: str, value: Any, threshold: Any) -> None:
    for item in criteria:
        if item["id"] == criterion_id:
            item.update({"state": state, "value": value, "threshold": threshold})
            return
    raise ReportValidationError("unknown_criterion")


def build_report(*, run_id: str, settings: ProviderSettings) -> dict[str, Any]:
    secret_values = _configured_secret_values(settings)
    return {
        "run_id": run_id,
        "harness_commit": _git_commit(),
        "corpus_revision": "stage1-mini",
        "environment": {
            "os": os.uname().sysname,
            "machine_class": os.uname().machine,
            "python": platform.python_version(),
            "lock_id": lock_id(),
            "llm_model": _redacted_model_name(settings.llm_model, secret_values=secret_values),
            "embedding_model": _redacted_model_name(settings.embedding_model, secret_values=secret_values),
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


def validate_report(
    report: dict[str, Any],
    *,
    fixture_texts: tuple[str, ...],
    secret_values: tuple[str, ...] = (),
) -> None:
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
        state = item.get("state")
        if state not in _CRITERION_STATES:
            raise ReportValidationError("report_criteria_state_invalid")
        if state == "not_measured":
            if item.get("value") is not None or item.get("threshold") is not None:
                raise ReportValidationError("report_criteria_not_measured_invalid")
        elif not _numeric(item.get("value")) or not _numeric(item.get("threshold")):
            raise ReportValidationError("report_criteria_measurement_invalid")

    _validate_quality(report["quality"])
    _validate_latency(report["latency"])
    _validate_resources(report["resources"])
    _validate_egress(report["egress"])
    _validate_duplicates(report["duplicates"])
    if report.get("recommendation") not in {"official", "fork", "stop"}:
        raise ReportValidationError("report_recommendation_invalid")

    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)
    _assert_secret_free(rendered, secret_values, code="report_contains_secret")
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
    fixture_texts: tuple[str, ...],
    secret_values: tuple[str, ...],
) -> None:
    validate_report(report, fixture_texts=fixture_texts, secret_values=secret_values)
    write_private_text(path, json.dumps(report, ensure_ascii=True, indent=2, sort_keys=False) + "\n", anchor=anchor)


def load_report(
    path: Path,
    *,
    fixture_texts: tuple[str, ...],
    secret_values: tuple[str, ...] = (),
) -> dict[str, Any]:
    try:
        payload = json.loads(read_private_text(path))
    except (HarnessError, OSError, ValueError) as exc:
        raise ReportValidationError("report_unreadable") from exc
    if not isinstance(payload, dict):
        raise ReportValidationError("report_not_object")
    validate_report(payload, fixture_texts=fixture_texts, secret_values=secret_values)
    return payload


def write_summary(
    path: Path,
    *,
    settings: ProviderSettings,
    metrics: CallMetrics,
    message_count: int,
    http_shapes: tuple[HttpShape, ...],
    outcome: str = "completed",
    readiness: SearchReadiness | None = None,
    research_inspection: ResearchInspection | None = None,
    anchor: Path | None = None,
) -> None:
    if not _SAFE_IDENTIFIER.fullmatch(outcome):
        raise ReportValidationError("summary_outcome_invalid")
    divisor = max(message_count, 1)
    usage_lines = _ingestion_usage_lines(metrics, divisor)
    cost_line = _rough_cost_line(settings, metrics, message_count)
    shape_lines = _http_shape_lines(http_shapes)
    readiness_lines = _search_readiness_lines(readiness)
    secret_values = _configured_secret_values(settings)
    profile_lines = _profile_known_absent_lines(
        readiness,
        model_name=settings.llm_model,
        secret_values=secret_values,
    )
    inspection_lines = _research_inspection_lines(research_inspection)
    rendered = "\n".join(
        (
            "# EverOS POC Stage 1 Sanity",
            "",
            f"Run outcome: {outcome}",
            "Ingestion means provider work attributed to add and explicit flush only; readiness and restart reads are excluded.",
            f"LLM ingestion calls per message: {metrics.ingestion_llm_calls / divisor:.2f}",
            f"Embedding ingestion calls per message: {metrics.ingestion_embedding_calls / divisor:.2f}",
            f"Total sidecar LLM calls: {metrics.llm_calls}",
            f"Total sidecar embedding calls: {metrics.embedding_calls}",
            *usage_lines,
            cost_line,
            "",
            *readiness_lines,
            *profile_lines,
            *inspection_lines,
            "",
            "Observed public HTTP shapes (redacted keys only):",
            *shape_lines,
            "",
        )
    )
    _assert_secret_free(rendered, secret_values, code="summary_contains_secret")
    write_private_text(path, rendered, anchor=anchor)


def _search_readiness_lines(readiness: SearchReadiness | None) -> tuple[str, ...]:
    if readiness is None:
        return ()
    if not _is_strict_int(readiness.timeout_ms) or readiness.timeout_ms <= 0:
        raise ReportValidationError("summary_readiness_timing_invalid")
    if not isinstance(readiness.measurement_started, bool):
        raise ReportValidationError("summary_readiness_timing_invalid")
    if not readiness.measurement_started:
        return (
            "Search readiness timing: not measured because flush did not complete.",
            "- Profile content via search: not measured.",
            "- Episode content via search: not measured.",
            "- Atomic fact via search: not measured.",
            "- Max observed cascade lag via search: not measured.",
        )
    observations: list[int] = []
    labels = (
        ("Profile content", "profile_ms"),
        ("Episode content", "episode_ms"),
        ("Atomic fact", "atomic_fact_ms"),
    )
    lines = ["Search readiness timing (first observation after flush completion):"]
    for label, key in labels:
        observed_ms = getattr(readiness, key)
        if observed_ms is None:
            lines.append(
                f"- {label} via search: not observed within {readiness.timeout_ms} ms after flush completion."
            )
            continue
        if not _is_strict_int(observed_ms) or observed_ms < 0 or observed_ms > readiness.timeout_ms:
            raise ReportValidationError("summary_readiness_timing_invalid")
        observations.append(observed_ms)
        lines.append(f"- {label} via search: first observed {observed_ms} ms after flush completion.")
    if observations:
        lines.append(f"- Max observed cascade lag via search: {max(observations)} ms.")
    else:
        lines.append("- Max observed cascade lag via search: not observed.")
    return tuple(lines)


def _profile_known_absent_lines(
    readiness: SearchReadiness | None,
    *,
    model_name: str,
    secret_values: tuple[str, ...],
) -> tuple[str, ...]:
    if readiness is None or not readiness.profile_known_absent:
        return ()
    rendered_model_name = _summary_model_name(model_name, secret_values=secret_values)
    return (
        "WARNING: profile content not retrievable via /search within the window; episode+fact retrieval succeeded; "
        "profile treated as known-absent.",
        f"Conclusion: profile not published/readable via public search in 1.1.3 + {rendered_model_name}; accepted as known behavior for MVP.",
    )


def _summary_model_name(model_name: Any, *, secret_values: tuple[str, ...]) -> str:
    """Render provider model metadata without making model syntax a gate."""
    if (
        not isinstance(model_name, str)
        or not model_name
        or len(model_name) > 256
        or _URI_PATTERN.search(model_name)
        or any(unicodedata.category(character).startswith("C") for character in model_name)
        or _contains_secret(model_name, secret_values)
    ):
        return _REDACTED_MODEL_NAME
    return model_name


def _research_inspection_lines(inspection: ResearchInspection | None) -> tuple[str, ...]:
    if inspection is None:
        return ()
    return (
        "Research-only isolated-root retention inspection (not a delivery gate):",
        f"- Visible Markdown artifacts: {_inspection_presence(inspection.markdown_present)}.",
        f"- Private SQLite state: {_inspection_presence(inspection.sqlite_present)}.",
        f"- Inspection outcome: {inspection.outcome}.",
    )


def _inspection_presence(value: bool | None) -> str:
    if value is None:
        return "unavailable"
    return "observed" if value else "not observed"


def _ingestion_usage_lines(metrics: CallMetrics, divisor: int) -> tuple[str, ...]:
    lines: list[str] = []
    if metrics.ingestion_llm_usage_records:
        lines.append(
            "LLM ingestion token usage: "
            f"input={metrics.ingestion_llm_input_tokens}, output={metrics.ingestion_llm_output_tokens}, "
            f"per_message_input={metrics.ingestion_llm_input_tokens / divisor:.2f}, "
            f"per_message_output={metrics.ingestion_llm_output_tokens / divisor:.2f}."
        )
    else:
        lines.append("LLM ingestion token usage: unavailable from provider responses.")
    if metrics.ingestion_embedding_usage_records:
        lines.append(
            "Embedding ingestion token usage: "
            f"input={metrics.ingestion_embedding_input_tokens}, "
            f"per_message_input={metrics.ingestion_embedding_input_tokens / divisor:.2f}."
        )
    else:
        lines.append("Embedding ingestion token usage: unavailable from provider responses.")
    return tuple(lines)


def _rough_cost_line(settings: ProviderSettings, metrics: CallMetrics, message_count: int) -> str:
    estimate = estimate_ingestion_cost(settings, metrics, message_count=message_count)
    if estimate is None:
        return "Rough provider cost per message: unavailable; no matching versioned pricing assumption or token usage."
    return (
        f"Rough provider cost per message: CNY {estimate.per_message_cny:.8f} "
        f"(estimate; total CNY {estimate.total_cny:.8f}; {estimate.assumption})."
    )


def _http_shape_lines(http_shapes: tuple[HttpShape, ...]) -> tuple[str, ...]:
    unique = tuple(dict.fromkeys(http_shapes))
    if not unique:
        return ("- none recorded",)
    return tuple(
        "- "
        f"phase={item.phase} method={item.method} route={item.route} status={item.status_code} "
        f"closed_code={item.closed_code if item.closed_code is not None else 'absent'} "
        f"request_keys={list(item.request_keys)} response_keys={list(item.response_keys)} "
        f"data_keys={list(item.data_keys)} response_paths={list(item.response_schema_paths)}"
        for item in unique
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


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _configured_secret_values(settings: ProviderSettings) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in (settings.llm_api_key, settings.embedding_api_key) if value))


def _redacted_model_name(model_name: str, *, secret_values: tuple[str, ...]) -> str:
    return _REDACTED_MODEL_NAME if _contains_secret(model_name, secret_values) else model_name


def _contains_secret(value: str, secret_values: tuple[str, ...]) -> bool:
    return any(secret in value for secret in secret_values if secret)


def _assert_secret_free(value: str, secret_values: tuple[str, ...], *, code: str) -> None:
    if _contains_secret(value, secret_values):
        raise ReportValidationError(code)


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
        if not _safe_identifier(item.get("query_id")) or not isinstance(item.get("pass"), bool):
            raise ReportValidationError("report_quality_value_invalid")
        if item.get("rank") is not None and (not _is_strict_int(item["rank"]) or item["rank"] < 1):
            raise ReportValidationError("report_quality_rank_invalid")
        if not _is_strict_int(item.get("latency_ms")) or item["latency_ms"] < 0:
            raise ReportValidationError("report_quality_latency_invalid")


def _validate_latency(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _LATENCY_KEYS:
        raise ReportValidationError("report_latency_schema_invalid")
    for measurements in value.values():
        if not isinstance(measurements, dict):
            raise ReportValidationError("report_latency_schema_invalid")
        for label, milliseconds in measurements.items():
            if not _safe_identifier(label) or not _is_strict_int(milliseconds) or milliseconds < 0:
                raise ReportValidationError("report_latency_value_invalid")


def _validate_resources(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _RESOURCE_KEYS:
        raise ReportValidationError("report_resources_schema_invalid")
    if any(not _is_strict_int(item) or item < 0 for item in value.values()):
        raise ReportValidationError("report_resources_value_invalid")


def _validate_egress(value: Any) -> None:
    if not isinstance(value, list) or not all(isinstance(host, str) and _safe_hostname(host) for host in value):
        raise ReportValidationError("report_egress_invalid")


def _validate_duplicates(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _DUPLICATE_KEYS:
        raise ReportValidationError("report_duplicates_schema_invalid")
    if not isinstance(value.get("observed"), str) or not _SAFE_OUTCOME.fullmatch(value["observed"]):
        raise ReportValidationError("report_duplicates_outcome_invalid")
    if not _is_strict_int(value.get("count")) or value["count"] < 0:
        raise ReportValidationError("report_duplicates_count_invalid")


def _safe_hostname(value: str) -> bool:
    if not value or len(value) > 253 or "/" in value or ":" in value:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", value))


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()
