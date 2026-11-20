"""Our own Feishu long connection, held against ``lark-oapi``'s client.

Feishu does not document the wire protocol, so the reference is the SDK the
adapter used until now. Three layers:

- the frame codec against the SDK's generated protobuf class, byte for byte;
- the adapter run twice against the same fake Feishu with the same script,
  once on each client: what the adapter received and what Feishu received must
  be the same;
- what a connection does when it is dropped, refused or stopped, for both
  clients where the SDK does it in a testable time, for ours alone otherwise.
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import socket
import threading
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import patch

import pytest

from modules.im import feishu_ws
from modules.im.feishu_sdk_ws import SdkLongConnection
from modules.im.feishu_ws import (
    ConnectionRefused,
    EventPieces,
    FeishuLongConnection,
    FrameError,
    FrameTap,
    answer_frame,
    decode_frame,
    encode_frame,
    ping_frame,
)
from tests.feishu_fake import FakeFeishu, card_action_event, message_event, running_adapter

BOTH = ["sdk", "own"]


def _sdk() -> Any:
    import lark_oapi.ws.client as sdk

    return sdk


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


# --- the codec, against the SDK's protobuf class -------------------------------


def _sdk_frame(**fields: Any) -> Any:
    frame = _sdk().Frame()
    headers = fields.pop("headers", [])
    for name, value in fields.items():
        setattr(frame, name, value)
    for key, value in headers:
        header = frame.headers.add()
        header.key, header.value = key, value
    return frame


def _random_frames(count: int) -> List[Any]:
    rnd = random.Random(20261120)
    frames = []
    for _ in range(count):
        fields: Dict[str, Any] = {
            "SeqID": rnd.getrandbits(rnd.choice([1, 20, 63, 64])),
            "LogID": rnd.getrandbits(rnd.choice([1, 40, 64])),
            "service": rnd.choice([0, 1, 33554678, 2**31 - 1, -1, -(2**31)]),
            "method": rnd.choice([0, 1]),
            "headers": [
                ("type", rnd.choice(["event", "card", "pong"])),
                ("message_id", f"{rnd.getrandbits(64):x}"),
                ("sum", str(rnd.randint(1, 4))),
                ("seq", "0"),
                ("trace_id", "链路-" + f"{rnd.getrandbits(32):x}"),
            ][: rnd.randint(0, 5)],
        }
        if rnd.random() < 0.5:
            fields["payload_encoding"] = "gzip"
        if rnd.random() < 0.5:
            fields["payload_type"] = "json"
        if rnd.random() < 0.9:
            fields["payload"] = rnd.randbytes(rnd.choice([0, 1, 200, 70000]))
        if rnd.random() < 0.5:
            fields["LogIDNew"] = f"{rnd.getrandbits(48):x}"
        frames.append(_sdk_frame(**fields))
    return frames


def test_frames_decode_and_encode_as_the_sdk_s_protobuf_does():
    for reference in _random_frames(500):
        wire = reference.SerializeToString()

        frame = decode_frame(wire)

        assert (frame.seq_id, frame.log_id, frame.service, frame.method) == (
            reference.SeqID,
            reference.LogID,
            reference.service,
            reference.method,
        )
        assert frame.headers == [(header.key, header.value) for header in reference.headers]
        assert (frame.payload or b"") == reference.payload
        assert (frame.payload is not None) == reference.HasField("payload")
        assert encode_frame(frame) == wire


def test_a_ping_is_the_sdk_s_ping():
    sdk = _sdk()

    assert encode_frame(ping_frame(33554678)) == sdk._new_ping_frame(33554678).SerializeToString()


@pytest.mark.parametrize(
    "code, result",
    [(200, None), (500, None), (200, {}), (200, {"toast": {"type": "info", "content": "已收到"}})],
    ids=["handled", "failed", "an empty card answer", "a card answer with text"],
)
def test_an_answer_is_built_as_the_sdk_builds_it(code: int, result: Optional[Dict[str, Any]]):
    sdk = _sdk()
    for reference in _random_frames(50):
        received = decode_frame(reference.SerializeToString())

        # lark_oapi/ws/client.py, ``_handle_data_frame``: add ``biz_rt`` (not for a failure), put the response in the payload.
        if code == 200:
            header = reference.headers.add()
            header.key, header.value = "biz_rt", "17"
        response = sdk.Response(code=code)
        if result is not None:
            response.data = base64.b64encode(sdk.JSON.marshal(result).encode("utf-8"))
        reference.payload = sdk.JSON.marshal(response).encode("utf-8")

        ours = encode_frame(answer_frame(received, code=code, result=result, handled_ms=17 if code == 200 else None))

        assert ours == reference.SerializeToString()


def _reference(wire: bytes) -> Any:
    frame = _sdk().Frame()
    frame.ParseFromString(wire)
    return frame


KNOWN = b"\x08\x07\x10\x08\x18\x09\x20\x01"  # SeqID 7, LogID 8, service 9, method 1
HEADER = b"\x0a\x04type\x12\x05event"  # key "type", value "event"



def _tag(number: int, wire: int) -> bytes:
    """A field's tag, written out by hand: the test must not lean on the codec it tests."""
    key, out = number << 3 | wire, bytearray()
    while key > 0x7F:
        out.append(key & 0x7F | 0x80)
        key >>= 7
    out.append(key)
    return bytes(out)


UNUSUAL_FRAMES = {
    "a varint and a bytes field no schema has": KNOWN + _tag(15, 0) + b"\x2a" + _tag(16, 2) + b"\x03new",
    "a group no schema has": KNOWN + _tag(20, 3) + _tag(1, 0) + b"\x05" + _tag(20, 4),
    "a group inside a group": KNOWN + _tag(20, 3) + _tag(21, 3) + b"\x08\x01" + _tag(21, 4) + _tag(20, 4),
    "a fixed32 and a fixed64 no schema has": KNOWN + _tag(17, 5) + b"\x01\x02\x03\x04" + _tag(18, 1) + bytes(8),
    "a varint no schema has, written longer than need be": KNOWN + _tag(15, 0) + b"\xaa\x80\x00",
    "a field no schema has inside a header": KNOWN + b"\x2a" + bytes([len(HEADER) + 2]) + HEADER + b"\x18\x01",
    "service as minus one in five bytes": b"\x08\x07\x10\x08\x18\xff\xff\xff\xff\x0f\x20\x01",
    "service as minus one in ten bytes": b"\x08\x07\x10\x08\x18" + b"\xff" * 9 + b"\x01\x20\x01",
    "method above 32 bits": b"\x08\x07\x10\x08\x18\x09\x20\x81\x80\x80\x80\x10",
    "a known field twice": KNOWN + b"\x18\x0a",
    "a known field with the wrong wire type": KNOWN + b"\x1a\x01x",
    "an empty payload": KNOWN + b"\x42\x00",
    "a known field under a tag written longer than need be": KNOWN + b"\x88\x00\x07",
    "a header under a tag written longer than need be": KNOWN + b"\xaa\x00" + bytes([len(HEADER)]) + HEADER,
}


