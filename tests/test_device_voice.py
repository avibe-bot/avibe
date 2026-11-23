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
from core.handlers.device_voice import attributed_speech, speech_projection
from core.scheduled_tasks import TaskExecutionStore
from core.vibe_agents import VibeAgentStore
from storage import message_deliveries
from storage.background import attach_agent_run_delivery_in_connection
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
        bots.create_bot(display_name="Synthetic companion", handle="littlevibey")
    requests = TaskExecutionStore()
    controller = SimpleNamespace(vibe_agent_store=agents, scheduled_task_service=SimpleNamespace(request_store=requests))
    yield controller
    agents.close()
    requests.sqlite_backend.close()


def finish_run(store, request_id, text, *, shared=False):
    """Synthetic backend output; real persisted Run, Delivery, Turn and output."""
    run = store.get_run(request_id)
    session_id = run["session_id"]
    turn_id = "turn_" + uuid4().hex
    delivery_id = "delivery_" + uuid4().hex
    with store.sqlite_backend.engine.begin() as conn:
        snapshot = message_deliveries.message_snapshot(
            scope_id=None, session_id=session_id, platform="avibe", author="harness", source="harness",
            message_type="harness", text=run["prompt"],
            metadata={"scheduled_provenance": {"platform_specific": {
                "task_trigger_kind": "agent_run", "task_execution_id": request_id,
            }}},
        )
        batch = [message_deliveries.insert_delivery(
            conn, delivery_id=delivery_id, session_id=session_id, priority="p3", state="queued",
            snapshot=snapshot, dispatch_text=run["prompt"],
        )]
        assert attach_agent_run_delivery_in_connection(conn, request_id, session_id=session_id, delivery_id=delivery_id)
        message_deliveries.claim_start_batch(
            conn, turn_id=turn_id, session_id=session_id, backend="vibey", deliveries=batch,
            dispatch_text=run["prompt"], attempt_id="atm_" + turn_id,
        )
        turn = message_deliveries.get_turn(conn, turn_id)
        message_deliveries.bind_native_start(conn, turn_id, expected_version=turn["version"],
                                            runtime_key="test", runtime_turn_id=turn_id, native_turn_id=turn_id)
        assert message_deliveries.materialize_start_acceptance(conn, turn_id=turn_id, evidence={"kind": "test"})
        if shared:
            extra = message_deliveries.insert_delivery(
                conn, delivery_id="web_" + uuid4().hex, session_id=session_id, priority="p1", state="queued",
                snapshot=message_deliveries.message_snapshot(
                    scope_id=None, session_id=session_id, platform="avibe", author="user", source="web",
                    message_type="user", text="Unrelated web input", metadata={},
                ), dispatch_text="Unrelated web input",
            )
            message_deliveries.open_steer_attempt(
                conn, extra["id"], expected_version=extra["version"], turn_id=turn_id,
                attempt_id="steer_" + turn_id, expected_native_turn_id=turn_id,
            )
            assert message_deliveries.materialize_steer_acceptance(
                conn, leader_delivery_id=extra["id"], expected_attempt_id="steer_" + turn_id,
                turn_id=turn_id, evidence={"kind": "test"},
            )
        message_deliveries.terminalize_turn(conn, turn_id, outcome="completed", settled_by="test",
                                           evidence_kind="synthetic_backend")
    store.sqlite_backend.record_turn_run_outputs(
        [request_id], output_id="terminal", text=text, provenance={"turn_id": turn_id, "run_id": request_id},
        terminal_status="succeeded",
    )
    return store.get_run(request_id)


def enqueue(store):
    return store.enqueue_agent_run(agent_name="littlevibey", session_id="ses_littlevibey",
                                   session_key="avibe::channel::ses_littlevibey", message="Synthetic voice").id


@pytest.mark.parametrize("shared", [False, True])
def test_result_requires_sole_durable_input(controller, shared):
    store = controller.scheduled_task_service.request_store
    run = finish_run(store, enqueue(store), "苹果是红色的。", shared=shared)
    with store.sqlite_backend.engine.connect() as conn:
        assert attributed_speech(run, conn) == (None if shared else "苹果是红色的。")
        for changed in (
            {**run, "status": "failed"},
            {**run, "result_text": "Unrelated final"},
            {**run, "result_payload": {"outputs": []}},
        ):
            assert attributed_speech(changed, conn) is None


def test_projection_does_not_send_full_long_answer():
    assert speech_projection("苹果是红色的。你想吃一个吗？" + "详情" * 200) == "苹果是红色的。你想吃一个吗？"
    assert speech_projection("```secret code```<silent>hidden</silent>你好。") == "你好。"
    assert speech_projection("长" * 500) == ""


