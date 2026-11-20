"""Using a reset card: one press, at most one vendor request, an outcome that is never guessed.

A card is the user's consumable and using one cannot be undone, so this module
is written around what must not happen:

* Two cards for one intention. A press is one call into the adapter's use,
  never repeated. A press is being handled from the moment it arrives until its
  answer is assembled and leaves this module, whatever has been settled
  meanwhile. Presses are admitted in arrival order: a press yields to every
  earlier press that was still being handled when it arrived and turns out to be
  on the same account, however late either account could be named (two Sources
  can share one). A press also says how many cards of its kind the page was
  showing; if a fresh reading shows another number the page was out of date (a
  second tab, after the first one's use) and the press is refused before
  anything is claimed or sent. That reading may itself lag, so the adapter is
  given the page's count and compares it once more with its own inventory read,
  the last before the request; if they differ nothing is sent and the claim is
  released. The account itself is claimed durably before anything is sent
  (``card_fences``), which covers a reload and a restart.
* "Used" or "not used" said wrongly. Either needs the vendor's own recognised
  statement and a fresh, recognised count of that same account that agrees with
  it. Everything else is ``unconfirmed``: the account stays locked.
* An unknown outcome that goes away by itself, or too soon. Only a reading the
  user asked for, begun after the outcome became unknown and at least a minute
  later (``card_fences.UNKNOWN_OUTCOME_WAIT``), recognised and made under that
  same account, lifts the lock.
* A use that surfaces as a generic error. Once the claim is taken a press ends
  as "no usable card" with the claim released, or as the closed use result.

Nothing here runs on behalf of a timer, an Agent or the CLI: the one caller is
the Web route, through the private controller route that carries it. No vendor
handle, account identity or upstream text reaches this module; the account is an
opaque key and the adapter's reply is a closed statement.

The fence is read and written on worker threads, and ``_mutation_lock`` is not
held while the vendor is called.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Optional

from .card_fences import IN_FLIGHT, KINDS, UNCONFIRMED, FenceUnavailable, FenceView
from .quota import QUOTA_REFRESH_INTERVAL, CardReading, CardReadingFloor, QuotaSourceRef, _iso
from .reset_cards import VENDORS

if TYPE_CHECKING:
    from .service import ModelHubService

logger = logging.getLogger(__name__)

LIMITS = ("five_hour", "seven_day", "seven_day_overage_included")
_REFUSALS = frozenset({"not_limited", "cooldown", "refused", "unavailable"})
# The adapter's statements that nothing was sent, and the refusal each press ends in.
_UNSENT = {"no_card": "reset_card_unavailable", "count_changed": "reset_card_count_changed"}
_ACCOUNT = re.compile(r"[a-f0-9]{64}")
_MAX_SOURCE_ID_CHARS = 128
_MAX_COUNT = 2**53 - 1


@dataclass(eq=False)
class Press:
    """One use being handled, from the moment it arrives until its outcome is settled."""

    source_id: str
    kind: str
    # How many cards of that kind the page was showing when the user confirmed.
    count: int
    since: str
    # The account this press is on, once it could be named; None when it could not.
    account: "asyncio.Future[Optional[str]]"
    # Its outcome is settled and its answer is being assembled. The summary then
    # shows what the fence holds; admission stays closed until the answer has left.
    settled: bool = False


def _error(code: str, status: int) -> Exception:
    from .service import ModelHubError

    return ModelHubError(code, status=status)


def checked_selection(payload: object) -> tuple[str, int]:
    """The kind and count of a use request, whose body is exactly ``{"kind": <kind>, "count": <positive integer>}``.

    ``count`` is how many cards of that kind the page was showing. The Web route
    and the controller both call this, so nothing else is ever carried between
    them or parsed on the controller's loop.
    """

    if (
        type(payload) is not dict
        or set(payload) != {"kind", "count"}
        or type(payload["kind"]) is not str
        or payload["kind"] not in KINDS
        or type(payload["count"]) is not int
        or not 1 <= payload["count"] <= _MAX_COUNT
    ):
        raise _error("invalid_parameter", 400)
    return payload["kind"], payload["count"]


def checked_source_id(source_id: object) -> str:
    if type(source_id) is not str or not 0 < len(source_id) <= _MAX_SOURCE_ID_CHARS:
        raise _error("invalid_parameter", 400)
    return source_id


def unconfirmed_use(kind: str) -> dict[str, Any]:
    """The use result when it is not known whether a card was spent."""

    return {"kind": kind, "outcome": "unconfirmed", "reason": None, "cleared": []}


def empty_summary() -> dict[str, Any]:
    """A quota summary that reports nothing, for a use result whose summary could not be read."""

    return {"refresh_interval_seconds": int(QUOTA_REFRESH_INTERVAL.total_seconds()), "sources": []}


def _held(cards: Mapping[str, Any]) -> Optional[dict[str, int]]:
    """Cards held per kind, from an inventory the vendor's own report established; None for any other."""

    rows = cards.get("cards")
    if cards.get("state") != "ok" or type(rows) is not list:
        return None
    held: dict[str, int] = {}
    for row in rows:
        if (
            type(row) is not dict
            or row.get("kind") not in KINDS
            or row["kind"] in held
            or type(row.get("count")) is not int
            or row["count"] < 1
        ):
            return None
        held[row["kind"]] = row["count"]
    return held