@pytest.mark.parametrize("name", list(UNUSUAL_FRAMES))
def test_unusual_frames_are_read_and_written_as_the_sdk_does(name: str):
    wire = UNUSUAL_FRAMES[name]
    reference = _reference(wire)

    frame = decode_frame(wire)

    assert (frame.seq_id, frame.log_id, frame.service, frame.method) == (
        reference.SeqID,
        reference.LogID,
        reference.service,
        reference.method,
    )
    assert frame.headers == [(header.key, header.value) for header in reference.headers]
    assert encode_frame(frame) == reference.SerializeToString()


@pytest.mark.parametrize(
    "wire",
    [
        b"\x08\x07\x10\x08\x18\x09",
        b"",
        KNOWN + b"\x2a\x06\x0a\x04type",
        KNOWN + b"\x2a\x00",
        b"\x88\x00\x07\x10\x08\x18\x09\x20\x01",
        KNOWN + b"\x2a\x0e\x8a\x00\x04type\x12\x05event",
    ],
    ids=[
        "no method",
        "nothing at all",
        "a header without a value",
        "an empty header",
        "SeqID only under a tag written longer than need be",
        "a header's key only under such a tag",
    ],
)
def test_a_frame_without_a_required_field_is_read_and_cannot_be_written_back(wire: bytes):
    """As with the SDK's class: it parses, and serialising it again is refused."""
    reference = _reference(wire)
    with pytest.raises(Exception, match="required"):
        reference.SerializeToString()

    frame = decode_frame(wire)

    assert frame.headers == [(header.key, header.value) for header in reference.headers]
    with pytest.raises(FrameError, match="required"):
        encode_frame(frame)


@pytest.mark.parametrize(
    "wire",
    [
        b"\x08",
        b"\x42\x05ab",
        b"\x0e\x01",
        b"\x32\x02\xff\xfe",
        _tag(20, 3) + b"\x08\x01",
        _tag(20, 4),
        KNOWN + b"\x00\x01",
    ],
    ids=[
        "a varint cut short",
        "a field cut short",
        "a wire type nobody sends",
        "text that is not UTF-8",
        "a group that never ends",
        "the end of a group that never began",
        "field number zero",
    ],
)
def test_bytes_that_are_not_a_frame_are_refused_by_both(wire: bytes):
    with pytest.raises(Exception):
        _reference(wire)
    with pytest.raises(FrameError):
        decode_frame(wire)


# --- an event in several pieces ---------------------------------------------------


def test_pieces_are_joined_in_order_whatever_order_they_arrive_in():
    pieces = EventPieces()

    assert pieces.add("m1", 3, 2, b"C") is None
    assert pieces.add("m2", 2, 0, b"x") is None
    assert pieces.add("m1", 3, 0, b"A") is None
    assert pieces.add("m1", 3, 1, b"B") == b"ABC"
    assert pieces.add("m2", 2, 1, b"y") == b"xy"
    assert pieces.add("m3", 1, 0, b"whole") == b"whole"


def test_pieces_of_an_event_that_never_completes_are_dropped_after_five_seconds():
    now = [1000.0]
    with patch.object(feishu_ws.time, "monotonic", lambda: now[0]):
        pieces = EventPieces()
        assert pieces.add("m1", 2, 0, b"A") is None
        now[0] += 4.9
        assert pieces.add("m2", 2, 0, b"x") is None
        now[0] += 5.1  # m1's piece is 10 s old, m2's 5.1 s

        # A late second piece finds nothing to join; the event is Feishu's to send again.
        assert pieces.add("m1", 2, 1, b"B") is None
        assert pieces.add("m2", 2, 1, b"y") is None


@pytest.mark.parametrize("total, index", [(2, 2), (2, -1), (3, 7)])
def test_a_piece_number_outside_the_event_is_refused(total: int, index: int):
    with pytest.raises(FrameError):
        EventPieces().add("m1", total, index, b"A")


# --- the same script on each client ---------------------------------------------

UNKNOWN_EVENT = {"schema": "2.0", "header": {"event_id": "e_unknown", "event_type": "im.chat.updated_v1"}, "event": {}}
UNREADABLE_EVENT = {**message_event(event_id="e_unreadable"), "event": "text"}


def _run_script(fake: FakeFeishu, mode: str) -> Dict[str, Any]:
    """Run the adapter on ``mode``'s client through one fixed script; return both sides' view of it."""
    long_text = "长" * 6000
    with running_adapter(fake, long_connection=mode) as adapter:
        fake.wait_until(fake.pings, what="the first ping")
        fake.push_event(message_event(event_id="e1", text="one"), message_id="m1")
        fake.push_event(card_action_event(), message_id="m2")
        fake.push_frames(list(reversed(fake.event_frames(message_event(event_id="e3", text=long_text), pieces=4, message_id="m3"))))
        fake.push_event(UNREADABLE_EVENT, message_id="m4")
        fake.push_event(UNKNOWN_EVENT, message_id="m5")
        fake.push_event(message_event(event_id="e1", text="one"), message_id="m6")  # Feishu sending e1 again
        acknowledged = [fake.wait_acknowledged(f"m{number}") for number in range(1, 7)]
    return {
        "endpoint request": fake.endpoint_requests,
        "first ping": fake.pings()[0].raw,
        "answers": [frame.bytes_without_timing() for frame in acknowledged],
        "answers, read": [(frame.headers["message_id"], frame.headers["seq"], frame.json()) for frame in acknowledged],
        "messages the adapter handled": adapter.messages,
        "clicks the adapter handled": adapter.clicks,
        "frames Feishu received in all": len(fake.received),
    }


def test_both_clients_do_the_same_with_the_same_script(monkeypatch: pytest.MonkeyPatch):
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    seen = {}
    for mode in BOTH:
        server = FakeFeishu().start()
        try:
            seen[mode] = _run_script(server, mode)
        finally:
            server.close()

    assert seen["own"] == seen["sdk"]
    # And the script did what it was written to do.
    assert seen["sdk"]["answers, read"] == [
        ("m1", "0", {"code": 200}),
        ("m2", "0", {"code": 200, "data": "e30="}),
        ("m3", "0", {"code": 200}),  # the pieces arrived last first: answered on piece 0
        ("m4", "0", {"code": 500}),
        ("m5", "0", {"code": 200}),
        ("m6", "0", {"code": 200}),
    ]
    assert len(seen["sdk"]["messages the adapter handled"]) == 2
    assert seen["sdk"]["endpoint request"] == [
        {"body": {"AppID": "cli_app", "AppSecret": "socket-secret"}, "locale": "zh"}
    ]


# --- dropped, refused, stopped -------------------------------------------------------


