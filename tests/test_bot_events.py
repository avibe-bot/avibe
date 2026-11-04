"""Bot invalidation crosses the CLI/controller/UI process boundary once per mutation.

The old route-only publisher left a connected browser stale after CLI mutations.
Exercise the real CLI, internal event socket, bridge, and browser SSE generator;
ordinary Bot CRUD tests only checked the new rows or the HTTP response.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from PIL import Image

from core import inbox_events
from core.bots import BotService
from storage.importer import ensure_sqlite_state
from tests.ui_server_test_helpers import csrf_headers
from vibe import inbox_bridge, internal_client, ui_server
from vibe.authorization import instance_owner_context
from vibe.ui_compat import g


@pytest.fixture()
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(inbox_events, "_CONTROLLER_PROCESS", False)
    ensure_sqlite_state()
    with BotService() as service:
        service.store.ensure_builtin_default_agents([])
    return tmp_path


@contextlib.asynccontextmanager
async def _live_bridge(monkeypatch):
    # Keep AF_UNIX paths short on macOS. All persistent state stays in the test home.
    with tempfile.TemporaryDirectory(prefix="bot-events-", dir="/tmp") as socket_dir:
        socket_path = str(Path(socket_dir) / "events.sock")
        monkeypatch.setenv("VIBE_INTERNAL_DISPATCH_SOCKET", socket_path)
        controller = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "from types import SimpleNamespace; from pathlib import Path; import sys, asyncio; "
            "from core.internal_server import serve; "
            "asyncio.run(serve(SimpleNamespace(), socket_path=Path(sys.argv[1])))",
            socket_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        monkeypatch.setattr(inbox_bridge, "_bridge_connected", False)
        monkeypatch.setattr(inbox_bridge, "_BACKOFF_INITIAL", 0.02)
        bridge = asyncio.create_task(inbox_bridge.run_inbox_bridge())
        try:
            async with asyncio.timeout(15):
                while not inbox_bridge.is_bridge_connected():
                    if controller.returncode is not None:
                        _, stderr = await controller.communicate()
                        pytest.fail(stderr.decode())
                    await asyncio.sleep(0.02)
            yield
        except TimeoutError:
            if controller.returncode is None:
                controller.terminate()
            _, stderr = await asyncio.wait_for(controller.communicate(), timeout=5)
            pytest.fail(f"controller/bridge timed out: {stderr.decode()}")
        finally:
            bridge.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bridge
            if controller.returncode is None:
                controller.terminate()
            try:
                await asyncio.wait_for(controller.communicate(), timeout=5)
            except TimeoutError:
                controller.kill()
                await controller.communicate()


@contextlib.asynccontextmanager
async def _browser_events():
    with ui_server.app.test_request_context("/api/events"):
        g.authorization_context = instance_owner_context()
        response = await ui_server.workbench_events()
        iterator = response.body_iterator.__aiter__()
        try:
            for _ in range(3):
                await anext(iterator)
            yield iterator
        finally:
            await iterator.aclose()


async def _bot_events_through_barrier(iterator):
    # This goes through the same ordered controller feed. Reading it proves that
    # an echoed Web event has reached the bridge, rather than guessing with sleep.
    await internal_client.publish_event("definitions.updated", {"definition_type": "watch"})
    events = []
    async with asyncio.timeout(5):
        async for chunk in iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            if "event: definitions.updated\n" in text:
                return events
            if "event: bots.changed\n" in text:
                data = next(line[6:] for line in text.splitlines() if line.startswith("data: "))
                events.append(json.loads(data)["data"])
    raise AssertionError("browser stream ended before the barrier")


async def _cli(*args):
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "from vibe.cli import main; main()", *args,
        env=dict(os.environ), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=20)
    except BaseException:
        if child.returncode is None:
            child.kill()
            await child.communicate()
        raise
    assert child.returncode == 0, (stdout.decode(), stderr.decode())
    return json.loads(stdout)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="Exercises the Unix-domain IPC transport")
@pytest.mark.parametrize("entry", ["cli", "web"])
async def test_bot_mutations_reach_a_connected_browser_once(home, monkeypatch, entry):
    """Committed CLI/Web writes produce one browser event, including Web's IPC echo."""
    client = ui_server.app.test_client()

    def web(method, path, **kwargs):
        response = getattr(client, method)(path, headers=csrf_headers(client), **kwargs)
        assert response.status_code < 300, response.get_json()
        return response.get_json()

    async def mutate(cli_args, method, path, payload=None):
        if entry == "cli":
            return await _cli(*cli_args)
        return await asyncio.to_thread(web, method, path, json=payload)

    async with _live_bridge(monkeypatch), _browser_events() as events:
        operations = [
            (("bot", "create", "--name", "Researcher", "--handle", "researcher"),
             "post", "/api/bots", {"display_name": "Researcher", "handle": "researcher"}),
            (("bot", "update", "researcher", "--name", "Scholar"),
             "patch", "/api/bots/researcher", {"display_name": "Scholar"}),
            (("bot", "update", "researcher", "--emoji", "R", "--hue", "210"),
             "patch", "/api/bots/researcher", {"avatar": {"emoji": "R", "hue": 210}}),
            (("bot", "remove", "researcher"), "delete", "/api/bots/researcher", None),
            (("bot", "set", "researcher", "--name", "Scholar"),
             "post", "/api/bots/set", {"agent_name": "researcher", "display_name": "Scholar"}),
        ]
        for cli_args, method, path, payload in operations:
            await mutate(cli_args, method, path, payload)
            received = await _bot_events_through_barrier(events)
            assert len(received) == 1, received
            assert received[0]["handle"] == "researcher"
            listed = await asyncio.to_thread(client.get, "/api/bots")
            bots = listed.get_json()["bots"]
            if method == "delete":
                assert [bot["handle"] for bot in bots] == ["main"]
            else:
                assert bots[-1]["display_name"] == ("Researcher" if path == "/api/bots" else "Scholar")

        # Agent edits share the same role field. Previously these also stayed stale.
        await _cli("agent", "update", "researcher", "--description", "Reviews evidence.")
        received = await _bot_events_through_barrier(events)
        assert len(received) == 1 and received[0]["handle"] == "researcher"
        listed = await asyncio.to_thread(client.get, "/api/bots")
        assert listed.get_json()["bots"][-1]["role"] == "Reviews evidence."

        if entry == "web":
            image = io.BytesIO()
            Image.new("RGB", (8, 8), "blue").save(image, format="PNG")
            uploaded = await asyncio.to_thread(web, "put", "/api/bots/researcher/avatar", content=image.getvalue())
            received = await _bot_events_through_barrier(events)
            assert len(received) == 1 and received[0]["handle"] == "researcher"
            assert uploaded["bot"]["avatar"]["kind"] == "image"

        # A failed transaction, an unchanged role, and a list read are not changes.
        refused = await asyncio.to_thread(
            client.patch, "/api/bots/researcher", json={"display_name": "Rolled back", "hide_details": "maybe"},
            headers=csrf_headers(client),
        )
        assert refused.status_code == 400
        await _cli("agent", "update", "researcher", "--description", "Reviews evidence.")
        assert await _bot_events_through_barrier(events) == []


