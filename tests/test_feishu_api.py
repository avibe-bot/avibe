"""Feishu message calls over plain HTTPS, held against ``lark-oapi`` on the wire.

``FeishuApi`` replaced the SDK's request classes for five IM v1 endpoints. The
contract is that Feishu cannot tell the difference and neither can the adapter:
for the same call, both send the same method, path, query, JSON body and
authorisation, and both read the same reply the same way. A loopback stub
plays Feishu for the two of them.

The adapter-level tests cover what moved with the calls: the tenant token the
adapter now supplies itself, and how long an event id is remembered.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qsl, urlsplit

import pytest

from config.v2_config import LarkConfig
from modules.im import MessageContext
from modules.im.feishu import FeishuBot
from modules.im.feishu_api import FeishuApi, FeishuApiResponse

TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
CARD = json.dumps({"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": '你好 "quoted" \\ é'}]}})


class _StubFeishu:
    """A loopback HTTP server that records each call and answers from a script."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.token_requests: list[dict[str, Any]] = []
        self.token_reply: dict[str, Any] = {"code": 0, "msg": "ok", "tenant_access_token": "t-stub", "expire": 7200}
        self.reply_status = 200
        self.reply_body: bytes = b"{}"
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                url = urlsplit(self.path)
                if url.path == TOKEN_PATH:
                    stub.token_requests.append(json.loads(raw))
                    status, body = 200, json.dumps(stub.token_reply).encode()
                else:
                    stub.calls.append(
                        {
                            "method": self.command,
                            "path": url.path,
                            "query": sorted(parse_qsl(url.query)),
                            "body": json.loads(raw) if raw else None,
                            "authorization": self.headers.get("Authorization"),
                            "json_content_type": (self.headers.get("Content-Type") or "").startswith(
                                "application/json"
                            ),
                        }
                    )
                    status, body = stub.reply_status, stub.reply_body
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_PATCH = do_DELETE = do_GET = _serve

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def answer(self, status: int, body: Any) -> None:
        self.reply_status = status
        self.reply_body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def feishu():
    stub = _StubFeishu()
    yield stub
    stub.close()


# --- the same five calls, written for each side -----------------------------


def _sdk_calls(client: Any) -> dict[str, Callable[[], Awaitable[Any]]]:
    """The requests the adapter built with the SDK before ``FeishuApi``."""
    from lark_oapi.api.im.v1 import (
        CreateMessageReactionRequest,
        CreateMessageReactionRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        DeleteMessageRequest,
        Emoji,
        PatchMessageRequest,
        PatchMessageRequestBody,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
    )

    def create(receive_id_type: str, receive_id: str, msg_type: str, content: str):
        body = CreateMessageRequestBody.builder().receive_id(receive_id).msg_type(msg_type).content(content).build()
        request = CreateMessageRequest.builder().receive_id_type(receive_id_type).request_body(body).build()
        return client.im.v1.message.acreate(request)

    def reply(message_id: str, msg_type: str, content: str):
        body = ReplyMessageRequestBody.builder().msg_type(msg_type).content(content).reply_in_thread(True).build()
        return client.im.v1.message.areply(
            ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        )

    def patch_message(message_id: str, content: str):
        body = PatchMessageRequestBody.builder().content(content).build()
        return client.im.v1.message.apatch(
            PatchMessageRequest.builder().message_id(message_id).request_body(body).build()
        )

    def react(message_id: str, emoji_type: str):
        body = (
            CreateMessageReactionRequestBody.builder()
            .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
            .build()
        )
        return client.im.v1.message_reaction.acreate(
            CreateMessageReactionRequest.builder().message_id(message_id).request_body(body).build()
        )

    return {
        "create in a chat": lambda: create("chat_id", "oc_chat", "interactive", CARD),
        "create for a user": lambda: create("open_id", "ou_user", "interactive", CARD),
        "create a file message": lambda: create("chat_id", "oc_chat", "file", '{"file_key": "file_v3_1"}'),
        "reply in a thread": lambda: reply("om_parent", "interactive", CARD),
        "reply with an image": lambda: reply("om_parent", "image", '{"image_key": "img_v3_1"}'),
        "patch a card": lambda: patch_message("om_card", CARD),
        "delete a message": lambda: client.im.v1.message.adelete(
            DeleteMessageRequest.builder().message_id("om_gone").build()
        ),
        "add a reaction": lambda: react("om_seen", "OnIt"),
        "an id that is not a plain segment": lambda: patch_message("om/1?x=1#y z", CARD),
    }


