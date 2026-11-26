"""Hermetic tests for the controller-independent stdio ACP client and update parser."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import psutil
import pytest

from modules.agents.acp.client import ACP_AUTH_REQUIRED, JSONRPC_METHOD_NOT_FOUND, AcpClient, AcpRpcError, AcpTransportError
from modules.agents.acp.events import AcpConfigOptionUpdate, AcpDiffBlock, AcpMessageChunkUpdate, AcpSessionInfoUpdate, AcpThoughtChunkUpdate, AcpToolCallUpdate, AcpUnknownUpdate, AcpUsageUpdate, parse_diff_blocks, parse_session_update
from tests.helpers.fake_acp_server import fake_acp_server_command

_CAP_ATTRS = (
    "supports_resume",
    "supports_list",
    "supports_fork",
    "supports_close",
    "supports_logout",
    "supports_images",
    "supports_additional_directories",
)


@pytest.mark.parametrize(
    ("config", "expected_cap", "expected_auth"),
    [
        (None, True, ["token"]),
        ({"agentCapabilities": {}, "authMethods": []}, False, []),
    ],
)
async def test_initialize_capabilities_full_and_minimal(
    tmp_path: Path, config: dict[str, Any] | None, expected_cap: bool, expected_auth: list[str]
) -> None:
    """Empty-object ACP capability markers evaluate True; omitted ones evaluate False."""
    async with AcpClient(fake_acp_server_command(config=config), cwd=tmp_path) as client:
        assert client.is_alive and isinstance(client._process.pid, int) and client._process.pid > 0 and client._process.returncode is None
        resp = await client.initialize(client_info={"name": "avibe-test", "version": "0.1.0"})
        assert resp["protocolVersion"] == 1
        assert client.agent_info["name"] == "fake-acp-server"
        assert [m["id"] for m in client.auth_methods] == expected_auth
        assert all(getattr(client, attr) is expected_cap for attr in _CAP_ATTRS)
    assert client.is_alive is False and client._process.returncode == 0


async def test_auth_required_authenticate_and_logout(tmp_path: Path) -> None:
    """Unauthenticated sessions fail with -32000; authenticate enables and logout revokes."""
    async with AcpClient(fake_acp_server_command(config={"requireAuth": True}), cwd=tmp_path) as client:
        await client.initialize()
        with pytest.raises(AcpRpcError) as exc_info:
            await client.session_list()
        assert exc_info.value.code == ACP_AUTH_REQUIRED
        assert [m["id"] for m in exc_info.value.data["authMethods"]] == ["token"]

        with pytest.raises(AcpRpcError) as bad_auth:
            await client.authenticate("nonexistent")
        assert bad_auth.value.code == -32602

        await client.authenticate("token")
        assert (await client.session_list())["sessions"] == []

        await client.logout()
        with pytest.raises(AcpRpcError) as after_logout:
            await client.session_new(str(tmp_path))
        assert after_logout.value.code == ACP_AUTH_REQUIRED


async def test_session_new_and_streaming_session_updates(tmp_path: Path) -> None:
    """All standard session/update kinds stream in order and complete before prompt returns."""
    received: list[tuple[str, dict[str, Any]]] = []

    async def on_notification(method: str, params: dict[str, Any]) -> None:
        received.append((method, params))

    async with AcpClient(fake_acp_server_command(), cwd=tmp_path) as client:
        client.on_notification(on_notification)
        await client.initialize()
        sid = (await client.session_new(str(tmp_path)))["sessionId"]

        def reject_before_write() -> None:
            assert client._write_lock.locked()
            raise ValueError("turn already finished")

        with pytest.raises(ValueError, match="turn already finished"):
            await client.session_prompt(sid, [{"type": "text", "text": "must-not-send"}], before_write=reject_before_write)
        assert client._pending == {} and received == []
        response = await client.session_prompt(sid, [{"type": "text", "text": "recall"}])
        assert response["stopReason"] == "end_turn"
        assert "must-not-send" not in json.dumps(received)
        received.clear()
        assert (await client.session_prompt(sid, [{"type": "text", "text": "stream_all"}]))["stopReason"] == "end_turn"

    assert all(method == "session/update" for method, _ in received)
    parsed = [parse_session_update(params) for _, params in received]

    assert isinstance(parsed[0], AcpThoughtChunkUpdate) and parsed[0].text == "planning edit"
    assert isinstance(parsed[1], AcpToolCallUpdate)
    assert (parsed[1].kind, parsed[1].tool_call_id, parsed[1].title, parsed[1].tool_kind, parsed[1].status) == (
        "tool_call", "tc-1", "Update greeting", "edit", "in_progress"
    )
    assert parsed[1].raw_input == {"path": "hello.py"} and parsed[1].locations == ({"path": "hello.py", "line": 1},)

    assert isinstance(parsed[2], AcpToolCallUpdate)
    assert (parsed[2].kind, parsed[2].status, parsed[2].raw_output) == ("tool_call_update", "completed", {"ok": True})
    assert len(parsed[2].diffs) == 1
    assert (parsed[2].diffs[0].path, parsed[2].diffs[0].old_text, parsed[2].diffs[0].new_text) == (
        "hello.py", "print('old')\n", "print('new')\n"
    )

    assert isinstance(parsed[3], AcpMessageChunkUpdate) and isinstance(parsed[4], AcpMessageChunkUpdate)
    assert (parsed[3].text or "") + (parsed[4].text or "") == "Updated hello.py."
    assert isinstance(parsed[5], AcpUsageUpdate) and (parsed[5].used, parsed[5].size) == (1200, 128000)
    assert parsed[5].cost == {"amount": 0.01, "currency": "USD"}
    assert isinstance(parsed[6], AcpUnknownUpdate) and parsed[6].update_type == "available_commands_update"
    assert isinstance(parsed[7], AcpSessionInfoUpdate)
    assert (parsed[7].title, parsed[7].updated_at) == ("Update greeting", "2026-11-24T12:00:00Z")
    assert isinstance(parsed[8], AcpUnknownUpdate) and parsed[8].update_type == "plan"


async def test_server_requests_permission_and_unknown_method(tmp_path: Path) -> None:
    """Server requests run concurrently during prompt; unknown methods return JSON-RPC -32601."""
    seen: list[tuple[str, dict[str, Any]]] = []
    chunks: list[str] = []

    async def on_notification(method: str, params: dict[str, Any]) -> None:
        if method == "session/update":
            upd = parse_session_update(params)
            if isinstance(upd, AcpMessageChunkUpdate) and upd.text:
                chunks.append(upd.text)

    async def on_server_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        seen.append((method, params))
        if method == "session/request_permission":
            return {"outcome": {"outcome": "selected", "optionId": "allow_once"}}
        raise AcpRpcError(JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")

    async with AcpClient(fake_acp_server_command(), cwd=tmp_path) as client:
        client.on_notification(on_notification)
        await client.initialize()
        sid = (await client.session_new(str(tmp_path)))["sessionId"]

        # Without a server-request handler, unknown requests automatically return -32601.
        res_no_handler = await client.session_prompt(sid, [{"type": "text", "text": "request_unknown"}])
        assert res_no_handler["clientResponse"]["error"]["code"] == JSONRPC_METHOD_NOT_FOUND

        client.on_server_request(on_server_request)
        res_perm = await client.session_prompt(sid, [{"type": "text", "text": "request_permission"}])
        assert res_perm["stopReason"] == "end_turn"

        res_with_handler = await client.session_prompt(sid, [{"type": "text", "text": "request_unknown"}])
        assert res_with_handler["clientResponse"]["error"]["code"] == JSONRPC_METHOD_NOT_FOUND

    assert seen[0][0] == "session/request_permission" and seen[0][1]["toolCall"]["toolCallId"] == "tc-perm-1"
    assert "permission:allow_once" in chunks


async def test_session_cancel_during_prompt(tmp_path: Path) -> None:
    """Sending session/cancel notification aborts an in-flight session/prompt."""
    started = asyncio.Event()

    async def on_notification(method: str, params: dict[str, Any]) -> None:
        if method == "session/update":
            upd = parse_session_update(params)
            if isinstance(upd, AcpMessageChunkUpdate) and upd.text == "working":
                started.set()

    async with AcpClient(fake_acp_server_command(), cwd=tmp_path) as client:
        client.on_notification(on_notification)
        await client.initialize()
        sid = (await client.session_new(str(tmp_path)))["sessionId"]
        prompt_task = asyncio.create_task(client.session_prompt(sid, [{"type": "text", "text": "wait_cancel"}]))
        await asyncio.wait_for(started.wait(), timeout=5.0)
        await client.session_cancel(sid)
        assert (await asyncio.wait_for(prompt_task, timeout=5.0))["stopReason"] == "cancelled"


async def test_session_set_config_option_model(tmp_path: Path) -> None:
    """Config option model mutations propagate updates and surface -32602 errors."""
    config_updates: list[AcpConfigOptionUpdate] = []
    replies: list[str] = []

    async def on_notification(method: str, params: dict[str, Any]) -> None:
        if method != "session/update":
            return
        upd = parse_session_update(params)
        if isinstance(upd, AcpConfigOptionUpdate):
            config_updates.append(upd)
        elif isinstance(upd, AcpMessageChunkUpdate) and upd.text:
            replies.append(upd.text)

    async with AcpClient(fake_acp_server_command(), cwd=tmp_path) as client:
        client.on_notification(on_notification)
        await client.initialize()
        sid = (await client.session_new(str(tmp_path)))["sessionId"]

        updated = await client.session_set_config_option(sid, "model", "model-beta")
        assert next(o for o in updated["configOptions"] if o["id"] == "model")["currentValue"] == "model-beta"
        assert len(config_updates) == 1
        assert any(o["id"] == "model" and o["currentValue"] == "model-beta" for o in config_updates[0].config_options)

        with pytest.raises(AcpRpcError) as bad_val:
            await client.session_set_config_option(sid, "model", "invalid-model")
        assert bad_val.value.code == -32602

        await client.session_set_config_option(sid, "model", "model-alpha")
        await client.session_prompt(sid, [{"type": "text", "text": "check"}])

    assert replies == ["echo:check|model=model-alpha|mode=code"]


async def test_cross_process_session_resume_list_and_close(tmp_path: Path) -> None:
    """Sessions survive across fresh subprocesses via resume without replaying live updates."""
    extra_dir = str(tmp_path / "extra-rules")
    cmd = fake_acp_server_command(state_file=tmp_path / "acp_state.json", config={"listPageSize": 1})

    async with AcpClient(cmd, cwd=tmp_path) as first:
        await first.initialize()
        s1 = (await first.session_new(str(tmp_path), additional_directories=[extra_dir]))["sessionId"]
        s2 = (await first.session_new(str(tmp_path)))["sessionId"]
        await first.session_prompt(s1, [{"type": "text", "text": "remember-marker"}])

    updates: list[Any] = []

    async def on_notification(method: str, params: dict[str, Any]) -> None:
        if method == "session/update":
            updates.append(parse_session_update(params))

    async with AcpClient(cmd, cwd=tmp_path) as second:
        second.on_notification(on_notification)
        await second.initialize()
        page1 = await second.session_list(cwd=str(tmp_path))
        page2 = await second.session_list(cwd=str(tmp_path), cursor=page1["nextCursor"])
        assert [s["sessionId"] for s in page1["sessions"] + page2["sessions"]] == [s1, s2]

        resumed = await second.session_resume(s1, str(tmp_path), additional_directories=[extra_dir])
        assert resumed["sessionId"] == s1 and updates == []

        await second.session_prompt(s1, [{"type": "text", "text": "recall"}])
        assert len(updates) == 1 and isinstance(updates[0], AcpMessageChunkUpdate)
        assert "remember-marker" in (updates[0].text or "") and extra_dir in (updates[0].text or "")

        with pytest.raises(AcpRpcError) as missing:
            await second.session_resume("sess-does-not-exist", str(tmp_path))
        assert missing.value.code == -32002

        await second.session_close(s1)
        await second.session_close(s2)
        assert (await second.session_list(cwd=str(tmp_path)))["sessions"] == []


async def test_subprocess_exit_mid_prompt_and_lifecycle_guards(tmp_path: Path) -> None:
    """Covers mid-prompt crash stderr capture, idempotent close, unstarted guard, missing binary, and forced kill."""
    client = AcpClient(fake_acp_server_command(), cwd=tmp_path)
    await client.start()
    try:
        await client.initialize()
        sid = (await client.session_new(str(tmp_path)))["sessionId"]
        with pytest.raises(AcpTransportError) as exc_info:
            await client.session_prompt(sid, [{"type": "text", "text": "crash_mid_prompt"}])
        assert "exited with code 2" in str(exc_info.value) and "fatal: simulated peer crash" in str(exc_info.value)
        assert client.is_alive is False and client._process.returncode == 2 and "fatal: simulated peer crash" in client._stderr_tail
    finally:
        await client.close()
        await client.close()

    unstarted = AcpClient(fake_acp_server_command(), cwd=tmp_path)
    assert unstarted.is_alive is False and unstarted._process is None
    with pytest.raises(AcpTransportError):
        await unstarted.initialize()

    with pytest.raises(AcpTransportError, match="Failed to launch ACP server"):
        await AcpClient(["/nonexistent/acp-binary-that-does-not-exist"], cwd=tmp_path).start()

    sleeper = AcpClient([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path)
    await sleeper.start()
    assert sleeper.is_alive is True
    await sleeper.close(graceful_timeout=0.05, terminate_timeout=2.0)
    assert sleeper.is_alive is False and sleeper._process.returncode not in (None, 0)


def _pid_running(pid: int) -> bool:
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.mark.parametrize("leader_mode", ["eof_exit_zero", "crash_before_close"])
async def test_close_reaps_surviving_tool_children_after_leader_exits(tmp_path: Path, leader_mode: str) -> None:
    """close() reaps surviving stdio-redirected tool children even after the leader exits cleanly or crashes."""
    pids_file = tmp_path / f"child-pids-{leader_mode}.json"
    tail = "sys.stdin.read()\nsys.exit(0)\n" if leader_mode == "eof_exit_zero" else "sys.exit(2)\n"
    leader_script = (
        "import json, os, subprocess, sys\n"
        "p = sys.argv[1]; c = 'import time; time.sleep(60)'\n"
        "kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "g = subprocess.Popen([sys.executable, '-c', c, p], **kw)\n"
        "d = subprocess.Popen([sys.executable, '-c', c, p], start_new_session=(os.name != 'nt'), **kw)\n"
        "open(p + '.tmp', 'w').write(json.dumps([os.getpid(), g.pid, d.pid])); os.replace(p + '.tmp', p)\n"
        + tail
    )

    child_pids: list[int] = []
    client = AcpClient([sys.executable, "-c", leader_script, str(pids_file)], cwd=tmp_path)
    try:
        await client.start()
        deadline = time.monotonic() + 10.0
        while not pids_file.exists():
            assert time.monotonic() < deadline, "leader did not spawn tool children"
            await asyncio.sleep(0.05)

        leader_pid, in_group_pid, detached_pid = json.loads(pids_file.read_text(encoding="utf-8"))
        child_pids = [in_group_pid, detached_pid]
        assert _pid_running(in_group_pid) and _pid_running(detached_pid)

        if leader_mode == "crash_before_close":
            crash_deadline = time.monotonic() + 5.0
            while client._process.returncode is None and time.monotonic() < crash_deadline:
                await asyncio.sleep(0.05)
            assert client._process.returncode == 2 and _pid_running(in_group_pid) and _pid_running(detached_pid)

        await client.close(graceful_timeout=5.0, terminate_timeout=2.0)
        assert client._process.returncode == (0 if leader_mode == "eof_exit_zero" else 2)
        assert not any(_pid_running(pid) for pid in (leader_pid, in_group_pid, detached_pid))
    finally:
        await client.close(graceful_timeout=1.0, terminate_timeout=1.0)
        for pid in child_pids:
            if _pid_running(pid):
                try:
                    os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                except OSError:
                    pass


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"update": {"sessionUpdate": "agent_message_chunk"}}, "sessionId"),
        ({"sessionId": "s1"}, "update"),
        ({"sessionId": "s1", "update": {}}, "sessionUpdate"),
        ({"sessionId": "s1", "update": {"sessionUpdate": "tool_call"}}, "toolCallId"),
    ],
)
def test_parse_session_update_rejects_malformed_envelopes(payload: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        parse_session_update(payload)


def test_parse_diff_blocks_extracts_valid_diffs() -> None:
    diffs = parse_diff_blocks(
        [
            {"type": "content", "content": {"type": "text", "text": "ignored"}},
            {"type": "diff", "path": "a.py", "newText": "new\n"},
            {"type": "diff", "path": "incomplete.py"},
            {"type": "diff", "path": "b.py", "oldText": "1", "newText": "2"},
        ]
    )
    assert diffs == (
        AcpDiffBlock(path="a.py", old_text=None, new_text="new\n", raw={"type": "diff", "path": "a.py", "newText": "new\n"}),
        AcpDiffBlock(path="b.py", old_text="1", new_text="2", raw={"type": "diff", "path": "b.py", "oldText": "1", "newText": "2"}),
    )


@pytest.mark.asyncio
async def test_acp_client_session_fork_applies_request_timeout() -> None:
    from modules.agents.acp.client import AcpClient

    client = AcpClient(["agy", "acp"], request_timeout=17.5)
    recorded: list[tuple[str, dict[str, Any], float | None]] = []

    async def _fake_send_request(method: str, params: Any = None, *, timeout: float | None = None) -> dict[str, Any]:
        recorded.append((method, dict(params or {}), timeout))
        return {"sessionId": "forked-1"}

    client.send_request = _fake_send_request  # type: ignore[method-assign]
    resp = await client.session_fork("src-1", "/tmp/work", additional_directories=["/tmp/rules"])
    assert resp == {"sessionId": "forked-1"}
    assert recorded == [
        (
            "session/fork",
            {"sessionId": "src-1", "cwd": "/tmp/work", "mcpServers": [], "additionalDirectories": ["/tmp/rules"]},
            17.5,
        )
    ]