@pytest.mark.parametrize("mode", BOTH)
def test_a_dropped_connection_is_replaced_through_a_new_endpoint(fake: FakeFeishu, mode: str):
    with running_adapter(fake, long_connection=mode) as adapter:
        fake.drop_connection()
        fake.wait_connected(2)
        acknowledged = fake.wait_acknowledged(fake.push_event(message_event()))

    assert len(fake.endpoint_requests) == 2
    assert acknowledged.connection == 1
    assert len(adapter.messages) == 1


@pytest.mark.parametrize("mode", BOTH)
@pytest.mark.parametrize(
    "replies",
    [
        [{"code": 1, "msg": "system busy"}],
        [{"code": 1000040343, "msg": "internal error"}],
        [(503, {"msg": "upstream unavailable"})],
        [(502, {})],
        [(200, ["a gateway's page, not Feishu's answer"])],
    ],
    ids=["busy", "internal error", "an HTTP error with a message", "an HTTP error without one", "a reply that is no object"],
)
def test_an_endpoint_that_cannot_answer_now_is_asked_again(fake: FakeFeishu, mode: str, replies: List[Any]):
    with running_adapter(fake, long_connection=mode):
        fake.endpoint_replies = list(replies)
        fake.drop_connection()
        fake.wait_connected(2)

    assert len(fake.endpoint_requests) == 3


@pytest.mark.parametrize("mode", BOTH)
def test_an_endpoint_that_refuses_the_app_is_not_asked_again(fake: FakeFeishu, mode: str):
    with running_adapter(fake, long_connection=mode):
        fake.endpoint_replies = [{"code": 10014, "msg": "app secret invalid"}]
        fake.drop_connection()
        fake.wait_until(lambda: len(fake.endpoint_requests) == 2, what="the refused endpoint request")
        threading.Event().wait(0.5)  # ten retry intervals

        assert len(fake.endpoint_requests) == 2
        assert fake.connections_opened == 1


FINAL = "final"
RETRIED = "retried"


@pytest.mark.parametrize("mode", BOTH)
@pytest.mark.parametrize(
    "status, headers, outcome",
    [
        (403, {"Handshake-Status": "403", "Handshake-Msg": "forbidden"}, FINAL),
        (
            400,
            {"Handshake-Status": "514", "Handshake-Msg": "too many", "Handshake-Autherrcode": "1000040350"},
            FINAL,
        ),
        (400, {"Handshake-Status": "514", "Handshake-Msg": "auth failed", "Handshake-Autherrcode": "7"}, RETRIED),
        (400, {"Handshake-Status": "500", "Handshake-Msg": "server error"}, RETRIED),
        (502, {}, RETRIED),
    ],
    ids=["forbidden", "over the connection limit", "another auth failure", "a server error", "a refusal without a reason"],
)
def test_a_refused_handshake_is_final_or_retried_as_feishu_says(
    fake: FakeFeishu, mode: str, status: int, headers: Dict[str, str], outcome: str
):
    with running_adapter(fake, long_connection=mode):
        fake.handshake_refusals = [(status, headers)]
        fake.drop_connection()
        fake.wait_until(lambda: fake.handshakes_refused == 1, what="the refused handshake")
        if outcome == RETRIED:
            fake.wait_connected(2)
        else:
            threading.Event().wait(0.5)  # ten retry intervals
            assert fake.connections_opened == 1
            assert len(fake.endpoint_requests) == 2


@pytest.mark.parametrize("mode", BOTH)
def test_the_ping_interval_is_the_server_s(fake: FakeFeishu, mode: str):
    fake.client_config["PingInterval"] = 0.1

    with running_adapter(fake, long_connection=mode):
        fake.wait_until(lambda: len(fake.pings()) >= 4, timeout=5, what="four pings at the server's interval")

    assert {ping.service for ping in fake.pings()} == {33554678}


@pytest.mark.parametrize("mode", BOTH)
def test_a_configuration_sent_with_a_pong_takes_effect(fake: FakeFeishu, mode: str):
    # The endpoint says to wait five minutes between attempts; the pong that answers the first ping says 50 ms.
    fake.client_config["ReconnectInterval"] = 300
    fake.pong_config = {**fake.client_config, "ReconnectInterval": 0.05}

    with running_adapter(fake, long_connection=mode):
        fake.wait_until(fake.pings, what="the first ping")
        fake.endpoint_replies = [{"code": 1, "msg": "system busy"}]
        fake.drop_connection()

        # One failed attempt, then the wait between attempts: over in a moment only on the pong's value.
        fake.wait_connected(2, timeout=5)


def test_a_stopped_connection_closes_cleanly_and_stays_closed(fake: FakeFeishu):
    with running_adapter(fake, long_connection="own"):
        pass
    fake.wait_until(lambda: fake.close_codes, what="the close")
    threading.Event().wait(0.5)  # ten retry intervals

    assert fake.close_codes == [1000]
    assert fake.connections_opened == 1
    assert len(fake.endpoint_requests) == 1


def test_a_peer_that_does_not_complete_the_close_is_cut_off_before_stop_returns(fake: FakeFeishu):
    fake.withhold_close_reply = True
    with running_adapter(fake, long_connection="own") as adapter:
        connection = adapter.bot._own_connection
        transport = fake.wait_until(lambda: connection._socket, what="the socket").transport
    # Leaving the block stopped the adapter and verified its threads gone, within their 5 s.

    assert fake.closes_withheld == 1
    assert transport.is_closing()


def test_stopping_three_times_from_three_threads_ends_once_and_cleanly(fake: FakeFeishu):
    fake.withhold_close_reply = True  # the close takes its 3 s: time for the other stops to land in it
    uncaught: List[Any] = []
    with patch.object(threading, "excepthook", uncaught.append):
        with running_adapter(fake, long_connection="own") as adapter:
            connection = adapter.bot._own_connection
            transport = fake.wait_until(lambda: connection._socket, what="the socket").transport
            stoppers = [threading.Thread(target=connection.stop) for _ in range(3)]
            for index, stopper in enumerate(stoppers):
                stopper.start()
                threading.Event().wait(0.3 * index)
            for stopper in stoppers:
                stopper.join(timeout=10)
            assert connection.stop() is True

    assert uncaught == []
    assert transport.is_closing()
    assert connection._loop.is_closed()


async def test_an_event_that_arrives_after_the_stop_is_neither_taken_nor_answered(fake: FakeFeishu):
    """Feishu sends an unanswered event again, and then the connection that replaced this one gets it."""
    taken: List[bytes] = []
    connection = FeishuLongConnection(
        base_url=fake.url, app_id="cli_app", app_secret="socket-secret", handle_event=taken.append
    )
    running = asyncio.create_task(connection.run())
    await asyncio.to_thread(fake.wait_connected)
    before = await asyncio.to_thread(fake.wait_acknowledged, fake.push_event(message_event(event_id="e_before")))

    connection._stopping = True  # what stop() does first; the cancellation follows from another thread
    late = fake.push_event(message_event(event_id="e_late"))
    await asyncio.sleep(0.3)
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)

    assert before.json() == {"code": 200}
    assert len(taken) == 1
    assert fake.acknowledgements(late) == []