def _decide(kind: str, reply: Mapping[str, Any], reading: Optional[CardReading], account: str) -> dict[str, Any]:
    """What a press may tell the user: the vendor's statement, if a fresh count of the same account agrees."""

    result = unconfirmed_use(kind)
    statement, before, cleared = reply.get("statement"), reply.get("before"), reply.get("cleared", [])
    if type(before) is not int or before < 1 or type(cleared) is not list:
        return result
    # The count must be the vendor's own report, read after the request, under this account.
    if reading is None or reading.account != account:
        return result
    held = _held(reading.cards)
    if held is None:
        return result
    after = held.get(kind, 0)
    if statement == "ack" and after < before:
        result.update(outcome="used", cleared=[limit for limit in LIMITS if limit in cleared])
    elif statement in _REFUSALS and after == before:
        result.update(outcome="not_used", reason=statement)
    # An acknowledgement without a lower count, a refusal beside a changed one, an
    # unknown statement: the outcome is not known.
    return result


async def _card_source(service: "ModelHubService", source_id: str) -> Optional[QuotaSourceRef]:
    try:
        sources = await asyncio.to_thread(service._quota_sources)
    except Exception:  # noqa: BLE001 - nothing was sent; the press ends as "no usable card"
        return None
    return next((source for source in sources if source.source_id == source_id and source.vendor in VENDORS), None)


async def _fresh_account(service: "ModelHubService", source: QuotaSourceRef) -> Optional[str]:
    """Which account the Source's grant belongs to, read now: a claim never rests on a remembered one."""

    reader = getattr(service.adapter, "reset_card_account_key", None)
    if not callable(reader):
        return None
    try:
        account = await reader(source.source_id, source.vendor, source.credential_ref)
    except Exception:  # noqa: BLE001 - an unreadable account is no account
        return None
    return account if isinstance(account, str) and _ACCOUNT.fullmatch(account) else None


async def _send_once(
    service: "ModelHubService",
    source: QuotaSourceRef,
    press: Press,
    request_id: str,
    account: str,
) -> Mapping[str, Any]:
    """The press's only call into the adapter's use. If it fails the outcome is unknown; it is not made again.

    It carries the count the page showed, for the adapter's last check before the request.
    """

    try:
        reply = await service.adapter.use_reset_card(
            source.source_id, source.vendor, source.credential_ref, press.kind, request_id, account, press.count
        )
    except Exception:  # noqa: BLE001 - never the exception's text, never a second call
        return {}
    return reply if isinstance(reply, dict) else {}


async def _settle(service: "ModelHubService", account: str, request_id: str, *, unknown: bool) -> bool:
    # The mark is taken now: every reading that began earlier, the press's own
    # re-read included, is numbered below it and can never lift this lock.
    mark = service.quota.tick()
    try:
        return await asyncio.to_thread(service.card_fences.settle, account, request_id, unknown=unknown, mark=mark)
    except Exception:  # noqa: BLE001 - not released: the claim on disk keeps the account locked
        return False


