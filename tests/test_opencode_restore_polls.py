"""Tests for ``OpenCodeAgent.restore_active_polls`` status recovery.

On a controller restart mid-OpenCode-turn, ``_reset_stale_agent_status`` flips
every ``running`` workbench session to ``idle``. ``restore_active_polls`` then
RESUMES the still-active OpenCode poll — but the resumed poll does NOT re-enter
``AgentService.handle_message`` (the inbound status chokepoint), so the avibe
sidebar dot would stay idle/gray for a backend turn that is still live unless the
restore path re-marks the session ``running`` itself. These tests lock that:

* an avibe poll → ``controller.set_agent_status(session_id, "running")``;
* an IM poll → NO status write (only avibe sessions get a dot).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_sessions import ActivePollInfo  # noqa: E402
from modules.agents.opencode.agent import OpenCodeAgent  # noqa: E402


def _make_poll(*, platform: str, base_session_id: str, opencode_session_id: str) -> ActivePollInfo:
    return ActivePollInfo(
        opencode_session_id=opencode_session_id,
        base_session_id=base_session_id,
        channel_id="chan",
        thread_id="thread",
        settings_key="key",
        working_path="/tmp/work",
        platform=platform,
    )


def _build_agent(active_polls: dict[str, ActivePollInfo]):
    """Assemble an ``OpenCodeAgent`` with the minimal collaborators
    ``restore_active_polls`` touches, plus a controller that records
    ``set_agent_status`` writes."""
    status_writes: list[tuple[str, str]] = []
    removed: list[str] = []
    request_sessions: list[tuple[str, str, str, str]] = []

    class _Server:
        async def list_messages(self, session_id, directory):
            # One in-progress assistant message → the session is "still active",
            # so the poll is restored (not pruned as stale).
            return [{"info": {"role": "assistant", "time": {}}}]

        async def mark_run_active(self, session_id):
            return None

        async def mark_run_inactive(self, session_id):
            return None

    class _PollLoop:
        async def run_restored_poll_loop(self, poll_info):
            return None

        async def remove_restored_ack(self, poll_info):
            return None

    class _SessionManager:
        def set_request_session(self, *args):
            request_sessions.append(args)
            return None

        def pop_request_session(self, *args):
            return None

    class _Sessions:
        def get_all_active_polls(self):
            return dict(active_polls)

        def remove_active_poll(self, session_id):
            removed.append(session_id)

    class _Controller:
        def __init__(self):
            from core.session_turns import SessionTurnManager

            # The restore path re-marks running via the turn owner, which delegates
            # to set_agent_status — wire a real manager so the full path is exercised.
            self.session_turns = SessionTurnManager(self)

        def set_agent_status(self, session_id, status):
            status_writes.append((session_id, status))

    agent = OpenCodeAgent.__new__(OpenCodeAgent)
    agent.controller = _Controller()
    agent.sessions = _Sessions()
    agent._poll_loop = _PollLoop()
    agent._session_manager = _SessionManager()
    agent._active_requests = {}

    server = _Server()

    async def _get_server():
        return server

    agent._get_server = _get_server
    return agent, status_writes, removed, request_sessions


def test_restored_avibe_poll_marks_session_running():
    poll = _make_poll(platform="avibe", base_session_id="ses_wb", opencode_session_id="oc-1")
    agent, status_writes, _, _ = _build_agent({"oc-1": poll})

    async def _run():
        restored = await agent.restore_active_polls()
        # Let the spawned restore task settle so it doesn't leak a warning.
        await asyncio.sleep(0)
        for task in list(agent._active_requests.values()):
            if not task.done():
                await task
        return restored

    restored = asyncio.run(_run())
    assert restored == 1
    # The restore path re-marks the avibe workbench session running via the
    # controller's status writer — keyed by the OpenCode base_session_id, which
    # for avibe IS the workbench session id (= agent_session_id / anchor).
    assert ("ses_wb", "running") in status_writes


def test_restored_im_poll_does_not_touch_agent_status():
    poll = _make_poll(platform="slack", base_session_id="slack:thread", opencode_session_id="oc-2")
    agent, status_writes, _, _ = _build_agent({"oc-2": poll})

    async def _run():
        restored = await agent.restore_active_polls()
        await asyncio.sleep(0)
        for task in list(agent._active_requests.values()):
            if not task.done():
                await task
        return restored

    restored = asyncio.run(_run())
    assert restored == 1
    # IM polls carry no workbench session id → no dot, so no status write at all.
    assert status_writes == []


def test_restored_telegram_dm_poll_keeps_typed_user_session_key():
    poll = ActivePollInfo(
        opencode_session_id="oc-telegram",
        base_session_id="telegram_58181121",
        channel_id="58181121",
        thread_id="",
        settings_key="58181121",
        working_path="/tmp/work",
        platform="telegram",
        user_id="58181121",
        processing_indicator={
            "platform": "telegram",
            "user_id": "58181121",
            "channel_id": "58181121",
            "thread_id": "",
            "is_dm": True,
        },
    )
    agent, _, _, request_sessions = _build_agent({"oc-telegram": poll})

    async def _run():
        restored = await agent.restore_active_polls()
        await asyncio.sleep(0)
        for task in list(agent._active_requests.values()):
            if not task.done():
                await task
        return restored

    restored = asyncio.run(_run())

    assert restored == 1
    assert request_sessions == [
        ("telegram_58181121", "oc-telegram", "/tmp/work", "telegram::user::58181121")
    ]


def test_restored_legacy_telegram_dm_poll_infers_typed_user_session_key():
    poll = ActivePollInfo(
        opencode_session_id="oc-telegram",
        base_session_id="telegram_58181121",
        channel_id="58181121",
        thread_id="",
        settings_key="58181121",
        working_path="/tmp/work",
        platform="telegram",
        user_id="58181121",
        processing_indicator={
            "platform": "telegram",
            "user_id": "58181121",
            "channel_id": "58181121",
            "thread_id": "",
        },
    )
    agent, _, _, request_sessions = _build_agent({"oc-telegram": poll})

    async def _run():
        restored = await agent.restore_active_polls()
        await asyncio.sleep(0)
        for task in list(agent._active_requests.values()):
            if not task.done():
                await task
        return restored

    restored = asyncio.run(_run())

    assert restored == 1
    assert request_sessions == [
        ("telegram_58181121", "oc-telegram", "/tmp/work", "telegram::user::58181121")
    ]


def test_restored_poll_prefers_persisted_typed_channel_session_key():
    poll = ActivePollInfo(
        opencode_session_id="oc-slack",
        base_session_id="slack_171717.123",
        channel_id="C1",
        thread_id="171717.123",
        settings_key="C1",
        working_path="/tmp/work",
        platform="slack",
        session_key="slack::channel::C1",
    )
    agent, _, _, request_sessions = _build_agent({"oc-slack": poll})

    async def _run():
        restored = await agent.restore_active_polls()
        await asyncio.sleep(0)
        for task in list(agent._active_requests.values()):
            if not task.done():
                await task
        return restored

    restored = asyncio.run(_run())

    assert restored == 1
    assert request_sessions == [
        ("slack_171717.123", "oc-slack", "/tmp/work", "slack::channel::C1")
    ]


def test_workbench_session_id_for_poll_resolution():
    avibe = _make_poll(platform="avibe", base_session_id="ses_wb", opencode_session_id="oc-1")
    slack = _make_poll(platform="slack", base_session_id="slack:thread", opencode_session_id="oc-2")
    assert OpenCodeAgent._workbench_session_id_for_poll(avibe) == "ses_wb"
    assert OpenCodeAgent._workbench_session_id_for_poll(slack) is None
