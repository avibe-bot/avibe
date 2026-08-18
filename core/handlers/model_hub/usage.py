"""Bounded, credential-free persistence for Model Hub token usage.

The turn gateway is the only place in the product that sees a complete upstream
model response, so it is the only place that can count tokens. This module owns
what happens to those counts afterwards: one bounded daily aggregate per served
source and model, and the read shape the settings page consumes.

Two properties are deliberate. `requests` is self-measured by our own code and is
always available; token counts are vendor-reported and may be absent, which is
why `token_reports` is tracked separately instead of treating a missing report as
zero usage. And nothing here ever feeds admission, routing, or cooldown — a
hostile upstream must not be able to change resolution behavior by lying about
usage.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final, Optional

from .stream_wire import USAGE_TOKEN_CEILING, ProtocolUsageReport

# Roughly two months of daily rows: long enough for a monthly view plus the
# previous cycle, short enough that the file stays small on a busy machine.
USAGE_RETENTION_DAYS: Final = 62
USAGE_MAX_ROWS: Final = 400
USAGE_DEFAULT_WINDOW_DAYS: Final = 30
# Source and model identifiers are already bounded by their own validators; this
# is the persistence-side backstop that keeps one row from growing without limit.
_MAX_IDENTIFIER_LENGTH: Final = 200

_COUNTER_KEYS: Final = (
    "requests",
    "token_reports",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
)


def local_usage_day(moment: datetime) -> date:
    """Bucket one moment into a local-calendar day.

    Avibe is local-first and the settings page already presents local days, so a
    day boundary here is the user's midnight, not UTC's.
    """

    return moment.astimezone().date()


def _bounded_counter(value: object) -> int:
    """Read one persisted counter, degrading anything unusable to zero."""

    if not isinstance(value, int) or isinstance(value, bool):
        return 0
    if value < 0:
        return 0
    return min(value, USAGE_TOKEN_CEILING)


def _identifier(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed or len(trimmed) > _MAX_IDENTIFIER_LENGTH:
        return None
    return trimmed


def _normalize_row(row: object) -> Optional[dict]:
    """Project one persisted row onto the current shape, or drop it."""

    if not isinstance(row, dict):
        return None
    day = _identifier(row.get("day"))
    source_id = _identifier(row.get("source_id"))
    model_id = _identifier(row.get("model_id"))
    if day is None or source_id is None or model_id is None:
        return None
    try:
        date.fromisoformat(day)
    except ValueError:
        return None
    normalized = {
        "day": day,
        "source_id": source_id,
        "model_id": model_id,
        **{key: _bounded_counter(row.get(key)) for key in _COUNTER_KEYS},
    }
    last_served_at = _identifier(row.get("last_served_at"))
    normalized["last_served_at"] = last_served_at
    return normalized


def _row_key(row: dict) -> tuple[str, str, str]:
    return (row["day"], row["source_id"], row["model_id"])


def _empty_totals() -> dict:
    return {key: 0 for key in _COUNTER_KEYS}


def _accumulate(target: dict, row: dict) -> None:
    for key in _COUNTER_KEYS:
        target[key] = min(target[key] + row[key], USAGE_TOKEN_CEILING)


def _newer_timestamp(current: Optional[str], candidate: Optional[str]) -> Optional[str]:
    if candidate is None:
        return current
    if current is None:
        return candidate
    return max(current, candidate)


class BoundedUsageLedger:
    """Persist served-turn token counts as a bounded daily aggregate."""

    def __init__(
        self,
        path: Path,
        *,
        max_rows: int = USAGE_MAX_ROWS,
        retention_days: int = USAGE_RETENTION_DAYS,
    ):
        self.path = path
        self.max_rows = max_rows
        self.retention_days = retention_days
        self._lock = threading.RLock()

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        rows: dict[tuple[str, str, str], dict] = {}
        for item in payload:
            row = _normalize_row(item)
            if row is None:
                continue
            existing = rows.get(_row_key(row))
            if existing is None:
                rows[_row_key(row)] = row
                continue
            _accumulate(existing, row)
            existing["last_served_at"] = _newer_timestamp(
                existing["last_served_at"],
                row["last_served_at"],
            )
        return sorted(rows.values(), key=_row_key)

    def _write(self, rows: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(
            sorted(rows, key=_row_key)[-self.max_rows :],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.path.parent,
            delete=False,
        ) as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_name = tmp.name
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, self.path)

    def record(
        self,
        *,
        source_id: str,
        model_id: str,
        usage: Optional[ProtocolUsageReport],
        at: datetime,
    ) -> None:
        """Fold one served turn into its day's row."""

        safe_source_id = _identifier(source_id)
        safe_model_id = _identifier(model_id)
        if safe_source_id is None or safe_model_id is None:
            return
        increment = {
            "day": local_usage_day(at).isoformat(),
            "source_id": safe_source_id,
            "model_id": safe_model_id,
            "requests": 1,
            "token_reports": 1 if usage is not None else 0,
            "input_tokens": usage.input_tokens if usage else 0,
            "cached_input_tokens": usage.cached_input_tokens if usage else 0,
            "output_tokens": usage.output_tokens if usage else 0,
            "last_served_at": at.isoformat(),
        }
        with self._lock:
            rows = {_row_key(row): row for row in self._read()}
            existing = rows.get(_row_key(increment))
            if existing is None:
                rows[_row_key(increment)] = increment
            else:
                _accumulate(existing, increment)
                existing["last_served_at"] = _newer_timestamp(
                    existing["last_served_at"],
                    increment["last_served_at"],
                )
            retained = self._retained(list(rows.values()), local_usage_day(at))
            self._write(retained)

    def _retained(self, rows: list[dict], today: date) -> list[dict]:
        oldest = (today - timedelta(days=self.retention_days - 1)).isoformat()
        return [row for row in rows if row["day"] >= oldest]

    def window(self, *, days: int, now: datetime) -> list[dict]:
        """Return the rows inside the trailing local-day window, oldest first."""

        bounded_days = max(1, min(int(days), self.retention_days))
        today = local_usage_day(now)
        first_day = (today - timedelta(days=bounded_days - 1)).isoformat()
        last_day = today.isoformat()
        with self._lock:
            rows = self._read()
        return [row for row in rows if first_day <= row["day"] <= last_day]

    def summary(self, *, days: int = USAGE_DEFAULT_WINDOW_DAYS, now: datetime) -> dict:
        """Aggregate the window into the label-free settings-page read shape."""

        bounded_days = max(1, min(int(days), self.retention_days))
        today = local_usage_day(now)
        rows = self.window(days=bounded_days, now=now)

        totals = _empty_totals()
        sources: dict[str, dict] = {}
        by_day: dict[str, dict] = {}
        for row in rows:
            _accumulate(totals, row)

            source = sources.setdefault(
                row["source_id"],
                {
                    "source_id": row["source_id"],
                    **_empty_totals(),
                    "last_served_at": None,
                    "models": {},
                },
            )
            _accumulate(source, row)
            source["last_served_at"] = _newer_timestamp(
                source["last_served_at"],
                row["last_served_at"],
            )
            model = source["models"].setdefault(
                row["model_id"],
                {"model_id": row["model_id"], **_empty_totals()},
            )
            _accumulate(model, row)

            day = by_day.setdefault(row["day"], {"day": row["day"], **_empty_totals()})
            _accumulate(day, row)

        return {
            "window_days": bounded_days,
            "from_day": (today - timedelta(days=bounded_days - 1)).isoformat(),
            "to_day": today.isoformat(),
            "totals": totals,
            "sources": [
                {
                    **{key: value for key, value in source.items() if key != "models"},
                    "models": sorted(
                        source["models"].values(),
                        key=lambda model: (-model["requests"], model["model_id"]),
                    ),
                }
                for source in sorted(
                    sources.values(),
                    key=lambda source: (-source["requests"], source["source_id"]),
                )
            ],
            "days": [by_day[day] for day in sorted(by_day)],
        }