async def _summary(service: "ModelHubService") -> dict[str, Any]:
    """The summary an answer carries: what is known now, without waiting on another account's reading.

    Its failure must not hide what happened to the card.
    """

    try:
        return await service.quota_summary(wait=False)
    except Exception:  # noqa: BLE001
        logger.warning("Model Hub could not read the quota summary for a reset card answer")
        return empty_summary()


async def _summary_after(service: "ModelHubService", fresh: Mapping[str, Any]) -> dict[str, Any]:
    """The summary a settled use returns, after the resume its fresh reading allows."""

    if fresh:
        try:
            # The existing rule of a forced read: the account's own fresh reading decides.
            await service._resume_accounts_with_quota({"sources": [fresh]})
        except Exception:  # noqa: BLE001
            logger.warning("Model Hub could not resume an account after a reset card use")
    return await _summary(service)


async def _refused(service: "ModelHubService", code: str, press: Optional[Press] = None) -> Exception:
    """A closed refusal: no card was used. It carries the summary as it now is, so the page can redraw."""

    from .service import ModelHubError

    if press is not None:
        # Its own summary shows what holds the account, not this press.
        press.settled = True
    return ModelHubError(code, status=409, data={"quota": await _summary(service)})


async def _transact(service: "ModelHubService", press: Press, source: QuotaSourceRef, account: str) -> dict[str, Any]:
    try:
        request_id = await asyncio.to_thread(service.card_fences.claim, account, press.kind, service.now())
    except FenceUnavailable:
        # Already claimed, or the claim could not be made durable: nothing is sent.
        raise await _refused(service, "reset_card_locked", press) from None
    decision = unconfirmed_use(press.kind)
    # The refusal this press ends in when the adapter says nothing was sent.
    unsent: Optional[str] = None
    fresh: Mapping[str, Any] = {}
    try:
        reply = await _send_once(service, source, press, request_id, account)
        statement = reply.get("statement")
        unsent = _UNSENT.get(statement) if type(statement) is str else None
        if unsent is None:
            fresh = await service.quota.reread(source)
            # Only the re-read's own answer: a reading another caller put in its place proves nothing here.
            reading = service.quota.card_reading(source) if fresh else None
            decision = _decide(press.kind, reply, reading, account)
    except Exception:  # noqa: BLE001 - whatever went wrong after the claim, the outcome is unknown
        unsent, decision = None, unconfirmed_use(press.kind)
        logger.warning("Model Hub reset card use could not be confirmed: source=%s", source.source_id)
    released = await _settle(
        service, account, request_id, unknown=unsent is None and decision["outcome"] == "unconfirmed"
    )
    # Settled: from here a summary shows what the fence holds, not a use in flight.
    # The press itself is still being handled until its answer is assembled.
    press.settled = True
    if not released:
        # Unknown, or established but the claim could not be released on disk: the
        # account stays locked, and the lock is what the user has to act on.
        decision = unconfirmed_use(press.kind)
    elif unsent is not None:
        if unsent == "reset_card_count_changed":
            # The reading made for this press agreed with the page and the adapter's own
            # did not: read once more, so the page does not redraw from the one that lagged.
            try:
                await service.quota.reread(source)
            except Exception:  # noqa: BLE001 - the refusal stands, with the latest summary there is
                pass
        raise await _refused(service, unsent, press)
    outcome = decision["outcome"] if decision["reason"] is None else f"{decision['outcome']}:{decision['reason']}"
    logger.info("Model Hub reset card use: source=%s kind=%s outcome=%s", source.source_id, press.kind, outcome)
    return {"quota": await _summary_after(service, fresh), "use": decision}


