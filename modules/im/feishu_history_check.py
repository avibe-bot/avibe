"""Did every message a person sent the bot arrive as an event?

While our own long connection is being proven against ``lark-oapi``'s
(``docs/plans/feishu-own-client.md``), this is the ground truth for "no event
lost": Feishu's own history of each direct chat, read back through the
permission the app already has, matched against the message events the adapter
received. A message in the history without an event is a lost event, by id.

Only direct chats are read: there the bot receives every message a person
sends, so the history and the events must agree. In a group it receives only
the messages that mention it.

Temporary: it runs only while ``long_connection`` is not ``sdk`` and goes away
with that switch. Ids and counts are logged, never content.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# A message is looked for once it is this old: time for the event to arrive.
_SETTLE_SECONDS = 120
# The check forgets nothing: every message event and every verdict is kept for as long as
# it lives. So that this has an end, it is over, and says so, once it holds this many of
# either. It gives no verdicts by halves.
_LIMIT = 50_000
# Pages (of 50) read of one chat in one go; a longer history is gone on with next time.
_PAGE_LIMIT = 40


@dataclass
class HistoryCounts:
    people_messages: int = 0
    matched: int = 0
    missing: int = 0
    arrived_late: int = 0
    readings_failed: int = 0
    # The check held as much as it may and is over: the counts are those up to then.
    ended: bool = False


class FeishuHistoryCheck:
    """``list_messages(chat_id, start, end, page_token)`` reads one page of a chat's history, oldest first.

    Events arrive on the connections' threads; the check runs on the adapter's
    loop. One lock is held for every change to what the check holds or counts
    and for every line it logs about that, and each such step first looks
    whether the check is over. So the end is one moment: what was decided
    before it is counted and logged before it, and nothing is after it.
    """

    def __init__(self, list_messages: Callable[[str, int, int, Optional[str]], Awaitable[Any]]) -> None:
        self._list_messages = list_messages
        self.counts = HistoryCounts()
        self._lock = threading.Lock()
        self._event_at: Dict[str, float] = {}
        self._missing: Dict[str, str] = {}  # message id -> chat id
        self._judged: set = set()  # message ids matched or reported
        self._checked_until: Dict[str, int] = {}
        # A reading that stopped at the page limit: chat id -> (its interval, where to go on).
        self._unfinished: Dict[str, Tuple[int, int, str]] = {}
        # What was last said about a chat that could not be read, so that it is said once.
        self._said: Dict[str, str] = {}
        self._started_at = int(time.time()) + 60  # the connection needs a moment to be there

    def snapshot(self) -> Dict[str, Any]:
        """The counts as they stand together, and how many chats are not read up to their last sweep.

        ``chats_behind`` counts the chats whose last reading failed or stopped
        at the page limit: while it is not 0, or ``ended`` is true, "nothing
        missing" is not a result.
        """
        with self._lock:
            return dict(vars(self.counts), chats_behind=len(self._said) + len(self._unfinished))

    def _end(self) -> None:
        """Called with the lock held: the last thing the check does. What it held is let go."""
        self.counts.ended = True
        self._event_at.clear()
        self._judged.clear()
        self._missing.clear()
        logger.warning(
            "Feishu history check: over after %d message events or verdicts; nothing is checked from here on", _LIMIT
        )

    def message_event(self, message_id: Optional[str]) -> None:
        """A message event arrived, by whichever connection, handled or dropped as a repeat."""
        if not message_id:
            return
        with self._lock:
            if self.counts.ended:
                return
            if message_id not in self._event_at and len(self._event_at) >= _LIMIT:
                self._end()
                return
            self._event_at[message_id] = time.time()
            chat_id = self._missing.pop(message_id, None)
            if chat_id is not None:
                self.counts.arrived_late += 1
                self.counts.missing -= 1
                logger.warning(
                    "Feishu history check: the event for message %s in chat %s arrived after the check",
                    message_id,
                    chat_id,
                )

    async def run_once(self, direct_chats: Iterable[str]) -> None:
        end = int(time.time()) - _SETTLE_SECONDS
        for chat_id in sorted(set(direct_chats)):
            if self.counts.ended:
                return
            unfinished = self._unfinished.get(chat_id)
            if unfinished is not None:
                start, until, page_token = unfinished  # the same interval, from where the reading stopped
            else:
                start, until, page_token = self._checked_until.get(chat_id, self._started_at), end, None
                if until <= start:
                    continue
            try:
                sent_by_people, next_page = await self._people_messages(chat_id, start, until, page_token)
            except _Over:
                return
            except _NotRead as failure:
                with self._lock:
                    if self.counts.ended:
                        return  # what a request still out brings back is not looked at
                    # Tried again next time, over the same interval: nothing is skipped for a failure.
                    self.counts.readings_failed += 1
                    self._unfinished.pop(chat_id, None)  # a page token may be what failed; begin the interval again
                    if self._said.get(chat_id) != str(failure):
                        self._said[chat_id] = str(failure)
                        logger.warning("Feishu history check: chat %s could not be read (%s)", chat_id, failure)
                continue
            with self._lock:
                if self.counts.ended:
                    return
                self._said.pop(chat_id, None)
                if next_page:
                    self._unfinished[chat_id] = (start, until, next_page)
                else:
                    self._unfinished.pop(chat_id, None)
                    self._checked_until[chat_id] = until
                for message_id in sent_by_people:
                    if message_id in self._judged:
                        continue  # read before: an interval begun again, or the edge of two
                    if len(self._judged) >= _LIMIT:
                        self._end()
                        return
                    self._judged.add(message_id)
                    self.counts.people_messages += 1
                    if message_id in self._event_at:
                        self.counts.matched += 1
                        continue
                    self._missing[message_id] = chat_id
                    self.counts.missing += 1
                    logger.error(
                        "Feishu history check: message %s in chat %s is in the history but no event arrived for it",
                        message_id,
                        chat_id,
                    )

    async def _people_messages(
        self, chat_id: str, start: int, end: int, page_token: Optional[str]
    ) -> Tuple[List[str], Optional[str]]:
        """Ids of the messages people sent in ``chat_id`` between ``start`` and ``end``, from ``page_token`` on.

        Returns them with the token to go on from, when the page limit stopped
        the reading before the interval's end. Raises ``_NotRead`` when Feishu
        does not give the history, for whatever reason (a reply that is not a
        page included), and ``_Over`` instead of asking for a page, or looking
        at one, once the check is over.
        """
        found: List[str] = []
        for _ in range(_PAGE_LIMIT):
            if self.counts.ended:
                raise _Over()
            try:
                reply = await self._list_messages(chat_id, start, end, page_token)
            except Exception as exc:
                raise _NotRead(type(exc).__name__) from None
            if self.counts.ended:
                raise _Over()  # what comes back after the end is not looked at
            if not reply.success():
                raise _NotRead(f"code={reply.code} msg={reply.msg}")
            people, page_token = _page(reply.data)
            found.extend(people)
            if page_token is None:
                return found, None
        return found, page_token


def _page(data: Any) -> Tuple[List[str], Optional[str]]:
    """The ids of the people's messages on one page, and the token of the next page if there is one.

    Anything that is not such a page is a reading that failed: to take half of
    it would be to pass over messages without saying so.
    """
    if data is None:
        data = {}
    items = data.get("items") if isinstance(data, dict) else 0
    if items is None:
        items = []
    if not isinstance(items, list):
        raise _NotRead("a reply that is not a page")
    people: List[str] = []
    for item in items:
        sender = item.get("sender") if isinstance(item, dict) else 0
        if sender is None:
            sender = {}
        if not isinstance(sender, dict):
            raise _NotRead("a page with an entry that is not a message")
        if sender.get("sender_type") != "user":
            continue
        message_id = item.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise _NotRead("a page with a message that has no id")
        people.append(message_id)
    if not data.get("has_more"):
        return people, None
    page_token = data.get("page_token")
    if not isinstance(page_token, str) or not page_token:
        raise _NotRead("a page that promises more and does not say where")
    return people, page_token


class _Over(Exception):
    """The check ended while a reading was under way."""


class _NotRead(Exception):
    """Feishu did not give a chat's history this time."""
