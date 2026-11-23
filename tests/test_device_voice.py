"""Isolated host provenance and real socket/codec two-turn device contracts."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import os
import struct
import socket
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import aiohttp
from aiohttp import web
import pytest

from core.bots import BotService
from core.control_requests import new_request
from core.handlers.cli_requests import handle_control_request
from core.handlers.device_voice import speech_projection
from core.scheduled_tasks import TaskExecutionStore
from core.vibe_agents import VibeAgentStore
from storage.importer import ensure_sqlite_state


@pytest.fixture
def plugin(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "packaging" / "avibe-device"))
    return __import__("avibe_device")


@pytest.fixture
def controller():
    ensure_sqlite_state()
    agents = VibeAgentStore()
    agents.ensure_builtin_default_agents([])
    with BotService() as bots:
        bots.create_bot(display_name="Synthetic companion", handle="littlevibey", model="synthetic-model")
    requests = TaskExecutionStore()
    from config.v2_config import V2Config

    controller = SimpleNamespace(config=V2Config.default(), vibe_agent_store=agents,
                                 scheduled_task_service=SimpleNamespace(request_store=requests))
    yield controller
    agents.close()
    requests.sqlite_backend.close()


def test_projection_does_not_send_full_long_answer():
    assert speech_projection("苹果是红色的。你想吃一个吗？" + "详情" * 200) == "苹果是红色的。你想吃一个吗？"
    assert speech_projection("`secret code`<silent>hidden</silent>你好。") == "你好。"
    assert speech_projection("长" * 500) == ""
    assert speech_projection("Done. unfinished") == "Done."
    assert speech_projection("unfinished") == ""
    assert speech_projection("Run `rm -rf /` now.") == "Run now."
    assert speech_projection("Hello. `unterminated code") == "Hello."
    assert speech_projection("Use ``secret`value`` carefully.") == "Use carefully."


@pytest.mark.parametrize(("source", "expected"), [
    ("# 标题。\n\n正文。第三句。", "标题。 正文。"),
    ("- **苹果**是红色的。\n- *梨子*是黄色的。", "苹果是红色的。 梨子是黄色的。"),
    ("1. First sentence.\n2. Second sentence.\n3. Third.", "First sentence. Second sentence."),
    ("> Quoted **prose**.\n>\n> - Nested prose.", "Quoted prose. Nested prose."),
    ("Ap**ple** is red.", "Apple is red."),
    ("Hello.\n\n```sh\nDO_NOT_SPEAK.\n```\n\n再见。", "Hello. 再见。"),
    ("你好。\n\n~~~sh\nDO_NOT_SPEAK.\n~~~\n\nGoodbye.", "你好。 Goodbye."),
    ("你好。\n\n```sh\nDO_NOT_SPEAK.", "你好。"),
    ("你好。\n\n~~~sh\nDO_NOT_SPEAK.", "你好。"),
    ("Hello.\n\n    DO_NOT_SPEAK.\n\n再见。", "Hello. 再见。"),
    ("Hello.\n\n\tDO_NOT_SPEAK.\n\n再见。", "Hello. 再见。"),
    ("Run:\n    rm -rf /.", ""),
    ("> Run:\n>     DO_NOT_SPEAK.", ""),
    ("- Hello.\n\n      DO_NOT_SPEAK.\n\n再见。", "Hello. 再见。"),
    ("Hello `DO_NOT_SPEAK` world.", "Hello world."),
    ("Hello ``secret`code`` world.", "Hello world."),
    ("Hello. `DO_NOT_SPEAK **MORE_CODE**.", "Hello."),
    ("Hello. ``DO_NOT_SPEAK ` more.", "Hello."),
    ("`DO_NOT_SPEAK.", ""),
    ("Before <code>DO_NOT_SPEAK.</code> after.", "Before after."),
    ("Before <kbd>DO_NOT_SPEAK.</kbd> after.", "Before after."),
    ("Before <pre>DO_NOT_SPEAK.</pre> after.", "Before after."),
    ("Before <unknown data-x='>'>DO_NOT_SPEAK.</unknown> after.", "Before after."),
    ("Before <CoDe><b>DO_NOT_SPEAK.</b></CoDe> after.", "Before after."),
    ("Before <code>DO_NOT_SPEAK. **MORE_CODE**</code> after.", "Before after."),
    ("Before <code>DO_NOT_SPEAK. `MORE_CODE`</code> after.", "Before after."),
    ("Hello. <code>DO_NOT_SPEAK.", ""),
    ("Hello. <unknown>DO_NOT_SPEAK.", ""),
    ("Hello. </unknown> DO_NOT_SPEAK.", ""),
    ("Hello. <code><b>DO_NOT_SPEAK.</code></b>", ""),
    ("Before <unknown>DO_NOT_SPEAK.\n\nMORE_CODE.</unknown> After.", "After."),
    ("Hello.\n\n<div>\nDO_NOT_SPEAK.\n</div>\n\n再见。", "Hello. 再见。"),
    ("Hello.\n\n<div>\nDO_NOT_SPEAK.\n\nMORE_CODE.", "Hello."),
    ("Hello.<br/> Goodbye.", "Hello. Goodbye."),
    ("Hello. <!-- DO_NOT_SPEAK. --> Goodbye.", "Hello. Goodbye."),
    ("Hello. ![DO_NOT_SPEAK.](https://example.org/image.png) Goodbye.", "Hello. Goodbye."),
    ("[Hello](https://example.org/private). [再见][ref]。\n\n[ref]: https://example.org/secret", "Hello. 再见。"),
    ("Visit <https://example.org/private> now.", "Visit now."),
    ("Write to <person@example.org> later.", "Write to later."),
    ("Visit https://example.org/private now.", "Visit now."),
    ("Visit HTTP://example.org/private now.", "Visit now."),
    ("Visit www.example.org/private now.", "Visit now."),
    ("Visit example.org/private now.", "Visit now."),
    ("Visit //example.org/private now.", "Visit now."),
    ("Visit ftp://example.org/private now.", "Visit now."),
    ("网址https://example.org/secret 不朗读。", "不朗读。"),
    ("Hello.\n\n| DO_NOT_SPEAK. |\n| --- |\n| MORE_CODE. |\n\n再见。", "Hello. 再见。"),
    ("Hello.\n\n---\n\nDO_NOT_SPEAK.", "Hello."),
    ("你好。\n\n***\n\nDO_NOT_SPEAK.", "你好。"),
    ("Hello.\n\n<silent>DO_NOT_SPEAK.\n~~~\nMORE_CODE\n~~~</silent>\n\n再见。", "Hello. 再见。"),
    ("Hello. <silent>DO_NOT_SPEAK.", ""),
    ("unfinished", ""),
    ("Done. unfinished", "Done."),
    ("甲" * 119 + "。", "甲" * 119 + "。"),
    ("甲" * 120 + "。", ""),
    ("甲" * 115 + "。第二句太长。", "甲" * 115 + "。"),
])
def test_projection_extracts_only_structured_prose(source, expected):
    assert speech_projection(source) == expected


async def site(app, bound_socket=None):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    listener = web.SockSite(runner, bound_socket) if bound_socket else web.TCPSite(runner, "127.0.0.1", 0)
    await listener.start()
    return runner, listener._server.sockets[0].getsockname()[1]


def audio(rate=24000, seconds=0.12, *, silent=False):
    pcm = b"".join(struct.pack("<h", 0 if silent else int(6000 * math.sin(2 * math.pi * 440 * i / rate)))
                   for i in range(int(rate * seconds)))
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return out.getvalue()


@pytest.mark.asyncio
async def test_remote_caller_cannot_use_device_proxy(monkeypatch):
    from vibe.authorization import AuthorizationContext
    monkeypatch.setattr("vibe.cli._cli_invocation_authority", lambda _: AuthorizationContext(is_remote=True))
    response = await handle_control_request(SimpleNamespace(), new_request("device.voice", {"action": "status"}).to_payload())
    assert response["payload"]["code"] == "local_device_only"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="The optional v1 host transport is Unix-only")
async def test_real_two_turn_socket_codec_host_cloud(plugin, controller, monkeypatch, tmp_path, caplog):
    """Only cloud providers and native model work are synthetic; wire/host/DB real."""
    pytest.importorskip("opuslib_next")
    from avibe_device.codec import Codec
    from avibe_device.host import Host
    from avibe_device.pairing import Pairing
    from avibe_device.provision import ProvisionWindow
    from avibe_device.server import AUDIO, create_app
    from config.v2_config import V2Config

    transcripts = iter(["苹果是什么颜色？", "那它可以吃吗？", "先不聊了"])
    provider_calls = []
    full_answers = [
        "**苹果**是红色的。\n\n~~~sh\nDO_NOT_SPEAK.\n~~~\n\n<pre>MORE_CODE.</pre>\n\n---\n\n快捷回复不朗读。",
        "可以，刚才说的[苹果](https://example.org/private)可以吃。\n\n    DO_NOT_SPEAK.",
    ]
    answers = iter(full_answers)
    store = controller.scheduled_task_service.request_store
    prompts = []
    conversation_ids = []

    async def transcribe(request):
        assert request.headers["X-Vibe-Instance-Id"] == "synthetic-instance"
        assert request.headers["X-Vibe-Device-Secret"] == "synthetic-cloud-secret"
        reader = await request.multipart()
        received = {}
        async for field in reader:
            received[field.name] = await field.read()
        with wave.open(io.BytesIO(received["file"]), "rb") as wav:
            assert (wav.getframerate(), wav.getnchannels()) == (16000, 1)
            assert wav.getnframes() > 0
        provider_calls.append("asr")
        return web.json_response({"text": next(transcripts)})

    async def speech(request):
        payload = await request.json()
        assert set(payload) == {"text"}
        assert "synthetic-cloud-secret" not in json.dumps(payload)
        assert payload["text"] in {"苹果是红色的。", "可以，刚才说的苹果可以吃。"}
        provider_calls.append(payload["text"])
        return web.Response(body=audio(), content_type="audio/wav")

    async def model(request):
        from tests.test_device_chat import served_headers, text_stream

        body = await request.json()
        assert not body.get("tools") and not body.get("functions")
        prompts.append(body["messages"])
        if len(prompts) == 2:
            assert any("苹果是什么颜色" in str(m) for m in body["messages"])
            assert any("是红色的" in str(m) for m in body["messages"])
            assert all("Unrelated Web" not in str(m) for m in body["messages"])
        return web.Response(body=text_stream(next(answers)), headers=served_headers())

    cloud = web.Application()
    cloud.router.add_post("/v1/audio/transcriptions", transcribe)
    cloud.router.add_post("/v1/audio/speech", speech)
    cloud.router.add_post("/v1/chat/completions", model)
    cloud_runner, cloud_port = await site(cloud)
    config = V2Config.default()
    # Inject only the established runtime credential boundary; never load config.
    controller.config = config
    monkeypatch.setattr("core.audio_asr.AudioAsrService._runtime_config",
                        lambda _: SimpleNamespace(base_url=f"http://127.0.0.1:{cloud_port}",
                                                  instance_id="synthetic-instance", device_secret="synthetic-cloud-secret"))
    config.audio_asr.enabled = True

    from tests.test_device_chat import SyntheticHub
    from core.internal_server import create_app as host_app, _create_controller_loop_server
    import uvicorn

    controller.model_hub_runtime = SyntheticHub(f"http://127.0.0.1:{cloud_port}/v1")
    socket_root = tempfile.TemporaryDirectory(prefix="avdv-", dir="/tmp")
    socket_path = Path(socket_root.name) / "host.sock"
    ipc_server = _create_controller_loop_server(uvicorn.Config(
        host_app(controller), uds=str(socket_path), lifespan="off", log_config=None, log_level="critical",
    ))
    ipc_task = asyncio.create_task(ipc_server.serve())
    for _ in range(100):
        if ipc_server.started:
            break
        await asyncio.sleep(0.01)
    assert ipc_server.started
    os.chmod(socket_path, 0o600)
    monkeypatch.setenv("VIBE_INTERNAL_DISPATCH_SOCKET", str(socket_path))
    pairing = Pairing(tmp_path / "plugin")
    token = pairing.pair("test-device")
    pairing.enable(True)
    bound_socket = socket.socket()
    bound_socket.bind(("127.0.0.1", 0))
    port = bound_socket.getsockname()[1]
    provision_file = tmp_path / "provision.json"
    provision_file.write_text(json.dumps({"websocket": {
        "url": f"ws://127.0.0.1:{port}/device/ws", "token": token, "version": 2,
    }}))
    provision_file.chmod(0o600)
    provision = ProvisionWindow(pairing, provision_file, bind="127.0.0.1", port=port)
    device_runner, port = await site(create_app(pairing, Host(), settling=0.01, provision=provision), bound_socket)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(f"http://127.0.0.1:{port}/device/ota",
                                   headers={"Device-Id": "test-device"}, json={"version": 2}) as response:
                assert response.status == 200
                config = (await response.json())["websocket"]
                assert config["version"] == 2
            headers = {"Device-Id": "test-device", "Authorization": "Bearer " + config["token"]}
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await client.ws_connect(f"http://127.0.0.1:{port}/device/ws")
            async with client.ws_connect(config["url"], headers=headers) as ws:
                await ws.send_json({"type": "hello", "version": 2, "features": {"avibe_voice": 1}, "audio_params": AUDIO})
                hello = await ws.receive_json(timeout=5)
                assert hello["avibe_voice"] == {"version": 1, "route_revision": 4}
                assert token not in json.dumps(hello) and "secret" not in json.dumps(hello)
                await ws.send_json({"type": "listen", "state": "detect", "protocol_version": 1,
                                    "route_revision": 4, "wakeword_id": "xiaowai-xiaowai",
                                    "session_id": hello["session_id"]})
                request_ids = []
                ordinary = await handle_control_request(controller, new_request("agent.run", {
                    "bot": "littlevibey", "queue": True, "no_reply": True, "message": "Unrelated Web privileged context",
                }).to_payload())
                assert ordinary["payload"]["ok"]
                for turn in range(3):
                    start = await ws.receive_json(timeout=10)
                    assert start["type"] == "listen" and start["state"] == "start"
                    request_ids.append(start["request_id"])
                    if not conversation_ids:
                        conversation_ids.append(controller.device_chat.active.id)
                    await ws.send_json(start)
                    encoder = Codec()
                    pcm = struct.pack("<960h", *[int(6000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(960)])
                    for frame in [pcm] * 3 + [b"\0" * 1920] * 14:
                        await ws.send_bytes(encoder.encode(frame))
                        await asyncio.sleep(0.004)
                    stop = await ws.receive_json(timeout=5)
                    assert stop == {**start, "state": "stop"}
                    await ws.send_json(stop)
                    if turn == 2:
                        assert (await ws.receive(timeout=10)).type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
                        break
                    tts = await ws.receive_json(timeout=10)
                    assert tts["type"] == "tts" and tts["request_id"] == start["request_id"]
                    # No binary output before the device accepts this playback.
                    with pytest.raises(asyncio.TimeoutError):
                        await ws.receive(timeout=0.03)
                    await ws.send_json({**tts, "type": "playback", "state": "started"})
                    frames = 0
                    while True:
                        packet = await ws.receive(timeout=5)
                        if packet.type == aiohttp.WSMsgType.TEXT:
                            sealed = json.loads(packet.data)
                            assert sealed == {**tts, "state": "stop"}
                            break
                        assert packet.type == aiohttp.WSMsgType.BINARY
                        assert len(encoder.decode(packet.data)) == 1920
                        frames += 1
                    assert frames == 2
                    with pytest.raises(asyncio.TimeoutError):
                        await ws.receive(timeout=0.03)
                    await ws.send_json({**tts, "type": "playback", "state": "drained",
                                        "boundary": "queues_empty_after_output_write", "tail_guard_ms": 80})
                assert len(set(request_ids)) == 3
        assert len(prompts) == 2
        history = controller.device_chat.history(conversation_ids[0])
        assert [row["output"] for row in history["turns"]] == full_answers
        assert len(store.list_runs()) == 1  # Only the ordinary Web Run, never device input.
        assert provider_calls == ["asr", "苹果是红色的。", "asr", "可以，刚才说的苹果可以吃。", "asr"]
        assert "synthetic-cloud-secret" not in caplog.text
        assert "苹果是什么颜色" not in caplog.text
    finally:
        await device_runner.cleanup()
        ipc_server.should_exit = True
        await asyncio.wait_for(ipc_task, 5)
        socket_root.cleanup()
        await cloud_runner.cleanup()


def test_pair_revoke_and_disabled_state(plugin, tmp_path):
    from avibe_device.pairing import Pairing
    pairing = Pairing(tmp_path / "plugin")
    token = pairing.pair("board")
    assert not pairing.accepts("board", "Bearer " + token)
    pairing.enable(True)
    assert pairing.accepts("board", "Bearer " + token)
    assert not pairing.accepts("other", "Bearer " + token)
    assert token not in pairing.path.read_text()
    pairing.enable(False)
    assert pairing.status()["paired"]
    pairing.revoke()
    assert not pairing.accepts("board", "Bearer " + token)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["revoke", "disable", "abort", "wrong_playback", "duplicate_drain", "own_mic", "offline"])
async def test_device_faults_do_not_replay_or_transcribe_playback(plugin, tmp_path, fault):
    pytest.importorskip("opuslib_next")
    from avibe_device.codec import Codec
    from avibe_device.pairing import Pairing
    from avibe_device.server import create_app

    fixture = json.loads((Path(__file__).parent / "fixtures/littlevibey-wire-v1.json").read_text())

    closed = asyncio.Event()

    class FakeHost:
        calls = []

        async def call(self, action, **kwargs):
            self.calls.append(action)
            if action == "open":
                return {"conversation_id": str(uuid4())}
            if action == "close":
                closed.set()
                return {}
            return {"text": "你好"} if action == "transcribe" else {"audio": base64.b64encode(audio()).decode()}

        async def answer(self, conversation_id, request_id, text):
            self.calls.append("answer")
            return "你好。"

    host = FakeHost()
    pairing = Pairing(tmp_path / "plugin")
    token = pairing.pair("test-device")
    pairing.enable(True)
    runner, port = await site(create_app(pairing, host, settling=0.01))
    headers = {"Device-Id": "test-device", "Authorization": "Bearer " + token}
    try:
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(f"http://127.0.0.1:{port}/device/ws", headers=headers) as ws:
                await ws.send_json(fixture["device_hello"])
                hello = await ws.receive_json(timeout=2)
                await ws.send_json({**fixture["wake"], "session_id": hello["session_id"]})
                capture = await ws.receive_json(timeout=2)
                await ws.send_json(capture)
                codec = Codec()
                voiced = struct.pack("<960h", *[int(6000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(960)])
                for pcm in [voiced] * 3 + [b"\0" * 1920] * 14:
                    await ws.send_bytes(codec.encode(pcm))
                    await asyncio.sleep(0.003)
                capture_stop = await ws.receive_json(timeout=2)
                await ws.send_json(capture_stop)
                tts = await ws.receive_json(timeout=2)
                await ws.send_json({**tts, "type": "playback", "state": "started"})
                while True:
                    event = await ws.receive(timeout=2)
                    if event.type == aiohttp.WSMsgType.TEXT:
                        assert json.loads(event.data)["state"] == "stop"
                        break
                receipt = {**tts, "type": "playback", "state": "drained",
                           "boundary": "queues_empty_after_output_write", "tail_guard_ms": 80}
                if fault == "revoke":
                    pairing.revoke()
                elif fault == "disable":
                    pairing.enable(False)
                elif fault == "abort":
                    await ws.send_json({"type": "abort", "session_id": hello["session_id"]})
                elif fault == "wrong_playback":
                    await ws.send_json({**receipt, "playback_id": "old"})
                elif fault == "duplicate_drain":
                    await ws.send_json(receipt)
                    await ws.send_json(receipt)
                elif fault == "own_mic":
                    await ws.send_bytes(codec.encode(voiced))
                else:
                    await ws.close()
                while True:
                    event = await ws.receive(timeout=2)
                    if event.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
                        break
                    # A duplicate arriving after an accepted drain may race the fresh
                    # gate command, but never gets acknowledged as fresh input.
                    assert fault == "duplicate_drain"
                    assert event.type == aiohttp.WSMsgType.TEXT
                    assert json.loads(event.data)["type"] == "listen"
                await asyncio.wait_for(closed.wait(), 2)
                assert host.calls == ["open", "transcribe", "answer", "speech", "close"]
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_repeated_stop_releases_listener_preserves_pairing_and_host(plugin, controller, tmp_path):
    pytest.importorskip("opuslib_next")
    from avibe_device.cli import serve
    from avibe_device.pairing import Pairing

    pairing = Pairing(tmp_path / "plugin")
    token = pairing.pair("test-device", name="My Vibey")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    provision_file = tmp_path / "provision.json"
    provision_file.write_text(json.dumps({"websocket": {
        "url": f"ws://127.0.0.1:{port}/device/ws", "token": token, "version": 2,
    }}))
    provision_file.chmod(0o600)
    for cycle in range(2):
        pairing.enable(True)
        stopped = asyncio.Event()
        task = asyncio.create_task(serve(pairing, bind="127.0.0.1", port=port, inactivity=20,
                                         threshold=600, stopped=stopped,
                                         provision_file=provision_file if cycle == 0 else None))
        try:
            async with aiohttp.ClientSession() as client:
                for attempt in range(30):
                    try:
                        async with client.get(f"http://127.0.0.1:{port}/device/ws") as response:
                            assert response.status == 401
                        break
                    except aiohttp.ClientConnectionError:
                        await asyncio.sleep(0.01)
                else:
                    pytest.fail("device listener did not start")
                async with client.post(f"http://127.0.0.1:{port}/device/ota",
                                       headers={"Device-Id": "test-device"}) as response:
                    assert response.status == (200 if cycle == 0 else 403)
            assert pairing.status()["running"]
            with pytest.raises(ValueError, match="device_already_running"):
                await serve(pairing, bind="127.0.0.1", port=0, inactivity=20,
                            threshold=600, stopped=asyncio.Event())
            pairing.enable(False)
            await asyncio.wait_for(task, 2)
            assert not pairing.status()["running"]
            with socket.socket() as proof:
                proof.bind(("127.0.0.1", port))
        finally:
            stopped.set()
            await task
        assert pairing.status()["paired"] and pairing.status()["name"] == "My Vibey"
        normal = await handle_control_request(controller, new_request("agent.run", {
            "bot": "littlevibey", "queue": True, "no_reply": True, "message": "Unrelated synthetic Web chat",
        }).to_payload())
        assert normal["payload"]["ok"], normal


@pytest.mark.asyncio
async def test_legacy_device_and_inactivity_fail_closed(plugin, tmp_path):
    pytest.importorskip("opuslib_next")
    from avibe_device.pairing import Pairing
    from avibe_device.server import AUDIO, create_app

    pairing = Pairing(tmp_path / "plugin")
    token = pairing.pair("test-device")
    pairing.enable(True)
    runner, port = await site(create_app(pairing, inactivity=0.05))
    try:
        async with aiohttp.ClientSession() as client:
            for features in ({}, {"avibe_voice": 1}):
                async with client.ws_connect(f"http://127.0.0.1:{port}/device/ws", headers={
                    "Device-Id": "test-device", "Authorization": "Bearer " + token,
                }) as ws:
                    await ws.send_json({"type": "hello", "version": 2, "audio_params": AUDIO, "features": features})
                    if features:
                        assert (await ws.receive_json(timeout=2))["type"] == "hello"
                    assert (await ws.receive(timeout=2)).type == aiohttp.WSMsgType.CLOSE
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_plugin_process_crash_leaves_host_chat_available(plugin, controller, tmp_path):
    pytest.importorskip("opuslib_next")
    from avibe_device.pairing import Pairing

    pairing = Pairing(tmp_path / "plugin")
    pairing.pair("test-device")
    pairing.enable(True)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    package = str(Path(__file__).parents[1] / "packaging" / "avibe-device")
    env = {**os.environ, "PYTHONPATH": package + os.pathsep + os.environ.get("PYTHONPATH", "")}
    process = subprocess.Popen(
        [sys.executable, "-m", "avibe_device.cli", "--state-dir", str(pairing.root),
         "serve", "--port", str(port)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        async with aiohttp.ClientSession() as client:
            for _ in range(100):
                try:
                    async with client.get(f"http://127.0.0.1:{port}/device/ws") as response:
                        assert response.status == 401
                    break
                except aiohttp.ClientConnectionError:
                    if process.poll() is not None:
                        pytest.fail("isolated process exited: " + process.stderr.read().decode())
                    await asyncio.sleep(0.02)
            else:
                pytest.fail("isolated optional process did not listen")
        process.kill()
        await asyncio.to_thread(process.wait, 5)
        normal = await handle_control_request(controller, new_request("agent.run", {
            "bot": "littlevibey", "queue": True, "no_reply": True, "message": "Synthetic host still works",
        }).to_payload())
        assert normal["payload"]["ok"], normal
        assert pairing.status()["paired"]
        assert not pairing.status()["running"]
        with socket.socket() as proof:
            proof.bind(("127.0.0.1", port))
    finally:
        if process.poll() is None:
            process.kill()
        await asyncio.to_thread(process.wait, 5)
        process.stderr.close()


def test_pair_cli_emits_only_private_device_provisioning(plugin, tmp_path, capsys):
    from avibe_device.cli import main
    from avibe_device.pairing import Pairing

    root = tmp_path / "pairing"
    provision = tmp_path / "provision.json"
    assert main(["--state-dir", str(root), "pair", "--device-id", "board", "--name", "My Vibey",
                 "--websocket-url", "ws://127.0.0.1:8765/device/ws",
                 "--provision-file", str(provision)]) == 0
    value = json.loads(provision.read_text())
    token = value["websocket"]["token"]
    assert token not in capsys.readouterr().out
    assert token not in (root / "device.json").read_text()
    assert provision.stat().st_mode & 0o777 == 0o600
    assert (root / "device.json").stat().st_mode & 0o777 == 0o600
    assert Pairing(root).status()["name"] == "My Vibey"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="The optional provisioning file is Unix owner-private")
@pytest.mark.parametrize("end", ["expiry", "disable", "revoke", "shutdown"])
async def test_ota_window_is_explicit_bounded_and_device_scoped(plugin, tmp_path, caplog, end):
    pytest.importorskip("audioop")  # Optional codec dependency on Python 3.13+.
    from avibe_device.pairing import Pairing
    from avibe_device.provision import ProvisionWindow
    from avibe_device.server import create_app

    pairing = Pairing(tmp_path / "plugin")
    token = pairing.pair("aa:bb:cc:dd:ee:ff")
    pairing.enable(True)
    payload = {"websocket": {"url": "ws://127.0.0.1:8765/device/ws", "token": token, "version": 2}}
    path = tmp_path / "provision.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    window = ProvisionWindow(pairing, path, bind="127.0.0.1", port=8765)
    # Provisioning uses no cloud/model capability and no host credential.
    runner, port = await site(create_app(pairing, object(), provision=window))
    url = f"http://127.0.0.1:{port}/device/ota"
    try:
        async with aiohttp.ClientSession() as client:
            for device_id in ("", "unpaired", "AA:BB:CC:DD:EE:FF"):
                async with client.post(url, headers={"Device-Id": device_id}) as response:
                    assert response.status == 403
                    assert token not in await response.text()
            for _ in range(2):  # A lost OTA response may be retried during the window.
                async with client.post(url, headers={"Device-Id": "aa:bb:cc:dd:ee:ff"},
                                       json={"firmware": {"version": "synthetic"}}) as response:
                    assert response.status == 200 and await response.json() == payload
                    assert response.headers["Cache-Control"] == "no-store"
            if end == "expiry":
                window.deadline = 0
            elif end == "disable":
                pairing.enable(False)
            elif end == "revoke":
                pairing.revoke()
            else:
                await runner.cleanup()
                runner, port = await site(create_app(pairing, object()))
                url = f"http://127.0.0.1:{port}/device/ota"
            async with client.post(url, headers={"Device-Id": "aa:bb:cc:dd:ee:ff"}) as response:
                assert response.status == 403 and token not in await response.text()
            assert window.document is None
            assert token not in pairing.path.read_text() and token not in caplog.text
    finally:
        await runner.cleanup()


@pytest.mark.skipif(os.name == "nt", reason="The optional provisioning file is Unix owner-private")
@pytest.mark.parametrize("invalid", ["disabled", "revoked", "token", "url", "extra", "permissions", "symlink", "seconds"])
def test_ota_window_rejects_invalid_provisioning_material(plugin, tmp_path, invalid):
    from avibe_device.pairing import Pairing
    from avibe_device.provision import ProvisionWindow

    pairing = Pairing(tmp_path / "plugin")
    token = pairing.pair("board")
    pairing.enable(True)
    payload = {"websocket": {"url": "ws://127.0.0.1:8765/device/ws", "token": token, "version": 2}}
    if invalid == "disabled":
        pairing.enable(False)
    elif invalid == "revoked":
        pairing.revoke()
    elif invalid == "token":
        payload["websocket"]["token"] = "different-device-token"
    elif invalid == "url":
        payload["websocket"]["url"] = "ws://127.0.0.1:9999/device/ws"
    elif invalid == "extra":
        payload["firmware"] = {"url": "must-not-serve"}
    path = tmp_path / "provision.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o644 if invalid == "permissions" else 0o600)
    if invalid == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(path)
        path = link
    with pytest.raises((ValueError, OSError)):
        ProvisionWindow(pairing, path, bind="127.0.0.1", port=8765, seconds=301 if invalid == "seconds" else 120)


@pytest.mark.asyncio
async def test_abort_cancels_pending_voice_capability(plugin, tmp_path):
    pytest.importorskip("opuslib_next")
    from avibe_device.codec import Codec
    from avibe_device.pairing import Pairing
    from avibe_device.server import create_app

    started, cancelled = asyncio.Event(), asyncio.Event()

    class PendingHost:
        async def call(self, action, **kwargs):
            if action == "open":
                return {"conversation_id": str(uuid4())}
            if action == "close":
                return {}
            assert action == "transcribe"
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    pairing = Pairing(tmp_path / "plugin")
    token = pairing.pair("board")
    pairing.enable(True)
    runner, port = await site(create_app(pairing, PendingHost()))
    fixture = json.loads((Path(__file__).parent / "fixtures/littlevibey-wire-v1.json").read_text())
    try:
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(f"http://127.0.0.1:{port}/device/ws", headers={
                "Device-Id": "board", "Authorization": "Bearer " + token,
            }) as ws:
                await ws.send_json(fixture["device_hello"])
                hello = await ws.receive_json(timeout=2)
                await ws.send_json({**fixture["wake"], "session_id": hello["session_id"]})
                await ws.send_json(await ws.receive_json(timeout=2))
                codec = Codec()
                voiced = struct.pack("<960h", *[int(6000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(960)])
                for pcm in [voiced] * 3 + [b"\0" * 1920] * 14:
                    await ws.send_bytes(codec.encode(pcm))
                    await asyncio.sleep(0.003)
                await ws.send_json(await ws.receive_json(timeout=2))
                await asyncio.wait_for(started.wait(), 2)
                await ws.send_json({"type": "abort", "session_id": hello["session_id"]})
                assert (await ws.receive(timeout=2)).type == aiohttp.WSMsgType.CLOSE
                await asyncio.wait_for(cancelled.wait(), 2)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["transcribe", "speech"])
async def test_real_host_disconnect_cancels_cloud_audio(monkeypatch, action):
    from core.internal_server import create_app

    started, cancelled, disconnected = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def pending_cloud(_config, actual_action, _value):
        assert actual_action == action
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("core.handlers.device_voice.cloud_audio", pending_cloud)
    app = create_app(SimpleNamespace(config=SimpleNamespace()))
    payload = new_request("device.voice", {
        "action": action, "audio" if action == "transcribe" else "text": "synthetic",
    }).to_payload()
    first = True

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(_message):
        pass

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": "/internal/control", "raw_path": b"/internal/control", "query_string": b"",
             "headers": [(b"content-type", b"application/json")], "server": ("localhost", 80), "client": ("localhost", 123)}
    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(started.wait(), 2)
        disconnected.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert cancelled.is_set()
    finally:
        disconnected.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
