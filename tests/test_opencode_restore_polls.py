"""Tests for ``OpenCodeAgent.restore_active_polls`` durable identity recovery.

On controller restart, durable Turn ownership stays running while OpenCode
rebuilds its logical/runtime/native mapping. The restored poll must publish that
mapping before Delivery reconciliation and then wait for the recovery-complete
barrier. Legacy polls without durable Turn history still restore their status
projection. These tests lock that:

* an avibe poll → ``controller.set_agent_status(session_id, "running")``;
* an IM poll → NO status write (only avibe sessions get a dot).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_sessions import ActivePollInfo  # noqa: E402
from core.services.agent_steering import SteerOutcome, SteerRequest, active_steer_identity, steer_active_turn  # noqa: E402
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


def _build_agent(active_polls: dict[str, ActivePollInfo], *, language: str = "en"):
    """Assemble an ``OpenCodeAgent`` with the minimal collaborators
    ``restore_active_polls`` touches, plus a controller that records
    ``set_agent_status`` writes."""
    status_writes: list[tuple[str, str]] = []
    removed: list[str] = []
    request_sessions: list[tuple[str, str, str, str]] = []
    prompt_calls: list[dict] = []

    class _Server:
        def __init__(self):
            self.messages = [{"info": {"role": "assistant", "time": {}}}]
            self.status = {"type": "busy"}
            self.status_error = None

        async def list_messages(self, session_id, directory):
            # One in-progress assistant message → the session is "still active",
            # so the poll is restored (not pruned as stale).
            return list(self.messages)

        async def mark_run_active(self, session_id):
            return None

        async def mark_run_inactive(self, session_id):
            return None

        async def get_session_status(self, session_id, directory):
            if self.status_error is not None:
                raise self.status_error
            return self.status

        async def prompt_async(self, **kwargs):
            prompt_calls.append(kwargs)

    class _PollLoop:
        async def run_restored_poll_loop(self, poll_info):
            return None

        async def remove_restored_ack(self, poll_info):
            return None

    class _SessionManager:
        def __init__(self):
            self.request_sessions = {}

        def set_request_session(self, *args):
            request_sessions.append(args)
            self.request_sessions[args[0]] = args[1:]
            return None

        def get_request_session(self, base_session_id):
            return self.request_sessions.get(base_session_id)

        def pop_request_session(self, *args):
            return self.request_sessions.pop(args[0], None)

    class _Sessions:
        def get_all_active_polls(self):
            return dict(active_polls)

        def remove_active_poll(self, session_id):
            removed.append(session_id)

    class _Controller:
        def __init__(self):
            from core.session_turns import SessionTurnManager

            self.config = SimpleNamespace(language=language)
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
    agent._steering_states = {}
    agent._restored_poll_servers = {}

    server = _Server()
    agent._client_manager = SimpleNamespace(_server_manager=server)

    async def _get_server():
        current_task = asyncio.current_task()
        return agent._restored_poll_servers.get(current_task, server)

    agent._get_server = _get_server
    agent.controller.agent_service = SimpleNamespace(agents={"opencode": agent}, _turn_gates={})
    agent._test_prompt_calls = prompt_calls
    agent._test_server = server
    return agent, status_writes, removed, request_sessions


def test_restored_poll_exposes_the_persisted_guarded_steering_owner() -> None:
    poll = _make_poll(platform="avibe", base_session_id="ses_wb", opencode_session_id="oc-1")
    poll.model_dict = {"providerID": "openai", "modelID": "gpt-5"}
    poll.reasoning_effort = "high"
    poll.processing_indicator = {
        "platform": "avibe",
        "opencode_native_steering": {
            "target_session_id": "ses_wb",
            "logical_turn_id": "logical-restored",
            "agent": "build",
            "system": "restored system prompt",
        },
    }
    agent, _, _, _ = _build_agent({"oc-1": poll})
    poll_started = asyncio.Event()
    release_poll = asyncio.Event()
    recovery_complete = asyncio.Event()
    agent.controller._delivery_recovery_complete = recovery_complete

    class _HeldPollLoop:
        async def run_restored_poll_loop(self, poll_info):
            poll_started.set()
            await release_poll.wait()

        async def remove_restored_ack(self, poll_info):
            return None

    agent._poll_loop = _HeldPollLoop()

    async def _run():
        restored = await agent.restore_active_polls()
        assert not poll_started.is_set()
        identity = active_steer_identity(
            agent.controller,
            "opencode",
            "ses_wb",
            expected_logical_turn_id="logical-restored",
        )
        assert identity is not None
        recovery_complete.set()
        await poll_started.wait()
        receipt = await steer_active_turn(
            agent.controller,
            "opencode",
            SteerRequest(
                target_session_id="ses_wb",
                expected_logical_turn_id=identity[0],
                expected_native_turn_id=identity[1],
                text="补充：`keep exact`",
            ),
        )
        release_poll.set()
        await asyncio.gather(*agent._active_requests.values())
        return restored, receipt

    restored, receipt = asyncio.run(_run())

    assert restored == 1
    assert receipt.outcome is SteerOutcome.ACCEPTED
    assert agent._test_prompt_calls == [
        {
            "session_id": "oc-1",
            "directory": "/tmp/work",
            "text": "补充：`keep exact`",
            "agent": "build",
            "model": {"providerID": "openai", "modelID": "gpt-5"},
            "reasoning_effort": "high",
            "system": "restored system prompt",
            "tools": {"question": False},
        }
    ]


def test_restore_publishes_workbench_status_after_native_identity_registration() -> None:
    poll = _make_poll(
        platform="avibe",
        base_session_id="ses_wb",
        opencode_session_id="oc-1",
    )
    agent, _, _, _ = _build_agent({"oc-1": poll})
    observed: list[tuple[bool, bool]] = []

    def restore_running(session_id: str) -> None:
        observed.append(
            (
                session_id in agent._active_requests,
                agent._session_manager.get_request_session(session_id) is not None,
            )
        )

    agent.controller.session_turns.restore_running = restore_running

    async def _run() -> int:
        restored = await agent.restore_active_polls()
        await asyncio.gather(*agent._active_requests.values())
        return restored

    assert asyncio.run(_run()) == 1
    assert observed == [(True, True)]


def test_restore_keeps_accepted_steer_with_post_assistant_user_evidence() -> None:
    poll = _make_poll(platform="avibe", base_session_id="ses_wb", opencode_session_id="oc-1")
    poll.baseline_message_ids = ["old-message"]
    agent, _, removed, _ = _build_agent({"oc-1": poll})
    agent._test_server.messages = [
        {
            "info": {
                "id": "primary-assistant",
                "role": "assistant",
                "time": {"completed": 1},
                "finish": "stop",
            }
        },
        {"info": {"id": "steer-user", "role": "user", "time": {}}, "parts": []},
    ]
    agent._test_server.status = {"type": "idle"}
    reconciled_messages: list[dict] = []

    class _ReconcilePollLoop:
        async def run_restored_poll_loop(self, poll_info):
            server = await agent._get_server()
            reconciled_messages.extend(
                await server.list_messages(
                    poll_info.opencode_session_id,
                    poll_info.working_path,
                )
            )

        async def remove_restored_ack(self, poll_info):
            return None

    agent._poll_loop = _ReconcilePollLoop()

    async def _run():
        restored = await agent.restore_active_polls()
        await asyncio.gather(*agent._active_requests.values())
        return restored

    assert asyncio.run(_run()) == 1
    assert removed == []
    assert reconciled_messages[-1]["info"]["error"]["name"] == (
        "NativeSessionEndedBeforeResult"
    )


def test_restore_does_not_treat_initial_user_prompt_as_steer_evidence() -> None:
    poll = _make_poll(platform="avibe", base_session_id="ses_wb", opencode_session_id="oc-1")
    agent, _, removed, _ = _build_agent({"oc-1": poll})
    agent._test_server.messages = [
        {"info": {"id": "primary-user", "role": "user", "time": {}}, "parts": []}
    ]
    agent._test_server.status = {"type": "idle"}

    assert asyncio.run(agent.restore_active_polls()) == 0
    assert removed == ["oc-1"]


def test_restore_excludes_baseline_assistant_from_steer_evidence() -> None:
    poll = _make_poll(platform="avibe", base_session_id="ses_wb", opencode_session_id="oc-1")
    poll.baseline_message_ids = ["baseline-assistant"]
    agent, _, removed, _ = _build_agent({"oc-1": poll})
    agent._test_server.messages = [
        {
            "info": {
                "id": "baseline-assistant",
                "role": "assistant",
                "time": {"completed": 1},
                "finish": "stop",
            },
            "parts": [],
        },
        {"info": {"id": "primary-user", "role": "user", "time": {}}, "parts": []},
    ]
    agent._test_server.status = {"type": "idle"}

    assert asyncio.run(agent.restore_active_polls()) == 0
    assert removed == ["oc-1"]


def test_restore_preserves_user_only_poll_when_native_status_is_unknown() -> None:
    poll = _make_poll(platform="avibe", base_session_id="ses_wb", opencode_session_id="oc-1")
    agent, _, removed, _ = _build_agent({"oc-1": poll}, language="zh")
    agent._test_server.messages = [
        {"info": {"id": "primary-user", "role": "user", "time": {}}, "parts": []}
    ]
    agent._test_server.status = {"type": "idle"}
    agent._test_server.status_error = TimeoutError("status unavailable")
    reconciled_messages: list[dict] = []

    class _ReconcilePollLoop:
        async def run_restored_poll_loop(self, poll_info):
            agent._test_server.status_error = None
            server = await agent._get_server()
            reconciled_messages.extend(
                await server.list_messages(
                    poll_info.opencode_session_id,
                    poll_info.working_path,
                )
            )

        async def remove_restored_ack(self, poll_info):
            return None

    agent._poll_loop = _ReconcilePollLoop()

    async def _run():
        restored = await agent.restore_active_polls()
        await asyncio.gather(*agent._active_requests.values())
        return restored

    assert asyncio.run(_run()) == 1
    assert removed == []
    assert reconciled_messages[-1]["info"]["error"] == {
        "name": "NativeSessionEndedBeforeResult",
        "data": {
            "message": (
                "OpenCode 已结束，但没有产出模型回复。Provider：(默认)；模型：(默认)；"
                "推理强度：(默认)。这次运行里 OpenCode 没有暴露服务商错误。"
            )
        },
    }


def test_busy_restore_reconciles_inserted_user_that_later_becomes_idle() -> None:
    poll = _make_poll(platform="avibe", base_session_id="ses_wb", opencode_session_id="oc-1")
    agent, _, removed, _ = _build_agent({"oc-1": poll})
    agent._test_server.messages = [
        {
            "info": {
                "id": "primary-assistant",
                "role": "assistant",
                "time": {"completed": 1},
                "finish": "stop",
            },
            "parts": [{"type": "text", "text": "primary"}],
        }
    ]
    status_calls = 0
    reconciled_messages: list[dict] = []

    async def _transitioning_status(session_id, directory):
        nonlocal status_calls
        status_calls += 1
        if status_calls == 2:
            agent._test_server.messages.append(
                {
                    "info": {"id": "steer-user", "role": "user", "time": {}},
                    "parts": [{"type": "text", "text": "extra input"}],
                }
            )
        return {"type": "busy" if status_calls < 3 else "idle"}

    agent._test_server.get_session_status = _transitioning_status

    class _ReconcilePollLoop:
        async def run_restored_poll_loop(self, poll_info):
            server = await agent._get_server()
            reconciled_messages.extend(
                await server.list_messages(
                    poll_info.opencode_session_id,
                    poll_info.working_path,
                )
            )

        async def remove_restored_ack(self, poll_info):
            return None

    agent._poll_loop = _ReconcilePollLoop()

    async def _run():
        restored = await agent.restore_active_polls()
        await asyncio.gather(*agent._active_requests.values())
        return restored

    assert asyncio.run(_run()) == 1
    assert status_calls >= 3
    assert removed == []
    assert reconciled_messages[-1]["info"]["error"]["name"] == (
        "NativeSessionEndedBeforeResult"
    )


def test_busy_restore_preserves_completed_answer_without_later_user() -> None:
    poll = _make_poll(platform="avibe", base_session_id="ses_wb", opencode_session_id="oc-1")
    agent, _, removed, _ = _build_agent({"oc-1": poll})
    agent._test_server.messages = [
        {
            "info": {
                "id": "primary-assistant",
                "role": "assistant",
                "time": {"completed": 1},
                "finish": "stop",
            },
            "parts": [{"type": "text", "text": "primary"}],
        }
    ]
    status_calls = 0
    reconciled_messages: list[dict] = []

    async def _busy_then_idle(session_id, directory):
        nonlocal status_calls
        status_calls += 1
        return {"type": "busy" if status_calls == 1 else "idle"}

    agent._test_server.get_session_status = _busy_then_idle

    class _ReconcilePollLoop:
        async def run_restored_poll_loop(self, poll_info):
            server = await agent._get_server()
            reconciled_messages.extend(
                await server.list_messages(
                    poll_info.opencode_session_id,
                    poll_info.working_path,
                )
            )

        async def remove_restored_ack(self, poll_info):
            return None

    agent._poll_loop = _ReconcilePollLoop()

    async def _run():
        restored = await agent.restore_active_polls()
        await asyncio.gather(*agent._active_requests.values())
        return restored

    assert asyncio.run(_run()) == 1
    assert status_calls == 2
    assert removed == []
    assert reconciled_messages[-1]["info"]["id"] == "primary-assistant"
    assert "error" not in reconciled_messages[-1]["info"]


def test_busy_restore_preserves_completed_answer_when_status_becomes_unavailable() -> None:
    poll = _make_poll(platform="avibe", base_session_id="ses_wb", opencode_session_id="oc-1")
    agent, _, removed, _ = _build_agent({"oc-1": poll})
    agent._test_server.messages = [
        {
            "info": {
                "id": "primary-assistant",
                "role": "assistant",
                "time": {"completed": 1},
                "finish": "stop",
            },
            "parts": [{"type": "text", "text": "primary"}],
        }
    ]
    status_calls = 0
    reconciled_messages: list[dict] = []

    async def _busy_then_unavailable(session_id, directory):
        nonlocal status_calls
        status_calls += 1
        if status_calls == 1:
            return {"type": "busy"}
        raise TimeoutError("status unavailable")

    agent._test_server.get_session_status = _busy_then_unavailable

    class _ReconcilePollLoop:
        async def run_restored_poll_loop(self, poll_info):
            server = await agent._get_server()
            reconciled_messages.extend(
                await server.list_messages(
                    poll_info.opencode_session_id,
                    poll_info.working_path,
                )
            )

        async def remove_restored_ack(self, poll_info):
            return None

    agent._poll_loop = _ReconcilePollLoop()

    async def _run():
        restored = await agent.restore_active_polls()
        await asyncio.gather(*agent._active_requests.values())
        return restored

    assert asyncio.run(_run()) == 1
    assert status_calls == 4
    assert removed == []
    assert reconciled_messages[-1]["info"]["id"] == "primary-assistant"
    assert "error" not in reconciled_messages[-1]["info"]


def test_unknown_restore_seeds_boundary_when_status_recovers_busy() -> None:
    poll = _make_poll(platform="avibe", base_session_id="ses_wb", opencode_session_id="oc-1")
    agent, _, removed, _ = _build_agent({"oc-1": poll})
    agent._test_server.messages = [
        {
            "info": {
                "id": "primary-assistant",
                "role": "assistant",
                "time": {"completed": 1},
                "finish": "stop",
            },
            "parts": [{"type": "text", "text": "primary"}],
        }
    ]
    agent._test_server.status_error = TimeoutError("status unavailable")
    status_calls = 0
    reconciled_messages: list[dict] = []

    async def _busy_then_idle(session_id, directory):
        nonlocal status_calls
        status_calls += 1
        if status_calls == 1:
            agent._test_server.messages.append(
                {
                    "info": {"id": "steer-user", "role": "user", "time": {}},
                    "parts": [{"type": "text", "text": "extra input"}],
                }
            )
        return {"type": "busy" if status_calls == 1 else "idle"}

    class _ReconcilePollLoop:
        async def run_restored_poll_loop(self, poll_info):
            agent._test_server.status_error = None
            agent._test_server.get_session_status = _busy_then_idle
            server = await agent._get_server()
            reconciled_messages.extend(
                await server.list_messages(
                    poll_info.opencode_session_id,
                    poll_info.working_path,
                )
            )

        async def remove_restored_ack(self, poll_info):
            return None

    agent._poll_loop = _ReconcilePollLoop()

    async def _run():
        restored = await agent.restore_active_polls()
        await asyncio.gather(*agent._active_requests.values())
        return restored

    assert asyncio.run(_run()) == 1
    assert status_calls == 2
    assert removed == []
    assert reconciled_messages[-1]["info"]["error"]["name"] == (
        "NativeSessionEndedBeforeResult"
    )


def test_restore_settles_incomplete_assistant_when_unknown_status_recovers_idle() -> None:
    poll = _make_poll(platform="avibe", base_session_id="ses_wb", opencode_session_id="oc-1")
    agent, _, removed, _ = _build_agent({"oc-1": poll})
    agent._test_server.messages = [
        {"info": {"id": "primary-user", "role": "user", "time": {}}, "parts": []},
        {"info": {"id": "partial-assistant", "role": "assistant", "time": {}}, "parts": []},
    ]
    agent._test_server.status = {"type": "idle"}
    agent._test_server.status_error = TimeoutError("status unavailable")
    reconciled_messages: list[dict] = []

    class _ReconcilePollLoop:
        async def run_restored_poll_loop(self, poll_info):
            agent._test_server.status_error = None
            server = await agent._get_server()
            reconciled_messages.extend(
                await server.list_messages(
                    poll_info.opencode_session_id,
                    poll_info.working_path,
                )
            )

        async def remove_restored_ack(self, poll_info):
            return None

    agent._poll_loop = _ReconcilePollLoop()

    async def _run():
        restored = await agent.restore_active_polls()
        await asyncio.gather(*agent._active_requests.values())
        return restored

    assert asyncio.run(_run()) == 1
    assert removed == []
    assert reconciled_messages[-1]["info"]["error"]["name"] == (
        "NativeSessionEndedBeforeResult"
    )


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


def test_restore_active_polls_filters_for_ready_platform() -> None:
    avibe_poll = _make_poll(platform="avibe", base_session_id="ses_wb", opencode_session_id="oc-avibe")
    discord_poll = _make_poll(
        platform="slack",
        base_session_id="discord:thread",
        opencode_session_id="oc-discord",
    )
    discord_poll.processing_indicator = {"platform": "discord"}
    agent, status_writes, _, request_sessions = _build_agent(
        {"oc-avibe": avibe_poll, "oc-discord": discord_poll}
    )

    async def _run():
        restored = await agent.restore_active_polls({"discord"})
        await asyncio.sleep(0)
        for task in list(agent._active_requests.values()):
            if not task.done():
                await task
        return restored

    restored = asyncio.run(_run())

    assert restored == 1
    assert status_writes == []
    assert [entry[1] for entry in request_sessions] == ["oc-discord"]


def test_restore_active_polls_derives_legacy_platform_from_session_key() -> None:
    poll = _make_poll(platform="", base_session_id="telegram:thread", opencode_session_id="oc-telegram")
    poll.session_key = "telegram::channel::chat-1"
    agent, status_writes, _, request_sessions = _build_agent({"oc-telegram": poll})

    async def _run():
        restored = await agent.restore_active_polls({"telegram"})
        await asyncio.sleep(0)
        for task in list(agent._active_requests.values()):
            if not task.done():
                await task
        return restored

    restored = asyncio.run(_run())

    assert restored == 1
    assert status_writes == []
    assert [entry[1] for entry in request_sessions] == ["oc-telegram"]


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
