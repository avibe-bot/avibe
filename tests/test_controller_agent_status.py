"""Unit tests for the workbench sidebar-dot status, driven by EXACTLY two
chokepoints (no per-path / per-backend instrumentation):

* inbound  — ``AgentService.handle_message`` marks an avibe session ``running``.
* outbound — ``MessageDispatcher.emit_agent_message`` settles a terminal
  ``result`` to ``idle`` (or ``failed`` when ``is_error``); see
  ``test_message_dispatcher_status``.

This file pins the inbound point + the avibe gating in
``Controller._session_id_from_context`` (only workbench turns carry a session id,
so IM/CLI turns never touch the dot).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.controller import Controller
from modules.agents.service import AgentService


def _ctx(session_id, *, platform="avibe"):
    spec = {"agent_session_id": session_id} if session_id else {}
    return SimpleNamespace(platform=platform, platform_specific=spec)


def test_session_id_from_context_reads_agent_session_id():
    assert Controller._session_id_from_context(_ctx("ses-1")) == "ses-1"
    # IM / CLI turns carry no workbench session id → resolve to None (dot skipped).
    assert Controller._session_id_from_context(SimpleNamespace(platform_specific={})) is None
    assert Controller._session_id_from_context(SimpleNamespace(platform_specific=None)) is None
    assert Controller._session_id_from_context(None) is None


def _service_with_capture():
    calls: list = []
    controller = SimpleNamespace(
        _session_id_from_context=staticmethod(Controller._session_id_from_context).__func__,
        set_agent_status=lambda sid, status: calls.append((sid, status)),
    )
    # The inbound chokepoint now marks running via the turn owner (FSM); wire a real
    # one so on_running reaches this stub's set_agent_status recorder.
    from core.session_turns import SessionTurnManager

    controller.session_turns = SessionTurnManager(controller)
    service = AgentService(controller)
    return service, calls


def test_inbound_marks_running_for_avibe_turn():
    service, calls = _service_with_capture()
    dispatched = []

    async def _handle(req):
        dispatched.append(req)

    service.agents["claude"] = SimpleNamespace(name="claude", handle_message=_handle)
    request = SimpleNamespace(context=_ctx("ses-abc"))

    asyncio.run(service.handle_message("claude", request))

    # Inbound chokepoint flips the dot green, then dispatches to the backend.
    assert calls == [("ses-abc", "running")]
    assert dispatched == [request]


def test_inbound_skips_non_avibe_turn():
    service, calls = _service_with_capture()

    async def _handle(req):
        pass

    service.agents["claude"] = SimpleNamespace(name="claude", handle_message=_handle)
    request = SimpleNamespace(context=_ctx(None, platform="slack"))

    asyncio.run(service.handle_message("claude", request))

    # IM turn carries no workbench session id → the dot is never touched.
    assert calls == []


def test_run_marks_running_at_acceptance_before_dispatch(monkeypatch, tmp_path):
    """The status flips ``running`` synchronously when the turn is ACCEPTED
    (in_flight registration), not when dispatch later starts: update_session's
    backend lock re-checks ``agent_status`` inside its UPDATE predicate, so the
    accept-time write closes the startup window where a cross-backend PATCH
    could land while the row still read idle."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()

    import core.session_turns as session_turns_module
    from core.session_turns import SessionTurnManager

    calls: list = []
    dispatched: list = []
    controller = SimpleNamespace(
        _session_id_from_context=staticmethod(Controller._session_id_from_context).__func__,
        set_agent_status=lambda sid, status: calls.append((sid, status)),
    )
    mgr = SessionTurnManager(controller)

    async def _dispatch(controller_arg, context, text, **kwargs):
        dispatched.append(text)

    monkeypatch.setattr(session_turns_module, "dispatch_turn", _dispatch)

    async def _exercise():
        await mgr._run("ses-accept", _ctx("ses-accept"), "hi")
        # _run returns right after acceptance; the dispatch task hasn't run yet
        # (single-threaded loop) — the running mark must already be recorded.
        assert ("ses-accept", "running") in calls
        assert dispatched == []
        turn = mgr.in_flight.get("ses-accept")
        assert turn is not None
        await turn.task

    asyncio.run(_exercise())
    assert dispatched == ["hi"]