@pytest.mark.asyncio
async def test_controller_bot_mutation_publishes_after_commit_without_ipc(home, monkeypatch):
    """Controller writers must not synchronously call their own socket or publish twice."""
    monkeypatch.setattr(inbox_events, "_CONTROLLER_PROCESS", True)

    def no_socket(*args, **kwargs):
        pytest.fail("controller attempted an IPC round trip to itself")

    monkeypatch.setattr(internal_client, "publish_event_sync", no_socket)
    snapshots = []

    def observe_commit(event_type, payload):
        if event_type == "bots.changed":
            with BotService() as reader:
                snapshots.append(reader.get_bot(payload["handle"]).display_name)

    callback_id = inbox_events.bus.subscribe_callback(observe_commit)
    sub_id, queue = inbox_events.bus.subscribe()
    try:
        with BotService() as service:
            await asyncio.to_thread(service.create_bot, display_name="Researcher")
        event_type, payload = await asyncio.wait_for(queue.get(), timeout=1)
        assert (event_type, payload) == ("bots.changed", {"handle": "researcher"})
        assert snapshots == ["Researcher"]
        assert queue.empty()
    finally:
        inbox_events.bus.unsubscribe(sub_id)
        inbox_events.bus.unsubscribe(callback_id)


@pytest.mark.asyncio
async def test_web_bot_update_still_reaches_browser_without_controller(home):
    """The shared publisher preserves Web-local delivery when IPC is unavailable."""
    client = ui_server.app.test_client()
    async with _browser_events() as events:
        response = await asyncio.to_thread(
            client.post, "/api/bots", json={"display_name": "Researcher"}, headers=csrf_headers(client),
        )
        assert response.status_code == 201
        frame = await asyncio.wait_for(anext(events), timeout=1)
        assert "event: bots.changed\n" in frame
        assert '"handle":"researcher"' in frame
