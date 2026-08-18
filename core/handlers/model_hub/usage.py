"""Bounded, credential-free persistence for Model Hub token usage.

Only the code that sees a complete upstream model response can count tokens, and
one turn can make several upstream calls. Two callers therefore report calls here,
over populations that ``InvokeHandle.stream is not None`` keeps disjoint: the
resolver reports every call it consumed itself, including a failover hop that
billed us before the turn moved on, and the turn gateway reports every call whose
body it forwarded. This module owns what happens to those counts afterwards: one
bounded daily aggregate per source and model, and the read shape the settings page
consumes.

Two properties are deliberate. `requests` is self-measured by our own code and is
always available; token counts are vendor-reported and may be absent, which is
why `token_reports` is tracked separately instead of treating a missing report as
zero usage. And nothing here ever feeds admission, routing, or cooldown — a
hostile upstream must not be able to change resolution behavior by lying about
usage.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Optional

from .identifiers import canonical_model_id
from .stream_wire import USAGE_TOKEN_CEILING, ProtocolUsageReport

logger = logging.getLogger(__name__)

# Roughly two months of daily rows: long enough for a monthly view plus the
# previous cycle, short enough that the file stays small on a busy machine.
USAGE_RETENTION_DAYS: Final = 62
USAGE_MAX_ROWS: Final = 400
USAGE_DEFAULT_WINDOW_DAYS: Final = 30
# Anything older than every instant this ledger can hold, so a row that never
# recorded one sorts as the least recently metered.
_OLDEST_INSTANT: Final = datetime.min.replace(tzinfo=timezone.utc)

_COUNTER_KEYS: Final = (
    "requests",
    "token_reports",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
)
# The cross-field guarantees the read contract makes, as (subset, superset).
# Each one is repaired on read, so a corrupt or hand-edited file degrades into a
# smaller true statement instead of publishing an impossible one: a coverage
# figure above 100% is more misleading than a conservative one.
_COUNTER_SUBSETS: Final = (
    ("cached_input_tokens", "input_tokens"),
    ("token_reports", "requests"),
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


def _text(value: object) -> Optional[str]:
    """Read one persisted text field, dropping anything that is not text.

    No length bound here: the only text fields this reads are a calendar day and
    an instant, and the parsers below already reject anything that is not one.
    The key fields go through `canonical_model_id` instead, so a ledger row is
    keyed by exactly the identity configuration admitted.
    """

    if not isinstance(value, str):
        return None
    return value.strip() or None


def _calendar_day(value: str) -> Optional[date]:
    """Read one ISO calendar day, or None when the text is not a bare date."""

    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _instant(value: object) -> Optional[datetime]:
    """Parse one persisted instant, or None when it is not an ISO date-time.

    A bare date is a day, not an instant, and the date-time parser would read one
    as midnight — inventing a time of day this ledger never wrote. The same parser
    that recognizes the ``day`` key decides that here, so the module holds one
    notion of what a day is.

    A naive value is read as local time, the same calendar the day buckets use.
    """

    if not isinstance(value, str) or _calendar_day(value) is not None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def _timestamp(value: object) -> Optional[str]:
    """Publish one persisted instant in the one spelling this module writes.

    The read surface promises an RFC 3339 date-time, and `datetime.fromisoformat`
    accepts far more than RFC 3339 describes — naive, space-separated, and, after
    an offset, even seconds. Publishing the file's text, or re-publishing the
    parsed value in the file's own offset, both leave the output shape decided by
    the input: fix one spelling and the next one is still reachable.

    So the file supplies only the instant, and this module supplies the spelling.
    Normalizing to UTC means the offset is `+00:00` by construction, whatever the
    file said, and there is no remaining spelling for a hand-edited value to
    reach. An unparseable value degrades the field to absent.
    """

    parsed = _instant(_text(value))
    if parsed is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _normalize_row(row: object) -> Optional[dict]:
    """Project one persisted row onto the current shape, or drop it."""

    if not isinstance(row, dict):
        return None
    day = _text(row.get("day"))
    source_id = canonical_model_id(row.get("source_id"))
    model_id = canonical_model_id(row.get("model_id"))
    if day is None or source_id is None or model_id is None:
        return None
    # Same rule as the instant above: the file supplies the day, this module
    # supplies its spelling. `date.fromisoformat` also reads `20260818` and
    # `2026-W34-2`, and the window bounds are compared as `YYYY-MM-DD` text — so
    # a row kept in another valid spelling would be silently outside every
    # window it belongs to, without even counting as dropped.
    calendar_day = _calendar_day(day)
    if calendar_day is None:
        return None
    normalized = {
        "day": calendar_day.isoformat(),
        "source_id": source_id,
        "model_id": model_id,
        **{key: _bounded_counter(row.get(key)) for key in _COUNTER_KEYS},
    }
    for subset, superset in _COUNTER_SUBSETS:
        normalized[subset] = min(normalized[subset], normalized[superset])
    normalized["last_metered_at"] = _timestamp(row.get("last_metered_at"))
    return normalized


def _recency(row: dict) -> tuple[str, datetime]:
    """Order rows oldest-metered first, so the bound evicts what costs least.

    Ordering by key instead would evict by spelling: an early-sorting model would
    be recreated and evicted again on every write while later-sorting stale rows
    survived, so its usage could never accumulate. Instants are compared as points
    in time — text order is not time order once two rows carry different offsets.
    """

    return (row["day"], _instant(row["last_metered_at"]) or _OLDEST_INSTANT)


def _row_key(row: dict) -> tuple[str, str, str]:
    return (row["day"], row["source_id"], row["model_id"])


def _empty_totals() -> dict:
    return {key: 0 for key in _COUNTER_KEYS}


def _accumulate(target: dict, row: dict) -> None:
    for key in _COUNTER_KEYS:
        target[key] = min(target[key] + row[key], USAGE_TOKEN_CEILING)


def _newer_timestamp(current: Optional[str], candidate: Optional[str]) -> Optional[str]:
    """Keep the later of two instants, comparing points in time.

    Text order is not time order once two rows carry different UTC offsets, which
    a state file merged across machines or an older release can hold.
    """

    if candidate is None:
        return current
    if current is None:
        return candidate
    current_instant = _instant(current)
    candidate_instant = _instant(candidate)
    if current_instant is None:
        return candidate
    if candidate_instant is None:
        return current
    return candidate if candidate_instant > current_instant else current


class BoundedUsageLedger:
    """Persist metered upstream-call token counts as a bounded daily aggregate."""

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
        # Degrading to empty keeps a broken optional-feature file from failing
        # startup, but the next write replaces that file — so this is the last
        # moment its history is recoverable, and saying nothing would erase it
        # silently.
        # Every way decoding can fail, by category rather than by the shapes a
        # particular file happens to hold: invalid UTF-8 and an integer past the
        # digit limit both raise plain `ValueError`, deep nesting raises
        # `RecursionError`, and either one escaping here would take down the read
        # route and then stop metering entirely — the loud-failure outcome this
        # degradation exists to prevent.
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            logger.warning("Model Hub usage ledger %s is unreadable: %s", self.path, exc)
            return []
        if not isinstance(payload, list):
            logger.warning("Model Hub usage ledger %s is not a list of rows", self.path)
            return []
        rows: dict[tuple[str, str, str], dict] = {}
        dropped = 0
        for item in payload:
            row = _normalize_row(item)
            if row is None:
                dropped += 1
                continue
            existing = rows.get(_row_key(row))
            if existing is None:
                rows[_row_key(row)] = row
                continue
            _accumulate(existing, row)
            existing["last_metered_at"] = _newer_timestamp(
                existing["last_metered_at"],
                row["last_metered_at"],
            )
        if dropped:
            logger.warning(
                "Model Hub usage ledger %s dropped %d unusable row(s)", self.path, dropped
            )
        return sorted(rows.values(), key=_row_key)

    def _write(self, rows: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        retained = sorted(rows, key=_recency)[-self.max_rows :]
        content = json.dumps(
            sorted(retained, key=_row_key),
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
        """Fold one metered upstream call into its day's row.

        The new row goes through the same normalization as a persisted one, so
        what this module writes and what it reads can never disagree about a
        bound, a subset, or an identity.
        """

        increment = _normalize_row(
            {
                "day": local_usage_day(at).isoformat(),
                "source_id": source_id,
                "model_id": model_id,
                "requests": 1,
                "token_reports": 1 if usage is not None else 0,
                "input_tokens": usage.input_tokens if usage else 0,
                "cached_input_tokens": usage.cached_input_tokens if usage else 0,
                "output_tokens": usage.output_tokens if usage else 0,
                "last_metered_at": at.isoformat(),
            }
        )
        if increment is None:
            # Unreachable for anything the hub admits: both boundaries that accept
            # a model id store the canonical form this row is keyed by. Loud rather
            # than silent so a future boundary that forgets shows up as lost
            # metering instead of as a quietly incomplete tab.
            logger.warning(
                "Model Hub usage metering skipped a call with an unusable identifier",
                extra={"source_id_usable": canonical_model_id(source_id) is not None},
            )
            return
        with self._lock:
            rows = {_row_key(row): row for row in self._read()}
            existing = rows.get(_row_key(increment))
            if existing is None:
                rows[_row_key(increment)] = increment
            else:
                _accumulate(existing, increment)
                existing["last_metered_at"] = _newer_timestamp(
                    existing["last_metered_at"],
                    increment["last_metered_at"],
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
                    "last_metered_at": None,
                    "models": {},
                },
            )
            _accumulate(source, row)
            source["last_metered_at"] = _newer_timestamp(
                source["last_metered_at"],
                row["last_metered_at"],
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
