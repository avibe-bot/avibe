"""The Feishu adapter's side of event dispatch: deduplication and what it ignores.

How an event is read is covered against the SDK in ``test_feishu_events.py``;
these tests cover what the adapter's callbacks do with one.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import pytest

from config.v2_config import LarkConfig
from modules.im.feishu import FeishuBot
from tests.feishu_fake import card_action_event, message_event


def _bot() -> FeishuBot:
    return FeishuBot(LarkConfig(app_id="app-id", app_secret="app-secret"))


def _payload(body: Dict[str, Any]) -> bytes:
    return json.dumps(body).encode("utf-8")


def test_a_message_feishu_delivers_twice_is_handled_once():
    bot = _bot()
    handled: List[Dict[str, Any]] = []
    bot._handle_message_event = handled.append  # type: ignore[method-assign]
    dispatcher = bot._build_event_handler()

    dispatcher.handle(_payload(message_event(event_id="evt_1")))
    dispatcher.handle(_payload(message_event(event_id="evt_1")))
    dispatcher.handle(_payload(message_event(event_id="evt_2")))

    assert len(handled) == 2


def test_a_failure_while_handling_a_message_is_logged_and_still_acknowledged(caplog: pytest.LogCaptureFixture):
    bot = _bot()

    def broken(_event: Dict[str, Any]) -> None:
        raise RuntimeError("handler broke")

    bot._handle_message_event = broken  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger="modules.im.feishu"):
        answer = bot._build_event_handler().handle(_payload(message_event()))

    # Feishu is not asked to send it again: the event was delivered, the fault is ours.
    assert answer is None
    assert "Error in on_message_receive: handler broke" in caplog.text


def test_a_card_click_is_answered_even_when_handling_it_fails(caplog: pytest.LogCaptureFixture):
    bot = _bot()

    def broken(_event: Dict[str, Any]) -> None:
        raise RuntimeError("click broke")

    bot._handle_card_action = broken  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger="modules.im.feishu"):
        answer = bot._build_event_handler().handle(_payload(card_action_event()))

    # Without an answer Feishu shows the user a "request failed" toast.
    assert answer == {}
    assert "Error in on_card_action: click broke" in caplog.text


@pytest.mark.parametrize("kind", ["created", "deleted"])
def test_reaction_events_are_ignored_without_error(kind: str, caplog: pytest.LogCaptureFixture):
    bot = _bot()
    body = {
        "schema": "2.0",
        "header": {"event_id": f"evt_{kind}", "event_type": f"im.message.reaction.{kind}_v1"},
        "event": {"message_id": "om_1", "reaction_type": {"emoji_type": "OnIt"}},
    }

    with caplog.at_level(logging.DEBUG, logger="modules.im.feishu"):
        answer = bot._build_event_handler().handle(_payload(body))

    assert answer is None
    assert f"Ignoring Feishu reaction event: im.message.reaction.{kind}_v1" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
