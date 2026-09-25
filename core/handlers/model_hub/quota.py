"""Subscription quota: parse the vendors' own usage reports into rate-limit windows.

A logged-in subscription (Claude, ChatGPT/Codex) exposes how much of each of its
rate-limit windows is used through a model-free account endpoint. The engine that
holds the grant makes the call; this module only turns the returned body into the
`quota-summary.schema.json` window shape. It is a report, like usage metering:
nothing in resolution, admission, or cooldown reads it.

Parsing is defensive by construction. A vendor may add, rename, or null a window
at any time, so a window we cannot read is skipped, a window we do not recognise
is rendered generically from its upstream name, and a body that is not the shape
at all raises `SubscriptionQuotaError("malformed")` — a per-Source failure, never
a page failure. Only the parsed fields leave this module; the body does not.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Final, Literal, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

QuotaWindowKind = Literal["session", "weekly", "model_weekly", "other"]
QuotaFailure = Literal["auth_expired", "rate_limited", "unsupported", "unavailable", "malformed"]

SESSION_WINDOW_SECONDS: Final = 5 * 3600
WEEKLY_WINDOW_SECONDS: Final = 7 * 86400
# Upstream labels are user-visible; bound them so a hostile body cannot flood the page.
_MAX_LABEL_CHARS: Final = 64
_MAX_WINDOWS: Final = 16
# Rows read from any one upstream array or object. A report carries a handful;
# a hostile or runaway body stops costing work here, before anything is built.
_MAX_UPSTREAM_ROWS: Final = 64
# A usage report is a few KiB; refuse anything far larger before decoding it.
_MAX_BODY_BYTES: Final = 256 * 1024

QUOTA_VENDORS: Final = frozenset({"anthropic", "openai", "codex"})

# Claude's legacy top-level keys. `seven_day_oauth_apps` is an app-surface limit
# Claude Code itself does not present, so it is deliberately not a window here.
_CLAUDE_TOP_LEVEL: Final[Mapping[str, tuple[QuotaWindowKind, Optional[str]]]] = {
    "five_hour": ("session", None),
    "seven_day": ("weekly", None),
    "seven_day_opus": ("model_weekly", "Opus"),
    "seven_day_sonnet": ("model_weekly", "Sonnet"),
    "seven_day_overage_included": ("model_weekly", "Fable"),
}
_CLAUDE_SKIPPED_TOP_LEVEL: Final = frozenset({"seven_day_oauth_apps", "extra_usage", "limits"})

# Codex meters Spark under a feature id rather than a model name.
_CODEX_FEATURE_LABELS: Final[Mapping[str, str]] = {"codex_bengalfox": "Spark"}


class SubscriptionQuotaError(RuntimeError):
    """A sanitized, per-Source quota failure. Carries no upstream text."""

    def __init__(self, reason: QuotaFailure, *, retry_after_seconds: Optional[float] = None):
        super().__init__(f"subscription quota {reason}")
        self.reason: QuotaFailure = reason
        self.retry_after_seconds = retry_after_seconds


def _label(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:_MAX_LABEL_CHARS] or None


def _number(value: object) -> Optional[float]:
    """The one numeric coercion for upstream values: anything unreadable is missing."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        # JSON integers are unbounded; one too large for a float is unreadable, not a crash.
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _percent(value: object) -> Optional[float]:
    number = _number(value)
    if number is None:
        return None
    rounded = round(min(100.0, max(0.0, number)), 1)
    # 100 means spent; rounding must not promote a limit with headroom to it.
    return 99.9 if rounded >= 100.0 and number < 100.0 else rounded


def _seconds(value: object) -> Optional[int]:
    number = _number(value)
    if number is None or number < 1:
        return None
    return int(number)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_timestamp(value: object) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return _iso(moment)
    except (OverflowError, ValueError):
        return None


