#!/usr/bin/env python3
"""Hosted ordinary-user driver -> daemon/proxy -> core -> SDK transport proof.

Only shell lifecycle/state and the HTTP Controller adapter are test-owned. All
native calls and errors come from the supplied source-built production driver.
No fallback upstream, monkeypatch, canned refusal or local desktop execution.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

EXPECTED_MESSAGE = (
    'This window runs as administrator, so Avibe cannot control it. '
    'Reopen the app without "Run as administrator" to continue.'
)


def check_refusal(result: dict, *, folded: bool) -> None:
    if result.get("isError") is not True:
        raise AssertionError("native elevated type must return isError=true")
    payload = result["structuredContent"]
    if payload.get("code") != "protected_window":
        raise AssertionError(f"wrong refusal code: {payload!r}")
    if payload.get("params") != {"reason": "administrator"}:
        raise AssertionError(f"wrong refusal params: {payload!r}")
    if payload.get("message") != EXPECTED_MESSAGE:
        raise AssertionError(f"wrong refusal message: {payload!r}")
    if folded and payload.get("detail") != EXPECTED_MESSAGE:
        raise AssertionError("core must preserve detail and its message alias")


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


async def stop_child(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    if process.stdin:
        process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), 10)
    except asyncio.TimeoutError:
        process.kill()  # Exact retained child, never a process-name search.
        await asyncio.wait_for(process.wait(), 10)


def remove_owned_state(work: Path) -> None:
    state = work / "state"
    if state.exists():
        shutil.rmtree(state)
    if state.exists():
        raise RuntimeError("owned Computer Use state directory survived cleanup")


async def exercise(args: argparse.Namespace, job: int) -> None:
    # No Avibe import until all state paths point into the owned fixture.
    from aiohttp import web
    from core.agent_core.tools.windows_job import active_processes
    from core.computer_server import ComputerUseServer, JsonRpcUpstream
    from core.computer_use import ComputerUseStatus, read_computer_use_state
    from core.private_fs import ensure_private_directory, make_private_file, windows_security

    fixture = json.loads(args.input.read_text(encoding="utf-8-sig"))
    work = args.work.resolve()
    if windows_security().current_user_sid != fixture["expected_sid"]:
        raise RuntimeError("worker is not the disposable Users-only identity")
    if ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("ordinary worker unexpectedly has administrator membership")
    ensure_private_directory(work / "state")
    binary = Path(fixture["binary"])
    if hashlib.sha256(binary.read_bytes()).hexdigest() != fixture["binary_sha256"]:
        raise RuntimeError("source-built binary hash changed")
    pipe = rf"\\.\pipe\avibe-refusal-{uuid.uuid4()}"
    pid_file = work / "daemon.pid"
    evidence = {"raw_type_results": [], "folded_type_results": []}
    daemon = node = None
    server = runner = None
    daemon_log = (work / "daemon.stderr").open("wb")
    daemon_out = (work / "daemon.stdout").open("wb")

    class EvidenceUpstream(JsonRpcUpstream):
        async def request(self, method, params):
            response = await super().request(method, params)
            if method == "tools/call" and params.get("name") == "type_text":
                evidence["raw_type_results"].append({
                    "pid": params["arguments"].get("pid"), "response": response,
                })
            return response

    try:
        env = dict(os.environ)
        env.update({
            "CUA_DRIVER_EMBEDDED": "1",
            "CUA_DRIVER_PARENT_LIVENESS_STDIN": "1",
            "CUA_DRIVER_PERMISSION_MODE": "standard",
            "CUA_DRIVER_RS_TELEMETRY_ENABLED": "false",
            "CUA_DRIVER_RS_UPDATE_CHECK": "false",
        })
        daemon = await asyncio.create_subprocess_exec(
            str(binary), "serve", "--socket", pipe, "--pid-file", str(pid_file),
            "--no-overlay", stdin=asyncio.subprocess.PIPE,
            stdout=daemon_out, stderr=daemon_log, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        from ctypes import wintypes
        wait_pipe = ctypes.WinDLL("kernel32", use_last_error=True).WaitNamedPipeW
        wait_pipe.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        wait_pipe.restype = wintypes.BOOL
        deadline = time.monotonic() + 30
        while not (pid_file.is_file() and wait_pipe(pipe, 50)):
            if daemon.returncode is not None or time.monotonic() >= deadline:
                raise RuntimeError("source-built ordinary daemon did not bind its private pipe")
            await asyncio.sleep(0.1)

        snapshot = args.repo / "desktop/cua-driver/tools-windows-v0.31.0.json"
        state_path = work / "state/computer-use.json"
        write_json(state_path, {
            "schema_version": 1, "enabled": True, "state": "ready", "reason": None,
            "shell_pid": os.getpid(), "instance_id": str(uuid.uuid4()), "generation": 1,
            "driver_version": "0.31.0", "socket_path": pipe,
            "proxy_executable": str(binary), "host_bundle_id": "bot.avibe.desktop.refusal-test",
            "tool_snapshot": {"path": str(snapshot), "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest()},
        })
        make_private_file(state_path)
        state = read_computer_use_state(state_path)
        # This harness owns the daemon, not a desktop shell. Do not claim shell
        # lock/readiness acceptance. The actual upstream_factory starts the real
        # embedded MCP proxy, which must reach this real daemon and its gate.
        def status():
            return ComputerUseStatus(
                "ready" if daemon.returncode is None else "unavailable", None, state,
            )
        server = ComputerUseServer(
            state_path=state_path, status_reader=status, upstream_factory=EvidenceUpstream.start,
        )
        connection = str(uuid.uuid4())
        instance = str(uuid.uuid4())
        headers = {"x-refusal-instance": instance}

        async def request_handler(request):
            body = await request.json()
            if body.get("connection_token") != connection:
                raise web.HTTPForbidden()
            arguments = dict(body["arguments"])
            arguments["session"] = "windows-native-refusal-transport"
            result = await server.call_tool(
                body["tool"], arguments, handle_descriptor=body.get("descriptor"),
            )
            if body["tool"] == "type_text":
                evidence["folded_type_results"].append({"pid": arguments.get("pid"), "result": result})
            return web.json_response({"result": result}, headers=headers)

        async def close_handler(request):
            body = await request.json()
            if body.get("connection_token") != connection:
                raise web.HTTPForbidden()
            return web.json_response({"ok": True}, headers=headers)

        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_post("/internal/computer-script/request", request_handler)
        app.router.add_post("/internal/computer-script/close", close_handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        config_path = work / "sdk-config.json"
        sdk_result_path = work / "sdk-result.json"
        write_json(config_path, {
            **fixture, "repo": str(args.repo), "expected_message": EXPECTED_MESSAGE,
            "sdk_result": str(sdk_result_path),
            "runtime": {
                "endpoint": {"transport": "tcp", "host": "127.0.0.1", "port": port,
                             "headers": {}, "instance_id": instance, "instance_header": "x-refusal-instance"},
                "connection_token": connection, "artifact_directory": str(work),
                "runtime_state_path": str(work / "sdk-runtime.json"),
            },
        })
        with (work / "sdk.stdout").open("wb") as stdout, (work / "sdk.stderr").open("wb") as stderr:
            node = await asyncio.create_subprocess_exec(
                fixture["node"], str(Path(__file__).with_name("assert-sdk.mjs")), str(config_path),
                stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            code = await asyncio.wait_for(node.wait(), 180)
        if code != 0:
            raise RuntimeError(f"real SDK assertions failed with exit {code}; see sdk.stderr")
        for label, key in (("raw_type_results", "response"), ("folded_type_results", "result")):
            records = evidence[label]
            if [row["pid"] for row in records] != [fixture["elevated"]["pid"], fixture["control"]["pid"]]:
                raise AssertionError(f"expected exactly one elevated refusal and one positive input: {label}")
            elevated = records[0][key]
            control = records[1][key]
            if key == "response":
                elevated, control = elevated["result"], control["result"]
            check_refusal(elevated, folded=(key == "result"))
            if control.get("isError") is True:
                raise AssertionError("ordinary positive control returned an error")
        evidence["sdk"] = json.loads(sdk_result_path.read_text(encoding="utf-8"))
        evidence["transport_assertions_passed"] = True
        evidence["native_readback_required_from_parent"] = True
        evidence["locked_desktop"] = "not exercised; hosted cannot prove it"
        evidence["shell_readiness"] = "test-owned state; not desktop shell acceptance"
        evidence["binary_sha256"] = fixture["binary_sha256"]
    finally:
        # Always keep raw/folded diagnostics, including pre-fix generic errors.
        write_json(work / "transport.json", evidence)
        try:
            await stop_child(node)
        finally:
            try:
                if runner is not None:
                    await runner.cleanup()
                if server is not None:
                    await server.close()
            finally:
                try:
                    await stop_child(daemon)
                finally:
                    daemon_log.close()
                    daemon_out.close()
        deadline = time.monotonic() + 10
        while active_processes(job) != 1:
            if time.monotonic() >= deadline:
                raise RuntimeError("owned worker job still contains child processes after cleanup")
            await asyncio.sleep(0.1)
        # The state directory has the ordinary worker's private ACL. Remove it
        # while that identity still owns the process; the elevated parent cannot
        # delete it merely because it created the surrounding fixture root.
        remove_owned_state(work)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt" or any(os.environ.get(key) != value for key, value in {
        "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_OS": "Windows",
    }.items()):
        raise RuntimeError("native refusal transport requires disposable hosted Windows")
    args.repo = args.repo.resolve()
    args.work.mkdir(parents=True, exist_ok=True)
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "AVIBE_HOME", "XDG_CONFIG_HOME",
                "XDG_STATE_HOME", "XDG_CACHE_HOME", "CUA_DRIVER_RS_HOME", "CUA_DRIVER_TELEMETRY_HOME"):
        os.environ[key] = str(args.work / "home")
    Path(os.environ["HOME"]).mkdir(exist_ok=True)
    sys.path.insert(0, str(args.repo))
    from core.agent_core.tools.windows_job import create_kill_on_close_job

    # Before any child starts, join a non-breakaway job. Its non-inherited
    # handle is deliberately retained until OS process teardown: even a hard
    # worker timeout/crash kills every driver/proxy/Node descendant. Closing
    # it earlier would kill this worker too and erase its normal exit code.
    job = create_kill_on_close_job()
    asyncio.run(exercise(args, job))


if __name__ == "__main__":
    main()
