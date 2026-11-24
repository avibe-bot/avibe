"""Hermetic tests for the controller-independent stdio ACP client and update parser."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from modules.agents.acp import (
    ACP_AUTH_REQUIRED,
    JSONRPC_METHOD_NOT_FOUND,
    AcpAvailableCommandsUpdate,
    AcpClient,
    AcpConfigOptionUpdate,
    AcpDiffBlock,
    AcpMessageChunkUpdate,
    AcpRpcError,
    AcpSessionInfoUpdate,
    AcpThoughtChunkUpdate,
    AcpToolCallUpdate,
    AcpTransportError,
    AcpUnknownUpdate,
    AcpUsageUpdate,
    parse_diff_blocks,
    parse_session_update,
)
from tests.helpers.fake_acp_server import fake_acp_server_command


async def test_initialize_capabilities_full_and_minimal(tmp_path: Path) -> None:
    """Empty-object ACP capability markers evaluate True; omitted ones evaluate False."""
    async with AcpClient(fake_acp_server_command(), cwd=tmp_path) as client:
        assert client.is_alive
        assert isinstance(client.pid, int) and client.pid > 0
        assert client.exit_code is None

        resp = await client.initialize(client_info={"name": "avibe-test", "version": "0.1.0"})
        assert resp["protocolVersion"] == 1
        assert client.protocol_version == 1
        assert client.agent_info["name"] == "fake-acp-server"
        assert client.agent_capabilities["loadSession"] is True
        assert [m["id"] for m in client.auth_methods] == ["token"]

        assert client.supports_resume is True
        assert client.supports_load is True
        assert client.supports_list is True
        assert client.supports_fork is True
        assert client.supports_close is True
        assert client.supports_logout is True
        assert client.supports_images is True
        assert client.supports_embedded_context is True
        assert client.supports_additional_directories is True

    assert client.is_alive is False
    assert client.pid is None
    assert client.exit_code == 0

    minimal_cmd = fake_acp_server_command(
        config={
            "agentCapabilities": {},
            "authMethods": [],
        }
    )
    async with AcpClient(minimal_cmd, cwd=tmp_path) as minimal_client:
        await minimal_client.initialize()
        assert minimal_client.auth_methods == ()
        assert minimal_client.supports_resume is False
        assert minimal_client.supports_load is False
        assert minimal_client.supports_list is False
        assert minimal_client.supports_fork is False
        assert minimal_client.supports_close is False
        assert minimal_client.supports_logout is False
        assert minimal_client.supports_images is False
        assert minimal_client.supports_embedded_context is False
        assert minimal_client.supports_additional_directories is False


async def test_auth_required_authenticate_and_logout(tmp_path: Path) -> None:
    """Unauthenticated sessions fail with -32000; authenticate enables and logout revokes."""
    cmd = fake_acp_server_command(config={"requireAuth": True})
    async with AcpClient(cmd, cwd=tmp_path) as client:
        await client.initialize()

        with pytest.raises(AcpRpcError) as exc_info:
            await client.session_list()
        assert exc_info.value.code == ACP_AUTH_REQUIRED
        assert exc_info.value.data == {
            "authMethods": [
                {
                    "id": "token",
                    "name": "Token Auth",
                    "description": "Authenticate with a test token",
                }
            ]
        }

        with pytest.raises(AcpRpcError) as bad_auth:
            await client.authenticate("nonexistent")
        assert bad_auth.value.code == -32602

        await client.authenticate("token")
        listed = await client.session_list()
        assert listed["sessions"] == []

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

        created = await client.session_new(str(tmp_path))
        session_id = created["sessionId"]

        result = await client.session_prompt(
            session_id,
            [{"type": "text", "text": "stream_all"}],
        )
        assert result["stopReason"] == "end_turn"

    assert all(method == "session/update" for method, _ in received)
    parsed = [parse_session_update(params) for _, params in received]

    assert isinstance(parsed[0], AcpThoughtChunkUpdate)
    assert parsed[0].text == "planning edit"
    assert parsed[0].raw["sessionUpdate"] == "agent_thought_chunk"

    assert isinstance(parsed[1], AcpToolCallUpdate)
    assert parsed[1].kind == "tool_call"
    assert parsed[1].tool_call_id == "tc-1"
    assert parsed[1].title == "Update greeting"
    assert parsed[1].tool_kind == "edit"
    assert parsed[1].status == "in_progress"
    assert parsed[1].raw_input == {"path": "hello.py"}
    assert parsed[1].locations == ({"path": "hello.py", "line": 1},)

    assert isinstance(parsed[2], AcpToolCallUpdate)
    assert parsed[2].kind == "tool_call_update"
    assert parsed[2].tool_call_id == "tc-1"
    assert parsed[2].status == "completed"
    assert parsed[2].raw_output == {"ok": True}
    assert len(parsed[2].diffs) == 1
    assert parsed[2].diffs[0].path == "hello.py"
    assert parsed[2].diffs[0].old_text == "print('old')\n"
    assert parsed[2].diffs[0].new_text == "print('new')\n"

    assert isinstance(parsed[3], AcpMessageChunkUpdate)
    assert isinstance(parsed[4], AcpMessageChunkUpdate)
    assert (parsed[3].text or "") + (parsed[4].text or "") == "Updated hello.py."

    assert isinstance(parsed[5], AcpUsageUpdate)
    assert parsed[5].used == 1200
    assert parsed[5].size == 128000
    assert parsed[5].cost == {"amount": 0.01, "currency": "USD"}

    assert isinstance(parsed[6], AcpAvailableCommandsUpdate)
    assert len(parsed[6].commands) == 1
    assert parsed[6].commands[0].name == "review"
    assert parsed[6].commands[0].description == "Review workspace changes"
    assert parsed[6].commands[0].input_hint == "optional path"

    assert isinstance(parsed[7], AcpSessionInfoUpdate)
    assert parsed[7].title == "Update greeting"
    assert parsed[7].updated_at == "2026-11-24T12:00:00Z"

    assert isinstance(parsed[8], AcpUnknownUpdate)
    assert parsed[8].update_type == "plan"
    assert parsed[8].raw["entries"] == [{"content": "edit file", "status": "completed"}]


async def test_server_request_permission_during_pending_prompt(tmp_path: Path) -> None:
    """Server-initiated requests arriving while session/prompt is pending are handled concurrently."""
    seen_requests: list[tuple[str, dict[str, Any]]] = []
    chunks: list[str] = []

    async def on_notification(method: str, params: dict[str, Any]) -> None:
        if method == "session/update":
            update = parse_session_update(params)
            if isinstance(update, AcpMessageChunkUpdate) and update.text:
                chunks.append(update.text)

    async def on_server_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        seen_requests.append((method, params))
        if method == "session/request_permission":
            return {"outcome": {"outcome": "selected", "optionId": "allow_once"}}
        raise AcpRpcError(JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")

    async with AcpClient(fake_acp_server_command(), cwd=tmp_path) as client:
        client.on_notification(on_notification)
        client.on_server_request(on_server_request)
        await client.initialize()
        session_id = (await client.session_new(str(tmp_path)))["sessionId"]

        result = await client.session_prompt(
            session_id,
            [{"type": "text", "text": "request_permission"}],
        )

    assert result["stopReason"] == "end_turn"
    assert len(seen_requests) == 1
    assert seen_requests[0][0] == "session/request_permission"
    assert seen_requests[0][1]["toolCall"]["toolCallId"] == "tc-perm-1"
    assert chunks == ["permission:allow_once"]


async def test_unknown_server_request_returns_method_not_found(tmp_path: Path) -> None:
    """Unknown server-initiated requests return JSON-RPC -32601 with or without a handler."""
    async with AcpClient(fake_acp_server_command(), cwd=tmp_path) as client:
        await client.initialize()
        session_id = (await client.session_new(str(tmp_path)))["sessionId"]

        # Case 1: no server-request callback registered -> automatic -32601 rejection
        res_no_handler = await client.session_prompt(
            session_id,
            [{"type": "text", "text": "request_unknown"}],
        )
        assert res_no_handler["stopReason"] == "end_turn"
        assert res_no_handler["clientResponse"]["error"]["code"] == JSONRPC_METHOD_NOT_FOUND

        # Case 2: callback registered that explicitly rejects unknown methods
        async def strict_handler(method: str, params: dict[str, Any]) -> dict[str, Any]:
            raise AcpRpcError(JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")

        client.on_server_request(strict_handler)
        res_with_handler = await client.session_prompt(
            session_id,
            [{"type": "text", "text": "request_unknown"}],
        )
        assert res_with_handler["stopReason"] == "end_turn"
        assert res_with_handler["clientResponse"]["error"]["code"] == JSONRPC_METHOD_NOT_FOUND


async def test_session_cancel_during_prompt(tmp_path: Path) -> None:
    """Sending session/cancel notification aborts an in-flight session/prompt."""
    started = asyncio.Event()

    async def on_notification(method: str, params: dict[str, Any]) -> None:
        if method == "session/update":
            update = parse_session_update(params)
            if isinstance(update, AcpMessageChunkUpdate) and update.text == "working":
                started.set()

    async with AcpClient(fake_acp_server_command(), cwd=tmp_path) as client:
        client.on_notification(on_notification)
        await client.initialize()
        session_id = (await client.session_new(str(tmp_path)))["sessionId"]

        prompt_task = asyncio.create_task(
            client.session_prompt(session_id, [{"type": "text", "text": "wait_cancel"}])
        )
        await asyncio.wait_for(started.wait(), timeout=5.0)
        await client.session_cancel(session_id)
        result = await asyncio.wait_for(prompt_task, timeout=5.0)

    assert result["stopReason"] == "cancelled"


async def test_session_set_config_option_model_and_mode(tmp_path: Path) -> None:
    """Config option, model, and mode mutations propagate updates and surface -32602 errors."""
    config_updates: list[AcpConfigOptionUpdate] = []
    replies: list[str] = []

    async def on_notification(method: str, params: dict[str, Any]) -> None:
        if method != "session/update":
            return
        update = parse_session_update(params)
        if isinstance(update, AcpConfigOptionUpdate):
            config_updates.append(update)
        elif isinstance(update, AcpMessageChunkUpdate) and update.text:
            replies.append(update.text)

    async with AcpClient(fake_acp_server_command(), cwd=tmp_path) as client:
        client.on_notification(on_notification)
        await client.initialize()
        session_id = (await client.session_new(str(tmp_path)))["sessionId"]

        updated = await client.session_set_config_option(session_id, "model", "model-beta")
        model_opt = next(opt for opt in updated["configOptions"] if opt["id"] == "model")
        assert model_opt["currentValue"] == "model-beta"
        assert len(config_updates) == 1
        assert any(
            opt["id"] == "model" and opt["currentValue"] == "model-beta"
            for opt in config_updates[0].config_options
        )

        with pytest.raises(AcpRpcError) as bad_val:
            await client.session_set_config_option(session_id, "model", "invalid-model")
        assert bad_val.value.code == -32602

        await client.session_set_model(session_id, "model-alpha")
        await client.session_set_mode(session_id, "plan")
        await client.session_prompt(session_id, [{"type": "text", "text": "check"}])

    assert replies == ["echo:check|model=model-alpha|mode=plan"]


async def test_cross_process_session_resume_list_load_and_close(tmp_path: Path) -> None:
    """Sessions survive across fresh subprocesses via resume without replaying live updates."""
    state_file = tmp_path / "acp_state.json"
    extra_dir = str(tmp_path / "extra-rules")
    cmd = fake_acp_server_command(state_file=state_file, config={"listPageSize": 1})

    # Process 1: create two sessions and run a prompt in the first
    async with AcpClient(cmd, cwd=tmp_path) as first:
        await first.initialize()
        s1 = (
            await first.session_new(str(tmp_path), additional_directories=[extra_dir])
        )["sessionId"]
        s2 = (await first.session_new(str(tmp_path)))["sessionId"]
        await first.session_prompt(s1, [{"type": "text", "text": "remember-marker"}])

    # Process 2: fresh subprocess lists, resumes without replay, recalls state, loads, closes
    resume_updates: list[Any] = []

    async def on_notification(method: str, params: dict[str, Any]) -> None:
        if method == "session/update":
            resume_updates.append(parse_session_update(params))

    async with AcpClient(cmd, cwd=tmp_path) as second:
        second.on_notification(on_notification)
        await second.initialize()

        page1 = await second.session_list(cwd=str(tmp_path))
        assert len(page1["sessions"]) == 1
        assert "nextCursor" in page1
        page2 = await second.session_list(cwd=str(tmp_path), cursor=page1["nextCursor"])
        listed_ids = [s["sessionId"] for s in page1["sessions"] + page2["sessions"]]
        assert listed_ids == [s1, s2]

        # Resume must not replay history notifications
        resumed = await second.session_resume(s1, str(tmp_path), additional_directories=[extra_dir])
        assert resumed["sessionId"] == s1
        assert resume_updates == []

        await second.session_prompt(s1, [{"type": "text", "text": "recall"}])
        assert len(resume_updates) == 1
        assert isinstance(resume_updates[0], AcpMessageChunkUpdate)
        assert "remember-marker" in (resume_updates[0].text or "")
        assert extra_dir in (resume_updates[0].text or "")

        # session/load replays history notifications
        resume_updates.clear()
        await second.session_load(s1, str(tmp_path))
        assert any(
            isinstance(u, AcpUnknownUpdate) and u.update_type == "user_message_chunk"
            for u in resume_updates
        )
        assert any(isinstance(u, AcpMessageChunkUpdate) for u in resume_updates)

        # Missing session resume fails explicitly
        with pytest.raises(AcpRpcError) as missing:
            await second.session_resume("sess-does-not-exist", str(tmp_path))
        assert missing.value.code == -32002

        # Close removes the session
        await second.session_close(s1)
        await second.session_close(s2)
        remaining = await second.session_list(cwd=str(tmp_path))
        assert remaining["sessions"] == []


async def test_subprocess_exit_mid_prompt_surfaces_error_and_stderr_tail(tmp_path: Path) -> None:
    """Subprocess crash mid-prompt fails the pending future and captures exit code and stderr."""
    client = AcpClient(fake_acp_server_command(), cwd=tmp_path)
    await client.start()
    try:
        await client.initialize()
        session_id = (await client.session_new(str(tmp_path)))["sessionId"]

        with pytest.raises(AcpTransportError) as exc_info:
            await client.session_prompt(
                session_id,
                [{"type": "text", "text": "crash_mid_prompt"}],
            )

        assert "exited with code 2" in str(exc_info.value)
        assert "fatal: simulated peer crash" in str(exc_info.value)
        assert client.is_alive is False
        assert client.exit_code == 2
        assert "fatal: simulated peer crash" in client.stderr_tail
    finally:
        await client.close()
        # close() is idempotent
        await client.close()


async def test_lifecycle_guards(tmp_path: Path) -> None:
    """Client rejects requests before start and wraps missing binary launch failures."""
    unstarted = AcpClient(fake_acp_server_command(), cwd=tmp_path)
    assert unstarted.is_alive is False
    assert unstarted.pid is None
    assert unstarted.exit_code is None
    with pytest.raises(AcpTransportError):
        await unstarted.initialize()

    bad_binary = AcpClient(["/nonexistent/acp-binary-that-does-not-exist"], cwd=tmp_path)
    with pytest.raises(AcpTransportError, match="Failed to launch ACP server"):
        await bad_binary.start()

    # Subprocess ignoring stdin EOF is terminated cleanly via signal_process_tree
    sleeper = AcpClient([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path)
    await sleeper.start()
    assert sleeper.is_alive is True
    await sleeper.close(graceful_timeout=0.05, terminate_timeout=2.0)
    assert sleeper.is_alive is False
    assert sleeper.exit_code is not None and sleeper.exit_code != 0


def test_parse_session_update_validation_and_diff_blocks() -> None:
    """Parser rejects malformed envelopes and extracts valid diff blocks cleanly."""
    with pytest.raises(ValueError, match="sessionId"):
        parse_session_update({"update": {"sessionUpdate": "agent_message_chunk"}})

    with pytest.raises(ValueError, match="update"):
        parse_session_update({"sessionId": "s1"})

    with pytest.raises(ValueError, match="sessionUpdate"):
        parse_session_update({"sessionId": "s1", "update": {}})

    with pytest.raises(ValueError, match="toolCallId"):
        parse_session_update({"sessionId": "s1", "update": {"sessionUpdate": "tool_call"}})

    diffs = parse_diff_blocks(
        [
            {"type": "content", "content": {"type": "text", "text": "ignored"}},
            {"type": "diff", "path": "a.py", "newText": "new\n"},
            {"type": "diff", "path": "incomplete.py"},
            {"type": "diff", "path": "b.py", "oldText": "1", "newText": "2"},
        ]
    )
    assert diffs == (
        AcpDiffBlock(
            path="a.py",
            old_text=None,
            new_text="new\n",
            raw={"type": "diff", "path": "a.py", "newText": "new\n"},
        ),
        AcpDiffBlock(
            path="b.py",
            old_text="1",
            new_text="2",
            raw={"type": "diff", "path": "b.py", "oldText": "1", "newText": "2"},
        ),
    )
