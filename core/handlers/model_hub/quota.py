"""Subscription quota: parse the vendors' own usage reports into rate-limit windows.

A logged-in subscription (Claude, ChatGPT/Codex, Gemini through Antigravity, Grok)
exposes how much of each of its rate-limit windows is used through a model-free
account endpoint. Kimi's account sign-in has no such report. The engine that
holds the grant makes the call; this module only turns the returned body into the
`quota-summary.schema.json` window shape. It is a report, like usage metering:
resolution and admission never read it. The one reader is the refresh the user
presses, which resumes an account paused for quota unless this report still shows
the limit used up (`ModelHubService.quota_summary`).

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
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Final, Literal, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

# `session`, `weekly` and `monthly` stop the whole account when spent; the
# `model_*` kinds stop only the models `scope_model` names.
QuotaWindowKind = Literal["session", "weekly", "monthly", "model_session", "model_weekly", "other"]
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

QUOTA_VENDORS: Final = frozenset({"anthropic", "openai", "codex", "gemini", "xai"})
# The vendors whose report can carry reset cards.
RESET_CARD_VENDORS: Final = frozenset({"anthropic", "openai", "codex"})

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


def _moment(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        # Only a moment that can be written back in UTC is readable.
        _iso(moment)
    except (OverflowError, ValueError):
        return None
    return moment


def _iso_timestamp(value: object) -> Optional[str]:
    moment = _moment(value)
    return _iso(moment) if moment is not None else None


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
    label_is_key: bool = False,
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
    # Only the parser knows whether `label` is an upstream key or display text.
    if label_is_key:
        window["label_is_key"] = True
    return window


def _unique_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    members = dict(pairs)
    if len(members) != len(pairs):
        raise ValueError("repeated member")
    return members


def _not_json(constant: str) -> Any:
    raise ValueError("not a JSON number")


def _load_object(body: object, *, allow_array: bool = False, unique: bool = False) -> Any:
    """Decode a vendor body that must be a JSON object (or array) and not an error envelope.

    ``unique`` is for a body something will be decided on: an object that names
    a member twice, at any depth, says two things and is refused, where an
    ordinary decoder would quietly keep the last. So is a body that carries
    ``NaN`` or an infinity, which is not JSON, anywhere.
    """

    if not isinstance(body, (str, bytes, bytearray)):
        raise SubscriptionQuotaError("malformed")
    size = len(body.encode("utf-8", "surrogatepass")) if isinstance(body, str) else len(body)
    if size > _MAX_BODY_BYTES:
        raise SubscriptionQuotaError("malformed")
    try:
        payload = json.loads(
            body,
            object_pairs_hook=_unique_members if unique else None,
            parse_constant=_not_json if unique else None,
        )
    except (TypeError, UnicodeDecodeError, ValueError, RecursionError):
        raise SubscriptionQuotaError("malformed") from None
    if not (allow_array and isinstance(payload, list)) and (not isinstance(payload, dict) or "error" in payload):
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
        label_is_key = False
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
            display = scope_model or _label(surface.get("display_name"))
            generic = display or _label(upstream_kind)
            if generic is None:
                continue
            label_is_key = display is None
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
                label_is_key=label_is_key,
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
                label_is_key=kind == "other",
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


# Claude's usage report names no plan; its OAuth profile's organization does.
# Claude Code CLI 2.1.280 reads the same profile: `organization_type` names the
# product (claude_max / claude_pro / claude_team / claude_enterprise) and
# `rate_limit_tier` separates the Max tiers. Team seats can carry a Max tier, so
# the tier counts only on a claude_max organization.
_CLAUDE_MAX_TIER_PLANS: Final[Mapping[str, str]] = {
    "default_claude_max_20x": "max_20x",
    "default_claude_max_5x": "max_5x",
}


def parse_claude_plan(body: object) -> Optional[str]:
    """Read the plan from `GET https://api.anthropic.com/api/oauth/profile`; None when unreadable.

    A readable profile always names the product, so a plan read from it replaces
    the last one even when it carries no built-in fee: Team and Enterprise are
    named as themselves, and a Max tier this table does not know is plain `max`.
    None means the read told us nothing — a body we cannot read — and the caller
    keeps the plan it last knew.
    """

    try:
        payload = _load_object(body)
    except SubscriptionQuotaError:
        return None
    organization = payload.get("organization")
    if not isinstance(organization, dict):
        return None
    kind = _label(organization.get("organization_type"))
    if kind is None:
        return None
    kind = kind.lower()
    if kind != "claude_max":
        return kind.removeprefix("claude_") or None
    tier = organization.get("rate_limit_tier")
    return _CLAUDE_MAX_TIER_PLANS.get(tier.strip().lower(), "max") if isinstance(tier, str) else "max"


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
            named = (
                _CODEX_FEATURE_LABELS.get(feature) if isinstance(feature, str) else None
            ) or _label(item.get("limit_name"))
            # Named by `metered_feature` alone, the label is that feature's id.
            scope_model = named or _label(feature)
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
                        label_is_key=named is None,
                    )
                )
    plan = _label(payload.get("plan_type"))
    return {"plan": plan, "windows": _ordered(windows)}


# Antigravity meters Gemini per model group ("Gemini Models", "Claude and GPT
# models"), each with its own 5-hour and weekly bucket; no bucket stops the
# whole account, so every known one is model-scoped.
_GEMINI_WINDOWS: Final[Mapping[str, tuple[QuotaWindowKind, int]]] = {
    "5h": ("model_session", SESSION_WINDOW_SECONDS),
    "weekly": ("model_weekly", WEEKLY_WINDOW_SECONDS),
}
_GEMINI_WINDOW_SUFFIXES: Final = ("-5h", "-weekly", "_5h", "_weekly")
_GEMINI_ROW_ORDER: Final = {"model_session": 0, "model_weekly": 1, "other": 2}


def _gemini_reading(bucket: Mapping[str, Any]) -> Optional[tuple[float, Optional[str]]]:
    """(used %, reset) of one bucket, or None when it carries no reading."""

    resets_at = _iso_timestamp(bucket.get("resetTime"))
    if "remainingFraction" not in bucket:
        # Proto3 JSON omits a zero: a spent bucket arrives with only its reset.
        return (100.0, resets_at) if resets_at is not None else None
    remaining = _number(bucket.get("remainingFraction"))
    if remaining is None:
        return None
    remaining = min(1.0, max(0.0, remaining))
    used = _percent((1.0 - remaining) * 100)
    if used is None:
        return None
    # An untouched bucket reports a rolling "full window from now" reset that
    # moves with every read; it has nothing to give back, so it has no reset.
    return used, None if remaining >= 1.0 else resets_at


def _gemini_group_key(bucket_id: str) -> str:
    for suffix in _GEMINI_WINDOW_SUFFIXES:
        if bucket_id.lower().endswith(suffix) and len(bucket_id) > len(suffix):
            return bucket_id[: -len(suffix)]
    return bucket_id


def parse_gemini_quota(body: object) -> dict[str, Any]:
    """Parse `POST {cloudcode-pa}/v1internal:retrieveUserQuotaSummary`.

    `groups[]` name the models that share their buckets; an older answer lists
    bare top-level `buckets[]`. A group's own name is display text; without one,
    the bucket id (less its window suffix) stands in, marked as a key. An empty
    answer (`{}`, the proto3 form of no groups) is a report of no quota.
    """

    payload = _load_object(body)
    groups = payload.get("groups", [])
    loose = payload.get("buckets", [])
    if not isinstance(groups, list) or not isinstance(loose, list):
        raise SubscriptionQuotaError("malformed")
    rows: list[Mapping[str, Any]] = [group for group in groups[:_MAX_UPSTREAM_ROWS] if isinstance(group, dict)]
    if not any(isinstance(group.get("buckets"), list) and group["buckets"] for group in rows):
        rows = [{"buckets": loose}]
    windows: list[dict[str, Any]] = []
    for group_index, group in enumerate(rows):
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            continue
        group_name = _label(group.get("displayName"))
        group_windows: list[dict[str, Any]] = []
        for index, bucket in enumerate(buckets[:_MAX_UPSTREAM_ROWS]):
            if not isinstance(bucket, dict) or bucket.get("disabled") is True:
                continue
            reading = _gemini_reading(bucket)
            if reading is None:
                continue
            used, resets_at = reading
            bucket_id = _label(bucket.get("bucketId"))
            scope = group_name or (_gemini_group_key(bucket_id) if bucket_id else None)
            if scope is None:
                continue
            window_name = bucket.get("window")
            known = _GEMINI_WINDOWS.get(window_name) if isinstance(window_name, str) else None
            if known is not None:
                kind, seconds = known
                label = scope
            else:
                kind, seconds = "other", None
                named = _label(window_name) or _label(bucket.get("displayName"))
                label = f"{scope} · {named}" if named else scope
            group_windows.append(
                _window(
                    window_id=f"bucket:{bucket_id or f'{group_index}:{index}'}",
                    kind=kind,
                    label=label,
                    used_pct=used,
                    window_seconds=seconds,
                    resets_at=resets_at,
                    scope_model=scope[:_MAX_LABEL_CHARS] if kind != "other" else None,
                    label_is_key=group_name is None,
                )
            )
        # Each group reads 5-hour first, then weekly, then anything unrecognised.
        group_windows.sort(key=lambda window: _GEMINI_ROW_ORDER[window["kind"]])
        windows.extend(group_windows)
    return {"plan": None, "windows": _ordered(windows)}


_GROK_PERIODS: Final[Mapping[str, QuotaWindowKind]] = {"WEEKLY": "weekly", "MONTHLY": "monthly"}


def parse_grok_quota(body: object) -> dict[str, Any]:
    """Parse `GET https://cli-chat-proxy.grok.com/v1/billing?format=credits`.

    One account-wide credit window: weekly or monthly by `currentPeriod.type`,
    resetting at the period's end. Grok omits a zero percentage, so a missing
    one reads as 0 only beside a well-formed weekly or monthly period.
    """

    payload = _load_object(body)
    config = payload.get("config")
    if not isinstance(config, dict):
        raise SubscriptionQuotaError("malformed")
    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
    period_type = period.get("type")
    period_key = period_type.strip().upper().removeprefix("USAGE_PERIOD_TYPE_") if isinstance(period_type, str) else ""
    kind: QuotaWindowKind = _GROK_PERIODS.get(period_key, "other")
    start, end = _moment(period.get("start")), _moment(period.get("end"))
    span = _seconds((end - start).total_seconds()) if start is not None and end is not None else None
    if "creditUsagePercent" in config:
        used = _percent(config["creditUsagePercent"])
    else:
        used = 0.0 if kind != "other" and span is not None else None
    if used is None:
        return {"plan": None, "windows": []}
    if kind != "other":
        label, label_is_key = kind, False
    else:
        label, label_is_key = (_span_label(span), False) if span is not None else ("credit_usage", True)
    window = _window(
        window_id="credits",
        kind=kind,
        label=label,
        used_pct=used,
        window_seconds=span,
        resets_at=_iso(end) if end is not None else None,
        label_is_key=label_is_key,
    )
    return {"plan": None, "windows": [window]}


# The two model-scoped kinds share a rank: a parser emits them model by model,
# and the stable sort keeps each model's windows together.
_KIND_ORDER: Final = {"session": 0, "weekly": 1, "monthly": 2, "model_session": 3, "model_weekly": 3, "other": 4}


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
    if normalized == "gemini":
        return parse_gemini_quota(body)
    if normalized == "xai":
        return parse_grok_quota(body)
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
# The plan read follows the quota read inside the same deadline; it is optional.
CLAUDE_PLAN_FETCH_TIMEOUT_SECONDS: Final = 5.0
_RATE_LIMIT_COOLDOWN_FLOOR: Final = timedelta(minutes=5)
_RATE_LIMIT_COOLDOWN_CEILING: Final = timedelta(hours=1)

QuotaFetch = Callable[[str, str, str], Awaitable[Mapping[str, Any]]]
_CARD_ACCOUNT: Final = re.compile(r"[a-f0-9]{64}")


@dataclass(frozen=True)
class QuotaSourceRef:
    source_id: str
    vendor: str
    credential_ref: str
    display_name: str
    account_label: Optional[str]


@dataclass(frozen=True)
class CardReading:
    """One vendor reading of an account's reset cards, for the decisions that rest on it.

    ``sequence`` orders vendor readings against the moment a card use became
    unknown and ``started_at`` is when this one began, on the monotonic clock;
    ``forced`` says the user asked for it; ``account`` is the opaque key of the
    account the same reading was made under. None of it is serialized.
    """

    sequence: int
    started_at: float
    forced: bool
    account: Optional[str]
    cards: Mapping[str, Any]


@dataclass(frozen=True)
class CardReadingFloor:
    """What a reading must be to answer an account's unknown card use.

    Numbered above ``sequence``, and begun at or after ``ripe_at`` on the
    monotonic clock: the wait is elapsed time, whatever the wall clock does.
    """

    sequence: int
    ripe_at: float


@dataclass
class _QuotaEntry:
    credential_ref: str
    snapshot: Optional[dict[str, Any]] = None
    fetched_at: Optional[datetime] = None
    attempted_at: Optional[datetime] = None
    failure: Optional[QuotaFailure] = None
    cooldown_until: Optional[datetime] = None
    task: Optional[asyncio.Task[None]] = None
    # The reading last started on this entry, whether or not it has answered.
    sequence: int = 0
    forced: bool = False
    started_at: Optional[float] = None
    # The reading the snapshot came from, and the account it was made under.
    snapshot_sequence: int = 0
    snapshot_forced: bool = False
    snapshot_started_at: Optional[float] = None
    card_account: Optional[str] = None


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

    def __init__(
        self,
        fetch: QuotaFetch,
        *,
        now: Callable[[], datetime],
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._fetch = fetch
        self._now = now
        # Elapsed time, for when a reading began; `now` is the wall clock of the throttles.
        self._monotonic = monotonic
        self._entries: dict[str, _QuotaEntry] = {}
        self._fetch_slots = asyncio.Semaphore(QUOTA_MAX_CONCURRENT_FETCHES)
        self._sequence = itertools.count(1)

    def tick(self) -> int:
        """A point in the order of vendor readings: every reading started later is numbered above it."""

        return next(self._sequence)

    async def summary(
        self,
        sources: Sequence[QuotaSourceRef],
        *,
        force: bool = False,
        answering: Optional[Mapping[str, CardReadingFloor]] = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """Report every Source, re-reading the ones that are due.

        ``answering`` is for a forced read while card uses are unknown: the
        floor of each such account, by its opaque key. Once an account's floor
        can be met, its Sources need a reading the user asked for that meets
        it, so the forced-refresh interval does not hold that one back. A
        vendor's rate-limit cooldown still does.

        ``wait=False`` reports what is known now and names the Sources still
        being read in ``pending``, for an answer that must not wait on them.
        """

        live = {source.source_id for source in sources}
        for source_id in list(self._entries):
            if source_id not in live:
                self._entries.pop(source_id)
        pending = [
            task
            for source in sources
            if (task := self._schedule(source, force=force, answering=answering)) is not None
        ]
        if pending and wait:
            # A slow vendor must not hold the page: whatever has not answered by
            # the deadline keeps running and lands in the next read.
            await asyncio.wait(pending, timeout=QUOTA_READ_DEADLINE_SECONDS)
        payload: dict[str, Any] = {
            "refresh_interval_seconds": int(QUOTA_REFRESH_INTERVAL.total_seconds()),
            "sources": [self._payload(source) for source in sources],
        }
        # Named so the page can re-read shortly instead of showing a gap as a failure.
        still_reading = [
            source.source_id
            for source in sources
            if (entry := self._entries.get(source.source_id)) is not None
            and entry.task is not None
            and not entry.task.done()
        ]
        if still_reading:
            payload["pending"] = still_reading
        return payload

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

    def _schedule(
        self,
        source: QuotaSourceRef,
        *,
        force: bool,
        answering: Optional[Mapping[str, CardReadingFloor]] = None,
    ) -> Optional[asyncio.Task[None]]:
        if source.vendor not in QUOTA_VENDORS:
            return None
        entry = self._entry(source)
        now = self._now()
        floor: Optional[CardReadingFloor] = None
        if force and answering and source.vendor in RESET_CARD_VENDORS:
            if entry.card_account is not None:
                # Its own account's unknown use, and nobody else's.
                floor = answering.get(entry.card_account)
            else:
                # Not read under any account yet: held to what would answer every one
                # of them. Until then the ordinary refresh limit applies to it.
                floor = CardReadingFloor(
                    sequence=max(each.sequence for each in answering.values()),
                    ripe_at=max(each.ripe_at for each in answering.values()),
                )
        # Whether this Source owes the user a reading that can answer an unknown card
        # use: the floor can be met by now, and the reading last started here does not.
        owed = (
            floor is not None
            and self._monotonic() >= floor.ripe_at
            and not (
                entry.forced
                and entry.sequence > floor.sequence
                and entry.started_at is not None
                and entry.started_at >= floor.ripe_at
            )
        )
        if entry.task is not None and not entry.task.done():
            if not owed:
                # Concurrent reads, and repeated refreshes of one unknown use, share one fetch.
                return entry.task
            # The reading in flight began too early to answer; it lands on the detached entry.
            entry = self._detach(source, entry)
        if entry.cooldown_until is not None and now < entry.cooldown_until:
            return None
        interval = QUOTA_FORCED_REFRESH_INTERVAL if force else QUOTA_REFRESH_INTERVAL
        if not owed and entry.attempted_at is not None and now - entry.attempted_at < interval:
            return None
        return self._start(source, entry, forced=force)

    def _detach(self, source: QuotaSourceRef, previous: _QuotaEntry) -> _QuotaEntry:
        """A fresh entry that keeps what was last read and waits for none of what is in flight."""

        entry = _QuotaEntry(
            credential_ref=source.credential_ref,
            snapshot=previous.snapshot,
            fetched_at=previous.fetched_at,
            snapshot_sequence=previous.snapshot_sequence,
            snapshot_forced=previous.snapshot_forced,
            snapshot_started_at=previous.snapshot_started_at,
            card_account=previous.card_account,
        )
        self._entries[source.source_id] = entry
        return entry

    def _start(self, source: QuotaSourceRef, entry: _QuotaEntry, *, forced: bool) -> asyncio.Task[None]:
        # A reading begins here for the rules that rest on it; its vendor request follows.
        entry.sequence, entry.forced, entry.started_at = self.tick(), forced, self._monotonic()
        entry.task = asyncio.create_task(
            self._refresh(source, entry, sequence=entry.sequence, forced=forced, started_at=entry.started_at),
            name=f"model-hub-quota-{source.source_id}",
        )
        return entry.task

    async def _refresh(
        self,
        source: QuotaSourceRef,
        entry: _QuotaEntry,
        *,
        sequence: int,
        forced: bool,
        started_at: float,
    ) -> None:
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
        plan = parsed.get("plan")
        if plan is None and entry.snapshot is not None:
            # No plan means this read could not name one — Claude's is a best-effort
            # second read — so it must not blank the last. A named plan, one without
            # a built-in fee included, replaces it.
            plan = entry.snapshot["plan"]
        entry.snapshot = {"plan": plan, "windows": windows}
        if "reset_cards" in parsed:
            entry.snapshot["reset_cards"] = parsed["reset_cards"]
        account = parsed.get("reset_card_account")
        entry.card_account = account if isinstance(account, str) and _CARD_ACCOUNT.fullmatch(account) else None
        entry.snapshot_sequence, entry.snapshot_forced, entry.snapshot_started_at = sequence, forced, started_at
        entry.fetched_at = self._now()
        entry.failure = None
        entry.cooldown_until = None

    async def reread(self, source: QuotaSourceRef) -> dict[str, Any]:
        """Read one Source again for a card use: its own vendor request, never a cached or shared one.

        Empty when the Source was re-authenticated or read again meanwhile, so
        that what came back cannot be taken for this Source's current account.
        """

        previous = self._entries.get(source.source_id)
        if previous is not None and previous.credential_ref == source.credential_ref:
            entry = self._detach(source, previous)
        else:
            entry = _QuotaEntry(credential_ref=source.credential_ref)
            self._entries[source.source_id] = entry
        await asyncio.shield(self._start(source, entry, forced=True))
        if self._entries.get(source.source_id) is not entry:
            return {}
        return self._payload(source)

    def card_reading(self, source: QuotaSourceRef) -> Optional[CardReading]:
        """The current reading of this Source's cards, or None when its last attempt did not answer."""

        entry = self._entries.get(source.source_id)
        if (
            entry is None
            or entry.credential_ref != source.credential_ref
            or entry.snapshot is None
            or entry.snapshot_started_at is None
            or entry.failure is not None
            or "reset_cards" not in entry.snapshot
        ):
            return None
        return CardReading(
            sequence=entry.snapshot_sequence,
            started_at=entry.snapshot_started_at,
            forced=entry.snapshot_forced,
            account=entry.card_account,
            cards=entry.snapshot["reset_cards"],
        )

    def card_account(self, source: QuotaSourceRef) -> Optional[str]:
        """The account this Source was last read under. For display only: it decides nothing."""

        entry = self._entries.get(source.source_id)
        if entry is None or entry.credential_ref != source.credential_ref:
            return None
        return entry.card_account

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
        if source.vendor in RESET_CARD_VENDORS:
            # Cards are current or unread: a failed attempt never shows the last count as today's.
            snapshot = entry.snapshot if entry and entry.snapshot and not entry.failure else {}
            payload["reset_cards"] = snapshot.get("reset_cards") or {"state": "unread", "lock": None, "cards": []}
        failure = entry.failure if entry is not None else None
        if failure is None:
            if entry is None or entry.snapshot is None:
                return {**payload, "state": "error", "error_key": "models.quota.error.unavailable"}
            return {**payload, "state": "ok"}
        if failure == "auth_expired":
            return {**payload, "state": "auth_expired", "error_key": "models.quota.error.auth_expired"}
        state = "stale" if entry is not None and entry.snapshot is not None else "error"
        return {**payload, "state": state, "error_key": f"models.quota.error.{failure}"}