def _unix_timestamp(value: object) -> Optional[str]:
    seconds = _seconds(value)
    if seconds is None:
        return None
    try:
        return _iso(datetime.fromtimestamp(seconds, tz=timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None


def _window(
    *,
    window_id: str,
    kind: QuotaWindowKind,
    label: str,
    used_pct: float,
    window_seconds: Optional[int],
    resets_at: Optional[str],
    scope_model: Optional[str] = None,
) -> dict[str, Any]:
    window: dict[str, Any] = {
        "id": window_id[:128],
        "kind": kind,
        # Labels are composed from upstream parts, so bound the final text here.
        "label": _label(label) or window_id[:_MAX_LABEL_CHARS],
        "used_pct": used_pct,
        "window_seconds": window_seconds,
        "resets_at": resets_at,
    }
    if scope_model is not None:
        window["scope_model"] = scope_model
    return window


def _load_object(body: object) -> dict[str, Any]:
    if not isinstance(body, (str, bytes, bytearray)):
        raise SubscriptionQuotaError("malformed")
    size = len(body.encode("utf-8", "surrogatepass")) if isinstance(body, str) else len(body)
    if size > _MAX_BODY_BYTES:
        raise SubscriptionQuotaError("malformed")
    try:
        payload = json.loads(body)
    except (TypeError, UnicodeDecodeError, ValueError):
        raise SubscriptionQuotaError("malformed") from None
    if not isinstance(payload, dict) or "error" in payload:
        raise SubscriptionQuotaError("malformed")
    try:
        # An escaped lone surrogate decodes but cannot be served as UTF-8 JSON.
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (UnicodeEncodeError, ValueError):
        raise SubscriptionQuotaError("malformed") from None
    return payload


def _claude_limit_rows(rows: list[object]) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for index, row in enumerate(rows[:_MAX_UPSTREAM_ROWS]):
        if not isinstance(row, dict):
            continue
        used = _percent(row.get("percent"))
        if used is None:
            continue
        upstream_kind = row.get("kind")
        scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
        model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
        scope_model = _label(model.get("display_name"))
        resets_at = _iso_timestamp(row.get("resets_at"))
        # Classify on the server's `kind`, never on its display text.
        if upstream_kind == "session":
            kind: QuotaWindowKind = "session"
            seconds: Optional[int] = SESSION_WINDOW_SECONDS
            label = "session"
        elif upstream_kind == "weekly_all":
            kind, seconds, label = "weekly", WEEKLY_WINDOW_SECONDS, "weekly"
        elif upstream_kind == "weekly_scoped" and scope_model:
            kind, seconds, label = "model_weekly", WEEKLY_WINDOW_SECONDS, scope_model
        else:
            surface = scope.get("surface") if isinstance(scope.get("surface"), dict) else {}
            generic = scope_model or _label(surface.get("display_name")) or _label(upstream_kind)
            if generic is None:
                continue
            kind = "other"
            seconds = WEEKLY_WINDOW_SECONDS if row.get("group") == "weekly" else None
            label = generic
        windows.append(
            _window(
                window_id=f"limit:{upstream_kind if isinstance(upstream_kind, str) else index}:{scope_model or index}",
                kind=kind,
                label=label,
                used_pct=used,
                window_seconds=seconds,
                resets_at=resets_at,
                scope_model=scope_model if kind == "model_weekly" else None,
            )
        )
    return windows


def _claude_top_level(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    # The fixed keys are always read; only unknown keys count against the row cap.
    fixed = [(key, payload[key]) for key in _CLAUDE_TOP_LEVEL if key in payload]
    unknown = ((key, value) for key, value in payload.items() if key not in _CLAUDE_TOP_LEVEL)
    for key, value in itertools.chain(fixed, itertools.islice(unknown, _MAX_UPSTREAM_ROWS)):
        if key in _CLAUDE_SKIPPED_TOP_LEVEL or not isinstance(value, dict) or "utilization" not in value:
            continue
        used = _percent(value.get("utilization"))
        if used is None:
            continue
        kind, scope_model = _CLAUDE_TOP_LEVEL.get(key, ("other", None))
        seconds = {
            "session": SESSION_WINDOW_SECONDS,
            "weekly": WEEKLY_WINDOW_SECONDS,
            "model_weekly": WEEKLY_WINDOW_SECONDS,
        }.get(kind)
        label = scope_model or _label(key)
        if label is None:
            continue
        windows.append(
            _window(
                window_id=key,
                kind=kind,
                label=label,
                used_pct=used,
                window_seconds=seconds,
                resets_at=_iso_timestamp(value.get("resets_at")),
                scope_model=scope_model,
            )
        )
    return windows


def parse_claude_quota(body: object) -> dict[str, Any]:
    """Parse `GET https://api.anthropic.com/api/oauth/usage`.

    Prefer the server's `limits[]` rows when present: they carry the server's own
    model names. Otherwise fall back to the fixed top-level window keys.
    """

    payload = _load_object(body)
    rows = payload.get("limits")
    windows = _claude_limit_rows(rows) if isinstance(rows, list) and rows else []
    if not windows:
        windows = _claude_top_level(payload)
    if not windows and not any(key in payload for key in (*_CLAUDE_TOP_LEVEL, "limits")):
        raise SubscriptionQuotaError("malformed")
    return {"plan": None, "windows": _ordered(windows)}


def _codex_kind(seconds: Optional[int]) -> QuotaWindowKind:
    if seconds is None:
        return "other"
    if abs(seconds - SESSION_WINDOW_SECONDS) <= 900:
        return "session"
    if abs(seconds - WEEKLY_WINDOW_SECONDS) <= 3 * 3600:
        return "weekly"
    return "other"


def _codex_window(value: object) -> Optional[tuple[float, Optional[int], Optional[str]]]:
    if not isinstance(value, dict):
        return None
    used = _percent(value.get("used_percent"))
    if used is None:
        return None
    return used, _seconds(value.get("limit_window_seconds")), _unix_timestamp(value.get("reset_at"))


def _span_label(seconds: Optional[int]) -> str:
    """A window length as written: whole days, whole hours, else hours and minutes."""
    if seconds is None:
        return ""
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    hours, minutes = divmod(max(1, round(seconds / 60)), 60)
    if not minutes:
        return f"{hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def parse_codex_quota(body: object) -> dict[str, Any]:
    """Parse `GET https://chatgpt.com/backend-api/wham/usage`."""

    payload = _load_object(body)
    rate_limit = payload.get("rate_limit")
    additional = payload.get("additional_rate_limits")
    if not isinstance(rate_limit, dict) and not isinstance(additional, list) and "plan_type" not in payload:
        raise SubscriptionQuotaError("malformed")
    windows: list[dict[str, Any]] = []
    if isinstance(rate_limit, dict):
        for slot in ("primary_window", "secondary_window"):
            parsed = _codex_window(rate_limit.get(slot))
            if parsed is None:
                continue
            used, seconds, resets_at = parsed
            kind = _codex_kind(seconds)
            windows.append(
                _window(
                    window_id=slot,
                    kind=kind,
                    label=slot if kind != "other" else (_span_label(seconds) or slot),
                    used_pct=used,
                    window_seconds=seconds,
                    resets_at=resets_at,
                )
            )
    if isinstance(additional, list):
        for index, item in enumerate(additional[:_MAX_UPSTREAM_ROWS]):
            if not isinstance(item, dict) or not isinstance(item.get("rate_limit"), dict):
                continue
            feature = item.get("metered_feature")
            scope_model = (
                _CODEX_FEATURE_LABELS.get(feature) if isinstance(feature, str) else None
            ) or _label(item.get("limit_name")) or _label(feature)
            if scope_model is None:
                continue
            for slot in ("primary_window", "secondary_window"):
                parsed = _codex_window(item["rate_limit"].get(slot))
                if parsed is None:
                    continue
                used, seconds, resets_at = parsed
                weekly = _codex_kind(seconds) == "weekly"
                span = _span_label(seconds)
                windows.append(
                    _window(
                        window_id=f"additional:{index}:{slot}",
                        kind="model_weekly" if weekly else "other",
                        label=scope_model if weekly or not span else f"{scope_model} · {span}",
                        used_pct=used,
                        window_seconds=seconds,
                        resets_at=resets_at,
                        scope_model=scope_model if weekly else None,
                    )
                )
    plan = _label(payload.get("plan_type"))
    return {"plan": plan, "windows": _ordered(windows)}


_KIND_ORDER: Final = {"session": 0, "weekly": 1, "model_weekly": 2, "other": 3}


def _ordered(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique = []
    for window in windows:
        if window["id"] in seen:
            continue
        seen.add(window["id"])
        unique.append(window)
    unique.sort(key=lambda window: _KIND_ORDER[window["kind"]])
    return unique[:_MAX_WINDOWS]


def parse_subscription_quota(vendor: str, body: object) -> dict[str, Any]:
    normalized = vendor.strip().lower()
    if normalized == "anthropic":
        return parse_claude_quota(body)
    if normalized in {"openai", "codex"}:
        return parse_codex_quota(body)
    raise SubscriptionQuotaError("unsupported")


# ── Service-side cache ──────────────────────────────────────────────────────

QUOTA_REFRESH_INTERVAL: Final = timedelta(minutes=5)
# A forced refresh re-reads a Source at most this often; a click storm is served
# the snapshot it just produced instead of reaching the vendor again.
QUOTA_FORCED_REFRESH_INTERVAL: Final = timedelta(seconds=30)
QUOTA_READ_DEADLINE_SECONDS: Final = 12.0
# Each fetch reads the engine's auth inventory and calls a vendor; many Sources
# queue here instead of all reaching the engine at once.
QUOTA_MAX_CONCURRENT_FETCHES: Final = 4
QUOTA_FETCH_TIMEOUT_SECONDS: Final = 10.0
_RATE_LIMIT_COOLDOWN_FLOOR: Final = timedelta(minutes=5)
_RATE_LIMIT_COOLDOWN_CEILING: Final = timedelta(hours=1)

QuotaFetch = Callable[[str, str, str], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True)
class QuotaSourceRef:
    source_id: str
    vendor: str
    credential_ref: str
    display_name: str
    account_label: Optional[str]


@dataclass
class _QuotaEntry:
    credential_ref: str
    snapshot: Optional[dict[str, Any]] = None
    fetched_at: Optional[datetime] = None
    attempted_at: Optional[datetime] = None
    failure: Optional[QuotaFailure] = None
    cooldown_until: Optional[datetime] = None
    task: Optional[asyncio.Task[None]] = None


def _cooldown(now: datetime, retry_after_seconds: Optional[float]) -> datetime:
    # Clamp in seconds first: a hostile Retry-After must not overflow timedelta.
    floor = _RATE_LIMIT_COOLDOWN_FLOOR.total_seconds()
    ceiling = _RATE_LIMIT_COOLDOWN_CEILING.total_seconds()
    seconds = retry_after_seconds if retry_after_seconds and math.isfinite(retry_after_seconds) else floor
    return now + timedelta(seconds=min(ceiling, max(floor, seconds)))


class SubscriptionQuotaCache:
    """Last-good quota per Source, re-read at most every refresh interval.

    A read re-fetches each Source whose last attempt is older than the interval
    and is not cooling down after a vendor 429; concurrent reads share one fetch.
    A failure never discards the last good snapshot: the Source is reported
    `stale` (or `auth_expired`) with the windows it last had.
    """

    def __init__(self, fetch: QuotaFetch, *, now: Callable[[], datetime]):
        self._fetch = fetch
        self._now = now
        self._entries: dict[str, _QuotaEntry] = {}
        self._fetch_slots = asyncio.Semaphore(QUOTA_MAX_CONCURRENT_FETCHES)

    async def summary(self, sources: Sequence[QuotaSourceRef], *, force: bool = False) -> dict[str, Any]:
        live = {source.source_id for source in sources}
        for source_id in list(self._entries):
            if source_id not in live:
                self._entries.pop(source_id)
        pending = [
            task
            for source in sources
            if (task := self._schedule(source, force=force)) is not None
        ]
        if pending:
            # A slow vendor must not hold the page: whatever has not answered by
            # the deadline keeps running and lands in the next read.
            await asyncio.wait(pending, timeout=QUOTA_READ_DEADLINE_SECONDS)
        return {
            "refresh_interval_seconds": int(QUOTA_REFRESH_INTERVAL.total_seconds()),
            "sources": [self._payload(source) for source in sources],
        }

    def forget(self, source_id: str) -> None:
        """Drop a Source's snapshot, failure, and throttle, e.g. after re-authentication.

        The credential ref alone is not grant identity: re-authentication can keep it.
        An in-flight read of the old grant lands on the detached entry and is discarded.
        """

        self._entries.pop(source_id, None)

    def _entry(self, source: QuotaSourceRef) -> _QuotaEntry:
        entry = self._entries.get(source.source_id)
        if entry is None or entry.credential_ref != source.credential_ref:
            # A re-authenticated Source is a different grant; its old windows are not its own.
            entry = _QuotaEntry(credential_ref=source.credential_ref)
            self._entries[source.source_id] = entry
        return entry

    def _schedule(self, source: QuotaSourceRef, *, force: bool) -> Optional[asyncio.Task[None]]:
        if source.vendor not in QUOTA_VENDORS:
            return None
        entry = self._entry(source)
        if entry.task is not None and not entry.task.done():
            return entry.task
        now = self._now()
        if entry.cooldown_until is not None and now < entry.cooldown_until:
            return None
        interval = QUOTA_FORCED_REFRESH_INTERVAL if force else QUOTA_REFRESH_INTERVAL
        if entry.attempted_at is not None and now - entry.attempted_at < interval:
            return None
        entry.task = asyncio.create_task(
            self._refresh(source, entry),
            name=f"model-hub-quota-{source.source_id}",
        )
        return entry.task

    async def _refresh(self, source: QuotaSourceRef, entry: _QuotaEntry) -> None:
        try:
            async with self._fetch_slots:
                # The throttle counts from the vendor request, not from the queue:
                # a read that waited for a slot has not yet asked the vendor.
                entry.attempted_at = self._now()
                parsed = await self._fetch(source.source_id, source.vendor, source.credential_ref)
            windows = parsed.get("windows")
            if not isinstance(windows, list):
                raise SubscriptionQuotaError("malformed")
        except SubscriptionQuotaError as exc:
            entry.failure = exc.reason
            if exc.reason == "rate_limited":
                entry.cooldown_until = _cooldown(self._now(), exc.retry_after_seconds)
            logger.info("Model Hub quota read failed for %s: %s", source.source_id, exc.reason)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let one Source's failure become the page's; never log the body.
            entry.failure = "unavailable"
            logger.warning("Model Hub quota read failed for %s", source.source_id, exc_info=False)
            return
        entry.snapshot = {"plan": parsed.get("plan"), "windows": windows}
        entry.fetched_at = self._now()
        entry.failure = None
        entry.cooldown_until = None

    def _payload(self, source: QuotaSourceRef) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_id": source.source_id,
            "vendor": source.vendor,
            "display_name": source.display_name,
            "account_label": source.account_label,
            "plan": None,
            "fetched_at": None,
            "windows": [],
        }
        if source.vendor not in QUOTA_VENDORS:
            return {**payload, "state": "unsupported", "error_key": "models.quota.error.unsupported"}
        entry = self._entries.get(source.source_id)
        if entry is not None and entry.snapshot is not None:
            payload.update(
                plan=entry.snapshot["plan"],
                windows=entry.snapshot["windows"],
                fetched_at=_iso(entry.fetched_at) if entry.fetched_at else None,
            )
        failure = entry.failure if entry is not None else None
        if failure is None:
            if entry is None or entry.snapshot is None:
                return {**payload, "state": "error", "error_key": "models.quota.error.unavailable"}
            return {**payload, "state": "ok"}
        if failure == "auth_expired":
            return {**payload, "state": "auth_expired", "error_key": "models.quota.error.auth_expired"}
        state = "stale" if entry is not None and entry.snapshot is not None else "error"
        return {**payload, "state": state, "error_key": f"models.quota.error.{failure}"}
