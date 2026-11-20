"""Feishu events read by our own dispatcher, held against ``lark-oapi``'s.

The reference is the release the lock file pins (1.7.5): its models define the
shape the adapter has received, and ``pyproject.toml`` asks for no older one.

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
import asyncio
import threading
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import patch

import pytest

from config.v2_config import LarkConfig
from modules.im import feishu_sdk_ws
from modules.im.feishu import FeishuBot

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
    "a reaction with a null event": {**_reaction_event("created"), "event": None},
    "a reaction without an event": {key: value for key, value in _reaction_event("deleted").items() if key != "event"},
    "a reaction with fields the SDK does not know": _with(_reaction_event("created"), brand_new={"x": 1}),
    "a message whose schema is another version": {**message_event(), "schema": "3.0"},
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
    "a reaction event that is a string": json.dumps({**_reaction_event("created"), "event": "text"}).encode(),
    "a reaction type that is a string": json.dumps(_with(_reaction_event("created"), reaction_type="OK")).encode(),
    "a reaction's user that is a string": json.dumps(_with(_reaction_event("deleted"), user_id="ou_user")).encode(),
    "a schema that is a number": json.dumps({**message_event(), "schema": 2}).encode(),
    "a schema that is false": json.dumps({**message_event(), "schema": False}).encode(),
    "a 2.0 event without a header": json.dumps({"schema": "2.0", "event": {}}).encode(),
    "a 1.0 event whose event is null": json.dumps({"uuid": "u", "event": None}).encode(),
    "a uuid that is a number": json.dumps({"uuid": 7, "event": {"type": "p2p_chat_create"}}).encode(),
    "a body that is a list": b"[]",
    "a body that is null": b"null",
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
        {"uuid": "u", "type": "event_callback"},
        {"token": "t"},
    ],
    ids=["a 2.0 event we do not handle", "a 1.0 event", "a 1.0 event with no body", "neither"],
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
    # Reconnect at once and try again quickly; a real server says 30 and 120 seconds.
    server = FakeFeishu(
        client_config={"PingInterval": 120, "ReconnectCount": -1, "ReconnectInterval": 0.05, "ReconnectNonce": 0}
    ).start()
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
    with running_adapter(fake, "cli_first"):
        pass
    with running_adapter(fake, "cli_second") as second:
        acknowledged = fake.wait_acknowledged(fake.push_event(message_event()))
        threading.Event().wait(0.5)  # ten of the fake's retry intervals

        assert [request["body"]["AppID"] for request in fake.endpoint_requests] == ["cli_first", "cli_second"]
        assert fake.connections_opened == 2
        assert acknowledged.json() == {"code": 200}
        assert len(second.messages) == 1


def test_an_adapter_stopped_while_it_waits_to_reconnect_does_not_come_back_either(fake: FakeFeishu):
    """The SDK decides to reconnect once and then only sleeps and retries; stopping must end that too."""
    fake.client_config["ReconnectInterval"] = 2
    with running_adapter(fake, "cli_first") as first:
        fake.endpoint_replies = [{"code": 1, "msg": "system busy"}]
        fake.drop_connection()
        fake.wait_until(lambda: len(fake.endpoint_requests) == 2, what="the failed reconnect")
        # Stopped here, two seconds before its next attempt.
    fake.client_config["ReconnectInterval"] = 0.05
    with running_adapter(fake, "cli_second") as second:
        threading.Event().wait(2.5)  # past the moment the first one would have tried again
        acknowledged = fake.wait_acknowledged(fake.push_event(message_event()))

        assert [request["body"]["AppID"] for request in fake.endpoint_requests] == [
            "cli_first",
            "cli_first",
            "cli_second",
        ]
        assert acknowledged.json() == {"code": 200}
        assert (len(first.messages), len(second.messages)) == (0, 1)


def test_stopping_leaves_no_thread_and_reports_no_error(fake: FakeFeishu, caplog: pytest.LogCaptureFixture):
    with caplog.at_level("WARNING"):
        with running_adapter(fake) as adapter:
            fake.wait_acknowledged(fake.push_event(message_event()))
        connection = adapter.bot._sdk_connection

    assert connection.is_alive() is False
    assert fake.wait_until(lambda: fake.close_codes, what="the close") == [1000]
    # Not "Event loop stopped before Future completed", not "Task exception was never retrieved".
    assert [record.getMessage() for record in caplog.records if record.name != "Lark"] == []
    assert [record for record in caplog.records if "disconnected" not in record.getMessage()] == []


def test_the_client_s_own_clean_up_runs_on_the_loop_that_runs_the_client(fake: FakeFeishu):
    """The SDK schedules its fragment-cache sweep on the constructing thread's loop; a loop nobody runs never sweeps."""
    with running_adapter(fake) as adapter:
        connection = adapter.bot._sdk_connection
        client = fake.wait_until(lambda: connection._client, what="the client")

        assert client._cache._cron.get_loop() is connection._loop
        assert connection._loop.is_running()