def test_stopping_while_waiting_to_reconnect_does_not_wait_out_the_interval(fake: FakeFeishu):
    fake.client_config["ReconnectInterval"] = 300
    with running_adapter(fake, long_connection="own") as adapter:
        fake.endpoint_replies = [{"code": 1, "msg": "system busy"}]
        fake.drop_connection()
        fake.wait_until(lambda: len(fake.endpoint_requests) == 2, what="the failed reconnect")
    # Leaving the block stopped the adapter and joined its threads within their 5 s.

    assert adapter.bot.verify_stopped()


def test_the_connection_s_ticket_is_not_logged(fake: FakeFeishu, caplog: pytest.LogCaptureFixture):
    with caplog.at_level("INFO", logger="modules.im.feishu_ws"):
        with running_adapter(fake, long_connection="own"):
            pass

    assert "connected to ws://127.0.0.1" in caplog.text
    assert "conn_id=7000" in caplog.text
    for secret in ("access_key", "ticket", "once"):
        assert secret not in caplog.text


@pytest.mark.parametrize("mode", BOTH)
def test_the_number_of_attempts_is_the_one_in_force_when_reconnecting_began(fake: FakeFeishu, mode: str):
    fake.client_config["ReconnectCount"] = 1
    with running_adapter(fake, long_connection=mode):
        # The one attempt allowed is refused at the handshake, after its endpoint said "without limit".
        fake.client_config["ReconnectCount"] = -1
        fake.handshake_refusals = [(503, {})]
        fake.drop_connection()
        fake.wait_until(lambda: fake.handshakes_refused == 1, what="the refused handshake")
        threading.Event().wait(0.5)  # ten retry intervals

        assert len(fake.endpoint_requests) == 2
        assert fake.connections_opened == 1


@pytest.mark.parametrize("mode", BOTH)
@pytest.mark.parametrize(
    "proxy_spelling", [lambda url: url, lambda url: url.split("://")[1]], ids=["with its scheme", "host and port alone"]
)
def test_the_endpoint_is_asked_for_through_the_environment_s_proxy(
    fake: FakeFeishu, mode: str, proxy_spelling: Any, monkeypatch: pytest.MonkeyPatch
):
    """A deployment that reaches Feishu's API only through its proxy must still get an endpoint."""
    # The fake stands in for the proxy: a proxied request names the whole URL, and the fake answers by its path.
    monkeypatch.setenv("HTTP_PROXY", proxy_spelling(fake.url))
    monkeypatch.setenv("http_proxy", proxy_spelling(fake.url))
    unreachable = "http://feishu-open-platform.invalid"
    received: List[bytes] = []

    class Handler:
        def _do_without_validation(self, payload: bytes) -> None:
            received.append(payload)

    if mode == "sdk":
        connection: Any = SdkLongConnection(
            app_id="cli_app", app_secret="socket-secret", domain=unreachable, event_handler=Handler()
        )
        connection.start()
    else:
        connection = FeishuLongConnection(
            base_url=unreachable, app_id="cli_app", app_secret="socket-secret", handle_event=received.append
        )
        connection.start()
    try:
        fake.wait_connected()
        fake.wait_acknowledged(fake.push_event(message_event()))
    finally:
        assert connection.stop() is True

    assert len(received) == 1


class _RecordingProxy:
    """An HTTP proxy that lets nothing through and notes what was asked of it."""

    def __init__(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.asked: List[str] = []
        asked = self.asked

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def do_POST(self) -> None:
                asked.append(self.path)
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_CONNECT = do_GET = do_POST

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.mark.parametrize("mode", BOTH)
@pytest.mark.parametrize("no_proxy", ["127.0.0.0/8", "127.0.0.1", "*"])
def test_the_app_secret_is_not_sent_to_a_proxy_the_environment_exempts_this_host_from(
    fake: FakeFeishu, mode: str, no_proxy: str, monkeypatch: pytest.MonkeyPatch
):
    """Which proxy sees the endpoint request, and when none does, is decided as the SDK's request decided it."""
    proxy = _RecordingProxy()
    try:
        for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, proxy.url)
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.setenv(name, no_proxy)

        with running_adapter(fake, long_connection=mode):
            pass
    finally:
        proxy.close()

    assert proxy.asked == []
    assert fake.endpoint_requests[0]["body"]["AppSecret"] == "socket-secret"


def test_a_stop_just_after_the_socket_opened_still_closes_it(fake: FakeFeishu):
    """The socket exists and nobody but this coroutine knows it yet: the stop's cancellation must not orphan it."""
    fake.withhold_close_reply = True
    opened: List[Any] = []
    connection = FeishuLongConnection(
        base_url=fake.url, app_id="cli_app", app_secret="socket-secret", handle_event=lambda _payload: None
    )
    connect = feishu_ws.websockets.connect

    async def connect_then_stop(*args: Any, **kwargs: Any) -> Any:
        socket = await connect(*args, **kwargs)
        opened.append(socket)
        connection.stop(timeout=0)  # from the connection's own thread: asks, and does not wait
        return socket

    with patch.object(feishu_ws.websockets, "connect", connect_then_stop):
        connection.start()
        fake.wait_until(lambda: opened and not connection.is_alive(), timeout=10, what="the connection to end")

    assert connection.stop() is True
    assert opened[0].transport.is_closing()
    assert connection._loop.is_closed()
    assert connection._held is None


