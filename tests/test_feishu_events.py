"""Feishu events read by our own dispatcher, held against ``lark-oapi``'s.

Two contracts:

- the adapter's callbacks receive exactly what they received when the SDK
  parsed the event and the adapter flattened its models: the same fields, the
  same ``None`` for a field Feishu left out, nothing the SDK's model did not
  know, and the same failure for a payload of the wrong shape;
- over a real socket, with the SDK's long-connection client delivering to our
  dispatcher, an event reaches the adapter once and Feishu gets the same
  acknowledgement as before.
"""

from __future__ import annotations

import base64
import json
import threading
from typing import Any, Dict, Iterator, List, Optional

import pytest

from modules.im.feishu_events import FeishuEventDispatcher, FeishuEventError
from tests.feishu_fake import FakeFeishu, card_action_event, message_event, running_adapter

# --- the reference: the SDK's dispatcher and the adapter's old glue ----------


def _flatten(obj: Any) -> Any:
    """The adapter's former ``_event_to_dict``: an SDK model as plain dicts and lists."""
    if isinstance(obj, dict):
        return {key: _flatten(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_flatten(item) for item in obj]
    if hasattr(obj, "__dict__"):
        return {key: _flatten(value) for key, value in obj.__dict__.items() if not key.startswith("_")}
    return obj


def _through_the_sdk(payload: bytes) -> Dict[str, Any]:
    """What the adapter's callbacks received, and what went back to Feishu, with the SDK's dispatcher."""
    import lark_oapi as lark
    from lark_oapi.core.json import JSON
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

    seen: List[Any] = []

    def on_message(data: Any) -> None:
        if data.event:
            seen.append(("message", data.header.event_id, _flatten(data.event)))

    def on_card(data: Any) -> Any:
        seen.append(("card", _flatten(data.event) if data.event else {}))
        return P2CardActionTriggerResponse({})

    def on_reaction(data: Any) -> None:
        seen.append(("reaction", data.header.event_id, data.header.event_type))

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card)
        .register_p2_im_message_reaction_created_v1(on_reaction)
        .register_p2_im_message_reaction_deleted_v1(on_reaction)
        .build()
    )
    try:
        result = handler._do_without_validation(payload)
    except Exception:
        return {"seen": seen, "failed": True}
    # The long-connection client puts ``JSON.marshal(result)`` into the acknowledgement.
    return {"seen": seen, "failed": False, "answer": None if result is None else JSON.marshal(result)}


def _through_ours(payload: bytes) -> Dict[str, Any]:
    seen: List[Any] = []
    dispatcher = FeishuEventDispatcher(
        on_message=lambda event_id, event: seen.append(("message", event_id, event)),
        on_card_action=lambda event: seen.append(("card", event)),
        on_reaction=lambda event_id, event_type: seen.append(("reaction", event_id, event_type)),
    )
    try:
        result = dispatcher.handle(payload)
    except FeishuEventError:
        return {"seen": seen, "failed": True}
    return {"seen": seen, "failed": False, "answer": None if result is None else json.dumps(result)}


def _reaction_event(kind: str) -> Dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {"event_id": f"evt_{kind}", "event_type": f"im.message.reaction.{kind}_v1", "app_id": "cli_app"},
        "event": {
            "message_id": "om_message",
            "reaction_type": {"emoji_type": "OnIt"},
            "operator_type": "user",
            "user_id": {"open_id": "ou_user"},
            "action_time": "1627641418803",
        },
    }


def _with(event: Dict[str, Any], **changes: Any) -> Dict[str, Any]:
    """``event`` with top-level keys of its ``event`` object replaced (``None`` removes one)."""
    body = json.loads(json.dumps(event))
    for key, value in changes.items():
        if value is None:
            body["event"].pop(key, None)
        else:
            body["event"][key] = value
    return body


MENTION = {
    "key": "@_user_1",
    "id": {"union_id": "on_bot", "user_id": "", "open_id": "ou_bot"},
    "name": "Avibe",
    "tenant_key": "tenant",
    "mentioned_type": "bot",
    "a_field_the_sdk_does_not_know": True,
}