def test_an_adapter_stopped_before_its_connection_exists_never_connects(fake: FakeFeishu):
    """A stop during a slow start (here: the bot-info lookup) finds no connection to stop; none may follow."""
    reached, release = threading.Event(), threading.Event()

    async def slow_bot_info() -> None:
        reached.set()
        await asyncio.to_thread(release.wait, 5)

    with patch.object(LarkConfig, "api_base_url", property(lambda _self: fake.url)):
        bot = FeishuBot(LarkConfig(app_id="cli_app", app_secret="socket-secret"))
        bot._fetch_bot_info = slow_bot_info  # type: ignore[method-assign]
        thread = threading.Thread(target=bot.run, daemon=True)
        thread.start()
        assert reached.wait(5)

        bot.stop()
        release.set()
        thread.join(timeout=5)
        threading.Event().wait(0.5)

    assert not thread.is_alive()
    assert bot.verify_stopped() is True
    assert (fake.connections_opened, fake.endpoint_requests) == (0, [])


def _client_that_retries_at_once(sdk: Any, **methods: Any) -> type:
    """The SDK's client with its own retry defaults (up to 30 s, then every 120 s) shortened, as an endpoint would."""

    base = sdk.Client

    def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
        base.__init__(self, *args, **kwargs)
        self._reconnect_nonce, self._reconnect_interval = 0, 0.05

    return type("RetriesAtOnce", (base,), {"__init__": __init__, **methods})


def _connection(fake: FakeFeishu) -> Any:
    return feishu_sdk_ws.SdkLongConnection(
        app_id="cli_app", app_secret="socket-secret", domain=fake.url, event_handler=FeishuEventDispatcher()
    )


@pytest.mark.parametrize("reply", ["withheld", "trickled"])
def test_an_endpoint_that_does_not_finish_answering_is_given_up_and_asked_again(fake: FakeFeishu, reply: str):
    """The SDK asks for its endpoint with a blocking request and no timeout, on the thread that runs everything."""
    if reply == "withheld":
        fake.endpoint_delays = [4.0]
    else:
        fake.endpoint_trickles = [4.0]  # never silent for 0.2 s, so no socket timeout ends it
    sdk = feishu_sdk_ws.sdk_ws_client_module()
    connection = _connection(fake)

    with (
        patch.object(feishu_sdk_ws, "_ENDPOINT_TIMEOUT", (1.0, 1.0)),
        patch.object(feishu_sdk_ws, "_ENDPOINT_DEADLINE_SECONDS", 0.5),
        patch.object(sdk, "Client", _client_that_retries_at_once(sdk)),
    ):
        connection.start()
        try:
            fake.wait_connected(timeout=3)  # sooner than the first reply would have ended
            assert len(fake.endpoint_requests) == 2
        finally:
            assert connection.stop() is True


@pytest.mark.parametrize("reply", ["withheld", "trickled"])
def test_a_connection_stopped_while_its_endpoint_is_answering_ends_at_once(fake: FakeFeishu, reply: str):
    if reply == "withheld":
        fake.endpoint_delays = [4.0]
    else:
        fake.endpoint_trickles = [4.0]
    connection = _connection(fake)
    connection.start()
    fake.wait_until(lambda: fake.endpoint_requests, what="the endpoint request")

    assert connection.stop(timeout=1) is True

    # The slot is free again: the next connection starts at once.
    with running_adapter(fake):
        pass
    assert fake.connections_opened == 1


