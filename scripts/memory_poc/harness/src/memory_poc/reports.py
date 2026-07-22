from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .constants import CRITERIA_IDS
from .environment import ProviderSettings, lock_id
from .errors import ReportValidationError
from .paths import ensure_regular_file_mode, workspace_root

_URL_PATTERN = re.compile(r"https?://", re.IGNORECASE)
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


def validate_report(report: dict[str, Any]) -> None:
    if set(report) != _TOP_LEVEL_KEYS:
        raise ReportValidationError("report_top_level_schema_invalid")
    if not isinstance(report.get("run_id"), str) or not report["run_id"]:
        raise ReportValidationError("report_run_id_invalid")
    criteria = report.get("criteria")
    if not isinstance(criteria, list) or tuple(item.get("id") for item in criteria if isinstance(item, dict)) != CRITERIA_IDS:
        raise ReportValidationError("report_criteria_schema_invalid")
    if report.get("recommendation") not in {"official", "fork", "stop"}:
        raise ReportValidationError("report_recommendation_invalid")
    rendered = json.dumps(report, ensure_ascii=True, sort_keys=True)
    if _URL_PATTERN.search(rendered):
        raise ReportValidationError("report_contains_url")
    if any(token in rendered for token in ("LLM_API_KEY", "EMBEDDING_API_KEY", "Authorization")):
        raise ReportValidationError("report_contains_sensitive_field")


def write_report(path: Path, report: dict[str, Any]) -> None:
    validate_report(report)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    ensure_regular_file_mode(path)


def load_report(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReportValidationError("report_unreadable") from exc
    if not isinstance(payload, dict):
        raise ReportValidationError("report_not_object")
    validate_report(payload)
    return payload


def write_summary(path: Path, *, llm_calls: int, embedding_calls: int, message_count: int) -> None:
    divisor = max(message_count, 1)
    path.write_text(
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
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    ensure_regular_file_mode(path)


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
