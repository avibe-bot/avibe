"""Contract coverage for Show checkpoints across every turn entry point."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from types import SimpleNamespace

import httpx

from config import paths
from core import inbox_events, internal_server, show_git
from core.git_binary import ResolvedGit
from core.inbox_events import InboxEventBus
from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.message_output import MessageOutput
from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore, TaskExecutionStore
from core.services.dispatch import dispatch_turn
from core.session_turns import SessionTurnManager, emit_matches_active_turn
from core.show_git import POST_TURN, PRE_TURN, ShowGitCheckpointService, TurnCheckpointContext
from modules.agents.service import AgentService
from modules.im import MessageContext
from storage.importer import ensure_sqlite_state


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


class _MessageHandler:
    def __init__(self, controller) -> None:
        self.controller = controller

    async def _run(self, context: MessageContext, message: str) -> None:
        session_id = (context.platform_specific or {}).get("agent_session_id")
        request = SimpleNamespace(
            context=context,
            message=message,
            composite_session_id=f"runtime:{session_id}",
            processing_indicator=None,
        )
        await self.controller.agent_service.handle_message("checkpoint-probe", request)

    async def handle_user_message(self, context: MessageContext, message: str):
        await self._run(context, message)
        return None

    async def handle_scheduled_message(self, context: MessageContext, message: str, parsed_session_key=None):
        del parsed_session_key
        await self._run(context, message)
        return None


class _Controller:
    def __init__(self, checkpoint_service: ShowGitCheckpointService) -> None:
        self.config = SimpleNamespace(reply_enhancements=False)
        self.show_git_checkpoint_service = checkpoint_service
        self.statuses = []
        self.session_turns = SessionTurnManager(self)
        self.session_turn_gate = self.session_turns
        self.agent_service = AgentService(self)
        self.agent_service.register(_TerminalAgent(self))
        self.message_dispatcher = _TerminalDispatcher(self)
        self.message_handler = _MessageHandler(self)
        self.processing_indicator = None
        self.command_handler = SimpleNamespace(handle_stop=self._stop)

    @staticmethod
    async def _stop(_context) -> bool:
        return True

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

    def register_turn_sink(self, session_key: str, **kwargs) -> None:
        self.session_turns.register_turn_sink(session_key, **kwargs)

    def pop_turn_sink(self, session_key: str, done_event=None) -> None:
        self.session_turns.pop_turn_sink(session_key, done_event)

    def get_turn_sink(self, session_key: str):
        return self.session_turns.get_turn_sink(session_key)

    def mark_turn_complete(self, context=None) -> None:
        if context is None:
            return
        sink = self.get_turn_sink(self._get_session_key(context))
        if sink is None or not emit_matches_active_turn(sink, context):
            return
        done = sink.get("done_event")
        if done is not None:
            done.set()

    def set_agent_status(self, session_id: str, status: str) -> None:
        self.statuses.append((session_id, status))

    def update_thread_message_id(self, context) -> None:
        self.message_dispatcher.update_thread_message_id(context)

    async def emit_agent_message(self, context, message_type, text, **kwargs):
        try:
            return await self.message_dispatcher.emit_agent_message(
                context=context,
                message_type=message_type,
                text=text,
                **kwargs,
            )
        finally:
            self.session_turns.on_terminal_delivery_complete(context)

    @staticmethod
    def _t(key: str, **_kwargs) -> str:
        return key


def _context(name: str, *, platform: str, trigger_kind: str | None = None) -> MessageContext:
    session_id = f"ses_{name}"
    metadata = {
        "platform": platform,
        "agent_session_id": session_id,
        "task_execution_id": f"run-{name}",
    }
    if trigger_kind:
        metadata["task_trigger_kind"] = trigger_kind
        metadata["turn_source"] = "scheduled"
    return MessageContext(
        user_id="user",
        channel_id=session_id,
        platform=platform,
        message_id=f"message-{name}",
        platform_specific=metadata,
    )


def test_all_turn_entrypoints_reach_checkpoint_subscriber(monkeypatch, tmp_path) -> None:
    """Scenario: MESSAGE-DELIVERY-006."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    contexts = {
        "im_message": _context("im_message", platform="slack"),
        "workbench_chat": _context("workbench_chat", platform="avibe"),
        "internal_dispatch": _context("internal_dispatch", platform="avibe"),
        "agent_run_sync": _context("agent_run_sync", platform="slack", trigger_kind="agent_run"),
        "agent_run_async": _context("agent_run_async", platform="slack", trigger_kind="agent_run"),
        "scheduled_task": _context("scheduled_task", platform="slack", trigger_kind="scheduled"),
        "watch_callback": _context("watch_callback", platform="slack", trigger_kind="watch"),
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
    monkeypatch.setattr(inbox_events, "bus", bus)
    service.start(bus)
    controller = _Controller(service)
    lifecycle = []
    subscription_id = bus.subscribe_callback(
        lambda event_type, data: lifecycle.append((event_type, data))
        if event_type in {"turn.start", "turn.end"}
        else None
    )
    for context in contexts.values():
        paths.get_show_page_dir(context.platform_specific["agent_session_id"]).mkdir(parents=True)

    scheduled = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
    )

    async def _build_scheduled_context(
        _target,
        *,
        execution_id: str,
        **_kwargs,
    ) -> MessageContext:
        return contexts[execution_id]

    monkeypatch.setattr(scheduled, "_build_context", _build_scheduled_context)

    async def _build_dispatch_payload(payload) -> tuple[str, MessageContext]:
        return "edit show page", contexts[payload["entrypoint"]]

    monkeypatch.setattr(internal_server, "_build_dispatch_payload", _build_dispatch_payload)
    app = internal_server.create_app(controller)

    async def _exercise() -> None:
        await dispatch_turn(controller, contexts["im_message"], "IM message")

        workbench = contexts["workbench_chat"]
        workbench_id = workbench.platform_specific["agent_session_id"]
        await controller.session_turns.submit(workbench_id, workbench, "Workbench chat")
        await controller.session_turns.in_flight[workbench_id].task

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/internal/dispatch",
                json={"session_id": "ses_internal_dispatch", "entrypoint": "internal_dispatch"},
            )
        assert response.status_code == 200

        # Sync and async CLI modes enqueue the same Agent Run execution; only
        # the CLI caller's wait behavior differs. Exercise that executor twice.
        for name in ("agent_run_sync", "agent_run_async"):
            await scheduled._execute_agent_run(
                session_key="slack::channel::C123",
                session_id=None,
                post_to=None,
                deliver_key=None,
                message=name,
                execution_id=name,
            )

        for name in ("scheduled_task", "watch_callback"):
            await scheduled._execute_request(
                session_key="slack::channel::C123",
                session_id=None,
                post_to=None,
                deliver_key=None,
                prompt=name,
                execution_id=name,
                trigger_kind=contexts[name].platform_specific["task_trigger_kind"],
            )

    try:
        asyncio.run(_exercise())
    finally:
        bus.unsubscribe(subscription_id)
        service.stop()

    expected_lifecycle = []
    for name, context in contexts.items():
        session_id = context.platform_specific["agent_session_id"]
        expected_lifecycle.extend(
            [
                ("turn.start", {"session_id": session_id}),
                ("turn.end", {"session_id": session_id}),
            ]
        )
        assert checkpoint_calls[session_id] == [PRE_TURN, POST_TURN], name
    assert lifecycle == expected_lifecycle
