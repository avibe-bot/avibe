"""Closed, credential-free projections of vendors' reset-card inventories.

A projection is the vendor's own recognised report, or it is not a projection:
anything else raises ``malformed`` and is shown as ``unread``. Whether a card was
used, and whether an unknown outcome may be lifted, are decided on these counts,
so this parser is strict where the window parser beside it is forgiving:

* A body is recognised by what only the vendor's report carries (its card
  block, or the members its usage report always names), never by looking
  like something that could be rendered.
* No member is named twice, at any depth.
* Every field that can remove a row or change a count has its type checked
  before it is used. A row is left out because the vendor says it is not held
  (none left, paused, not started, expired), never because it could not be read.
* An answer does not contradict what was asked or itself in a field it carries:
  a usage report that names an account names the one it was asked for, a total
  carried twice is the same total, and one grant is one row. A field an answer
  leaves out is not required.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .quota import RESET_CARD_VENDORS, SubscriptionQuotaError, _iso, _load_object, _moment, _number


VENDORS = RESET_CARD_VENDORS

# The two account-wide windows Claude's usage report always names, null when idle.
_CLAUDE_USAGE_WINDOWS = ("five_hour", "seven_day")


def unread() -> dict:
    return {"state": "unread", "lock": None, "cards": []}


def _ok(cards: list) -> dict:
    return {"state": "ok", "lock": None, "cards": cards}


def _malformed() -> SubscriptionQuotaError:
    return SubscriptionQuotaError("malformed")


def _typed(row: dict, key: str, kind: type, *, required: bool = False) -> Any:
    """``row[key]`` of exactly ``kind``; None when absent or null and not required; malformed otherwise."""

    value = row.get(key)
    if value is None:
        if required:
            raise _malformed()
        return None
    if type(value) is not kind:
        raise _malformed()
    return value


def _time(row: dict, key: str) -> datetime | None:
    if row.get(key) is None:
        return None
    time = _moment(row[key])
    if time is None:
        raise _malformed()
    return time


def _count(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise _malformed()
    return value


def _rows(value: Any) -> list[dict]:
    if type(value) is not list or not all(type(row) is dict for row in value):
        raise _malformed()
    return value


def _strings(row: dict, key: str) -> list[str]:
    values = _typed(row, key, list) or []
    if not all(type(value) is str for value in values):
        raise _malformed()
    return values


def _card(kind, count, expiry, stated, blocked, until=None):
    return dict(
        kind=kind,
        count=count,
        expires_at=_iso(expiry) if expiry else None,
        stated=stated,
        usable=blocked is None,
        blocked=blocked,
        blocked_until=_iso(until) if blocked == "cooldown" and until else None,
    )


def _is_claude_usage_report(report: dict) -> bool:
    """Whether a body without a card block is Claude's own usage report.

    Deliberately not ``parse_claude_quota``: that renders anything shaped like
    a window so that a new one shows, which makes it no proof of whose answer
    this is.
    """

    for name in _CLAUDE_USAGE_WINDOWS:
        if name not in report:
            return False
        window = report[name]
        if window is None:
            continue
        if type(window) is not dict or "utilization" not in window:
            return False
        if window["utilization"] is not None and _number(window["utilization"]) is None:
            return False
    return True


def claude_cards(body: object, now: datetime, *, candidates: list | None = None) -> dict:
    """Project Claude's card block; ``candidates`` collects (kind, expiry, handle) of usable grants.

    ``now`` is the moment the body was received: expiry is judged against it.
    """

    report = _load_object(body, unique=True)
    if "cedar_ember" not in report:
        # The vendor leaves the block out for an account that holds no cards. Only
        # its own usage report can say so; an empty or foreign object says nothing.
        if not _is_claude_usage_report(report):
            raise _malformed()
        return _ok([])
    block = report["cedar_ember"]
    if block is None:
        # Named and null: the vendor's own statement that none are held.
        return _ok([])
    if type(block) is not dict:
        raise _malformed()
    eligible = _typed(block, "eligible", bool, required=True)
    at_limit = _typed(block, "at_limit", bool)
    cooldown = _time(block, "cooldown_until")
    seen: set[str] = set()
    groups: dict[str, list] = {}
    for row in _rows(block.get("grants")):
        # Read every field before any of them decides: a row is never dropped unread.
        count = _count(row.get("resets_left"))
        start, end = _time(row, "starts_at"), _time(row, "ends_at")
        paused = _typed(row, "paused", bool)
        declared = _typed(row, "type", str)
        gives_back_weekly = _typed(row, "gives_back_weekly", bool)
        usable_now = _typed(row, "usable_now", bool)
        requires_limit = _typed(row, "use_requires_limit", bool)
        blocking = _typed(row, "blocking", list)
        clears = _strings(row, "clears")
        # Before any row is left out: two rows for one grant, one held and one not,
        # are two answers about one card.
        grant_id = _typed(row, "id", str, required=True)
        if not grant_id.strip() or grant_id in seen:
            raise _malformed()
        seen.add(grant_id)
        if paused or not count or (start and start > now) or (end and end <= now):
            continue
        if gives_back_weekly:
            kind = "fable"
        elif declared == "full":
            kind = "all"
        elif declared in ("weekly", "session"):
            kind = declared
        else:
            session = "five_hour" in clears
            other = any(value != "five_hour" for value in clears)
            kind = "session" if session and not other else "weekly" if other and not session else "all"
        if not eligible:
            blocked = "unavailable"
        elif cooldown and cooldown > now:
            blocked = "cooldown"
        elif requires_limit is not False and at_limit is False:
            blocked = "needs_limit"
        elif usable_now is True and not blocking and (requires_limit is False or at_limit is True):
            blocked = None
        else:
            blocked = "unavailable"
        stated = kind != "all" or ("five_hour" in clears and "seven_day" in clears)
        if blocked is None and candidates is not None:
            candidates.append((kind, end, grant_id))
        groups.setdefault(kind, []).append((count, end, stated, blocked))
    cards = []
    for kind, grants in sorted(groups.items()):
        blocked = next(
            reason for reason in (None, "cooldown", "needs_limit", "unavailable") if any(g[3] == reason for g in grants)
        )
        cards.append(
            _card(
                kind,
                _count(sum(g[0] for g in grants)),
                min((g[1] for g in grants if g[1]), default=None),
                all(g[2] for g in grants),
                blocked,
                cooldown,
            )
        )
    return _ok(cards)


def codex_count(body: object, account: object) -> int:
    """How many reset credits ChatGPT's usage report says are available to ``account``.

    ``account`` is the account the report was asked for, as the grant names it.
    It is compared here and goes nowhere else.
    """

    report = _load_object(body, unique=True)
    # Codex's usage report always names the plan. A body that does not is not that
    # report, whatever else it holds, and says nothing about credits.
    plan = _typed(report, "plan_type", str, required=True)
    if not plan.strip():
        raise _malformed()
    # A report that names an account, the empty name included, names the one it was
    # asked for: otherwise this is another account's count. Left out or null, it names none.
    if _typed(report, "account_id", str) not in (None, account):
        raise _malformed()
    _typed(report, "rate_limit", dict)
    block = _typed(report, "rate_limit_reset_credits", dict)
    if block is None:
        return 0
    return _count(block.get("available_count"))


def codex_cards(count: int, details: object, now: datetime, *, candidates: list | None = None) -> dict:
    """Project ChatGPT's credits; ``candidates`` collects (kind, expiry, handle) of available ones.

    ``count`` is the usage report's own; the details only say which credits can
    be used and when they expire. ``now`` is the moment the details were received.
    """

    if not count:
        return _ok([])
    envelope = _load_object(details, allow_array=True, unique=True)
    if type(envelope) is list:
        rows = _rows(envelope)
    else:
        # A total the details carry themselves is the usage report's, or the vendor's
        # two totals disagree and neither is a count to decide on.
        for total in ("available_count", "availableCount"):
            if envelope.get(total) is not None and _count(envelope[total]) != count:
                raise _malformed()
        rows = _rows(
            next(
                (
                    envelope[k]
                    for k in ("credits", "rate_limit_reset_credits", "items", "data")
                    if envelope.get(k) is not None
                ),
                None,
            )
        )
    expires = []
    for row in rows:
        reset_type = _typed(row, "reset_type" if "reset_type" in row else "resetType", str)
        status = _typed(row, "status", str)
        end = _time(row, "expires_at" if "expires_at" in row else "expiresAt")
        credit_id = next((_typed(row, key, str) for key in ("id", "credit_id", "creditId") if key in row), None)
        if reset_type != "codex_rate_limits" or status != "available":
            continue
        if (not end or end > now) and credit_id and credit_id.strip():
            expires.append(end)
            if candidates is not None:
                candidates.append(("account", end, credit_id))
    return _ok(
        [
            _card(
                "account",
                count,
                min((end for end in expires if end), default=None),
                False,
                None if expires else "unavailable",
            )
        ]
    )