def test_a_stop_before_or_during_start_is_not_an_error_and_wins(fake: FakeFeishu):
    before = FeishuLongConnection(
        base_url=fake.url, app_id="cli_app", app_secret="socket-secret", handle_event=lambda _payload: None
    )
    assert before.stop() is True
    before.start()
    assert before.is_alive() is False

    failures: List[BaseException] = []

    def attempt(action: Any) -> None:
        try:
            action()
        except BaseException as exc:
            failures.append(exc)

    for _ in range(40):
        racing = FeishuLongConnection(
            base_url=fake.url, app_id="cli_app", app_secret="socket-secret", handle_event=lambda _payload: None
        )
        threads = [threading.Thread(target=attempt, args=(racing.start,)), threading.Thread(target=attempt, args=(racing.stop,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert racing.stop() is True

    assert failures == []
    threading.Event().wait(0.3)
    assert before.is_alive() is False


def test_nothing_feishu_or_the_network_says_reaches_the_log(fake: FakeFeishu, caplog: pytest.LogCaptureFixture):
    """A refusal can quote the secret or the ticket back, in any spelling; so none of its text is logged."""
    with socket.socket() as placeholder:  # a loopback port with nothing behind it
        placeholder.bind(("127.0.0.1", 0))
        closed_port = placeholder.getsockname()[1]
    ticket = "T%2FICKET%3DMARK"  # as it travels; decoded it reads T/ICKET=MARK
    fake.endpoint_replies = [
        (503, {"msg": "bad credentials for socket-secret"}),
        {"code": 1000040343, "msg": "internal error near socket-secret"},
        {"code": 0, "data": {"URL": f"ws://127.0.0.1:{closed_port}/ws/v2?device_id=1&service_id=2&ticket={ticket}"}},
        {"code": 0, "data": {"URL": f"ws://127.0.0.1:1/ws/v2?ticket={ticket}"}},  # names no device
    ]
    # Then a real endpoint, whose handshake is refused twice with the ticket quoted back, both ways.
    fake.handshake_refusals = [
        (400, {"Handshake-Status": "500", "Handshake-Msg": "invalid ticket: once and T%2FICKET%3DMARK"}),
        (400, {"Handshake-Status": "500", "Handshake-Msg": "invalid ticket: T/ICKET=MARK"}),
    ]
    connection = FeishuLongConnection(
        base_url=fake.url, app_id="cli_app", app_secret="socket-secret", handle_event=lambda _payload: None
    )
    connection.config.reconnect_nonce, connection.config.reconnect_interval = 0, 0.02

    with caplog.at_level("DEBUG"):  # everything, the WebSocket library's own handshake lines included
        connection.start()
        try:
            fake.wait_connected(timeout=10)
        finally:
            assert connection.stop() is True

    ours = [record.getMessage() for record in caplog.records if record.name.startswith("modules.im")]
    failures = [line.split("err: ")[1] for line in ours if "connect failed" in line]
    assert failures[:2] == ["the endpoint answered HTTP 503", "the endpoint failed (code 1000040343)"]
    assert failures[2].startswith("the handshake failed (ConnectionRefusedError")  # a kind and a number, no text
    assert failures[3:] == [
        "the endpoint URL is unusable or names no device or service",
        "the handshake was refused (status 500, auth code 0)",
        "the handshake was refused (status 500, auth code 0)",
    ]
    # Every logger but the fake server's own access log, which is Feishu's side of the wire.
    client_side = "\n".join(record.getMessage() for record in caplog.records if record.name != "aiohttp.access")
    for marker in ("socket-secret", "ICKET", "ticket=", "invalid ticket", "bad credentials", "internal error near"):
        assert marker not in client_side


def test_an_app_the_endpoint_refuses_is_told_by_code_alone(fake: FakeFeishu, caplog: pytest.LogCaptureFixture):
    fake.endpoint_code, fake.endpoint_msg = 10014, "app secret invalid: socket-secret"
    connection = FeishuLongConnection(
        base_url=fake.url, app_id="cli_app", app_secret="socket-secret", handle_event=lambda _payload: None
    )

    connection.start()
    fake.wait_until(lambda: not connection.is_alive(), what="the connection to give up")

    assert "Feishu long connection refused, not trying again: 10014: the endpoint refused the app" in caplog.text
    assert "socket-secret" not in caplog.text and "app secret invalid" not in caplog.text
    assert len(fake.endpoint_requests) == 1


def test_a_stop_does_not_wait_for_an_endpoint_that_is_slow_to_answer(fake: FakeFeishu):
    fake.endpoint_delays = [3.0]
    connection = FeishuLongConnection(
        base_url=fake.url, app_id="cli_app", app_secret="socket-secret", handle_event=lambda _payload: None
    )
    connection.start()
    fake.wait_until(lambda: fake.endpoint_requests, what="the endpoint request")

    assert connection.stop(timeout=1) is True

    threading.Event().wait(3.5)  # the reply comes after all, to nobody
    assert fake.connections_opened == 0


def test_endpoint_requests_that_do_not_end_do_not_pile_up(fake: FakeFeishu):
    """One at a time: while a request given up on is still out, no other is sent."""
    fake.endpoint_delays = [2.0]
    connection = FeishuLongConnection(
        base_url=fake.url, app_id="cli_app", app_secret="socket-secret", handle_event=lambda _payload: None
    )
    connection.config.reconnect_nonce, connection.config.reconnect_interval = 0, 0.05
    with patch.object(feishu_ws, "_ENDPOINT_TIMEOUT_SECONDS", 0.3):
        connection.start()
        try:
            threading.Event().wait(1.5)  # given up on after 0.3 s; some twenty attempts since
            assert len(fake.endpoint_requests) == 1
            fake.wait_connected(timeout=8)  # the first one ended, and the next went out
        finally:
            assert connection.stop() is True

    assert len(fake.endpoint_requests) == 2


# --- the two clients side by side ----------------------------------------------------


def test_in_parallel_an_event_is_handled_once_whichever_connection_gets_it(
    fake: FakeFeishu, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level("INFO", logger="modules.im.feishu"):
        with running_adapter(fake, connections=2, long_connection="parallel") as adapter:
            first = fake.push_event(message_event(event_id="e1", text="机密一"), connection=0)
            second = fake.push_event(message_event(event_id="e2", text="机密二"), connection=1)
            # Feishu sends e1 again, and this time the other connection gets it.
            again = fake.push_event(message_event(event_id="e1", text="机密一"), connection=1)
            answers = [fake.wait_acknowledged(message_id) for message_id in (first, second, again)]

    assert [answer.connection for answer in answers] == [0, 1, 1]
    assert [answer.json() for answer in answers] == [{"code": 200}] * 3
    assert sorted(json.loads(message["message"]["content"])["text"] for message in adapter.messages) == [
        "机密一",
        "机密二",
    ]
    # Which connection each event came by is on record, ids and counts only.
    assert {app["body"]["AppID"] for app in fake.endpoint_requests} == {"cli_app"}
    assert sum(counts["events"] for counts in adapter.bot._connection_events.values()) == 3
    assert sum(counts["again"] for counts in adapter.bot._connection_events.values()) == 1
    assert "Feishu event e1 (im.message.receive_v1) via the" in caplog.text
    assert ", again" in caplog.text
    assert "机密" not in caplog.text


def test_listening_finds_nothing_to_report_on_the_sdk_s_own_frames(fake: FakeFeishu):
    with running_adapter(fake, long_connection="listen") as adapter:
        fake.wait_until(fake.pings, what="the first ping")
        ids = [
            fake.push_event(message_event(event_id="e1")),
            fake.push_event(card_action_event()),
            fake.push_event(message_event(event_id="e2", text="长" * 6000), pieces=3),
            fake.push_event(UNREADABLE_EVENT),
        ]
        for message_id in ids:
            fake.wait_acknowledged(message_id)
        tap = adapter.bot._frame_tap

    assert fake.connections_opened == 1  # listening opens no connection of its own
    assert len(adapter.messages) == 2 and len(adapter.clicks) == 1  # and the SDK's client still handles everything
    assert tap.counts.mismatches == 0
    assert tap.counts.frames_in == 7  # the pong and six data frames
    assert tap.counts.frames_out == 5  # the ping and four answers
    assert tap.counts.events == 4


def _event_frame(message_id: str, payload: bytes = b'{"private": "text"}') -> Any:
    return _sdk_frame(
        SeqID=1,
        LogID=2,
        service=3,
        method=1,
        headers=[("type", "event"), ("message_id", message_id), ("sum", "1"), ("seq", "0"), ("trace_id", "t")],
        payload=payload,
    )


def _sdk_answer(received: Any, payload: bytes, timing: Optional[str] = "3") -> bytes:
    if timing is not None:
        header = received.headers.add()
        header.key, header.value = "biz_rt", timing
    received.payload = payload
    return received.SerializeToString()


def test_the_tap_holds_the_other_client_to_what_this_codec_would_do(caplog: pytest.LogCaptureFixture):
    tap = FrameTap(_sdk().Frame)

    # As this module would: received, handed on, handled, answered with the same bytes.
    first = _event_frame("m1")
    tap.received(first.SerializeToString())
    tap.handled(b'{"private": "text"}', None, False)
    tap.sent(_sdk_answer(first, b'{"code": 200}'))
    card = _event_frame("m2")
    tap.received(card.SerializeToString())
    tap.handled(b'{"private": "text"}', {}, False)
    tap.sent(_sdk_answer(card, b'{"code": 200, "data": "e30="}'))
    failed = _event_frame("m3")
    tap.received(failed.SerializeToString())
    tap.handled(b'{"private": "text"}', None, True)
    tap.sent(_sdk_answer(failed, b'{"code": 500}', timing=None))
    assert tap.counts.mismatches == 0

    # Not as this module would.
    compact = _event_frame("m4")
    tap.received(compact.SerializeToString())
    tap.handled(b'{"private": "text"}', None, False)
    tap.sent(_sdk_answer(compact, b'{"code":200}'))  # another spelling of the same answer
    wrong_outcome = _event_frame("m5")
    tap.received(wrong_outcome.SerializeToString())
    tap.handled(b'{"private": "text"}', None, True)
    tap.sent(_sdk_answer(wrong_outcome, b'{"code": 200}'))  # the dispatcher failed; the answer says handled
    tap.handled(b'{"other": 1}', None, False)  # an event this module never saw arrive
    unasked = _event_frame("m6")
    tap.sent(_sdk_answer(unasked, b'{"code": 200}'))  # an answer to nothing

    assert tap.counts.mismatches == 4
    assert "an answer is built differently (message_id: m4)" in caplog.text
    assert "an answer is built differently (message_id: m5)" in caplog.text
    assert "an event was handed on that this codec did not join" in caplog.text
    assert "an answer to an event this codec would not have handed on (message_id: m6)" in caplog.text
    assert "private" not in caplog.text and "other" not in caplog.text


def test_the_tap_does_not_take_the_other_client_s_word_for_what_it_should_check():
    tap = FrameTap(_sdk().Frame)

    # A handled event answered without the timing header.
    untimed = _event_frame("m1")
    tap.received(untimed.SerializeToString())
    tap.handled(b'{"private": "text"}', None, False)
    tap.sent(_sdk_answer(untimed, b'{"code": 200}', timing=None))
    assert tap.counts.mismatches == 1

    # The same message id twice before either is handed on: two are owed, and one answer settles one.
    for _ in range(2):
        tap.received(_event_frame("m2").SerializeToString())
    tap.handled(b'{"private": "text"}', None, False)
    tap.sent(_sdk_answer(_event_frame("m2"), b'{"code": 200}'))
    assert tap.counts.mismatches == 1
    with patch.object(feishu_ws, "_TAP_PENDING_SECONDS", 0.0):
        tap.check()
    assert tap.counts.mismatches == 2  # the second was never handed on

    # An answer that comes after the time allowed, with nothing received in between.
    late = _event_frame("m3")
    tap.received(late.SerializeToString())
    tap.handled(b'{"private": "text"}', None, False)
    with patch.object(feishu_ws, "_TAP_PENDING_SECONDS", 0.0):
        tap.sent(_sdk_answer(late, b'{"code": 200}'))
    assert tap.counts.mismatches == 4  # not answered in time, and then an answer to nothing owed


def test_the_tap_counts_what_the_other_client_owed_and_never_did(caplog: pytest.LogCaptureFixture):
    tap = FrameTap(_sdk().Frame)
    tap.received(_event_frame("m_dropped").SerializeToString())  # joined here, never handed on there
    unanswered = _event_frame("m_unanswered", b"{}")
    tap.received(unanswered.SerializeToString())
    tap.handled(b"{}", None, False)  # handed on and handled, never answered
    assert tap.counts.mismatches == 0  # not yet owed
    with patch.object(feishu_ws, "_TAP_PENDING_SECONDS", 0.0):
        tap.check()  # as the adapter's report does, frames or no frames

    assert tap.counts.mismatches == 2
    assert "an event this codec joined was not handed on (message_id: m_dropped)" in caplog.text
    assert "an event that was handled was not answered (message_id: m_unanswered)" in caplog.text


def test_a_failure_inside_the_tap_does_not_reach_the_sdk_s_client(fake: FakeFeishu, caplog: pytest.LogCaptureFixture):
    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the tap broke")

    with patch.multiple(FrameTap, received=broken, handled=broken, sent=broken):
        with running_adapter(fake, long_connection="listen") as adapter:
            acknowledged = fake.wait_acknowledged(fake.push_event(message_event()))
            clicked = fake.wait_acknowledged(fake.push_event(card_action_event()))

    assert (len(adapter.messages), len(adapter.clicks)) == (1, 1)
    assert acknowledged.json() == {"code": 200}
    assert clicked.json() == {"code": 200, "data": "e30="}
    assert "Feishu long connection tap failed; the SDK's client is not affected" in caplog.text


def _frame_with_headers(message_id: str, body: Dict[str, Any], **headers: Optional[str]) -> bytes:
    """An event frame whose headers are as given: ``None`` leaves one out."""
    frame = _sdk().Frame()
    frame.SeqID, frame.LogID, frame.service, frame.method = 1, 2, 33554678, 1
    defaults = {"type": "event", "message_id": message_id, "sum": "1", "seq": "0", "trace_id": "t"}
    for key, value in {**defaults, **headers}.items():
        if value is not None:
            header = frame.headers.add()
            header.key, header.value = key, value
    frame.payload = json.dumps(body).encode("utf-8")
    return frame.SerializeToString()


@pytest.mark.parametrize("mode", ["sdk", "own", "listen"])
@pytest.mark.parametrize(
    "headers, handled",
    [
        ({"trace_id": None}, False),
        ({"seq": None}, False),
        ({"sum": "one"}, False),
        ({"seq": ""}, True),  # one piece: which piece is not asked
        ({"seq": "x", "sum": " 1 "}, True),
        ({"sum": "2", "seq": "x"}, False),
    ],
    ids=["no trace id", "no seq", "a sum that is no number", "an empty seq", "a seq that is no number", "the same of two"],
)
def test_a_frame_s_headers_are_read_as_the_sdk_s_client_reads_them(
    fake: FakeFeishu, mode: str, headers: Dict[str, Optional[str]], handled: bool
):
    """Five headers must be there; ``seq`` is only read as a number when there are several pieces."""
    with running_adapter(fake, long_connection=mode) as adapter:
        fake.push_frames([_frame_with_headers("m_odd", message_event(event_id="e_odd"), **headers)])
        after = fake.wait_acknowledged(fake.push_event(message_event(event_id="e_after")))
        tap = adapter.bot._frame_tap

    assert after.json() == {"code": 200}
    assert [frame.json() for frame in fake.acknowledgements("m_odd")] == ([{"code": 200}] if handled else [])
    assert len(adapter.messages) == (2 if handled else 1)
    if mode == "listen":
        assert tap.counts.mismatches == 0  # the two agree, whichever way


@pytest.mark.parametrize("mode", BOTH)
def test_an_endpoint_url_that_cannot_be_used_is_asked_for_again(fake: FakeFeishu, mode: str):
    with running_adapter(fake, long_connection=mode) as adapter:
        fake.endpoint_replies = [{"code": 0, "data": {"URL": "ws://[not-a-host/ws?ticket=TICKET-MARK"}}]
        fake.drop_connection()
        fake.wait_connected(2)
        fake.wait_acknowledged(fake.push_event(message_event()))

    assert len(fake.endpoint_requests) == 3
    assert len(adapter.messages) == 1


def test_an_unknown_long_connection_value_means_the_sdk_s(fake: FakeFeishu, caplog: pytest.LogCaptureFixture):
    with running_adapter(fake, long_connection="both-at-once") as adapter:
        fake.wait_acknowledged(fake.push_event(message_event()))

    assert adapter.bot._connection_mode == "sdk"
    assert "unknown long_connection 'both-at-once'" in caplog.text
    assert len(adapter.messages) == 1


def test_changing_the_long_connection_rebuilds_the_adapter():
    """The switch is a config field; the controller rebuilds a platform's client when its reconcile fields change."""
    from types import SimpleNamespace

    from config.v2_config import LarkConfig
    from core.controller import Controller

    def signature(mode: str) -> tuple:
        lark = LarkConfig(app_id="cli_app", app_secret="socket-secret", long_connection=mode)
        return Controller._runtime_reconcile_signature(SimpleNamespace(lark=lark, platform_configs={"lark": lark}), "lark")

    assert len({signature(mode) for mode in ("sdk", "listen", "parallel", "own")}) == 4


# --- the history check: every message a person sent, matched against the events ---------


class _Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock():
    clock = _Clock()
    with patch("modules.im.feishu_history_check.time.time", clock.time):
        yield clock


async def test_a_message_in_the_history_without_an_event_is_reported_by_id(
    fake: FakeFeishu, clock: _Clock, caplog: pytest.LogCaptureFixture
):
    with running_adapter(fake, long_connection="own") as adapter:
        bot = adapter.bot
        bot._dm_chat_ids.update({"oc_dm", "oc_other"})
        base = int(clock.now) + 60  # the check starts a minute after the adapter
        fake.history = {
            "oc_dm": [
                ("om_before", "user", base - 10),  # before the check started: not its business
                ("om_1", "user", base + 10),
                ("om_bot", "app", base + 20),  # the bot's own message: no event is due
                ("om_2", "user", base + 30),
                ("om_lost", "user", base + 40),
                ("om_fresh", "user", base + 290),  # too fresh to judge yet
            ],
            "oc_other": [],
        }
        for message_id in ("om_1", "om_2"):
            fake.wait_acknowledged(fake.push_event(message_event(event_id=f"e_{message_id}", message_id=message_id)))

        clock.now += 60 + 300
        with caplog.at_level("INFO", logger="modules.im"):
            await bot._report_connections()

        counts = bot._history_check.counts
        assert (counts.people_messages, counts.matched, counts.missing) == (3, 2, 1)
        assert "message om_lost in chat oc_dm is in the history but no event arrived for it" in caplog.text
        assert "history={'people_messages': 3, 'matched': 2, 'missing': 1" in caplog.text
        assert len([request for request in fake.history_requests if request["container_id"] == "oc_dm"]) == 2  # paged

        # Later: the lost message's event arrives after all (Feishu sent it again); the fresh one is judged now.
        fake.wait_acknowledged(fake.push_event(message_event(event_id="e_lost", message_id="om_lost")))
        clock.now += 600
        await bot._report_connections()

        assert (counts.people_messages, counts.matched, counts.missing, counts.arrived_late) == (4, 2, 1, 1)
        assert "the event for message om_lost in chat oc_dm arrived after the check" in caplog.text
        assert "message om_fresh in chat oc_dm is in the history but no event arrived for it" in caplog.text


async def test_a_chat_whose_history_cannot_be_read_is_said_once_and_tried_again(
    fake: FakeFeishu, clock: _Clock, caplog: pytest.LogCaptureFixture
):
    with running_adapter(fake, long_connection="own") as adapter:
        bot = adapter.bot
        bot._dm_chat_ids.add("oc_closed")
        fake.history_refused.add("oc_closed")

        for _ in range(2):
            clock.now += 600
            await bot._report_connections()

        counts = bot._history_check.counts
        assert (counts.readings_failed, counts.missing) == (2, 0)
        assert caplog.text.count("chat oc_closed could not be read (code=230027") == 1
        assert len(fake.history_requests) == 2  # asked again: the permission may have come since


async def test_a_reading_that_fails_once_skips_nothing(fake: FakeFeishu, clock: _Clock):
    with running_adapter(fake, long_connection="own") as adapter:
        bot = adapter.bot
        bot._dm_chat_ids.add("oc_dm")
        base = int(clock.now) + 60
        fake.history = {"oc_dm": [("om_lost", "user", base + 10)]}
        fake.history_replies = [(200, {"code": 1, "msg": "system busy"})]

        clock.now += 60 + 300
        await bot._report_connections()
        assert bot._history_check.counts.people_messages == 0
        clock.now += 600
        await bot._report_connections()

        counts = bot._history_check.counts
        assert (counts.readings_failed, counts.people_messages, counts.missing) == (1, 1, 1)


@pytest.mark.parametrize("seconds_apart", [10, 0], ids=["ten seconds apart", "all in one second"])
async def test_history_longer_than_one_reading_is_continued_not_skipped(
    fake: FakeFeishu, clock: _Clock, seconds_apart: int
):
    with running_adapter(fake, long_connection="own") as adapter:
        bot = adapter.bot
        bot._dm_chat_ids.add("oc_dm")
        base = int(clock.now) + 60
        # Five messages, two to a page; a reading stops after two pages.
        fake.history = {"oc_dm": [(f"om_{number}", "user", base + 10 + seconds_apart * number) for number in range(1, 6)]}

        clock.now += 60 + 300
        with patch("modules.im.feishu_history_check._PAGE_LIMIT", 2):
            await bot._report_connections()
            first = bot._history_check.counts.people_messages
            clock.now += 600
            await bot._report_connections()
            clock.now += 600
            await bot._report_connections()

        assert first == 4
        # All five were judged, each once: the second reading went on where the first stopped.
        assert bot._history_check.counts.people_messages == 5
        assert bot._history_check.counts.missing == 5
        assert [request.get("page_token", "") for request in fake.history_requests][:3] == ["", "2", "4"]


async def test_a_history_that_could_not_be_read_for_hours_is_still_judged_fairly(fake: FakeFeishu, clock: _Clock):
    """What arrived is remembered until the history has been read past it; beyond a week there is no verdict."""
    with running_adapter(fake, long_connection="own") as adapter:
        bot = adapter.bot
        bot._dm_chat_ids.add("oc_dm")
        base = int(clock.now) + 60
        fake.history = {"oc_dm": [("om_early", "user", base + 10)]}
        fake.wait_acknowledged(fake.push_event(message_event(event_id="e_early", message_id="om_early")))

        # Nine hours in which the history cannot be read.
        for _ in range(54):
            clock.now += 600
            fake.history_replies = [(200, {"code": 1, "msg": "system busy"})]
            await bot._report_connections()
        clock.now += 600
        await bot._report_connections()

        counts = bot._history_check.counts
        assert (counts.people_messages, counts.matched, counts.missing) == (1, 1, 0)

        # A second chat whose history opens up only after eight days: its old messages get no verdict.
        bot._dm_chat_ids.add("oc_late")
        bot._history_check._checked_until["oc_late"] = base
        fake.history["oc_late"] = [("om_ancient", "user", base + 20)]
        clock.now += 8 * 24 * 3600
        await bot._report_connections()

        assert (counts.missing, counts.not_checkable) == (0, 1)


def test_a_stop_does_not_wait_for_a_history_reading(fake: FakeFeishu):
    fake.history_delay = 20.0
    with patch("modules.im.feishu._CONNECTION_REPORT_SECONDS", 0.2):
        with running_adapter(fake, long_connection="own") as adapter:
            adapter.bot._dm_chat_ids.add("oc_dm")
            adapter.bot._history_check._started_at = 0  # a reading is due at once
            fake.wait_until(lambda: fake.history_requests, what="a history reading under way")
    # Leaving the block stopped the adapter and joined its thread within the harness's 10 s.

    assert adapter.bot.verify_stopped()


def test_no_history_is_read_while_the_sdk_s_client_is_the_only_one(fake: FakeFeishu):
    with running_adapter(fake) as adapter:
        fake.wait_acknowledged(fake.push_event(message_event()))

    assert adapter.bot._history_check is None
    assert fake.history_requests == []


# --- what the switch buys ----------------------------------------------------------------


def test_on_our_own_connection_the_process_never_imports_the_sdk():
    """The SDK's package imports every generated API (about 10,600 modules, 180 MiB); ``own`` must not touch it."""
    import subprocess
    import sys
    from pathlib import Path

    with socket.socket() as placeholder:  # a loopback port with nothing behind it
        placeholder.bind(("127.0.0.1", 0))
        closed_port = placeholder.getsockname()[1]
    script = f"""
import json, sys, threading, time
from tests import network_guard
sys.addaudithook(network_guard.audit)  # loopback only, as in the pytest process
from unittest.mock import patch
from config.v2_config import LarkConfig
from modules.im.feishu import FeishuBot

with patch.object(LarkConfig, "api_base_url", property(lambda _self: "http://127.0.0.1:{closed_port}")):
    bot = FeishuBot(LarkConfig(app_id="cli_app", app_secret="socket-secret", long_connection="own"))
    thread = threading.Thread(target=bot.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not (bot._own_connection and bot._own_connection.is_alive()):
        time.sleep(0.05)
    ran = bool(bot._own_connection and bot._own_connection.is_alive())
    bot.stop()
    thread.join(10)
print(json.dumps({{
    "own_connection_ran": ran,
    "stopped": bot.verify_stopped() and not thread.is_alive(),
    "sdk_modules": sorted(name for name in sys.modules if name.split(".")[0] == "lark_oapi")[:5],
}}))
"""
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.strip().splitlines()[-1]) == {
        "own_connection_ran": True,
        "stopped": True,
        "sdk_modules": [],
    }


# --- the switch in the config -------------------------------------------------------------


def test_the_switch_survives_a_save_that_does_not_mention_it_and_an_older_file_means_the_sdk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, sqlite_schema_db_factory: Any
):
    from config.v2_config import V2Config
    from tests.test_api_save_config_merge import _full_config_payload
    from vibe import api

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")
    payload = _full_config_payload()
    payload["lark"].update({"app_id": "cli_app", "app_secret": "socket-secret"})

    # A file written before the field existed.
    assert api.save_config(payload).lark.long_connection == "sdk"

    assert api.save_config({"lark": {"long_connection": "listen"}}).lark.long_connection == "listen"
    # The settings page saves the fields it knows.
    saved = api.save_config({"lark": {"app_id": "cli_app", "require_mention": True}})
    assert (saved.lark.long_connection, saved.lark.require_mention) == ("listen", True)
    assert V2Config.load().lark.long_connection == "listen"

    # A value nobody defined is kept as written and does not stop the config from loading.
    assert api.save_config({"lark": {"long_connection": "sideways"}}).lark.long_connection == "sideways"


def test_an_adapter_that_sets_nothing_loads_and_logs_what_it_did_before(fake: FakeFeishu, caplog: pytest.LogCaptureFixture):
    import subprocess
    import sys
    from pathlib import Path

    script = """
import sys
from config.v2_config import LarkConfig
from modules.im.feishu import FeishuBot
FeishuBot(LarkConfig(app_id="cli_app", app_secret="socket-secret"))
print(sorted(name for name in sys.modules if name in ("modules.im.feishu_ws", "modules.im.feishu_history_check", "websockets")))
"""
    done = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == "[]"

    with caplog.at_level("INFO", logger="modules.im.feishu"):
        with running_adapter(fake) as adapter:
            fake.wait_acknowledged(fake.push_event(message_event()))

    assert "Starting Feishu/Lark bot with WebSocket..." in caplog.text
    assert "long connection" not in caplog.text
    assert adapter.bot._own_connection is None and adapter.bot._frame_tap is None
