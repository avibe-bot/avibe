"""Contract coverage for Show checkpoints across every turn entry point."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from types import SimpleNamespace

from config import paths
from core import show_git
from core.git_binary import ResolvedGit
from core.inbox_events import InboxEventBus
from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.message_output import MessageOutput
from core.session_turns import SessionTurnManager
from core.show_git import POST_TURN, PRE_TURN, ShowGitCheckpointService, TurnCheckpointContext
from modules.agents.service import AgentService
from modules.im import MessageContext


class _Settings:
    @staticmethod
    def _canonicalize_message_type(message_type: str) -> str:
        return message_type

    @staticmethod
    def is_message_type_hidden(_settings_key: str, _message_type: str) -> bool:
        return False


class _IMClient:
    @staticmethod
    def should_use_thread_for_reply() -> bool:
        return False


class _TerminalDispatcher:
    def __init__(self, controller) -> None:
        self._delegate = ConsolidatedMessageDispatcher(controller)

    @staticmethod
    async def begin_status_bubble(_context) -> None:
        return None

    @staticmethod
    def update_thread_message_id(_context) -> None:
        return None

    async def emit_agent_message(self, **kwargs):
        return await self._delegate.emit_agent_message(**kwargs)


class _TerminalAgent:
    name = "checkpoint-probe"

    def __init__(self, controller) -> None:
        self.controller = controller

    @staticmethod
    def runtime_turn_key(request) -> str:
        return request.composite_session_id

    async def handle_message(self, request) -> None:
        await self.controller.emit_agent_message(
            request.context,
            "result",
            "",
            level="silent",
            output=MessageOutput(completes_turn=True, completes_run=False),
        )


class _Controller:
    def __init__(self, checkpoint_service: ShowGitCheckpointService) -> None:
        self.config = SimpleNamespace(reply_enhancements=False)
        self.show_git_checkpoint_service = checkpoint_service
        self.statuses = []
        self.session_turns = SessionTurnManager(self)
        self.agent_service = AgentService(self)
        self.agent_service.register(_TerminalAgent(self))
        self.message_dispatcher = _TerminalDispatcher(self)
        self.processing_indicator = None

    @staticmethod
    def _session_id_from_context(context) -> str | None:
        return (context.platform_specific or {}).get("agent_session_id")

    @staticmethod
    def _get_settings_key(context) -> str:
        return context.channel_id

    @staticmethod
    def get_settings_manager_for_context(_context) -> _Settings:
        return _Settings()

    @staticmethod
    def get_im_client_for_context(_context) -> _IMClient:
        return _IMClient()

    @staticmethod
    def _get_session_key(context) -> str:
        return f"{context.platform}::{context.channel_id}"

    def get_turn_sink(self, session_key: str):
        return self.session_turns.get_turn_sink(session_key)

    def set_agent_status(self, session_id: str, status: str) -> None:
        self.statuses.append((session_id, status))

    def update_thread_message_id(self, context) -> None:
        self.message_dispatcher.update_thread_message_id(context)

    async def emit_agent_message(self, context, message_type, text, **kwargs):
        return await self.message_dispatcher.emit_agent_message(
            context=context,
            message_type=message_type,
            text=text,
            **kwargs,
        )

    @staticmethod
    def mark_turn_complete(_context) -> None:
        return None


def test_all_turn_entrypoints_reach_checkpoint_subscriber(monkeypatch, tmp_path) -> None:
    """Scenario: MESSAGE-DELIVERY-006.

    Sync and async ``vibe agent run`` differ only in whether the CLI waits for
    the same persisted execution. Every listed source enters backend execution
    through ``AgentService`` and exits through the terminal dispatcher, so this
    enumeration locks the shared checkpoint projection at those two boundaries.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    entrypoints = {
        "im_message": {"platform": "slack", "turn_source": "human"},
        "workbench_chat": {"platform": "avibe", "turn_source": "human"},
        "internal_dispatch": {"platform": "avibe", "turn_source": "show_dispatch"},
        "agent_run_sync": {"platform": "avibe", "task_trigger_kind": "agent_run", "cli_waits": True},
        "agent_run_async": {"platform": "avibe", "task_trigger_kind": "agent_run", "cli_waits": False},
        "scheduled_task": {"platform": "avibe", "task_trigger_kind": "scheduled"},
        "watch_callback": {"platform": "avibe", "task_trigger_kind": "watch"},
    }
    checkpoint_calls = defaultdict(list)

    class _Repository:
        def __init__(self, session_id: str) -> None:
            self.session_id = session_id

        def checkpoint(self, checkpoint: str, **_kwargs) -> bool:
            checkpoint_calls[self.session_id].append(checkpoint)
            return True

    monkeypatch.setattr(
        show_git,
        "load_turn_checkpoint_context",
        lambda session_id, **_kwargs: TurnCheckpointContext(
            message=f"drive {session_id}",
            message_id=f"message-{session_id}",
        ),
    )
    service = ShowGitCheckpointService(ResolvedGit(path=tmp_path / "git", source="system"))
    monkeypatch.setattr(service, "_repository", lambda session_id: _Repository(session_id))
    monkeypatch.setattr(service, "_link_message", lambda _context, _session_id: True)
    bus = InboxEventBus()
    service.start(bus)
    controller = _Controller(service)
    lifecycle = []
    subscription_id = bus.subscribe_callback(
        lambda event_type, data: lifecycle.append((event_type, data))
        if event_type in {"turn.start", "turn.end"}
        else None
    )

    async def _exercise() -> None:
        for name, metadata in entrypoints.items():
            session_id = f"ses_{name}"
            paths.get_show_page_dir(session_id).mkdir(parents=True)
            context = MessageContext(
                user_id="user",
                channel_id=session_id,
                platform=metadata["platform"],
                message_id=f"message-{name}",
                platform_specific={
                    **metadata,
                    "agent_session_id": session_id,
                    "task_execution_id": f"run-{name}",
                },
            )
            request = SimpleNamespace(
                context=context,
                composite_session_id=f"runtime-{name}",
                processing_indicator=None,
            )
            await controller.agent_service.handle_message("checkpoint-probe", request)

    try:
        asyncio.run(_exercise())
    finally:
        bus.unsubscribe(subscription_id)
        service.stop()

    expected_lifecycle = []
    for name in entrypoints:
        session_id = f"ses_{name}"
        expected_lifecycle.extend(
            [
                ("turn.start", {"session_id": session_id}),
                ("turn.end", {"session_id": session_id}),
            ]
        )
        assert checkpoint_calls[session_id] == [PRE_TURN, POST_TURN]
    assert lifecycle == expected_lifecycle
