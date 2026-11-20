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
# Events are remembered as long as Feishu may still send one again.
_REMEMBER_SECONDS = 8 * 3600
# Pages (of 50) read of one chat in one go; a longer history is gone on with next time.
_PAGE_LIMIT = 40


@dataclass
class HistoryCounts:
    people_messages: int = 0
    matched: int = 0
    missing: int = 0
    arrived_late: int = 0
    readings_failed: int = 0


class FeishuHistoryCheck:
    """``list_messages(chat_id, start, end, page_token)`` reads one page of a chat's history, oldest first."""

    def __init__(self, list_messages: Callable[[str, int, int, Optional[str]], Awaitable[Any]]) -> None:
        self._list_messages = list_messages
        self.counts = HistoryCounts()
        # Events arrive on the connections' threads; the check runs on the adapter's loop.
        self._lock = threading.Lock()
        self._event_at: Dict[str, float] = {}
        self._missing: Dict[str, str] = {}  # message id -> chat id
        self._judged: Dict[str, float] = {}  # message id -> when it was matched or reported
        self._checked_until: Dict[str, int] = {}
        # A reading that stopped at the page limit: chat id -> (its interval, where to go on).
        self._unfinished: Dict[str, Tuple[int, int, str]] = {}
        # What was last said about a chat that could not be read, so that it is said once.
        self._said: Dict[str, str] = {}
        self._started_at = int(time.time()) + 60  # the connection needs a moment to be there

    def message_event(self, message_id: Optional[str]) -> None:
        """A message event arrived, by whichever connection, handled or dropped as a repeat."""
        if not message_id:
            return
        with self._lock:
            self._event_at[message_id] = time.time()
            chat_id = self._missing.pop(message_id, None)
        if chat_id is not None:
            self.counts.arrived_late += 1
            self.counts.missing -= 1
            logger.warning(
                "Feishu history check: the event for message %s in chat %s arrived after the check", message_id, chat_id
            )

    async def run_once(self, direct_chats: Iterable[str]) -> None:
        end = int(time.time()) - _SETTLE_SECONDS
        for chat_id in sorted(set(direct_chats)):
            unfinished = self._unfinished.get(chat_id)
            if unfinished is not None:
                start, until, page_token = unfinished  # the same interval, from where the reading stopped
            else:
                start, until, page_token = self._checked_until.get(chat_id, self._started_at), end, None
                if until <= start:
                    continue
            try:
                sent_by_people, next_page = await self._people_messages(chat_id, start, until, page_token)
            except _NotRead as failure:
                # Tried again next time, over the same interval: nothing is skipped for a failure.
                self.counts.readings_failed += 1
                self._unfinished.pop(chat_id, None)  # a page token may be what failed; begin the interval again
                if self._said.get(chat_id) != str(failure):
                    self._said[chat_id] = str(failure)
                    logger.warning("Feishu history check: chat %s could not be read (%s)", chat_id, failure)
                continue
            self._said.pop(chat_id, None)
            if next_page:
                self._unfinished[chat_id] = (start, until, next_page)
            else:
                self._unfinished.pop(chat_id, None)
                self._checked_until[chat_id] = until
            for message_id in sent_by_people:
                if message_id in self._judged:
                    continue  # read before: an interval begun again, or the edge of two
                self._judged[message_id] = time.time()
                self.counts.people_messages += 1
                with self._lock:
                    arrived = message_id in self._event_at
                    if not arrived:
                        self._missing[message_id] = chat_id
                if arrived:
                    self.counts.matched += 1
                else:
                    self.counts.missing += 1
                    logger.error(
                        "Feishu history check: message %s in chat %s is in the history but no event arrived for it",
                        message_id,
                        chat_id,
                    )
        forget_before = time.time() - _REMEMBER_SECONDS
        with self._lock:
            for message_id in [key for key, at in self._event_at.items() if at < forget_before]:
                del self._event_at[message_id]
        for message_id in [key for key, at in self._judged.items() if at < forget_before]:
            del self._judged[message_id]

    async def _people_messages(
        self, chat_id: str, start: int, end: int, page_token: Optional[str]
    ) -> Tuple[List[str], Optional[str]]:
        """Ids of the messages people sent in ``chat_id`` between ``start`` and ``end``, from ``page_token`` on.

        Returns them with the token to go on from, when the page limit stopped
        the reading before the interval's end. Raises ``_NotRead`` when Feishu
        does not give the history, for whatever reason.
        """
        found: List[str] = []
        for _ in range(_PAGE_LIMIT):
            try:
                reply = await self._list_messages(chat_id, start, end, page_token)
            except Exception as exc:
                raise _NotRead(type(exc).__name__) from None
            if not reply.success():
                raise _NotRead(f"code={reply.code} msg={reply.msg}")
            data = reply.data or {}
            for item in data.get("items") or []:
                sender = item.get("sender") or {}
                if sender.get("sender_type") == "user" and item.get("message_id"):
                    found.append(item["message_id"])
            page_token = data.get("page_token") or None
            if not data.get("has_more") or not page_token:
                return found, None
        return found, page_token


class _NotRead(Exception):
    """Feishu did not give a chat's history this time."""
