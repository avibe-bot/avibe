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

import asyncio
import json
import logging
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qsl, urlsplit

import pytest

from config.v2_config import LarkConfig
from modules.im import MessageContext
from modules.im.feishu import FeishuBot
from modules.im.feishu_api import FeishuApi, FeishuApiResponse

ROOT = Path(__file__).resolve().parents[1]
TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
CARD = json.dumps({"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": '你好 "quoted" \\ é'}]}})


class _StubFeishu:
    """A loopback HTTP server that records each call and answers from a script."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.token_requests: list[dict[str, Any]] = []
        self.token_reply: Any = {"code": 0, "msg": "ok", "tenant_access_token": "t-stub", "expire": 7200}
        self.token_content_type = "application/json"
        self.token_delay = 0.0
        self.reply_status = 200
        self.reply_body: bytes = b"{}"
        self.reply_headers: dict[str, str] = {}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                url = urlsplit(self.path)
                headers = {"Content-Type": "application/json"}
                if url.path == TOKEN_PATH:
                    stub.token_requests.append(json.loads(raw))
                    if stub.token_delay:
                        threading.Event().wait(stub.token_delay)
                    reply = stub.token_reply
                    status, body = 200, reply if isinstance(reply, bytes) else json.dumps(reply).encode()
                    headers["Content-Type"] = stub.token_content_type
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
                    headers.update(stub.reply_headers)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_PATCH = do_DELETE = do_GET = _serve

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()

    def answer(self, status: int, body: Any, headers: Optional[dict[str, str]] = None) -> None:
        self.reply_status = status
        self.reply_body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.reply_headers = headers or {}

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def feishu(monkeypatch: pytest.MonkeyPatch):
    # The SDK's HTTP clients read proxy settings from the environment; loopback must not go through one.
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    stub = _StubFeishu()
    yield stub
    stub.close()


# --- the same calls, written for each side ----------------------------------


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

    def create(receive_id_type: str, receive_id: Optional[str], msg_type: str, content: str):
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
        "create without a chat id": lambda: create("chat_id", None, "interactive", CARD),
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
        "create without a chat id": lambda: api.create_message("chat_id", None, "interactive", CARD),  # type: ignore[arg-type]
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
    "create without a chat id",
    "reply in a thread",
    "reply with an image",
    "patch a card",
    "delete a message",
    "add a reaction",
    "an id that is not a plain segment",
]

# name -> (HTTP status, body, extra headers)
REPLIES: dict[str, tuple[int, Any, dict[str, str]]] = {
    "success": (200, {"code": 0, "msg": "success", "data": {"message_id": "om_new", "chat_id": "oc_chat"}}, {}),
    "refused by Feishu": (400, {"code": 230002, "msg": "Bot/User can NOT be out of the chat.", "data": {}}, {}),
    "rate limited": (429, {"code": 99991400, "msg": "request trigger frequency limit"}, {}),
    "refused without data": (200, {"code": 230001, "msg": "Your request contains an invalid request parameter."}, {}),
    "success with an empty data object": (200, {"code": 0, "msg": "success", "data": {}}, {}),
    "success without data": (200, {"code": 0, "msg": "success"}, {}),
    "success with null data": (200, {"code": 0, "msg": "success", "data": None}, {}),
    "success with data that is a list": (200, {"code": 0, "msg": "success", "data": ["om_new"]}, {}),
    "a null envelope": (200, b"null", {}),
    "an envelope that is a list": (200, b"[]", {}),
    "an envelope that is a string": (200, b'"ok"', {}),
    "a redirect": (307, {"code": 123, "msg": "moved"}, {"Location": "/open-apis/elsewhere"}),
}


async def _as_the_adapter_reads(call: Callable[[], Awaitable[Any]], sends_a_message: bool, read_message_id) -> tuple:
    """What a call site ends up with: the envelope, the new message id after a send, or a failure.

    The message id is read the way the call sites read it, only after a
    success, so a reply that made them fail before makes them fail now.
    """
    try:
        reply = await call()
        envelope = (reply.success(), reply.code, reply.msg)
        if sends_a_message and reply.success():
            return ("reply", envelope, read_message_id(reply))
        return ("reply", envelope)
    except Exception as exc:
        return ("raised", json.JSONDecodeError) if isinstance(exc, json.JSONDecodeError) else ("raised",)


def _sdk_client(feishu: _StubFeishu, app_id: str) -> Any:
    import lark_oapi as lark

    return lark.Client.builder().app_id(app_id).app_secret("stub-secret").domain(feishu.url).build()


async def _token() -> str:
    return "t-stub"


async def _both(feishu: _StubFeishu, call_name: str) -> tuple[tuple, tuple]:
    sends_a_message = call_name.startswith(("create", "reply"))
    theirs = await _as_the_adapter_reads(
        _sdk_calls(_sdk_client(feishu, "cli_differential"))[call_name],
        sends_a_message,
        lambda reply: reply.data.message_id,
    )
    ours = await _as_the_adapter_reads(
        _own_calls(FeishuApi(feishu.url, _token))[call_name],
        sends_a_message,
        lambda reply: reply.data.get("message_id"),
    )
    return theirs, ours


@pytest.mark.parametrize("reply_name", list(REPLIES))
@pytest.mark.parametrize("call_name", CALL_NAMES)
async def test_request_and_reading_match_the_sdk(feishu: _StubFeishu, call_name: str, reply_name: str):
    feishu.answer(*REPLIES[reply_name])

    theirs, ours = await _both(feishu, call_name)

    # One request each, and the same one: neither follows the redirect.
    sdk_request, own_request = feishu.calls
    assert own_request == sdk_request
    assert ours == theirs


@pytest.mark.parametrize(
    "status, body",
    [(502, b"<html>Bad Gateway</html>"), (200, b""), (500, b"upstream connect error")],
    ids=["gateway page", "empty body", "plain text"],
)
async def test_a_reply_that_is_not_json_fails_as_it_did_with_the_sdk(feishu: _StubFeishu, status: int, body: bytes):
    feishu.answer(status, body)

    theirs, ours = await _both(feishu, "create in a chat")

    assert ours == theirs == ("raised", json.JSONDecodeError)


async def test_a_call_without_a_token_is_never_sent(feishu: _StubFeishu):
    async def refused() -> str:
        raise RuntimeError("obtain self tenant access token failed, code: 10014, msg: app secret invalid")

    with pytest.raises(RuntimeError, match="obtain self tenant access token failed"):
        await FeishuApi(feishu.url, refused).create_message("chat_id", "oc_chat", "interactive", CARD)

    assert feishu.calls == []


# --- the adapter: token and send path ----------------------------------------


@pytest.fixture
def bot(feishu: _StubFeishu):
    with patch.object(LarkConfig, "api_base_url", property(lambda _self: feishu.url)):
        yield FeishuBot(LarkConfig(app_id="cli_adapter", app_secret="adapter-secret"))


CONTEXT = MessageContext(user_id="ou_user", channel_id="oc_chat", platform="lark")
SENT = {"code": 0, "msg": "success", "data": {"message_id": "om_sent"}}


def test_adapter_sends_a_message_in_a_process_that_cannot_import_the_sdk(feishu: _StubFeishu):
    """A fresh interpreter, so that an SDK import anywhere on the way (module level included) fails it."""
    feishu.answer(200, SENT)
    script = f"""
