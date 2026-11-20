"""The one vendor request that uses a reset card, and how its answer is read.

Runs on an engine worker thread. The engine holds the grant and substitutes it
into the call; the card's handle and the account's identity are read, used and
dropped here. What leaves is closed: a statement about the request, the count
read just before it, and the limits the vendor named as cleared.

Two rules shape everything below.

* The write is sent at most once. Everything before it is a read, and a failed
  read means nothing was sent (``no_card``). After it there is no second
  attempt and no other card: whatever cannot be read as the vendor's own
  recognised answer is ``unknown``.
* A statement is only what the vendor said, read strictly. Whether a card was
  actually spent is decided by the controller from a fresh count. An answer is
  ``unknown`` here, so that it can never be half of that agreement, when it
  names a member twice, when a field has the wrong type, when its result and
  reason are not a known pair, and when a refusal carries anything that could
  be evidence of a redemption, including a member this client does not know.

The vendors' vocabulary comes from other clients of the same endpoints, not from
a live call: no card was used to build this.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from uuid import UUID

from core.handlers.model_hub import reset_cards
from core.handlers.model_hub.quota import QUOTA_FETCH_TIMEOUT_SECONDS, _load_object, _moment


CLAUDE = "https://api.anthropic.com/api"
CHATGPT = "https://chatgpt.com/backend-api/wham"
LIMITS = ("five_hour", "seven_day", "seven_day_overage_included")
# How long the one use request may take, as other clients of these endpoints allow.
# Giving up early does not stop the vendor: it only turns an answer into "unknown".
USE_REQUEST_TIMEOUT_SECONDS = 25.0

NO_CARD = "no_card"
UNKNOWN = "unknown"
ACK = "ack"
REFUSALS = frozenset({"not_limited", "cooldown", "refused", "unavailable"})

# Vendor-provided handles go into a JSON body: no control characters, delimiters
# or engine substitution markers.
_HANDLE = re.compile(r"[A-Za-z0-9_-]{1,256}")
# Anthropic's own bound on the request id.
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

# Anthropic answers `{result, reason?, cleared?, cooldown_until?}`. A pair not
# listed is unknown: an unrecognised or indeterminate reason
# (`stamp_indeterminate`, `reset_unconfirmed`), a success that carries a refusal,
# and a bare `unavailable`, which other clients also read as "may have happened".
# So is `already_used`: this request id was never sent before, so "already" can
# only mean a card went just now, through a repeat of this request on the way or
# through another client. Neither is "this press spent nothing".
_CLAUDE_REFUSAL_REASONS = ("not_next_grant", "unknown_grant", "no_grant", "grant_id_required", "tenure")
_CLAUDE_STATEMENTS: Mapping[tuple[str, Optional[str]], str] = {
    ("reset", None): ACK,
    ("not_limited", None): "not_limited",
    ("cooldown", None): "cooldown",
    ("ineligible", None): "refused",
    **{
        (result, reason): "refused"
        for result in ("ineligible", "unavailable")
        for reason in _CLAUDE_REFUSAL_REASONS
    },
}

# ChatGPT answers `{code, credit?: {status?, redeemed_at?, ...}, windows_reset?}`. Its
# `already_redeemed` is unknown for the same reason as Anthropic's `already_used`.
_CHATGPT_STATEMENTS: Mapping[str, str] = {
    "reset": ACK,
    "nothing_to_reset": "not_limited",
    "no_credit": "unavailable",
}

# What a refusal may carry. Anything else beside it might be evidence that a card
# went after all, in a member this client cannot read; that is not a refusal to
# rely on.
_CLAUDE_REFUSAL_MEMBERS = frozenset({"result", "reason", "cleared", "cooldown_until"})
_CHATGPT_REFUSAL_MEMBERS = frozenset({"code", "windows_reset", "credit"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def binding_key(vendor: str, identity: object) -> str:
    """The opaque key of a vendor account: stable across Sources, useless as an identifier."""

    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("no account identity")
    namespace = "anthropic" if vendor == "anthropic" else "chatgpt"
    return hashlib.sha256(f"{namespace}\0{identity}".encode()).hexdigest()


def claude_organization(profile: object) -> str:
    """The canonical organization uuid of an OAuth profile body; raises when it names none, or names it twice."""

    return str(UUID(_load_object(profile, unique=True)["organization"]["uuid"]))


def account_key_or_none(vendor: str, *, profile: object = None, account_id: object = None) -> Optional[str]:
    """The account a quota reading was made under, or None when the reading cannot say."""

    try:
        return binding_key(vendor, claude_organization(profile) if vendor == "anthropic" else account_id)
    except Exception:
        return None


def _call(client, auth, headers: Mapping[str, str], method: str, url: str, data: Optional[dict] = None) -> object:
    """One engine `/api-call`; the body of a 200, or an exception that carries no upstream text."""

    request: dict[str, Any] = {"auth_index": auth.auth_index, "method": method, "url": url, "header": dict(headers)}
    timeout = QUOTA_FETCH_TIMEOUT_SECONDS
    if data is not None:
        request["data"] = json.dumps(data, separators=(",", ":"))
        request["header"]["Content-Type"] = "application/json"
        timeout = USE_REQUEST_TIMEOUT_SECONDS
    reply = client.management_request("POST", "/api-call", payload=request, timeout=timeout)
    status = reply.get("status_code") if isinstance(reply, dict) else None
    if type(status) is not int or status != 200:
        raise ValueError("no vendor answer")
    return reply.get("body")


def account_key(*, client, auth, vendor: str, headers: Mapping[str, str]) -> str:
    """Read which account this grant belongs to, fresh. Raises when it cannot be read."""

    if vendor == "anthropic":
        return binding_key(vendor, claude_organization(_call(client, auth, headers, "GET", f"{CLAUDE}/oauth/profile")))
    return binding_key(vendor, auth.account_id)


def _optional(value: object, kind: type) -> bool:
    return value is None or type(value) is kind


def _claude_statement(result: Mapping[str, Any]) -> tuple[str, list[str]]:
    status, reason = result.get("result"), result.get("reason")
    cleared, cooldown = result.get("cleared"), result.get("cooldown_until")
    if (
        type(status) is not str
        or not _optional(reason, str)
        or not _optional(cleared, list)
        or not all(type(limit) is str for limit in cleared or ())
        or not (cooldown is None or _moment(cooldown) is not None)
    ):
        return UNKNOWN, []
    statement = _CLAUDE_STATEMENTS.get((status, reason or None), UNKNOWN)
    if statement != ACK and (cleared or not _CLAUDE_REFUSAL_MEMBERS.issuperset(result)):
        # A refusal that also cleared a limit, or says more than a refusal says.
        return UNKNOWN, []
    return statement, [limit for limit in LIMITS if limit in (cleared or ())]


def _chatgpt_statement(result: Mapping[str, Any]) -> tuple[str, list[str]]:
    code, windows, credit = result.get("code"), result.get("windows_reset"), result.get("credit")
    if type(code) is not str or not _optional(windows, int) or (windows or 0) < 0 or not _optional(credit, dict):
        return UNKNOWN, []
    statement = _CHATGPT_STATEMENTS.get(code, UNKNOWN)
    if statement == ACK:
        status, redeemed = (credit or {}).get("status"), (credit or {}).get("redeemed_at")
        if (
            windows == 0
            or not _optional(status, str)
            or status == "available"
            or (redeemed is not None and _moment(redeemed) is None)
        ):
            # A reset that reset nothing, or whose credit is unreadable or still available.
            return UNKNOWN, []
    elif windows or credit is not None or not _CHATGPT_REFUSAL_MEMBERS.issuperset(result):
        # "Nothing was redeemed" beside a credit, a window that was reset, or more
        # than a refusal says. A credit's status alone can say it was redeemed.
        return UNKNOWN, []
    return statement, []


def statement_from_reply(vendor: str, result: object, before: int) -> dict[str, Any]:
    """Reduce a vendor's answer to the use request to a closed statement.

    Types are checked, only recognised combinations are statements, and an
    answer whose parts disagree is ``unknown``.
    """

    if not isinstance(result, dict):
        return {"statement": UNKNOWN}
    statement, cleared = (_claude_statement if vendor == "anthropic" else _chatgpt_statement)(result)
    if statement == UNKNOWN:
        return {"statement": UNKNOWN}
    return {"statement": statement, "before": before, "cleared": cleared}


def _prepare(*, client, auth, vendor: str, kind: str, request_id: str, headers, expected_key: str):
    """Read the inventory, pick the card and name the request. Reads only: nothing is used here."""

    if not _REQUEST_ID.fullmatch(request_id):
        raise ValueError("request id")
    candidates: list[tuple[str, Optional[datetime], str]] = []
    # Expiry is judged when each inventory arrives, not when it was asked for.
    if vendor == "anthropic":
        usage = _call(client, auth, headers, "GET", f"{CLAUDE}/oauth/usage?cedar_ember=1&skip_spend=1")
        inventory = reset_cards.claude_cards(usage, _utcnow(), candidates=candidates)
    elif vendor in ("codex", "openai"):
        count = reset_cards.codex_count(_call(client, auth, headers, "GET", f"{CHATGPT}/usage"))
        details = _call(client, auth, headers, "GET", f"{CHATGPT}/rate-limit-reset-credits") if count else None
        inventory = reset_cards.codex_cards(count, details, _utcnow(), candidates=candidates)
    else:
        raise ValueError("vendor")
    before = next(card["count"] for card in inventory["cards"] if card["kind"] == kind and card["usable"])
    # The card the user chose by kind: usable now, earliest to expire. Never the vendor's
    # "next" card in its place, and never a second choice after a refusal.
    far = datetime.max.replace(tzinfo=timezone.utc)
    expires, handle = min(
        (end or far, handle) for name, end, handle in candidates if name == kind and _HANDLE.fullmatch(handle)
    )
    # The claim is on one account: the grant must still be that account's, read now.
    if vendor == "anthropic":
        organization = claude_organization(_call(client, auth, headers, "GET", f"{CLAUDE}/oauth/profile"))
        if binding_key(vendor, organization) != expected_key:
            raise ValueError("another account")
        url = f"{CLAUDE}/organizations/{organization}/reset_rate_limits"
        data = {"program": "cedar_ember", "grant_id": handle, "request_id": request_id}
    else:
        if binding_key(vendor, auth.account_id) != expected_key:
            raise ValueError("another account")
        url = f"{CHATGPT}/rate-limit-reset-credits/consume"
        data = {"redeem_request_id": request_id, "credit_id": handle}
    if expires <= _utcnow():
        # It ran out during the reads above: not sent, rather than sent to be refused.
        raise ValueError("expired")
    return url, data, before


def use_once(*, client, auth, vendor: str, kind: str, request_id: str, headers, expected_key: str) -> dict[str, Any]:
    """Use one card of ``kind``. Returns a closed statement; never raises, retries or echoes the vendor."""

    try:
        url, data, before = _prepare(
            client=client,
            auth=auth,
            vendor=vendor,
            kind=kind,
            request_id=request_id,
            headers=headers,
            expected_key=expected_key,
        )
    except Exception:
        # Only reads were made: no card of that kind can be used now, and nothing was sent.
        return {"statement": NO_CARD}
    try:
        # The only write, made once. A transport error, a timeout, a status other than
        # 200 or an unreadable body leaves the outcome unknown; none of them is retried.
        result = _load_object(_call(client, auth, headers, "POST", url, data), unique=True)
    except Exception:
        return {"statement": UNKNOWN}
    try:
        return statement_from_reply(vendor, result, before)
    except Exception:
        return {"statement": UNKNOWN}