async def _handle(service: "ModelHubService", press: Press, earlier: tuple[Press, ...]) -> dict[str, Any]:
    try:
        source: Optional[QuotaSourceRef] = None
        account: Optional[str] = None
        try:
            source = await _card_source(service, press.source_id)
            if source is not None:
                account = await _fresh_account(service, source)
        finally:
            # Later presses compare against it, whatever becomes of this one.
            press.account.set_result(account)
        if source is None or account is None:
            raise await _refused(service, "reset_card_unavailable", press)
        for other in earlier:
            # It was being handled when this press arrived. If it is on the same
            # account it wins, even when it has settled and released its claim since.
            if await other.account == account:
                raise await _refused(service, "reset_card_locked", press)
        view = await asyncio.to_thread(service.card_fences.view)
        if view.damaged or account in view.locks:
            # Locked already: no vendor is asked on behalf of a press that cannot go on.
            raise await _refused(service, "reset_card_locked", press)
        # What the page was showing must be what is held now. A second tab that still
        # shows the count from before the first one's use is refused here, before
        # anything is claimed or sent.
        fresh = await service.quota.reread(source)
        reading = service.quota.card_reading(source) if fresh else None
        held = _held(reading.cards) if reading is not None and reading.account == account else None
        if held is None:
            raise await _refused(service, "reset_card_unavailable", press)
        if held.get(press.kind, 0) != press.count:
            raise await _refused(service, "reset_card_count_changed", press)
        return await _transact(service, press, source, account)
    finally:
        # Only now may another press on this Source or this account be admitted: the
        # answer, with its summary, is assembled.
        service._card_presses.remove(press)


def _observed(task: "asyncio.Task[Any]") -> None:
    if not task.cancelled():
        task.exception()


async def use(service: "ModelHubService", source_id: object, payload: object) -> dict[str, Any]:
    """Use one card of the requested kind on the Source's account. See the module docstring."""

    kind, count = checked_selection(payload)
    source_id = checked_source_id(source_id)
    presses = service._card_presses
    # No await between this check and the append: admission is one step on the loop.
    if any(press.source_id == source_id for press in presses):
        raise await _refused(service, "reset_card_locked")
    press = Press(source_id, kind, count, _iso(service.now()), asyncio.get_running_loop().create_future())
    earlier = tuple(presses)
    presses.append(press)
    task = asyncio.create_task(_handle(service, press, earlier), name="model-hub-reset-card-use")
    task.add_done_callback(_observed)
    # A page that goes away does not stop a use that may already be at the vendor.
    return await asyncio.shield(task)


async def unknown_floors(service: "ModelHubService") -> dict[str, CardReadingFloor]:
    """Per account with an unknown use, what a reading must be to answer it."""

    return await asyncio.to_thread(service.card_fences.unknown_floors)


async def lift_unknown(service: "ModelHubService", sources: list[QuotaSourceRef]) -> None:
    """After the user's forced read: let each recognised reading answer the unknown use of its own account.

    Only readings the user asked for qualify, and the fence lifts a lock only
    for a reading that began after its outcome became unknown and at least its
    wait later. A background or cached reading, one that was already under way
    or came too soon, an unread inventory or another account's reading leaves
    the lock where it is.
    """

    readings = []
    for source in sources:
        if source.vendor not in VENDORS:
            continue
        reading = service.quota.card_reading(source)
        if (
            reading is not None
            and reading.forced
            and reading.account is not None
            and _held(reading.cards) is not None
        ):
            readings.append((reading.account, reading.sequence, reading.started_at))
    if readings:
        await asyncio.to_thread(service.card_fences.release_after_readings, readings)


def _shown_lock(view: FenceView, account: Optional[str], vendor: str) -> Optional[dict[str, str]]:
    if account is not None and not view.damaged:
        lock = view.locks.get(account)
        return dict(lock) if lock is not None else None
    if view.locked:
        # Whose lock it is cannot be told: the account was not read yet, or the
        # store is unreadable. Shown locked rather than free; a press is decided
        # by a fresh read of the account either way.
        return {"reason": UNCONFIRMED, "kind": "all" if vendor == "anthropic" else "account", "since": view.since}
    return None


async def overlay(service: "ModelHubService", sources: list[QuotaSourceRef], summary: dict[str, Any]) -> None:
    """Put each account's lock on its rows, from memory and the fence: no vendor is asked here."""

    view = await asyncio.to_thread(service.card_fences.view)
    presses = {press.source_id: press for press in service._card_presses if not press.settled}
    if not presses and not view.locked:
        return
    by_id = {source.source_id: source for source in sources}
    for row in summary["sources"]:
        source = by_id.get(row["source_id"])
        if "reset_cards" not in row or source is None:
            continue
        press = presses.get(row["source_id"])
        if press is not None:
            lock: Optional[dict[str, str]] = {"reason": IN_FLIGHT, "kind": press.kind, "since": press.since}
        else:
            lock = _shown_lock(view, service.quota.card_account(source), row["vendor"])
        row["reset_cards"] = {**row["reset_cards"], "lock": lock}
