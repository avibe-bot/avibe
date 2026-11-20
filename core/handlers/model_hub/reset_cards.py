"""Closed, credential-free projections of vendors' reset-card inventories."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .quota import SubscriptionQuotaError, _iso, _load_object, _moment


VENDORS = frozenset({"anthropic", "codex", "openai"})


def unread() -> dict:
    return {"state": "unread", "lock": None, "cards": []}


def _ok(cards: list) -> dict:
    return {"state": "ok", "lock": None, "cards": cards}


def _time(row: dict, key: str) -> datetime | None:
    if row.get(key) is None:
        return None
    time = _moment(row[key])
    if time is None:
        raise SubscriptionQuotaError("malformed")
    return time


def _count(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise SubscriptionQuotaError("malformed")
    return value


def _rows(value: Any) -> list[dict]:
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise SubscriptionQuotaError("malformed")
    return value


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


def claude_cards(body: object, now: datetime) -> dict:
    block = _load_object(body).get("cedar_ember")
    if block is None:
        return _ok([])
    if not isinstance(block, dict):
        raise SubscriptionQuotaError("malformed")
    cooldown = _time(block, "cooldown_until")
    seen: set[str] = set()
    groups: dict[str, list] = {}
    for row in _rows(block.get("grants")):
        count = _count(row.get("resets_left"))
        start, end = _time(row, "starts_at"), _time(row, "ends_at")
        if row.get("paused", False) is not False or not count or (start and start > now) or (end and end <= now):
            continue
        grant_id = row.get("id")
        if not isinstance(grant_id, str) or not grant_id.strip() or grant_id in seen:
            raise SubscriptionQuotaError("malformed")
        seen.add(grant_id)
        clears = row.get("clears", [])
        if not isinstance(clears, list) or not all(isinstance(v, str) for v in clears):
            raise SubscriptionQuotaError("malformed")
        kind = row.get("type")
        if row.get("gives_back_weekly") is True:
            kind = "fable"
        elif kind == "full":
            kind = "all"
        elif kind not in ("weekly", "session"):
            session = "five_hour" in clears
            other = any(value != "five_hour" for value in clears)
            kind = "session" if session and not other else "weekly" if other and not session else "all"
        if block.get("eligible") is not True:
            blocked = "unavailable"
        elif cooldown and cooldown > now:
            blocked = "cooldown"
        elif row.get("use_requires_limit", True) is True and block.get("at_limit") is False:
            blocked = "needs_limit"
        elif (
            row.get("usable_now") is True
            and row.get("blocking", []) == []
            and (row.get("use_requires_limit") is False or block.get("at_limit") is True)
        ):
            blocked = None
        else:
            blocked = "unavailable"
        stated = kind != "all" or ("five_hour" in clears and "seven_day" in clears)
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


def codex_count(body: object) -> int:
    block = _load_object(body).get("rate_limit_reset_credits")
    if block is None:
        return 0
    if not isinstance(block, dict):
        raise SubscriptionQuotaError("malformed")
    return _count(block.get("available_count"))


def codex_cards(count: int, details: object, now: datetime) -> dict:
    if not count:
        return _ok([])
    envelope = _load_object(details, allow_array=True)
    if isinstance(envelope, list):
        rows = _rows(envelope)
    else:
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
        if row.get("reset_type", row.get("resetType")) != "codex_rate_limits" or row.get("status") != "available":
            continue
        end = _time(row, "expires_at" if "expires_at" in row else "expiresAt")
        credit_id = row.get("id", row.get("credit_id", row.get("creditId")))
        if (not end or end > now) and isinstance(credit_id, str) and credit_id.strip():
            expires.append(end)
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
