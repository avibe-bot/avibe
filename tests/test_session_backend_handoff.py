"""The first turn after a session switched backend (docs/plans/2026-10-22-session-backend-switch.md)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.agent_input import AgentInputMetadata
from core.handlers.message_handler import MessageHandler
from core.session_handoff import BACKEND_HANDOFF_TURN_KEY, HANDOFF_BUDGET_CHARS, render_backend_handoff
from modules.im import MessageContext
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import messages
from storage.settings_service import upsert_scope
from storage.workbench_sessions_service import create_session

_HANDOFF = {"id": "switch-1", "from_backend": "claude", "from_native": "n1", "to_backend": "vibey", "switched_at": ""}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seed_session(tmp_path, monkeypatch) -> str:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="proj_handoff", now="2026-10-22T00:00:00Z"
        )
        sid = create_session(conn, scope_id=scope_id, agent_backend="vibey", agent_name="vibey")["id"]
        for index, (author, kind, text) in enumerate(
            (
                ("user", "user", "Please migrate the parser to the new API."),
                ("agent", "result", "Done with the lexer; the parser is next."),
                ("user", "user", "Keep the old CLI flags working."),
            )
        ):
            stamp = f"2026-10-22T00:00:0{index}Z"
            conn.execute(
                messages.insert().values(
                    id=f"msg-{index}",
                    scope_id=scope_id,
                    session_id=sid,
                    platform="avibe",
                    author=author,
                    type=kind,
                    content_text=text,
                    content_json="{}",
                    metadata_json="{}",
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
    return sid


def _turn(sid: str, *, native: str = "") -> tuple[MessageContext, SimpleNamespace]:
    context = MessageContext(
        user_id="U1",
        channel_id=sid,
        platform="avibe",
        platform_specific={
            "agent_session_id": sid,
            "agent_session_target": {
                "id": sid,
                "agent_backend": "vibey",
                "native_session_id": native,
                "metadata": {"backend_handoff": dict(_HANDOFF)},
            },
        },
    )
    request = SimpleNamespace(input_metadata=AgentInputMetadata(user_id="U1", user_name="Alex"))
    return context, request


def _handler(agent) -> MessageHandler:
    handler = MessageHandler.__new__(MessageHandler)
    handler.controller = SimpleNamespace(agent_service=SimpleNamespace(agents={"vibey": agent}))
    return handler


@pytest.mark.anyio
async def test_switched_session_turn_releases_stale_runtime_and_carries_the_handoff(tmp_path, monkeypatch):
    """The first turn on the new backend drops any runtime that backend still caches
    for the session, prefixes its input with the handoff (the canonical text is
    untouched), and is tagged so its native-start receipt spends the marker. Once
    the new backend bound a native (a retried first turn), nothing is released but
    the handoff is still carried until a receipt lands."""

    sid = _seed_session(tmp_path, monkeypatch)
    agent = SimpleNamespace(release_native_runtime=AsyncMock())
    handler = _handler(agent)

    context, request = _turn(sid)
    await handler._attach_backend_handoff(context, "vibey", request)
    agent.release_native_runtime.assert_awaited_once_with(request)
    assert context.platform_specific[BACKEND_HANDOFF_TURN_KEY] == "switch-1"
    handoff = request.input_metadata.handoff
    assert f"Avibe session `{sid}`" in handoff
    assert "Keep the old CLI flags working." in handoff
    assert handoff.index("Please migrate the parser") < handoff.index("Keep the old CLI flags")
    config = SimpleNamespace(include_time_info=False, include_user_info=True)
    rendered = request.input_metadata.render("next step", config)
    assert rendered.startswith("[Avibe handoff]")
    assert rendered.endswith("[Alex<U1>]\nnext step")

    agent.release_native_runtime.reset_mock()
    context, request = _turn(sid, native=sid)
    await handler._attach_backend_handoff(context, "vibey", request)
    agent.release_native_runtime.assert_not_awaited()
    assert request.input_metadata.handoff

    context, request = _turn(sid)
    context.platform_specific["agent_session_target"]["metadata"] = {}
    await handler._attach_backend_handoff(context, "vibey", request)
    agent.release_native_runtime.assert_not_awaited()
    assert request.input_metadata.handoff is None
    assert BACKEND_HANDOFF_TURN_KEY not in context.platform_specific


def test_handoff_keeps_the_newest_conversation_within_its_budget():
    """However long the history, the block stays within budget, keeps the newest
    messages in chronological order, says older ones were left out, and always
    ends with the query for the full record."""

    history = [
        {"type": "user" if index % 2 == 0 else "result", "text": f"message {index} " + "x" * 3000, "created_at": ""}
        for index in range(40)
    ]
    open_work = [{"item_kind": "watch", "id": f"watch-{index}", "label": "CI", "status": "enabled"} for index in range(30)]
    block = render_backend_handoff(
        "sesabc", _HANDOFF, to_backend="vibey", messages=history, has_older=True, open_work=open_work
    )

    assert len(block) <= HANDOFF_BUDGET_CHARS
    assert "message 39 " in block
    assert "message 0 " not in block
    assert block.index("message 38 ") < block.index("message 39 ")
    assert "(earlier messages omitted)" in block
    assert "- ... 18 more" in block
    assert "WHERE session_id='sesabc'" in block
    assert block.rstrip().endswith("[End of Avibe handoff]")
