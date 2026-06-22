from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from modules.agents.base import AgentRequest
from modules.agents.opencode.session import OpenCodeResumeUnavailableError, OpenCodeSessionManager
from modules.im import MessageContext
from modules.sessions_facade import SessionsFacade


def _request() -> AgentRequest:
    return AgentRequest(
        context=MessageContext(user_id="U1", channel_id="C1", platform_specific={}),
        message="hello",
        working_path="/repo",
        base_session_id="base-1",
        composite_session_id="base-1:/repo",
        session_key="slack::channel::C1",
    )


def _seed_opencode_messages(tmp_path, native_session_id: str, roles: list[str]) -> None:
    db_path = tmp_path / "opencode" / "opencode.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, data TEXT)")
        conn.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT, message_id TEXT, time_created INTEGER, data TEXT)"
        )
        for index, role in enumerate(roles, start=1):
            message_id = f"oc-msg-{index}"
            conn.execute(
                "INSERT INTO message (id, data) VALUES (?, ?)",
                (message_id, json.dumps({"role": role})),
            )
            conn.execute(
                "INSERT INTO part (id, session_id, message_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
                (f"part-{index}", native_session_id, message_id, index, json.dumps({"type": "text"})),
            )


def _seed_opencode_completed_messages(tmp_path, native_session_id: str, roles: list[str]) -> None:
    db_path = tmp_path / "opencode" / "opencode.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, data TEXT)")
        conn.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT, message_id TEXT, time_created INTEGER, data TEXT)"
        )
        for index, role in enumerate(roles, start=1):
            message_id = f"oc-msg-{index}"
            data = {"role": role}
            if role == "assistant":
                data["time"] = {"completed": index}
            conn.execute(
                "INSERT INTO message (id, data) VALUES (?, ?)",
                (message_id, json.dumps(data)),
            )
            conn.execute(
                "INSERT INTO part (id, session_id, message_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
                (f"part-{index}", native_session_id, message_id, index, json.dumps({"type": "text"})),
            )


def _seed_opencode_tool_call_messages(tmp_path, native_session_id: str) -> None:
    db_path = tmp_path / "opencode" / "opencode.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, data TEXT)")
        conn.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT, message_id TEXT, time_created INTEGER, data TEXT)"
        )
        messages = [
            {"role": "user"},
            {
                "role": "assistant",
                "time": {"completed": 2},
                "finish": "tool-calls",
            },
        ]
        for index, data in enumerate(messages, start=1):
            message_id = f"oc-msg-{index}"
            conn.execute(
                "INSERT INTO message (id, data) VALUES (?, ?)",
                (message_id, json.dumps(data)),
            )
            conn.execute(
                "INSERT INTO part (id, session_id, message_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
                (f"part-{index}", native_session_id, message_id, index, json.dumps({"type": "text"})),
            )