async def site(app):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    listener = web.TCPSite(runner, "127.0.0.1", 0)
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
    from avibe_device.server import AUDIO, create_app
    from config.v2_config import V2Config

    transcripts = iter(["苹果是什么颜色？", "那它可以吃吗？", "先不聊了"])
    provider_calls = []
    answers = iter(["苹果是红色的。", "可以，刚才说的苹果可以吃。"])
    store = controller.scheduled_task_service.request_store
    prompts = []

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
        provider_calls.append(payload["text"])
        return web.Response(body=audio(), content_type="audio/wav")

    cloud = web.Application()
    cloud.router.add_post("/v1/audio/transcriptions", transcribe)
    cloud.router.add_post("/v1/audio/speech", speech)
    cloud_runner, cloud_port = await site(cloud)
    config = V2Config.default()
    # Inject only the established runtime credential boundary; never load config.
    controller.config = config
    monkeypatch.setattr("core.audio_asr.AudioAsrService._runtime_config",
                        lambda _: SimpleNamespace(base_url=f"http://127.0.0.1:{cloud_port}",
                                                  instance_id="synthetic-instance", device_secret="synthetic-cloud-secret"))
    config.audio_asr.enabled = True

    async def control(request):
        assert "Authorization" not in request.headers
        payload = await request.json()
        response = await handle_control_request(controller, payload)
        if payload["arguments"].get("action") == "submit" and response["payload"].get("ok"):
            run_id = response["payload"]["run"]["id"]
            run = store.get_run(run_id)
            assert run["session_id"] == "ses_littlevibey"
            prompts.append(run["prompt"])
            # The second synthetic answer consumes the first prompt as context.
            if len(prompts) == 2:
                assert "苹果是什么颜色" in prompts[0] and "那它可以吃吗" in prompts[1]
            finish_run(store, run_id, next(answers))
        return web.json_response(response)

    ipc = web.Application(client_max_size=3_000_000)
    ipc.router.add_post("/internal/control", control)
    ipc_runner = web.AppRunner(ipc, access_log=None)
    await ipc_runner.setup()
    socket_root = tempfile.TemporaryDirectory(prefix="avdv-", dir="/tmp")
    socket_path = Path(socket_root.name) / "host.sock"
    await web.UnixSite(ipc_runner, str(socket_path)).start()
    os.chmod(socket_path, 0o600)
    monkeypatch.setenv("VIBE_INTERNAL_DISPATCH_SOCKET", str(socket_path))
    pairing = Pairing(tmp_path / "plugin")
    token = pairing.pair("test-device")
    pairing.enable(True)
    device_runner, port = await site(create_app(pairing, Host(), settling=0.01))
    headers = {"Device-Id": "test-device", "Authorization": f"Bearer {token}"}
    try:
        async with aiohttp.ClientSession() as client:
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await client.ws_connect(f"http://127.0.0.1:{port}/device/ws")
            async with client.ws_connect(f"http://127.0.0.1:{port}/device/ws", headers=headers) as ws:
                await ws.send_json({"type": "hello", "version": 2, "features": {"avibe_voice": 1}, "audio_params": AUDIO})
                hello = await ws.receive_json(timeout=5)
                assert hello["avibe_voice"] == {"version": 1, "route_revision": 4}
                assert token not in json.dumps(hello) and "secret" not in json.dumps(hello)
                await ws.send_json({"type": "listen", "state": "detect", "protocol_version": 1,
                                    "route_revision": 4, "wakeword_id": "xiaowai-xiaowai",
                                    "session_id": hello["session_id"]})
                request_ids = []
                for turn in range(3):
                    start = await ws.receive_json(timeout=10)
                    assert start["type"] == "listen" and start["state"] == "start"
                    request_ids.append(start["request_id"])
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
        assert provider_calls == ["asr", "苹果是红色的。", "asr", "可以，刚才说的苹果可以吃。", "asr"]
        assert "synthetic-cloud-secret" not in caplog.text
        assert "苹果是什么颜色" not in caplog.text
    finally:
        await device_runner.cleanup()
        await ipc_runner.cleanup()
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

    class FakeHost:
        calls = []

        async def call(self, action, **kwargs):
            self.calls.append(action)
            return {"text": "你好"} if action == "transcribe" else {"audio": base64.b64encode(audio()).decode()}

        async def answer(self, request_id, text):
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
                assert host.calls == ["transcribe", "answer", "speech"]
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_repeated_stop_releases_listener_preserves_pairing_and_host(plugin, controller, tmp_path):
    pytest.importorskip("opuslib_next")
    from avibe_device.cli import serve
    from avibe_device.pairing import Pairing

    pairing = Pairing(tmp_path / "plugin")
    pairing.pair("test-device", name="My Vibey")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    for _ in range(2):
        pairing.enable(True)
        stopped = asyncio.Event()
        task = asyncio.create_task(serve(pairing, bind="127.0.0.1", port=port, inactivity=20,
                                         threshold=600, stopped=stopped))
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
async def test_abort_cancels_pending_voice_capability(plugin, tmp_path):
    pytest.importorskip("opuslib_next")
    from avibe_device.codec import Codec
    from avibe_device.pairing import Pairing
    from avibe_device.server import create_app

    started, cancelled = asyncio.Event(), asyncio.Event()

    class PendingHost:
        async def call(self, action, **kwargs):
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
