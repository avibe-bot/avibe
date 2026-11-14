"""Isolated real controller idle producer; no mocked idle value or clock.

Constructs the actual Controller and its real session/backend ownership objects,
starts ONLY its actual UpdateActivity lifecycle (not the standalone dependency/
package-check loop), and serves actual control-IPC + UI HTTP idle endpoints.
No Controller.run(), agent turn, IM, tunnel, live service, or installed Runtime
is started. Claude/Vibey owner objects remain registered by the real Controller.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import socket
import sys
import time

from common import hosted, save


def deny_external(root: Path) -> None:
    def audit(event, args):
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec"}:
            raise RuntimeError("Idle fixture cannot launch backend/tool processes")
        if event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex"}:
            if args[0] not in (None, "127.0.0.1", "::1", "localhost"):
                raise RuntimeError("Idle fixture denied external DNS resolution")
        if event == "socket.connect":
            address = args[1]
            if isinstance(address, tuple):
                if address[0] not in ("127.0.0.1", "::1"):
                    raise RuntimeError("Idle fixture denied non-loopback connection")
            elif isinstance(address, (str, bytes)):
                if not Path(os.fsdecode(address)).resolve().is_relative_to(root):
                    raise RuntimeError("Idle fixture denied external Unix socket")
            else:
                raise RuntimeError("Unknown socket address")
    sys.addaudithook(audit)


def append(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"monotonic": time.monotonic(), **event}) + "\n")
        stream.flush()


def create_controller(root: Path):
    """Match main.py: construct the real Controller before entering its loop."""
    from config import paths
    from config.v2_config import V2Config, ModelHubConfig
    from config.v2_compat import to_app_config
    from storage.importer import ensure_sqlite_state
    from core.controller import Controller

    assert paths.get_config_path().resolve().is_relative_to(root)
    assert paths.get_sqlite_state_path().resolve().is_relative_to(root)
    if paths.get_config_path().exists() or paths.get_sqlite_state_path().exists():
        raise RuntimeError("Controller must start from fresh fixture-owned state")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    config = V2Config.default()
    config.setup_completed = True
    config.ui.setup_host, config.ui.setup_port = "127.0.0.1", port
    config.ui.open_browser = False
    config.runtime.default_cwd = str(root)
    config.platforms.enabled = []
    config.platforms.primary = config.platform = "avibe"
    config.model_hub = ModelHubConfig.from_payload({"enabled": False})
    for name in ("claude", "codex", "opencode"):
        getattr(config.agents, name).enabled = False
    config.update.auto_update = False
    config.update.notify_admins = False
    config.save()
    ensure_sqlite_state()
    controller = Controller(to_app_config(config))
    return controller, listener


async def serve(root: Path, controller, listener: socket.socket) -> None:
    from core import internal_server
    from core.inbox_events import publish_runtime_activity
    from vibe import ui_server, internal_client
    import uvicorn

    port = listener.getsockname()[1]
    # Real owner lifecycle on the same asyncio loop as control IPC. The fresh
    # database has no durable work to restore; no backend starts in this fixture.
    controller._loop = asyncio.get_running_loop()
    controller._delivery_recovery_complete.set()
    # Start the actual controller-owned idle producer, not UpdateChecker.start:
    # that method also starts dependency reconciliation before its package-check
    # configuration guard. Native idle IPC only requires this checker/producer.
    # The real Claude/Vibey ownership objects remain registered and are probed;
    # disabling optional native backends never substitutes fake empty owners.
    controller.update_checker._desktop_activity.start()
    internal = internal_server.start(controller)

    async def observed_app(scope, receive, send):
        idle = scope.get("path") == "/api/desktop/update/idle"
        status, body = None, bytearray()

        async def observe(message):
            nonlocal status
            if idle and message["type"] == "http.response.start":
                status = message["status"]
            if idle and message["type"] == "http.response.body":
                body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    append(root / "idle.jsonl", {"status": status, "body": json.loads(body)})
            await send(message)

        await ui_server.app(scope, receive, observe)

    server = internal_server._create_controller_loop_server(uvicorn.Config(
        observed_app, lifespan="off", log_level="warning", access_log=False,
    ))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        deadline = time.monotonic() + 30
        while True:
            if internal.done():
                await internal
                raise RuntimeError("Control IPC stopped during preflight")
            try:
                observation = await internal_client.desktop_update_idle()
                if server.started:
                    break
            except internal_client.InternalServerUnavailable:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError("Actual controller idle producer unavailable")
            await asyncio.sleep(0.1)
        # Starts cold; refusing an already-aged value guards accidental reuse.
        assert observation["idle_seconds"] < 60
        save(root / "runtime-ready.json", {"local": f"http://127.0.0.1:{port}", "pid": os.getpid(),
                                           "initial_idle": observation, "started_monotonic": time.monotonic()})
        seen = 0
        while True:
            if internal.done() or serving.done():
                raise RuntimeError("Actual controller/UI server stopped")
            path = root / "runtime-commands.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            for line in lines[seen:]:
                command = json.loads(line)
                if command["action"] == "activity":
                    # Exercise the same synchronous bus callback that short
                    # turns/user activity use; never set last_activity or clock.
                    publish_runtime_activity()
                    fresh = await internal_client.desktop_update_idle()
                    assert fresh["idle_seconds"] < 5
                    append(root / "runtime-events.jsonl", {"event": "activity", "id": command["id"], "idle": fresh})
                elif command["action"] == "stop":
                    return
                else:
                    raise RuntimeError("Unsupported controller fixture command")
            seen = len(lines)
            await asyncio.sleep(0.1)
    finally:
        server.should_exit = True
        controller.update_checker._desktop_activity.stop()
        internal.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await internal
        await asyncio.wait_for(serving, timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = hosted(args.root)
    os.environ["VIBE_INTERNAL_DISPATCH_SOCKET"] = str(root / "i.sock")
    deny_external(root)
    controller, listener = create_controller(root)
    try:
        asyncio.run(serve(root, controller, listener))
    finally:
        listener.close()


if __name__ == "__main__":
    main()
