"""The first turn after a session switched backend (docs/plans/2026-10-22-session-backend-switch.md)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.agent_input import AgentInputMetadata
from core.handlers.message_handler import MessageHandler
from core.session_handoff import BACKEND_HANDOFF_TURN_KEY, HANDOFF_BUDGET_CHARS, render_backend_handoff
from modules.im import MessageContext
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, messages
from storage.settings_service import upsert_scope
from storage.workbench_sessions_service import create_session

_HANDOFF = {"id": "switch-1", "from_backend": "claude", "from_native": "n1", "to_backend": "vibey", "switched_at": ""}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seed_session(tmp_path, monkeypatch, *, native: str = "") -> str:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="proj_handoff", now="2026-10-22T00:00:00Z"
        )
        sid = create_session(conn, scope_id=scope_id, agent_backend="vibey", agent_name="vibey")["id"]
        conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == sid)
            .values(native_session_id=native, metadata_json=json.dumps({"backend_handoff": dict(_HANDOFF)}))
        )
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


def _im_turn(sid: str, **spec) -> tuple[MessageContext, SimpleNamespace]:
    """An IM turn: the session is selected by its run target, which can predate the
    switch and still offer the replaced native."""
    context = MessageContext(
        user_id="U1",
        channel_id="C1",
        platform="slack",
        platform_specific={
            "agent_run_target": {"agent_session_id": sid, "agent_backend": "vibey", "native_session_id": "n1"},
            **spec,
        },
    )
    request = SimpleNamespace(input_metadata=AgentInputMetadata(user_id="U1", user_name="Alex"))
    return context, request


def _handler(agent, backend: str = "vibey") -> MessageHandler:
    from config import paths
    from storage.sessions_service import SQLiteSessionsService

    handler = MessageHandler.__new__(MessageHandler)
    handler.controller = SimpleNamespace(agent_service=SimpleNamespace(agents={backend: agent}))
    handler.sessions = SQLiteSessionsService(paths.get_sqlite_state_path())
    return handler


@pytest.mark.anyio
async def test_switched_session_turn_releases_stale_runtime_and_carries_the_handoff(tmp_path, monkeypatch):
    """The first turn on the new backend reads the session row by id, whichever
    projection selected it. With no native bound yet it drops any runtime that
    backend still caches and stops the projections offering the replaced native; it
    prefixes the input with the handoff (the canonical text is untouched) and is
    tagged so its native-start receipt can spend the marker."""

    sid = _seed_session(tmp_path, monkeypatch)
    agent = SimpleNamespace(release_native_runtime=AsyncMock())
    context, request = _im_turn(sid)
    await _handler(agent)._attach_backend_handoff(context, "vibey", request)

    agent.release_native_runtime.assert_awaited_once_with(request)
    assert context.platform_specific["agent_run_target"]["native_session_id"] == ""
    assert context.platform_specific[BACKEND_HANDOFF_TURN_KEY] == "switch-1"
    handoff = request.input_metadata.handoff
    assert f"Avibe session `{sid}`" in handoff
    assert handoff.index("Please migrate the parser") < handoff.index("Keep the old CLI flags")
    rendered = request.input_metadata.render("next step", SimpleNamespace(include_time_info=False))
    assert rendered.startswith("[Avibe handoff]")
    assert rendered.endswith("[Alex<U1>]\nnext step")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("backend", "spec", "carries"),
    [
        # Bound but not yet accepted (a first turn whose start then failed): no
        # release, the handoff is still owed.
        ("vibey", {}, True),
        # A one-off run on another backend does not continue this conversation.
        ("claude", {}, False),
        # Nor does a subagent's namespaced conversation.
        ("vibey", {"routing_subagent": "reviewer"}, False),
        ("vibey", {"backend_base_session_id": "base:reviewer"}, False),
    ],
)
async def test_only_turns_continuing_the_switched_conversation_carry_the_handoff(
    tmp_path, monkeypatch, backend, spec, carries
):
    sid = _seed_session(tmp_path, monkeypatch, native="vibey-native")
    agent = SimpleNamespace(release_native_runtime=AsyncMock())
    context, request = _im_turn(sid, **spec)
    await _handler(agent, backend)._attach_backend_handoff(context, backend, request)

    agent.release_native_runtime.assert_not_awaited()
    assert bool(request.input_metadata.handoff) is carries
    assert (BACKEND_HANDOFF_TURN_KEY in context.platform_specific) is carries


@pytest.mark.anyio
async def test_a_handoff_that_cannot_be_built_fails_the_turn(tmp_path, monkeypatch):
    """The first accepted turn must be the one carrying the handoff, so a store
    failure fails the turn before dispatch instead of starting without it."""

    sid = _seed_session(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "core.session_handoff.build_backend_handoff",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("database is locked")),
    )
    context, request = _im_turn(sid)
    with pytest.raises(RuntimeError, match="database is locked"):
        await _handler(SimpleNamespace(release_native_runtime=AsyncMock()))._attach_backend_handoff(
            context, "vibey", request
        )
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