def test_a_stop_between_two_steps_of_the_client_s_start_still_ends_it(fake: FakeFeishu):
    """The client's start runs one step after another: connect; on failure disconnect, then reconnect.

    A stop that lands after one step has ended and before the next has begun
    finds nothing running to cancel. The next step must not open a connection.
    """
    fake.endpoint_replies = [(503, {"msg": "busy"})]
    sdk = feishu_sdk_ws.sdk_ws_client_module()
    connection = _connection(fake)

    disconnect = sdk.Client._disconnect

    async def stopped_here(client: Any) -> None:
        connection._stop_requested.set()  # what stop() does first; at this exact step of the client's start
        await disconnect(client)

    with patch.object(sdk, "Client", _client_that_retries_at_once(sdk, _disconnect=stopped_here)):
        connection.start()
        fake.wait_until(lambda: not connection.is_alive(), timeout=5, what="the connection's thread to end")
        threading.Event().wait(0.5)  # ten of the client's retry intervals

    assert len(fake.endpoint_requests) == 1  # the 503, and no second request
    assert fake.connections_opened == 0
    assert connection._loop.is_closed()
    with running_adapter(fake):  # the slot is free
        pass


def test_an_event_that_arrives_after_the_stop_is_refused_not_taken(fake: FakeFeishu):
    """Feishu sends a refused event again, and then the connection that replaced this one gets it."""
    handled: List[bytes] = []

    class Handler:
        def _do_without_validation(self, payload: bytes) -> None:
            handled.append(payload)

    connection = feishu_sdk_ws.SdkLongConnection(
        app_id="cli_app", app_secret="socket-secret", domain=fake.url, event_handler=Handler()
    )
    connection.start()
    fake.wait_connected()
    try:
        handler = fake.wait_until(lambda: connection._client, what="the client")._event_handler
        handler._do_without_validation(b"{}")
        connection._stop_requested.set()
        with pytest.raises(Exception, match="stopping"):
            handler._do_without_validation(b"{}")
    finally:
        assert connection.stop() is True

    assert handled == [b"{}"]


def test_a_connection_that_cannot_get_a_loop_frees_the_slot(fake: FakeFeishu, caplog: pytest.LogCaptureFixture):
    uncaught: List[Any] = []
    with patch.object(threading, "excepthook", uncaught.append):
        with patch.object(feishu_sdk_ws.asyncio, "new_event_loop", side_effect=OSError(24, "Too many open files")):
            connection = _connection(fake)
            connection.start()
            fake.wait_until(lambda: not connection.is_alive(), what="the connection's thread to end")

    assert uncaught == []
    assert "Feishu WS thread could not run" in caplog.text
    with running_adapter(fake):  # the slot is free
        pass
    assert fake.connections_opened == 1


def test_stopping_twice_and_from_two_threads_ends_once_and_cleanly(fake: FakeFeishu):
    uncaught: List[Any] = []
    with patch.object(threading, "excepthook", uncaught.append):
        with running_adapter(fake) as adapter:
            connection = adapter.bot._sdk_connection
            stoppers = [threading.Thread(target=connection.stop) for _ in range(3)]
            for stopper in stoppers:
                stopper.start()
            for stopper in stoppers:
                stopper.join(timeout=10)
            assert connection.stop() is True

    assert uncaught == []
    assert connection._loop.is_closed()
    assert fake.wait_until(lambda: fake.close_codes, what="the close") == [1000]


def test_the_setup_wizard_s_temporary_connection_refuses_events(fake: FakeFeishu, monkeypatch: pytest.MonkeyPatch):
    """Feishu gives an event to one connection of the app; one that lands here must be sent again, not swallowed."""
    from vibe import api

    monkeypatch.setattr(api, "_lark_api_base", lambda _domain="feishu": fake.url)
    try:
        assert api.lark_temp_ws_start("cli_wizard", "wizard-secret", "feishu")["ok"] is True
        connection = api._temp_ws_connection
        fake.wait_connected()
        refused = fake.wait_acknowledged(fake.push_event(message_event()))
    finally:
        assert api.lark_temp_ws_stop() == {"ok": True}

    assert refused.json() == {"code": 500}
    assert fake.endpoint_requests[0]["body"] == {"AppID": "cli_wizard", "AppSecret": "wizard-secret"}
    # Stopping it ends its thread; it used to stay in the SDK's loop for good.
    assert connection.is_alive() is False
    assert api._temp_ws_connection is None


def test_a_wizard_connection_that_has_not_stopped_is_kept_and_said_so(monkeypatch: pytest.MonkeyPatch):
    from vibe import api

    class _Stuck:
        def stop(self, timeout: float = 5.0) -> bool:
            return False

    stuck = _Stuck()
    monkeypatch.setattr(api, "_temp_ws_connection", stuck)

    assert api.lark_temp_ws_stop()["ok"] is False
    assert api.lark_temp_ws_start("cli_wizard", "wizard-secret", "feishu")["ok"] is False
    assert api._temp_ws_connection is stuck