EVENTS: Dict[str, Dict[str, Any]] = {
    "a direct message": message_event(),
    "a group message that mentions the bot": message_event(
        chat_type="group", content='{"text":"@_user_1 hello"}', mentions=[MENTION], user_agent="Mozilla/5.0"
    ),
    "a reply in a thread": message_event(root_id="om_root", parent_id="om_parent", thread_id="omt_thread"),
    "a rich-text post": message_event(
        message_type="post",
        content=json.dumps({"title": "t", "content": [[{"tag": "text", "text": "正文"}, {"tag": "img", "image_key": "k"}]]}),
    ),
    "an image": message_event(message_type="image", content='{"image_key":"img_v3"}'),
    "a file": message_event(message_type="file", content='{"file_key":"file_v3","file_name":"a.pdf"}'),
    "a message with fields the SDK does not know": message_event(update_time="1", brand_new_field={"x": 1}),
    "a message with explicit nulls": message_event(root_id=None, mentions=None, lark_agent_context=None),
    "a message with an agent context": message_event(lark_agent_context={"active_chat_id": "oc_other", "more": 1}),
    "a message with a null mention": message_event(mentions=[None, MENTION]),
    "a message without a sender": _with(message_event(), sender=None),
    "a message with a null sender id": _with(message_event(), sender={"sender_id": None, "sender_type": "user"}),
    "a message event with an empty event": {**message_event(), "event": {}},
    "a message event with a null event": {**message_event(), "event": None},
    "a message event without an event": {key: value for key, value in message_event().items() if key != "event"},
    "a message without an event id": {**message_event(), "header": {"event_type": "im.message.receive_v1"}},
    "a button click": card_action_event(),
    "a form submission": card_action_event(name="submit", form_value={"cwd": "/tmp", "nested": {"a": [1, 2]}}),
    "a select change": card_action_event(tag="select_static", option="claude", value={}),
    "a multi-select change": card_action_event(tag="multi_select_static", options=["a", "b"], checked=True),
    "a click with fields the SDK does not know": card_action_event(input_value="typed", unknown={"x": 1}),
    "a click with a null value": card_action_event(value=None),
    "a click with an empty event": {**card_action_event(), "event": {}},
    "a click with a null event": {**card_action_event(), "event": None},
    "a reaction added": _reaction_event("created"),
    "a reaction removed": _reaction_event("deleted"),
}

UNREADABLE: Dict[str, bytes] = {
    "not JSON": b"<html>",
    "an event that is a string": json.dumps({**message_event(), "event": "text"}).encode(),
    "a header that is a string": json.dumps({**message_event(), "header": "h"}).encode(),
    "a message that is a list": json.dumps(_with(message_event(), message=["x"])).encode(),
    "mentions that are not a list": json.dumps(message_event(mentions={"key": "@_user_1"})).encode(),
    "a mention that is a string": json.dumps(message_event(mentions=["@_user_1"])).encode(),
    "a sender id that is a string": json.dumps(_with(message_event(), sender={"sender_id": "ou_user"})).encode(),
    "a click value that is a string": json.dumps(card_action_event(value="stop")).encode(),
    "a form value that is a list": json.dumps(card_action_event(form_value=["a"])).encode(),
    "options that are a string": json.dumps(card_action_event(options="a")).encode(),
    "a click context that is a list": json.dumps(_with(card_action_event(), context=[1])).encode(),
}


@pytest.mark.parametrize("name", list(EVENTS))
def test_the_adapter_receives_what_it_received_through_the_sdk(name: str):
    payload = json.dumps(EVENTS[name], ensure_ascii=False).encode("utf-8")

    theirs = _through_the_sdk(payload)
    ours = _through_ours(payload)

    assert theirs["failed"] is False
    assert ours == theirs


@pytest.mark.parametrize("name", list(UNREADABLE))
def test_a_payload_of_the_wrong_shape_fails_as_it_did_and_reaches_no_callback(name: str):
    theirs = _through_the_sdk(UNREADABLE[name])
    ours = _through_ours(UNREADABLE[name])

    assert ours == theirs == {"seen": [], "failed": True}


@pytest.mark.parametrize(
    "body",
    [
        {"schema": "2.0", "header": {"event_id": "e", "event_type": "im.chat.updated_v1"}, "event": {}},
        {"uuid": "u", "type": "event_callback", "event": {"type": "p2p_chat_create"}},
        {"schema": "2.0", "event": {}},
    ],
    ids=["a 2.0 event we do not handle", "a 1.0 event", "an event without a header"],
)
def test_an_event_we_do_not_handle_is_acknowledged_and_ignored(body: Dict[str, Any]):
    """The one intended difference: the SDK failed these, and Feishu then sent each four more times."""
    payload = json.dumps(body).encode()

    assert _through_the_sdk(payload) == {"seen": [], "failed": True}
    assert _through_ours(payload) == {"seen": [], "failed": False, "answer": None}