def test_opencode_reused_session_attaches_agent_session_id() -> None:
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value="oc-session-1"),
        ensure_agent_session_id=Mock(return_value="sesk8m4q2p7x"),
        bind_agent_session=Mock(return_value="sesk8m4q2p7x"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    server = SimpleNamespace(get_session=AsyncMock(return_value={"id": "oc-session-1"}))
    request = _request()

    session_id = asyncio.run(manager.get_or_create_session_id(request, server))

    assert session_id == "oc-session-1"
    assert request.context.platform_specific["agent_session_id"] == "sesk8m4q2p7x"
    # Anchor is the bare base now (cwd is per-request, not part of the key) —
    # one OpenCode session per (scope, anchor), reused across working dirs.
    sessions.ensure_agent_session_id.assert_called_once_with(
        "slack::channel::C1",
        "opencode",
        "base-1",
    )
    sessions.bind_agent_session.assert_called_once_with(
        "slack::channel::C1",
        "opencode",
        "base-1",
        "oc-session-1",
        workdir="/repo",
    )


def test_opencode_create_session_does_not_pass_vibe_title() -> None:
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value=None),
        ensure_agent_session_id=Mock(return_value="sesk8m4q2p7x"),
        bind_agent_session=Mock(return_value="sesk8m4q2p7x"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    server = SimpleNamespace(create_session=AsyncMock(return_value={"id": "oc-session-1"}))
    request = _request()

    session_id = asyncio.run(manager.get_or_create_session_id(request, server))

    assert session_id == "oc-session-1"
    server.create_session.assert_awaited_once_with(directory="/repo")


def test_opencode_forks_pending_native_source() -> None:
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value=None),
        ensure_agent_session_id=Mock(return_value="ses-fork"),
        bind_agent_session=Mock(return_value="ses-fork"),
        bind_agent_session_by_id=Mock(return_value="ses-fork"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    server = SimpleNamespace(
        fork_session=AsyncMock(return_value={"id": "oc-fork"}),
        create_session=AsyncMock(),
        list_messages=AsyncMock(),
    )
    request = _request()
    request.context.platform_specific = {
        "agent_session_id": "ses-fork",
        "agent_session_target": {
            "id": "ses-fork",
            "agent_backend": "opencode",
            "native_session_id": "",
            "native_session_fork": {
                "source_session_id": "ses-source",
                "source_native_session_id": "oc-source",
                "source_backend": "opencode",
            },
        },
    }

    session_id = asyncio.run(manager.get_or_create_session_id(request, server))

    assert session_id == "oc-fork"
    server.fork_session.assert_awaited_once_with("oc-source", directory="/repo", message_id=None)
    server.create_session.assert_not_awaited()
    sessions.bind_agent_session_by_id.assert_called_once_with(
        "ses-fork",
        "oc-fork",
        workdir="/repo",
        vibe_agent_id=None,
        vibe_agent_name=None,
        vibe_agent_backend=None,
    )


def test_opencode_idle_fork_ignores_stale_native_message_point() -> None:
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value=None),
        ensure_agent_session_id=Mock(return_value="ses-fork"),
        bind_agent_session=Mock(return_value="ses-fork"),
        bind_agent_session_by_id=Mock(return_value="ses-fork"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    server = SimpleNamespace(
        fork_session=AsyncMock(return_value={"id": "oc-fork"}),
        create_session=AsyncMock(),
        list_messages=AsyncMock(),
    )
    request = _request()
    request.context.platform_specific = {
        "agent_session_id": "ses-fork",
        "agent_session_target": {
            "id": "ses-fork",
            "agent_backend": "opencode",
            "native_session_id": "",
            "native_session_fork": {
                "source_session_id": "ses-source",
                "source_native_session_id": "oc-source",
                "source_backend": "opencode",
                "trim_latest_running_turn": False,
                "opencode_fork_message_id": "oc-msg-prev",
            },
        },
    }

    session_id = asyncio.run(manager.get_or_create_session_id(request, server))

    assert session_id == "oc-fork"
    server.fork_session.assert_awaited_once_with("oc-source", directory="/repo", message_id=None)
    server.list_messages.assert_not_awaited()
    server.create_session.assert_not_awaited()


def test_opencode_startup_window_fork_does_not_infer_message_point() -> None:
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value=None),
        ensure_agent_session_id=Mock(return_value="ses-fork"),
        bind_agent_session=Mock(return_value="ses-fork"),
        bind_agent_session_by_id=Mock(return_value="ses-fork"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    server = SimpleNamespace(
        fork_session=AsyncMock(return_value={"id": "oc-fork"}),
        create_session=AsyncMock(),
        list_messages=AsyncMock(),
    )
    request = _request()
    request.context.platform_specific = {
        "agent_session_id": "ses-fork",
        "agent_session_target": {
            "id": "ses-fork",
            "agent_backend": "opencode",
            "native_session_id": "",
            "native_session_fork": {
                "source_session_id": "ses-source",
                "source_native_session_id": "oc-source",
                "source_backend": "opencode",
                "trim_latest_running_turn": True,
                "native_turn_started": False,
            },
        },
    }

    session_id = asyncio.run(manager.get_or_create_session_id(request, server))

    assert session_id == "oc-fork"
    server.fork_session.assert_awaited_once_with("oc-source", directory="/repo", message_id=None)
    server.list_messages.assert_not_awaited()
    server.create_session.assert_not_awaited()


def test_opencode_running_fork_uses_persisted_native_message_point() -> None:
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value=None),
        ensure_agent_session_id=Mock(return_value="ses-fork"),
        bind_agent_session=Mock(return_value="ses-fork"),
        bind_agent_session_by_id=Mock(return_value="ses-fork"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    server = SimpleNamespace(
        fork_session=AsyncMock(return_value={"id": "oc-fork"}),
        create_session=AsyncMock(),
        list_messages=AsyncMock(),
    )
    request = _request()
    request.context.platform_specific = {
        "agent_session_id": "ses-fork",
        "agent_session_target": {
            "id": "ses-fork",
            "agent_backend": "opencode",
            "native_session_id": "",
            "native_session_fork": {
                "source_session_id": "ses-source",
                "source_native_session_id": "oc-source",
                "source_backend": "opencode",
                "trim_latest_running_turn": True,
                "native_turn_started": True,
                "opencode_fork_message_id": "oc-msg-3",
            },
        },
    }

    session_id = asyncio.run(manager.get_or_create_session_id(request, server))

    assert session_id == "oc-fork"
    server.list_messages.assert_not_awaited()
    server.fork_session.assert_awaited_once_with("oc-source", directory="/repo", message_id="oc-msg-3")
    server.create_session.assert_not_awaited()


def test_opencode_running_first_turn_fork_uses_user_boundary() -> None:
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value=None),
        ensure_agent_session_id=Mock(return_value="ses-fork"),
        bind_agent_session=Mock(return_value="ses-fork"),
        bind_agent_session_by_id=Mock(return_value="ses-fork"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    server = SimpleNamespace(
        fork_session=AsyncMock(return_value={"id": "oc-fork"}),
        create_session=AsyncMock(),
        list_messages=AsyncMock(),
    )
    request = _request()
    request.context.platform_specific = {
        "agent_session_id": "ses-fork",
        "agent_session_target": {
            "id": "ses-fork",
            "agent_backend": "opencode",
            "native_session_id": "",
            "native_session_fork": {
                "source_session_id": "ses-source",
                "source_native_session_id": "oc-source",
                "source_backend": "opencode",
                "trim_latest_running_turn": True,
                "native_turn_started": True,
                "opencode_fork_message_id": "oc-msg-1",
            },
        },
    }

    session_id = asyncio.run(manager.get_or_create_session_id(request, server))

    assert session_id == "oc-fork"
    server.list_messages.assert_not_awaited()
    server.fork_session.assert_awaited_once_with("oc-source", directory="/repo", message_id="oc-msg-1")
    server.create_session.assert_not_awaited()


def test_opencode_running_fork_without_persisted_point_rechecks_native_boundary(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    _seed_opencode_messages(tmp_path, "oc-source", ["user", "assistant", "user"])
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value=None),
        ensure_agent_session_id=Mock(return_value="ses-fork"),
        bind_agent_session=Mock(return_value="ses-fork"),
        bind_agent_session_by_id=Mock(return_value="ses-fork"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    server = SimpleNamespace(
        fork_session=AsyncMock(return_value={"id": "oc-fork"}),
        create_session=AsyncMock(),
        list_messages=AsyncMock(),
    )
    request = _request()
    request.context.platform_specific = {
        "agent_session_id": "ses-fork",
        "agent_session_target": {
            "id": "ses-fork",
            "agent_backend": "opencode",
            "native_session_id": "",
            "native_session_fork": {
                "source_session_id": "ses-source",
                "source_native_session_id": "oc-source",
                "source_backend": "opencode",
                "trim_latest_running_turn": True,
                "native_turn_started": False,
            },
        },
    }

    session_id = asyncio.run(manager.get_or_create_session_id(request, server))

    assert session_id == "oc-fork"
    server.list_messages.assert_not_awaited()
    server.fork_session.assert_awaited_once_with("oc-source", directory="/repo", message_id="oc-msg-3")
    server.create_session.assert_not_awaited()


def test_opencode_tool_call_turn_without_persisted_point_rechecks_native_boundary(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    _seed_opencode_tool_call_messages(tmp_path, "oc-source")
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value=None),
        ensure_agent_session_id=Mock(return_value="ses-fork"),
        bind_agent_session=Mock(return_value="ses-fork"),
        bind_agent_session_by_id=Mock(return_value="ses-fork"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    server = SimpleNamespace(
        fork_session=AsyncMock(return_value={"id": "oc-fork"}),
        create_session=AsyncMock(),
        list_messages=AsyncMock(),
    )
    request = _request()
    request.context.platform_specific = {
        "agent_session_id": "ses-fork",
        "agent_session_target": {
            "id": "ses-fork",
            "agent_backend": "opencode",
            "native_session_id": "",
            "native_session_fork": {
                "source_session_id": "ses-source",
                "source_native_session_id": "oc-source",
                "source_backend": "opencode",
                "trim_latest_running_turn": True,
                "native_turn_started": False,
            },
        },
    }

    session_id = asyncio.run(manager.get_or_create_session_id(request, server))

    assert session_id == "oc-fork"
    server.list_messages.assert_not_awaited()
    server.fork_session.assert_awaited_once_with("oc-source", directory="/repo", message_id="oc-msg-1")
    server.create_session.assert_not_awaited()


def test_opencode_stale_saved_boundary_rechecks_current_native_boundary(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    _seed_opencode_messages(tmp_path, "oc-source", ["user", "assistant", "user"])
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value=None),
        ensure_agent_session_id=Mock(return_value="ses-fork"),
        bind_agent_session=Mock(return_value="ses-fork"),
        bind_agent_session_by_id=Mock(return_value="ses-fork"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    server = SimpleNamespace(
        fork_session=AsyncMock(return_value={"id": "oc-fork"}),
        create_session=AsyncMock(),
        list_messages=AsyncMock(),
    )
    request = _request()
    request.context.platform_specific = {
        "agent_session_id": "ses-fork",
        "agent_session_target": {
            "id": "ses-fork",
            "agent_backend": "opencode",
            "native_session_id": "",
            "native_session_fork": {
                "source_session_id": "ses-source",
                "source_native_session_id": "oc-source",
                "source_backend": "opencode",
                "source_message_id": "msg-user",
                "trim_latest_running_turn": True,
                "native_turn_started": True,
                "opencode_fork_message_id": "oc-msg-1",
            },
        },
    }

    with patch(
        "modules.agents.opencode.session.fork_source_state",
        return_value=SimpleNamespace(
            anchor_is_terminal_agent_output=False,
            has_messages_after_anchor=True,
            has_terminal_agent_output_after_anchor=False,
        ),
    ):
        session_id = asyncio.run(manager.get_or_create_session_id(request, server))

    assert session_id == "oc-fork"
    server.list_messages.assert_not_awaited()
    server.fork_session.assert_awaited_once_with("oc-source", directory="/repo", message_id="oc-msg-3")
    server.create_session.assert_not_awaited()


def test_opencode_saved_boundary_is_ignored_when_source_completed_before_anchor() -> None:
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value=None),
        ensure_agent_session_id=Mock(return_value="ses-fork"),
        bind_agent_session=Mock(return_value="ses-fork"),
        bind_agent_session_by_id=Mock(return_value="ses-fork"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    server = SimpleNamespace(
        fork_session=AsyncMock(return_value={"id": "oc-fork"}),
        create_session=AsyncMock(),
        list_messages=AsyncMock(),
    )
    request = _request()
    request.context.platform_specific = {
        "agent_session_id": "ses-fork",
        "agent_session_target": {
            "id": "ses-fork",
            "agent_backend": "opencode",
            "native_session_id": "",
            "native_session_fork": {
                "source_session_id": "ses-source",
                "source_native_session_id": "oc-source",
                "source_backend": "opencode",
                "source_message_id": "msg-result",
                "trim_latest_running_turn": True,
                "native_turn_started": True,
                "opencode_fork_message_id": "oc-msg-user",
            },
        },
    }

    with patch(
        "modules.agents.opencode.session.fork_source_state",
        return_value=SimpleNamespace(
            anchor_is_terminal_agent_output=True,
            has_terminal_agent_output_after_anchor=False,
        ),
    ):
        session_id = asyncio.run(manager.get_or_create_session_id(request, server))

    assert session_id == "oc-fork"
    server.list_messages.assert_not_awaited()
    server.fork_session.assert_awaited_once_with("oc-source", directory="/repo", message_id=None)
    server.create_session.assert_not_awaited()


def test_opencode_saved_boundary_is_ignored_when_source_completed_after_anchor() -> None:
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value=None),
        ensure_agent_session_id=Mock(return_value="ses-fork"),
        bind_agent_session=Mock(return_value="ses-fork"),
        bind_agent_session_by_id=Mock(return_value="ses-fork"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    server = SimpleNamespace(
        fork_session=AsyncMock(return_value={"id": "oc-fork"}),
        create_session=AsyncMock(),
        list_messages=AsyncMock(),
    )
    request = _request()
    request.context.platform_specific = {
        "agent_session_id": "ses-fork",
        "agent_session_target": {
            "id": "ses-fork",
            "agent_backend": "opencode",
            "native_session_id": "",
            "native_session_fork": {
                "source_session_id": "ses-source",
                "source_native_session_id": "oc-source",
                "source_backend": "opencode",
                "source_message_id": "msg-user",
                "trim_latest_running_turn": True,
                "native_turn_started": True,
                "opencode_fork_message_id": "oc-msg-user",
            },
        },
    }

    with patch(
        "modules.agents.opencode.session.fork_source_state",
        return_value=SimpleNamespace(
            anchor_is_terminal_agent_output=False,
            has_terminal_agent_output_after_anchor=True,
        ),
    ):
        session_id = asyncio.run(manager.get_or_create_session_id(request, server))

    assert session_id == "oc-fork"
    server.list_messages.assert_not_awaited()
    server.fork_session.assert_awaited_once_with("oc-source", directory="/repo", message_id=None)
    server.create_session.assert_not_awaited()


def test_opencode_saved_boundary_is_used_when_anchor_is_not_completed() -> None:
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value=None),
        ensure_agent_session_id=Mock(return_value="ses-fork"),
        bind_agent_session=Mock(return_value="ses-fork"),
        bind_agent_session_by_id=Mock(return_value="ses-fork"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    server = SimpleNamespace(
        fork_session=AsyncMock(return_value={"id": "oc-fork"}),
        create_session=AsyncMock(),
        list_messages=AsyncMock(),
    )
    request = _request()
    request.context.platform_specific = {
        "agent_session_id": "ses-fork",
        "agent_session_target": {
            "id": "ses-fork",
            "agent_backend": "opencode",
            "native_session_id": "",
            "native_session_fork": {
                "source_session_id": "ses-source",
                "source_native_session_id": "oc-source",
                "source_backend": "opencode",
                "source_message_id": "msg-user",
                "trim_latest_running_turn": True,
                "native_turn_started": True,
                "opencode_fork_message_id": "oc-msg-user",
            },
        },
    }

    with patch(
        "modules.agents.opencode.session.fork_source_state",
        return_value=SimpleNamespace(
            anchor_is_terminal_agent_output=False,
            has_terminal_agent_output_after_anchor=False,
        ),
    ):
        session_id = asyncio.run(manager.get_or_create_session_id(request, server))

    assert session_id == "oc-fork"
    server.list_messages.assert_not_awaited()
    server.fork_session.assert_awaited_once_with("oc-source", directory="/repo", message_id="oc-msg-user")
    server.create_session.assert_not_awaited()


def test_opencode_completed_source_turn_without_saved_boundary_preserves_source_history(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    _seed_opencode_completed_messages(tmp_path, "oc-source", ["user", "assistant"])
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value=None),
        ensure_agent_session_id=Mock(return_value="ses-fork"),
        bind_agent_session=Mock(return_value="ses-fork"),
        bind_agent_session_by_id=Mock(return_value="ses-fork"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    server = SimpleNamespace(
        fork_session=AsyncMock(return_value={"id": "oc-fork"}),
        create_session=AsyncMock(),
        list_messages=AsyncMock(),
    )
    request = _request()
    request.context.platform_specific = {
        "agent_session_id": "ses-fork",
        "agent_session_target": {
            "id": "ses-fork",
            "agent_backend": "opencode",
            "native_session_id": "",
            "native_session_fork": {
                "source_session_id": "ses-source",
                "source_native_session_id": "oc-source",
                "source_backend": "opencode",
                "source_message_id": "msg-user",
                "trim_latest_running_turn": True,
                "native_turn_started": False,
            },
        },
    }

    session_id = asyncio.run(manager.get_or_create_session_id(request, server))

    assert session_id == "oc-fork"
    server.list_messages.assert_not_awaited()
    server.fork_session.assert_awaited_once_with("oc-source", directory="/repo", message_id=None)
    server.create_session.assert_not_awaited()


def test_opencode_reserved_agent_session_id_is_not_replaced() -> None:
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value="oc-session-1"),
        ensure_agent_session_id=Mock(return_value="ses-different"),
        bind_agent_session=Mock(return_value="ses-different"),
        bind_agent_session_by_id=Mock(return_value="ses-reserved"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    server = SimpleNamespace(get_session=AsyncMock(return_value={"id": "oc-session-1"}))
    request = _request()
    request.context.platform_specific = {
        "agent_session_id": "ses-reserved",
        "agent_session_target": {"id": "ses-reserved"},
    }

    session_id = asyncio.run(manager.get_or_create_session_id(request, server))

    assert session_id == "oc-session-1"
    assert request.context.platform_specific["agent_session_id"] == "ses-reserved"
    sessions.ensure_agent_session_id.assert_not_called()
    sessions.bind_agent_session.assert_not_called()
    sessions.bind_agent_session_by_id.assert_called_once_with(
        "ses-reserved",
        "oc-session-1",
        workdir="/repo",
        vibe_agent_id=None,
        vibe_agent_name=None,
        vibe_agent_backend=None,
    )


def test_opencode_resumes_reserved_native_session_id() -> None:
    """When the reserved workbench row carries a native session id, resume from
    THAT (by-PK), NOT the (session_key, anchor) projection. This is the restart-
    resume fix: the by-PK bind WRITE and the resume READ must agree, else avibe
    forks a fresh OpenCode session after a controller restart and loses context."""
    sessions = SimpleNamespace(
        # If this projection lookup were used, resume would pick the WRONG (or no)
        # session — the test asserts it is never consulted.
        get_agent_session_id=Mock(return_value="oc-from-projection"),
        ensure_agent_session_id=Mock(return_value="ses-different"),
        bind_agent_session=Mock(return_value="ses-different"),
        bind_agent_session_by_id=Mock(return_value="ses-reserved"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    server = SimpleNamespace(get_session=AsyncMock(return_value={"id": "oc-native-reserved"}))
    request = _request()
    request.context.platform_specific = {
        "agent_session_id": "ses-reserved",
        "agent_session_target": {"id": "ses-reserved", "native_session_id": "oc-native-reserved"},
    }

    session_id = asyncio.run(manager.get_or_create_session_id(request, server))

    assert session_id == "oc-native-reserved"
    # The reserved native short-circuits the projection lookup entirely.
    sessions.get_agent_session_id.assert_not_called()
    sessions.ensure_agent_session_id.assert_not_called()
    # Validated against the server, then re-bound to the reserved row by PK.
    server.get_session.assert_awaited_once_with("oc-native-reserved", "/repo", raise_on_error=True)
    sessions.bind_agent_session_by_id.assert_called_once_with(
        "ses-reserved",
        "oc-native-reserved",
        workdir="/repo",
        vibe_agent_id=None,
        vibe_agent_name=None,
        vibe_agent_backend=None,
    )


def test_opencode_subagent_uses_reserved_native_session_id() -> None:
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value="oc-subagent"),
        ensure_agent_session_id=Mock(return_value="ses-subagent"),
        bind_agent_session=Mock(return_value="ses-subagent"),
        bind_agent_session_by_id=Mock(return_value="ses-reserved"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    server = SimpleNamespace(get_session=AsyncMock(return_value={"id": "oc-subagent"}))
    request = _request()
    request.subagent_name = "reviewer"
    request.context.platform_specific = {
        "agent_session_id": "ses-reserved",
        "agent_session_target": {
            "id": "ses-reserved",
            "native_session_id": "oc-main",
            "agent_backend": "opencode",
        },
    }

    session_id = asyncio.run(manager.get_or_create_session_id(request, server))

    assert session_id == "oc-main"
    sessions.get_agent_session_id.assert_not_called()
    server.get_session.assert_awaited_once_with("oc-main", "/repo", raise_on_error=True)
    sessions.bind_agent_session.assert_not_called()
    sessions.bind_agent_session_by_id.assert_called_once_with(
        "ses-reserved",
        "oc-main",
        workdir="/repo",
        vibe_agent_id=None,
        vibe_agent_name=None,
        vibe_agent_backend=None,
    )
    assert request.context.platform_specific["agent_session_id"] == "ses-reserved"


def test_opencode_fails_loud_when_existing_session_invalid() -> None:
    """An existing mapped session that no longer validates on the server must
    RAISE (context loss), not silently create a fresh session and hide it."""
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value="oc-existing"),
        ensure_agent_session_id=Mock(return_value="ses-1"),
        bind_agent_session=Mock(return_value="ses-1"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    create = AsyncMock(return_value={"id": "oc-new"})
    server = SimpleNamespace(get_session=AsyncMock(return_value=None), create_session=create)
    request = _request()

    with pytest.raises(OpenCodeResumeUnavailableError):
        asyncio.run(manager.get_or_create_session_id(request, server))
    create.assert_not_awaited()  # must NOT silently recreate


def test_opencode_transport_error_during_validation_is_not_mislabeled_expiry() -> None:
    """A transport/connection error validating an existing session must propagate
    as-is (transient), NOT be converted into a session-expiry error."""
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value="oc-existing"),
        ensure_agent_session_id=Mock(return_value="ses-1"),
        bind_agent_session=Mock(return_value="ses-1"),
    )
    manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")
    server = SimpleNamespace(
        get_session=AsyncMock(side_effect=ConnectionError("server down")),
        create_session=AsyncMock(),
    )
    request = _request()

    with pytest.raises(ConnectionError):
        asyncio.run(manager.get_or_create_session_id(request, server))


def test_session_facade_ensure_fallback_does_not_clear_existing_native_session() -> None:
    class _LegacyStore:
        def __init__(self):
            self.maps = {"slack::channel::C1": {"codex": {"base-1": "thread-old"}}}

        def get_agent_map(self, user_id, agent_name):
            return self.maps.setdefault(user_id, {}).setdefault(agent_name, {})

    facade = SessionsFacade(_LegacyStore())

    assert facade.ensure_agent_session_id("slack::channel::C1", "codex", "base-1") is None
    assert facade.get_agent_session_id("slack::channel::C1", "base-1", "codex") == "thread-old"


def test_sessions_facade_remove_agent_session_delegates_to_store() -> None:
    class _Store:
        def __init__(self):
            self.removed = []

        def remove_agent_session(self, user_id, agent_name, thread_id):
            self.removed.append((user_id, agent_name, thread_id))
            return True

    store = _Store()
    facade = SessionsFacade(store)

    assert facade.remove_agent_session("telegram::user::58181121", "claude", "telegram_58181121") is True
    facade.clear_agent_session_mapping("telegram::user::58181121", "claude", "telegram_58181121")

    assert store.removed == [
        ("telegram::user::58181121", "claude", "telegram_58181121"),
        ("telegram::user::58181121", "claude", "telegram_58181121"),
    ]
