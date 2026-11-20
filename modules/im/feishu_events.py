"""Feishu/Lark events: from the JSON Feishu delivers to the adapter's callbacks.

Four event types are handled: a received message, a card click, and a reaction
added or removed. Anything else is acknowledged and ignored.

The adapter and everything behind it have always received an event as the
``lark-oapi`` models rendered it: the fields the model knows, each one present
(``None`` when Feishu left it out), nested objects as plain dicts, and nothing
the model does not know. ``MESSAGE_EVENT`` and ``CARD_ACTION_EVENT`` are those
shapes, as of lark-oapi 1.7.5, so nothing downstream sees a difference. A
payload the SDK's models refused (a list where an object belongs) is refused
here too. ``tests/test_feishu_events.py`` holds all of it against the SDK.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

MESSAGE_RECEIVED = "im.message.receive_v1"
CARD_ACTION = "card.action.trigger"
REACTION_EVENTS = frozenset({"im.message.reaction.created_v1", "im.message.reaction.deleted_v1"})

# A shape is a dict of field name -> shape of that field. ``VALUE`` is a field
# taken as delivered; a one-item list is a list of that shape; ``MAPPING`` and
# ``LIST`` are taken as delivered but must be an object or an array.
VALUE = None
MAPPING = "mapping"
LIST = "list"

_USER_ID = {"user_id": VALUE, "open_id": VALUE, "union_id": VALUE}

MESSAGE_EVENT = {
    "sender": {"sender_id": _USER_ID, "sender_type": VALUE, "tenant_key": VALUE},
    "message": {
        "message_id": VALUE,
        "root_id": VALUE,
        "parent_id": VALUE,
        "create_time": VALUE,
        "update_time": VALUE,
        "chat_id": VALUE,
        "thread_id": VALUE,
        "chat_type": VALUE,
        "message_type": VALUE,
        "content": VALUE,
        "mentions": [{"key": VALUE, "id": _USER_ID, "mentioned_type": VALUE, "name": VALUE, "tenant_key": VALUE}],
        "user_agent": VALUE,
        "lark_agent_context": {"active_chat_id": VALUE},
    },
}

CARD_ACTION_EVENT = {
    "operator": {"tenant_key": VALUE, "user_id": VALUE, "open_id": VALUE, "union_id": VALUE},
    "token": VALUE,
    "action": {
        "value": MAPPING,
        "tag": VALUE,
        "option": VALUE,
        "timezone": VALUE,
        "name": VALUE,
        "form_value": MAPPING,
        "input_value": VALUE,
        "options": LIST,
        "checked": VALUE,
    },
    "host": VALUE,
    "delivery_type": VALUE,
    "context": {"url": VALUE, "preview_token": VALUE, "open_message_id": VALUE, "open_chat_id": VALUE},
}


REACTION_EVENT = {
    "message_id": VALUE,
    "reaction_type": {"emoji_type": VALUE},
    "operator_type": VALUE,
    "user_id": _USER_ID,
    "app_id": VALUE,
    "action_time": VALUE,
}


class FeishuEventError(Exception):
    """An event that cannot be read; the frame is answered as failed, so Feishu sends it again."""


def shape(value: Any, fields: Any, name: str = "event") -> Any:
    """Render ``value`` in the shape ``fields``: every known field, nothing else."""
    if fields is VALUE or value is None:
        return value
    if fields is MAPPING:
        if not isinstance(value, dict):
            raise FeishuEventError(f"{name} is not an object")
        return value
    if fields is LIST:
        if not isinstance(value, (list, tuple)):
            raise FeishuEventError(f"{name} is not an array")
        return list(value)
    if isinstance(fields, list):
        if not isinstance(value, (list, tuple)):
            raise FeishuEventError(f"{name} is not an array")
        return [shape(item, fields[0], name) for item in value]
    if not isinstance(value, dict):
        raise FeishuEventError(f"{name} is not an object")
    return {key: shape(value.get(key), sub, key) for key, sub in fields.items()}


def _has_text(value: Any, name: str) -> bool:
    """Whether an envelope field that holds text is there and not empty; a number in its place is malformed."""
    if value is None:
        return False
    try:
        return len(value) != 0
    except TypeError:
        raise FeishuEventError(f"{name} is not text") from None


class FeishuEventDispatcher:
    """Reads one delivered event and calls the adapter.

    ``handle`` returns what goes back to Feishu with the acknowledgement: an
    empty object for a card click (Feishu shows an error toast without one),
    nothing otherwise. It raises ``FeishuEventError`` for a payload it cannot
    read.
    """

    def __init__(
        self,
        *,
        on_message: Optional[Callable[[Optional[str], Dict[str, Any]], None]] = None,
        on_card_action: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_reaction: Optional[Callable[[Optional[str], str], None]] = None,
    ) -> None:
        self._on_message = on_message
        self._on_card_action = on_card_action
        self._on_reaction = on_reaction

    def handle(self, payload: bytes) -> Optional[Dict[str, Any]]:
        try:
            body = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise FeishuEventError(f"event is not JSON: {exc}") from None
        if not isinstance(body, dict):
            raise FeishuEventError("event is not an object")
        header = body.get("header")
        if header is not None and not isinstance(header, dict):
            raise FeishuEventError("header is not an object")
        event = body.get("event")
        event_id = (header or {}).get("event_id")

        if not _has_text(body.get("schema"), "schema"):
            # A 1.0 event (it carries a ``uuid`` and names its type inside ``event``) or
            # nothing we know. We handle no 1.0 event, but it must still be one.
            if _has_text(body.get("uuid"), "uuid") and "event" in body and not isinstance(event, dict):
                raise FeishuEventError("event is not an object")
            logger.debug("Ignoring Feishu event without a 2.0 schema")
            return None
        if header is None:
            raise FeishuEventError("a 2.0 event without a header")
        event_type = header.get("event_type")

        if event_type == MESSAGE_RECEIVED:
            shaped = shape(event, MESSAGE_EVENT)
            if self._on_message is not None and shaped is not None:
                self._on_message(event_id, shaped)
            return None
        if event_type == CARD_ACTION:
            shaped = shape(event, CARD_ACTION_EVENT)
            if self._on_card_action is not None:
                self._on_card_action(shaped if shaped is not None else {})
            return {}
        if event_type in REACTION_EVENTS:
            shape(event, REACTION_EVENT)  # read for its shape only: a malformed one is refused
            if self._on_reaction is not None:
                self._on_reaction(event_id, event_type)
            return None
        logger.debug("Ignoring Feishu event of type %s", event_type)
        return None

    # lark-oapi's long-connection client calls its handler by these names.
    def _do_without_validation(self, payload: bytes) -> Optional[Dict[str, Any]]:
        return self.handle(payload)

    do_without_validation = _do_without_validation
