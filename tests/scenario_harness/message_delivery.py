from __future__ import annotations

from types import SimpleNamespace

from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.message_output import MessageOutput
from modules.im import MessageContext
from tests.scenario_harness.core import BaseScenarioHarness, ScenarioControllerBase


class MessageDeliverySettingsManager:
    def _canonicalize_message_type(self, message_type):
        return message_type

    def is_message_type_hidden(self, settings_key, canonical_type):
        return False


class MessageDeliverySessionHandler:
    def __init__(self):
        self.finalized = []

    def finalize_scheduled_delivery(self, context, sent_message_id):
        self.finalized.append((context.channel_id, context.thread_id, sent_message_id))


class MessageDeliveryController(ScenarioControllerBase):
    def __init__(self, *, platform: str = "slack"):
        super().__init__(default_backend="codex", platform=platform)
        self.config.reply_enhancements = False
        self.session_handler = MessageDeliverySessionHandler()
        self.settings_manager = MessageDeliverySettingsManager()
        self.runtime_turn_current = True
        self.runtime_release_calls = 0
        self.turn_terminal_calls = 0
        self.stream_completion_calls = 0
        self.agent_service = SimpleNamespace(
            agents={},
            emit_matches_runtime_turn=lambda context: self.runtime_turn_current,
            release_runtime_turn=lambda context: setattr(
                self,
                "runtime_release_calls",
                self.runtime_release_calls + 1,
            ),
        )
        self.session_turns = SimpleNamespace(
            on_terminal_result=lambda context, is_error: setattr(
                self,
                "turn_terminal_calls",
                self.turn_terminal_calls + 1,
            )
        )

    def _get_session_key(self, context):
        return f"{context.platform or self.config.platform}::{context.channel_id}"

    def get_settings_manager_for_context(self, context):
        return self.settings_manager

    def mark_turn_complete(self, context):
        self.stream_completion_calls += 1


class MessageDeliveryHarness(BaseScenarioHarness):
    def __init__(self, *, platform: str = "slack", user_id: str = "scheduled", channel_id: str = "C123", thread_id=None):
        super().__init__(
            MessageDeliveryController(platform=platform),
            user_id=user_id,
            channel_id=channel_id,
        )
        self.context.thread_id = thread_id
        self.context.platform = platform
        self.dispatcher = ConsolidatedMessageDispatcher(self.controller)

    async def emit_result(self, text: str):
        return await self.dispatcher.emit_agent_message(self.context, "result", text)

    async def emit_output(self, text: str, output: MessageOutput):
        return await self.dispatcher.emit_agent_message(
            self.context,
            "result",
            text,
            output=output,
        )

    @property
    def sent_messages(self):
        return list(getattr(self.controller.im_client, "probe").matching("message"))

    @property
    def finalized_calls(self):
        return list(self.controller.session_handler.finalized)