import asyncio, sys
sys.modules["lark_oapi"] = None  # any import of the SDK now raises ImportError
from unittest.mock import patch
from config.v2_config import LarkConfig
from modules.im import MessageContext
from modules.im.feishu import FeishuBot

async def main():
    with patch.object(LarkConfig, "api_base_url", property(lambda _self: {feishu.url!r})):
        bot = FeishuBot(LarkConfig(app_id="cli_adapter", app_secret="adapter-secret"))
        context = MessageContext(user_id="ou_user", channel_id="oc_chat", platform="lark")
        print(await bot.send_message(context, "hello"), await bot.send_message(context, "again"))

asyncio.run(main())
"""
    done = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=120)

    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.split() == ["om_sent", "om_sent"]
    # One token for both sends, asked for with the app's own credentials.
    assert feishu.token_requests == [{"app_id": "cli_adapter", "app_secret": "adapter-secret"}]
    assert [call["authorization"] for call in feishu.calls] == ["Bearer t-stub", "Bearer t-stub"]
    assert feishu.calls[0]["query"] == [("receive_id_type", "chat_id")]
    assert feishu.calls[0]["body"]["receive_id"] == "oc_chat"


async def test_sends_that_start_together_share_one_token_request(feishu: _StubFeishu, bot: FeishuBot):
    feishu.answer(200, SENT)
    feishu.token_delay = 0.2

    sent = await asyncio.gather(*(bot.send_message(CONTEXT, f"message {index}") for index in range(3)))

    assert sent == ["om_sent"] * 3
    assert len(feishu.token_requests) == 1


@pytest.mark.parametrize("content_type", ["application/json", "text/plain"])
async def test_feishu_refusing_the_token_fails_the_send_with_its_reason(
    feishu: _StubFeishu, bot: FeishuBot, content_type: str, caplog: pytest.LogCaptureFixture
):
    # The reply quotes the secret back: the worst a refusal could do.
    feishu.token_reply = {"code": 10014, "msg": "app secret invalid: adapter-secret"}
    feishu.token_content_type = content_type

    with caplog.at_level(logging.ERROR, logger="modules.im.feishu"):
        with pytest.raises(RuntimeError) as failure:
            await bot.send_message(CONTEXT, "hello")
        quiet = await bot._get_tenant_token()

    # The SDK's wording for the same refusal, and nothing of the secret, in the error or in the log.
    assert str(failure.value) == "obtain self tenant access token failed, code: 10014, msg: app secret invalid: ***"
    assert quiet is None
    assert "adapter-secret" not in caplog.text
    assert "code: 10014" in caplog.text
    assert feishu.calls == []


async def test_a_token_reply_that_is_not_json_is_a_decoding_failure(feishu: _StubFeishu, bot: FeishuBot):
    feishu.token_reply = b"<html>Bad Gateway</html>"

    with pytest.raises(json.JSONDecodeError):
        await bot.send_message(CONTEXT, "hello")

    assert await bot._get_tenant_token() is None
    assert feishu.calls == []


@pytest.mark.parametrize(
    "expire, seconds_later, token_requests",
    [(7200, 6599, 1), (7200, 6601, 2), (30, 31, 2), (None, 1, 2)],
    ids=["within the lifetime", "ten minutes before expiry", "a lifetime shorter than the margin", "no lifetime given"],
)
async def test_a_token_is_reused_only_while_feishu_says_it_is_valid(
    feishu: _StubFeishu, bot: FeishuBot, clock: "_Clock", expire: Optional[int], seconds_later: int, token_requests: int
):
    feishu.answer(200, SENT)
    feishu.token_reply = {"code": 0, "msg": "ok", "tenant_access_token": "t-stub"}
    if expire is not None:
        feishu.token_reply["expire"] = expire

    await bot.send_message(CONTEXT, "first")
    clock.now += seconds_later
    await bot.send_message(CONTEXT, "second")

    assert len(feishu.token_requests) == token_requests


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


def test_a_key_renewed_while_another_thread_sweeps_is_not_lost(clock: _Clock):
    """The long connection's thread and the adapter's loop share the table."""
    bot = FeishuBot(LarkConfig(app_id="cli_adapter", app_secret="adapter-secret"))
    bot._is_duplicate_event("card:click", 30)
    clock.now += 61  # expired, and a sweep is due

    sweeping = threading.Event()
    renewed = threading.Event()

    class _PausingTable(dict):
        """Stops the sweeping thread after it has read the expired entry, before it deletes it."""

        def items(self):
            snapshot = list(super().items())
            if threading.current_thread().name == "sweeper":
                sweeping.set()
                renewed.wait(0.5)
            return snapshot

    bot._recent_event_ids = _PausingTable(bot._recent_event_ids)
    sweeper = threading.Thread(target=bot._is_duplicate_event, args=("evt_other",), name="sweeper")
    sweeper.start()
    assert sweeping.wait(5)

    # The other thread accepts a new click on the same key while the sweep is under way...
    accepted_as_new = bot._is_duplicate_event("card:click", 30)
    renewed.set()
    sweeper.join(5)

    # ...and the same click straight after is still recognised.
    assert accepted_as_new is False
    assert bot._is_duplicate_event("card:click", 30) is True


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

    await bot._async_handle_card_action(_card_click())  # accepted at 0 s
    clock.now += 10
    await bot._async_handle_card_action(_card_click())  # a duplicate, which must not extend the memory
    clock.now += 19
    await bot._async_handle_card_action(_card_click())  # 29 s after the accepted click
    assert bot.check_authorization.call_count == 1

    clock.now += 2
    await bot._async_handle_card_action(_card_click())  # 31 s after it
    assert bot.check_authorization.call_count == 2