def _own_calls(api: FeishuApi) -> dict[str, Callable[[], Awaitable[FeishuApiResponse]]]:
    return {
        "create in a chat": lambda: api.create_message("chat_id", "oc_chat", "interactive", CARD),
        "create for a user": lambda: api.create_message("open_id", "ou_user", "interactive", CARD),
        "create a file message": lambda: api.create_message("chat_id", "oc_chat", "file", '{"file_key": "file_v3_1"}'),
        "reply in a thread": lambda: api.reply_message("om_parent", "interactive", CARD, reply_in_thread=True),
        "reply with an image": lambda: api.reply_message(
            "om_parent", "image", '{"image_key": "img_v3_1"}', reply_in_thread=True
        ),
        "patch a card": lambda: api.patch_message("om_card", CARD),
        "delete a message": lambda: api.delete_message("om_gone"),
        "add a reaction": lambda: api.add_reaction("om_seen", "OnIt"),
        "an id that is not a plain segment": lambda: api.patch_message("om/1?x=1#y z", CARD),
    }


CALL_NAMES = [
    "create in a chat",
    "create for a user",
    "create a file message",
    "reply in a thread",
    "reply with an image",
    "patch a card",
    "delete a message",
    "add a reaction",
    "an id that is not a plain segment",
]

REPLIES = {
    "success": (200, {"code": 0, "msg": "success", "data": {"message_id": "om_new", "chat_id": "oc_chat"}}),
    "refused by Feishu": (400, {"code": 230002, "msg": "Bot/User can NOT be out of the chat.", "data": {}}),
    "rate limited": (429, {"code": 99991400, "msg": "request trigger frequency limit"}),
    "refused without data": (200, {"code": 230001, "msg": "Your request contains an invalid request parameter."}),
}


def _outcome(reply: Any, sends_a_message: bool) -> tuple:
    """What the adapter reads from a reply: the envelope, and the new message where one was sent."""
    envelope = (reply.success(), reply.code, reply.msg)
    if not sends_a_message:
        return envelope
    data = reply.data
    read = data.get if isinstance(data, dict) else (lambda key: getattr(data, key, None))
    return (*envelope, read("message_id"), read("chat_id"))


async def _settle(call: Callable[[], Awaitable[Any]], sends_a_message: bool = True) -> tuple:
    """What the caller gets: the reply as it reads it, or the exception type it catches."""
    try:
        return ("reply", _outcome(await call(), sends_a_message))
    except Exception as exc:
        return ("raised", type(exc))


def _sdk_client(feishu: _StubFeishu, app_id: str) -> Any:
    import lark_oapi as lark

    return lark.Client.builder().app_id(app_id).app_secret("stub-secret").domain(feishu.url).build()


async def _token() -> str:
    return "t-stub"


@pytest.mark.parametrize("reply_name", list(REPLIES))
@pytest.mark.parametrize("call_name", CALL_NAMES)
async def test_request_and_reading_match_the_sdk(feishu: _StubFeishu, call_name: str, reply_name: str):
    status, body = REPLIES[reply_name]
    feishu.answer(status, body)

    sends_a_message = call_name.startswith(("create", "reply"))

    theirs = await _settle(_sdk_calls(_sdk_client(feishu, "cli_differential"))[call_name], sends_a_message)
    ours = await _settle(_own_calls(FeishuApi(feishu.url, _token))[call_name], sends_a_message)

    sdk_request, own_request = feishu.calls
    assert own_request == sdk_request
    assert ours == theirs
    assert theirs[0] == "reply"


@pytest.mark.parametrize(
    "status, body",
    [(502, b"<html>Bad Gateway</html>"), (200, b""), (500, b"upstream connect error")],
    ids=["gateway page", "empty body", "plain text"],
)
async def test_a_reply_that_is_not_json_fails_as_it_did_with_the_sdk(feishu: _StubFeishu, status: int, body: bytes):
    feishu.answer(status, body)

    theirs = await _settle(_sdk_calls(_sdk_client(feishu, "cli_differential"))["create in a chat"])
    ours = await _settle(_own_calls(FeishuApi(feishu.url, _token))["create in a chat"])

    assert ours == theirs == ("raised", json.JSONDecodeError)


async def test_a_call_without_a_token_is_never_sent(feishu: _StubFeishu):
    async def refused() -> str:
        raise RuntimeError("obtain self tenant access token failed, code: 10014, msg: app secret invalid")

    with pytest.raises(RuntimeError, match="obtain self tenant access token failed"):
        await FeishuApi(feishu.url, refused).create_message("chat_id", "oc_chat", "interactive", CARD)

    assert feishu.calls == []


# --- the adapter: token and send path ----------------------------------------