# --- over a socket: the SDK's long-connection client, our dispatcher ---------


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeFeishu]:
    # The SDK fetches its endpoint with ``requests``, which reads proxy settings from the environment.
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    server = FakeFeishu().start()
    yield server
    server.close()


def _answer(acknowledgement: Any) -> Optional[Any]:
    data = acknowledgement.json().get("data")
    return None if data is None else json.loads(base64.b64decode(data))


def test_a_message_reaches_the_adapter_once_and_is_acknowledged(fake: FakeFeishu):
    with running_adapter(fake) as adapter:
        first = fake.push_event(message_event(event_id="evt_1", text="one"))
        again = fake.push_event(message_event(event_id="evt_1", text="one"))  # Feishu sending it again
        other = fake.push_event(message_event(event_id="evt_2", text="two"))
        acknowledged = [fake.wait_acknowledged(message_id) for message_id in (first, again, other)]

    assert [json.loads(message["message"]["content"])["text"] for message in adapter.messages] == ["one", "two"]
    assert [frame.json() for frame in acknowledged] == [{"code": 200}] * 3
    assert fake.endpoint_requests[0]["body"] == {"AppID": "cli_app", "AppSecret": "socket-secret"}


def test_an_event_delivered_in_pieces_is_joined(fake: FakeFeishu):
    long_text = "长" * 5000
    with running_adapter(fake) as adapter:
        message_id = fake.push_event(message_event(text=long_text), pieces=4)
        acknowledged = fake.wait_acknowledged(message_id)

    assert json.loads(adapter.messages[0]["message"]["content"])["text"] == long_text
    assert acknowledged.json() == {"code": 200}
    # One acknowledgement for the whole event, sent on its last piece.
    assert [frame.headers["seq"] for frame in fake.acknowledgements(message_id)] == ["3"]


def test_a_card_click_is_answered_with_an_empty_object(fake: FakeFeishu):
    with running_adapter(fake) as adapter:
        acknowledged = fake.wait_acknowledged(fake.push_event(card_action_event()))

    assert adapter.clicks[0]["action"]["value"] == {"key": "stop", "thread_id": ""}
    assert acknowledged.json()["code"] == 200
    assert _answer(acknowledged) == {}


def test_an_unreadable_event_is_answered_as_failed_and_an_unknown_one_as_handled(fake: FakeFeishu):
    unknown = {"schema": "2.0", "header": {"event_id": "e", "event_type": "im.chat.updated_v1"}, "event": {}}
    with running_adapter(fake) as adapter:
        failed = fake.wait_acknowledged(fake.push_event({**message_event(), "event": "text"}))
        ignored = fake.wait_acknowledged(fake.push_event(unknown))

    assert failed.json() == {"code": 500}
    assert ignored.json() == {"code": 200}
    assert adapter.messages == []


def test_a_stopped_adapter_does_not_come_back_when_the_next_one_starts(fake: FakeFeishu):
    """The SDK's clients share one event loop per process; a stopped client's reconnect must not ride the next start."""
    with running_adapter(fake, "cli_first"):
        pass
    with running_adapter(fake, "cli_second") as second:
        acknowledged = fake.wait_acknowledged(fake.push_event(message_event()))
        # Give a revived first client time to show up: the fake lets it reconnect at once.
        threading.Event().wait(1.0)

        assert [request["body"]["AppID"] for request in fake.endpoint_requests] == ["cli_first", "cli_second"]
        assert fake.connections_opened == 2
        assert acknowledged.json() == {"code": 200}
        assert len(second.messages) == 1


def test_the_setup_wizard_s_temporary_connection_refuses_events(fake: FakeFeishu, monkeypatch: pytest.MonkeyPatch):
    """Feishu gives an event to one connection of the app; one that lands here must be sent again, not swallowed."""
    from modules.im.feishu_sdk_ws import sdk_ws_client_module
    from vibe import api

    monkeypatch.setattr(api, "_lark_api_base", lambda _domain="feishu": fake.url)
    thread = None
    try:
        assert api.lark_temp_ws_start("cli_wizard", "wizard-secret", "feishu")["ok"] is True
        thread = api._temp_ws_thread
        fake.wait_connected()
        refused = fake.wait_acknowledged(fake.push_event(message_event()))
        assert api.lark_temp_ws_stop() == {"ok": True}
    finally:
        # The temporary client's thread sits in the SDK's shared loop for good; release it for the next test.
        loop = sdk_ws_client_module().loop
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=10)

    assert refused.json() == {"code": 500}
    assert fake.endpoint_requests[0]["body"] == {"AppID": "cli_wizard", "AppSecret": "wizard-secret"}

