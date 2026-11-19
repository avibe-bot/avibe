"""Value-safe validation shared by the mobile device API and local CLI."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import math
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

METRICS = ("steps", "activity", "workouts", "sleep")
PHONE_ERRORS = frozenset({
    "protected_data_unavailable", "capability_unavailable", "user_declined", "internal",
})
TERMINAL = frozenset({"succeeded", "failed", "expired", "canceled"})
MAX_BODY_BYTES = 256 * 1024


class MobileError(Exception):
    """Only fixed codes may cross the mobile boundary; never include input."""

    def __init__(self, code: str, status: int = 422, *, details: dict | None = None):
        self.code = code
        self.status = status
        self.details = details
        super().__init__(code)


def object_fields(value: Any, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise MobileError("invalid_payload")
    return value


def text(value: Any, *, limit: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MobileError("invalid_payload")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise MobileError("invalid_payload")
    return value


def integer(value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise MobileError("invalid_payload")
    return value


def timestamp(value: Any, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    try:
        result = datetime.fromisoformat(text(value, limit=50).replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError):
        raise MobileError("invalid_payload") from None
    return value


def day(value: Any) -> date:
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise MobileError("invalid_payload") from None


def timezone_name(value: Any) -> str:
    try:
        return ZoneInfo(text(value, limit=80)).key
    except (ValueError, ZoneInfoNotFoundError):
        raise MobileError("invalid_payload") from None


def metrics(value: Any) -> list[str]:
    if (
        not isinstance(value, list) or not value
        or any(not isinstance(item, str) or item not in METRICS for item in value)
        or len(set(value)) != len(value)
    ):
        raise MobileError("unsupported_metric")
    return value


def _number(value: Any) -> None:
    if type(value) not in {int, float} or value < 0 or (
        type(value) is float and not math.isfinite(value)
    ):
        raise MobileError("invalid_payload")


def _metric_value(metric: str, value: Any) -> None:
    if metric == "steps":
        object_fields(value, {"count"})
        integer(value["count"], 0, 2**53 - 1)
    elif metric == "activity":
        object_fields(value, {"exercise_minutes", "active_energy_kcal"})
        for item in value.values():
            _number(item)
    elif metric == "workouts":
        object_fields(value, {"workouts"})
        if not isinstance(value["workouts"], list) or len(value["workouts"]) > 1000:
            raise MobileError("invalid_payload")
        for workout in value["workouts"]:
            object_fields(workout, {"activity", "start_at", "end_at", "duration_minutes"},
                          {"active_energy_kcal", "distance_m"})
            text(workout["activity"], limit=80)
            timestamp(workout["start_at"])
            timestamp(workout["end_at"])
            for key in ("duration_minutes", "active_energy_kcal", "distance_m"):
                if key in workout:
                    _number(workout[key])
    else:
        object_fields(value, {"asleep_minutes", "in_bed_minutes", "start_at", "end_at", "stages"})
        for component in ("asleep_minutes", "in_bed_minutes"):
            if value[component] is not None:
                _number(value[component])
        timestamp(value["start_at"], nullable=True)
        timestamp(value["end_at"], nullable=True)
        if value["stages"] is not None:
            object_fields(value["stages"], set(), {"awake", "core", "deep", "rem", "unspecified"})
            for duration in value["stages"].values():
                _number(duration)


def validate_upload(payload: Any, *, allowed_metrics: list[str]) -> dict:
    object_fields(payload, {"device_seq", "computed_at", "window", "items", "failures"})
    integer(payload["device_seq"], 1, 2**63 - 1)
    timestamp(payload["computed_at"])
    window = object_fields(payload["window"], {"metrics", "from", "to"})
    names = metrics(window["metrics"])
    if not set(names) <= set(allowed_metrics):
        raise MobileError("unsupported_metric")
    start, end = day(window["from"]), day(window["to"])
    if not 0 <= (end - start).days <= 30:
        raise MobileError("invalid_payload")
    if not isinstance(payload["items"], list) or not isinstance(payload["failures"], list):
        raise MobileError("invalid_payload")
    failed = set()
    for failure in payload["failures"]:
        object_fields(failure, {"metric", "at", "code"})
        if failure["metric"] not in names or failure["metric"] in failed:
            raise MobileError("invalid_payload")
        if text(failure["code"]) not in PHONE_ERRORS:
            raise MobileError("invalid_payload")
        timestamp(failure["at"])
        failed.add(failure["metric"])
    expected = {
        (metric, (start + timedelta(days=offset)).isoformat())
        for metric in names if metric not in failed
        for offset in range((end - start).days + 1)
    }
    actual = set()
    for item in payload["items"]:
        object_fields(item, {"metric", "day", "timezone", "availability", "value",
                             "latest_sample_end_at", "queried_at", "closed"})
        pair = (text(item["metric"]), day(item["day"]).isoformat())
        if pair not in expected or pair in actual:
            raise MobileError("incomplete_window")
        actual.add(pair)
        timezone_name(item["timezone"])
        timestamp(item["latest_sample_end_at"], nullable=True)
        timestamp(item["queried_at"])
        if type(item["closed"]) is not bool:
            raise MobileError("invalid_payload")
        if item["availability"] == "empty" and item["value"] is None:
            continue
        if item["availability"] != "ok":
            raise MobileError("invalid_payload")
        _metric_value(item["metric"], item["value"])
    if actual != expected:
        raise MobileError("incomplete_window")
    return payload


def validate_result(payload: Any, requested_metrics: list[str]) -> dict:
    object_fields(payload, {"claim_token", "outcome"}, {"result", "error"})
    text(payload["claim_token"], limit=100)
    if text(payload["outcome"]) not in {"succeeded", "failed"}:
        raise MobileError("invalid_payload")
    if "result" in payload:
        result = object_fields(payload["result"], {"metrics"})["metrics"]
        if not isinstance(result, dict) or not set(result) <= set(requested_metrics):
            raise MobileError("invalid_payload")
        if any(not isinstance(state, str) or state not in {"ok", "empty", *PHONE_ERRORS}
               for state in result.values()):
            raise MobileError("invalid_payload")
    if "error" in payload:
        error = object_fields(payload["error"], {"code"})
        if text(error["code"]) not in PHONE_ERRORS:
            raise MobileError("invalid_payload")
    if payload["outcome"] == "failed" and "error" not in payload:
        raise MobileError("invalid_payload")
    if payload["outcome"] == "succeeded" and (
        "error" in payload or "result" not in payload
        or set(payload["result"]["metrics"]) != set(requested_metrics)
        or any(state not in {"ok", "empty"} for state in payload["result"]["metrics"].values())
    ):
        raise MobileError("invalid_payload")
    return payload