def _bot(feishu: _StubFeishu) -> FeishuBot:
    with patch.object(LarkConfig, "api_base_url", property(lambda _self: feishu.url)):
        return FeishuBot(LarkConfig(app_id="cli_adapter", app_secret="adapter-secret"))


@pytest.fixture
def without_sdk():
    """Make any ``import lark_oapi`` fail, as it will once the SDK is gone."""
    blocked = {name: None for name in list(sys.modules) if name == "lark_oapi" or name.startswith("lark_oapi.")}
    blocked["lark_oapi"] = None
    with patch.dict(sys.modules, blocked):
        yield


async def test_adapter_sends_a_message_without_the_sdk(feishu: _StubFeishu, without_sdk: None):
    feishu.answer(200, {"code": 0, "msg": "success", "data": {"message_id": "om_sent"}})
    bot = _bot(feishu)
    context = MessageContext(user_id="ou_user", channel_id="oc_chat", platform="lark")

    with patch.object(LarkConfig, "api_base_url", property(lambda _self: feishu.url)):
        first = await bot.send_message(context, "hello")
        second = await bot.send_message(context, "again")

    assert (first, second) == ("om_sent", "om_sent")
    # One token for both sends, asked for with the app's own credentials.
    assert feishu.token_requests == [{"app_id": "cli_adapter", "app_secret": "adapter-secret"}]
    assert [call["authorization"] for call in feishu.calls] == ["Bearer t-stub", "Bearer t-stub"]
    assert feishu.calls[0]["query"] == [("receive_id_type", "chat_id")]
    assert feishu.calls[0]["body"]["receive_id"] == "oc_chat"


async def test_feishu_refusing_the_token_fails_the_send_with_its_reason(feishu: _StubFeishu, without_sdk: None):
    feishu.token_reply = {"code": 10014, "msg": "app secret invalid"}
    bot = _bot(feishu)
    context = MessageContext(user_id="ou_user", channel_id="oc_chat", platform="lark")

    with patch.object(LarkConfig, "api_base_url", property(lambda _self: feishu.url)):
        with pytest.raises(RuntimeError) as failure:
            await bot.send_message(context, "hello")
        quiet = await bot._get_tenant_token()

    # The SDK's wording for the same refusal, and nothing of the secret.
    assert str(failure.value) == "obtain self tenant access token failed, code: 10014, msg: app secret invalid"
    assert "adapter-secret" not in str(failure.value)
    assert quiet is None
    assert feishu.calls == []


# --- the adapter: how long an event is remembered ----------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock():
    clock = _Clock()
    with patch("modules.im.feishu.time.time", clock.time):
        yield clock


@pytest.mark.parametrize("delay", [15, 5 * 60, 60 * 60, 6 * 60 * 60], ids=["15 s", "5 min", "1 h", "6 h"])
def test_an_event_feishu_sends_again_is_dropped_at_each_of_its_retries(clock: _Clock, delay: int):
    bot = FeishuBot(LarkConfig(app_id="cli_adapter", app_secret="adapter-secret"))
    assert bot._is_duplicate_event("evt_1") is False

    clock.now += delay

    assert bot._is_duplicate_event("evt_1") is True
    assert bot._is_duplicate_event("evt_2") is False


def test_event_ids_are_forgotten_after_the_last_retry(clock: _Clock):
    bot = FeishuBot(LarkConfig(app_id="cli_adapter", app_secret="adapter-secret"))
    for index in range(50):
        bot._is_duplicate_event(f"evt_{index}")

    clock.now += 7 * 60 * 60 + 61
    assert bot._is_duplicate_event("evt_later") is False

    assert list(bot._recent_event_ids) == ["evt_later"]
    assert bot._is_duplicate_event("evt_0") is False


def _card_click() -> dict[str, Any]:
    return {
        "operator": {"open_id": "ou_user"},
        "action": {"tag": "button", "value": {"key": "stop"}},
        "context": {"open_message_id": "om_card", "open_chat_id": "oc_chat"},
    }


async def test_the_same_card_click_goes_through_again_after_half_a_minute(clock: _Clock):
    bot = FeishuBot(LarkConfig(app_id="cli_adapter", app_secret="adapter-secret"))
    # The step after deduplication is the authorisation check: count the clicks that reach it.
    bot.check_authorization = Mock(return_value=SimpleNamespace(allowed=False, denial="not_admin"))  # type: ignore[method-assign]
    bot._send_auth_denial = AsyncMock()  # type: ignore[method-assign]

    await bot._async_handle_card_action(_card_click())
    clock.now += 10
    await bot._async_handle_card_action(_card_click())
    assert bot.check_authorization.call_count == 1

    clock.now += 31
    await bot._async_handle_card_action(_card_click())
    assert bot.check_authorization.call_count == 2
