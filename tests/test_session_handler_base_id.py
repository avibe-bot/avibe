from __future__ import annotations

import asyncio
from dataclasses import dataclass
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.handlers.session_handler import ClaudeSessionNotFoundError, SessionHandler
from modules.im import MessageContext


@dataclass
class _Config:
    platform: str = "discord"


class _FakeSessions:
    def __init__(self) -> None:
        self.alias_calls = []
        self.cross_scope_alias_calls = []
        self.clear_calls = []
        self.thread_marks = []
        self.alias_result = True
        self.cross_scope_alias_result = True
        self.clear_result = 1

    def alias_session_base(self, user_id, source_base_session_id, alias_base_session_id):
        self.alias_calls.append((user_id, source_base_session_id, alias_base_session_id))
        return self.alias_result

    def clear_session_base(self, user_id, base_session_id):
        self.clear_calls.append((user_id, base_session_id))
        return self.clear_result

    def alias_session_base_across_scopes(
        self,
        source_user_id,
        target_user_id,
        source_base_session_id,
        alias_base_session_id,
    ):
        self.cross_scope_alias_calls.append(
            (source_user_id, target_user_id, source_base_session_id, alias_base_session_id)
        )
        return self.cross_scope_alias_result

    def mark_thread_active(self, user_id, channel_id, thread_ts):
        self.thread_marks.append((user_id, channel_id, thread_ts))


class _Controller:
    def __init__(
        self,
        *,
        platform: str = "discord",
        dm_threads: bool = False,
        channel_message_sessions: bool = True,
    ) -> None:
        self.config = _Config()
        self.config.platform = platform
        self.sessions = _FakeSessions()
        self.im_client = type(
            "IM",
            (),
            {
                "formatter": None,
                "should_use_thread_for_dm_session": lambda self: dm_threads,
                "should_use_message_id_for_channel_session": lambda self, context=None: channel_message_sessions,
                "should_use_thread_for_reply": lambda self: platform in {"discord", "slack", "lark"},
            },
        )()
        self.settings_manager = type("Settings", (), {"sessions": None})()
        self.session_manager = object()
        self.claude_sessions = {}
        self.receiver_tasks = {}
        self.stored_session_mappings = {}

    def get_cwd(self, context: MessageContext) -> str:
        return "/tmp/workdir"

    def _get_settings_key(self, context: MessageContext) -> str:
        return context.user_id if (context.platform_specific or {}).get("is_dm") else context.channel_id

    def _get_session_key(self, context: MessageContext) -> str:
        return f"{getattr(context, 'platform', None) or 'test'}::{self._get_settings_key(context)}"

    def get_im_client_for_context(self, context: MessageContext):
        return self.im_client


class _FakeFormatter:
    @staticmethod
    def format_error(text: str) -> str:
        return f"ERR:{text}"


class _FakeIM:
    def __init__(self) -> None:
        self.formatter = _FakeFormatter()
        self.sent_messages = []

    @staticmethod
    def should_use_thread_for_dm_session() -> bool:
        return False

    @staticmethod
    def should_use_message_id_for_channel_session(context=None) -> bool:
        return True

    @staticmethod
    def should_use_thread_for_reply() -> bool:
        return True

    async def send_message(self, context: MessageContext, message: str) -> None:
        self.sent_messages.append((context, message))


def test_dm_session_base_id_uses_stable_channel_id() -> None:
    handler = SessionHandler(_Controller(platform="discord", dm_threads=False))
    context = MessageContext(
        user_id="u-1",
        channel_id="dm-123",
        thread_id="thread-999",
        message_id="msg-999",
        platform_specific={"is_dm": True},
    )

    assert handler.get_base_session_id(context) == "discord_dm-123"


def test_dm_session_base_id_uses_thread_when_platform_supports_dm_threads() -> None:
    handler = SessionHandler(_Controller(platform="lark", dm_threads=True))
    context = MessageContext(
        user_id="u-1",
        channel_id="dm-123",
        thread_id="thread-999",
        message_id="msg-999",
        platform_specific={"is_dm": True},
    )

    assert handler.get_base_session_id(context) == "lark_thread-999"


def test_base_session_id_prefers_context_platform_over_primary_config() -> None:
    handler = SessionHandler(_Controller(platform="slack", dm_threads=False))
    context = MessageContext(
        user_id="u-1",
        channel_id="wx-123",
        platform="wechat",
        message_id="msg-42",
        platform_specific={"is_dm": False},
    )

    assert handler.get_base_session_id(context) == "wechat_msg-42"


def test_slack_dm_session_base_id_uses_thread_when_supported() -> None:
    handler = SessionHandler(_Controller(platform="slack", dm_threads=True))
    context = MessageContext(
        user_id="u-1",
        channel_id="D123",
        thread_id="171717.999",
        message_id="171717.111",
        platform_specific={"is_dm": True},
    )

    assert handler.get_base_session_id(context) == "slack_171717.999"


def test_channel_session_base_id_keeps_thread_or_message_behavior() -> None:
    handler = SessionHandler(_Controller())
    context = MessageContext(
        user_id="u-1",
        channel_id="chan-123",
        message_id="msg-999",
        platform_specific={"is_dm": False},
    )

    assert handler.get_base_session_id(context) == "discord_msg-999"


def test_telegram_plain_group_session_base_id_uses_stable_channel_id() -> None:
    handler = SessionHandler(_Controller(platform="telegram", channel_message_sessions=False))
    context = MessageContext(
        user_id="u-1",
        channel_id="-100123",
        message_id="42",
        platform="telegram",
        platform_specific={"is_dm": False, "chat_type": "supergroup"},
    )

    assert handler.get_base_session_id(context) == "telegram_-100123"


def test_telegram_general_topic_session_base_id_includes_chat_and_canonical_topic() -> None:
    handler = SessionHandler(_Controller(platform="telegram", channel_message_sessions=False))
    first = MessageContext(
        user_id="u-1",
        channel_id="-100123",
        message_id="42",
        platform="telegram",
        platform_specific={"is_dm": False, "is_forum": True, "is_topic_message": True},
    )
    follow_up = MessageContext(
        user_id="u-1",
        channel_id="-100123",
        message_id="43",
        platform="telegram",
        platform_specific={"is_dm": False, "is_forum": True, "is_topic_message": True},
    )

    other_forum = MessageContext(
        user_id="u-1",
        channel_id="-100999",
        message_id="42",
        platform="telegram",
        platform_specific={"is_dm": False, "is_forum": True, "is_topic_message": True},
    )

    assert handler.get_base_session_id(first) == "telegram_-100123_1"
    assert handler.get_base_session_id(follow_up) == "telegram_-100123_1"
    assert handler.get_base_session_id(other_forum) == "telegram_-100999_1"


def test_scheduled_channel_session_uses_provisional_anchor_on_threaded_surfaces() -> None:
    controller = _Controller(platform="slack", dm_threads=False)
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="scheduled",
        channel_id="C123",
        platform="slack",
        platform_specific={"is_dm": False, "turn_source": "scheduled"},
    )

    base_session_id = handler.get_base_session_id(context, source="scheduled")

    assert base_session_id.startswith("slack_scheduled-")


def test_scheduled_telegram_group_session_reuses_channel_scope() -> None:
    controller = _Controller(platform="telegram", dm_threads=False, channel_message_sessions=False)
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="scheduled",
        channel_id="-100123",
        platform="telegram",
        platform_specific={"is_dm": False, "chat_type": "supergroup", "turn_source": "scheduled"},
    )

    assert handler.get_base_session_id(context, source="scheduled") == "telegram_-100123"


def test_scheduled_dm_session_reuses_flat_session_scope() -> None:
    controller = _Controller(platform="discord", dm_threads=False)
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="u-1",
        channel_id="dm-123",
        platform="discord",
        platform_specific={"is_dm": True, "turn_source": "scheduled"},
    )

    assert handler.get_base_session_id(context, source="scheduled") == "discord_dm-123"


def test_finalize_scheduled_delivery_aliases_provisional_base_and_marks_thread() -> None:
    controller = _Controller(platform="slack", dm_threads=False)
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="scheduled",
        channel_id="C123",
        platform="slack",
        platform_specific={
            "is_dm": False,
            "turn_source": "scheduled",
            "turn_base_session_id": "slack_scheduled-abc",
            "delivery_override": {"channel_id": "C123"},
            "scheduled_delivery_alias": {
                "mode": "sent_message",
                "session_key": "slack::C123",
                "clear_source": True,
            },
        },
    )

    handler.finalize_scheduled_delivery(context, "171717.123")

    assert controller.sessions.alias_calls == [("slack::C123", "slack_scheduled-abc", "slack_171717.123")]
    assert controller.sessions.clear_calls == [("slack::C123", "slack_scheduled-abc")]
    assert controller.sessions.thread_marks == [("scheduled", "C123", "171717.123")]


def test_finalize_scheduled_delivery_can_alias_into_delivery_scope() -> None:
    controller = _Controller(platform="slack", dm_threads=False)
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="scheduled",
        channel_id="C123",
        platform="slack",
        thread_id="171717.123",
        platform_specific={
            "is_dm": False,
            "turn_source": "scheduled",
            "turn_base_session_id": "slack_171717.123",
            "scheduled_delivery_alias": {
                "mode": "sent_message",
                "session_key": "slack::C999",
                "clear_source": False,
            },
            "delivery_override": {"channel_id": "C999"},
        },
    )

    handler.finalize_scheduled_delivery(context, "181818.456")

    assert controller.sessions.alias_calls == []
    assert controller.sessions.cross_scope_alias_calls == [
        ("slack::C123", "slack::C999", "slack_171717.123", "slack_181818.456")
    ]
    assert controller.sessions.clear_calls == []
    assert controller.sessions.thread_marks == [("scheduled", "C999", "181818.456")]


def test_finalize_scheduled_delivery_never_clears_a_reserved_definition_anchor() -> None:
    """A ``--create-session`` definition Session survives its first visible delivery.

    Regression for the orphaning bug: ``clear_source`` is decided upstream from
    ``session_target.thread_id is None``, which a durable definition anchor also
    satisfies, and the clear is a HARD delete. Deleting the row leaves
    ``run_definitions.session_id`` dangling, so every later fire dies at dispatch.
    The alias must still happen; only the delete is refused.
    """
    controller = _Controller(platform="discord", dm_threads=False)
    handler = SessionHandler(controller)
    definition_anchor = "discord_C123:definition_ab12cd34ef56"
    context = MessageContext(
        user_id="scheduled",
        channel_id="C123",
        platform="discord",
        platform_specific={
            "is_dm": False,
            "turn_source": "scheduled",
            "turn_base_session_id": definition_anchor,
            "agent_session_target": {
                "id": "sess-durable-1",
                "session_anchor": definition_anchor,
            },
            "delivery_override": {"channel_id": "C123"},
            "scheduled_delivery_alias": {
                "mode": "sent_message",
                "session_key": "discord::C123",
                "clear_source": True,
            },
        },
    )

    handler.finalize_scheduled_delivery(context, "919191.777")

    assert controller.sessions.alias_calls == [
        ("discord::C123", definition_anchor, "discord_919191.777")
    ]
    assert controller.sessions.clear_calls == []


def test_finalize_scheduled_delivery_still_clears_a_throwaway_provisional_anchor() -> None:
    """The guard is not a blanket disable: an unbound provisional anchor still clears."""
    controller = _Controller(platform="discord", dm_threads=False)
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="scheduled",
        channel_id="C123",
        platform="discord",
        platform_specific={
            "is_dm": False,
            "turn_source": "scheduled",
            "turn_base_session_id": "discord_scheduled-8f2c",
            "delivery_override": {"channel_id": "C123"},
            "scheduled_delivery_alias": {
                "mode": "sent_message",
                "session_key": "discord::C123",
                "clear_source": True,
            },
        },
    )

    handler.finalize_scheduled_delivery(context, "929292.888")

    assert controller.sessions.clear_calls == [("discord::C123", "discord_scheduled-8f2c")]


def test_finalize_scheduled_delivery_clears_when_reserved_row_is_a_different_anchor() -> None:
    """Only the reserved row's own anchor is protected.

    A context may carry a reserved Session while the turn ran off a separate provisional
    anchor. Protecting that unrelated anchor would leak throwaway rows, so the guard
    compares anchors rather than merely observing that a reserved row exists.
    """
    controller = _Controller(platform="discord", dm_threads=False)
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="scheduled",
        channel_id="C123",
        platform="discord",
        platform_specific={
            "is_dm": False,
            "turn_source": "scheduled",
            "turn_base_session_id": "discord_scheduled-7a1b",
            "agent_session_target": {
                "id": "sess-durable-2",
                "session_anchor": "discord_C123:definition_ffeeddccbbaa",
            },
            "delivery_override": {"channel_id": "C123"},
            "scheduled_delivery_alias": {
                "mode": "sent_message",
                "session_key": "discord::C123",
                "clear_source": True,
            },
        },
    )

    handler.finalize_scheduled_delivery(context, "939393.999")

    assert controller.sessions.clear_calls == [("discord::C123", "discord_scheduled-7a1b")]


def test_alias_session_base_clears_source_even_when_alias_already_exists() -> None:
    controller = _Controller(platform="slack", dm_threads=False)
    controller.sessions.alias_result = False
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="scheduled",
        channel_id="C123",
        platform="slack",
        platform_specific={"is_dm": False},
    )

    changed = handler.alias_session_base(
        context,
        source_base_session_id="slack_scheduled-abc",
        alias_base_session_id="slack_171717.123",
        clear_source=True,
    )

    assert changed is True
    assert controller.sessions.alias_calls == [("slack::C123", "slack_scheduled-abc", "slack_171717.123")]
    assert controller.sessions.clear_calls == [("slack::C123", "slack_scheduled-abc")]


def test_claude_session_not_found_error_is_reported_without_cleanup() -> None:
    controller = _Controller(platform="slack", dm_threads=False)
    controller.im_client = _FakeIM()
    handler = SessionHandler(controller)
    cleanup_calls = []

    async def _cleanup_session(composite_key: str) -> None:
        cleanup_calls.append(composite_key)

    handler.cleanup_session = _cleanup_session
    context = MessageContext(user_id="U123", channel_id="C123", platform="slack")

    asyncio.run(
        handler.handle_session_error(
            "slack_C123:/tmp/other",
            context,
            ClaudeSessionNotFoundError(
                session_id="11111111-1111-1111-1111-111111111111",
                working_path="/tmp/other",
            ),
        )
    )

    assert cleanup_calls == []
    assert len(controller.im_client.sent_messages) == 1
    _, message = controller.im_client.sent_messages[0]
    assert message.startswith("ERR:Claude Code could not find the historical session")
    assert "11111111-1111-1111-1111-111111111111" in message
    assert "/tmp/other" in message


def test_claude_sdk_buffer_error_cleans_up_broken_session() -> None:
    controller = _Controller(platform="slack", dm_threads=False)
    controller.im_client = _FakeIM()
    handler = SessionHandler(controller)
    cleanup_calls = []

    async def _cleanup_session(
        composite_key: str,
        *,
        current_receiver_task=None,
        reason=None,
    ) -> None:
        cleanup_calls.append((composite_key, current_receiver_task, reason))

    handler.cleanup_session = _cleanup_session
    context = MessageContext(user_id="U123", channel_id="C123", platform="slack")

    asyncio.run(
        handler.handle_session_error(
            "slack_C123:/tmp/workdir",
            context,
            RuntimeError("Failed to decode JSON: JSON message exceeded maximum buffer size of 1048576 bytes"),
        )
    )

    assert len(cleanup_calls) == 1
    cleanup_key, cleanup_task, cleanup_reason = cleanup_calls[0]
    assert cleanup_key == "slack_C123:/tmp/workdir"
    assert cleanup_task is not None
    assert cleanup_reason == "connection_broken"
    assert len(controller.im_client.sent_messages) == 1
    _, message = controller.im_client.sent_messages[0]
    assert message == "ERR:Connection to Claude was lost. Please try your message again."


def test_claude_terminated_process_cleans_up_and_reports_signal_diagnostic() -> None:
    controller = _Controller(platform="slack", dm_threads=False)
    controller.im_client = _FakeIM()
    handler = SessionHandler(controller)
    composite_key = "slack_C123:/tmp/workdir"
    controller.claude_sessions[composite_key] = SimpleNamespace(
        _transport=SimpleNamespace(_process=SimpleNamespace(returncode=-6)),
        _vibe_stderr_lines=["fatal: Claude CLI aborted", "transport closed"],
    )
    cleanup_calls = []

    async def _cleanup_session(
        key: str,
        *,
        current_receiver_task=None,
        expected_client=None,
        reason=None,
    ) -> None:
        cleanup_calls.append((key, current_receiver_task, reason))

    handler.cleanup_session = _cleanup_session
    context = MessageContext(user_id="U123", channel_id="C123", platform="slack")

    asyncio.run(
        handler.handle_session_error(
            composite_key,
            context,
            RuntimeError("Cannot write to terminated process (exit code: -6)"),
        )
    )

    assert len(cleanup_calls) == 1
    assert cleanup_calls[0][0] == composite_key
    assert cleanup_calls[0][1] is not None
    assert cleanup_calls[0][2] == "process_terminated"
    _, message = controller.im_client.sent_messages[0]
    assert message == (
        "ERR:Claude Code process terminated (SIGABRT (signal 6)); "
        "the session was reset. Please try your message again."
    )
    diagnostic = handler.claude_error_diagnostic(
        composite_key,
        RuntimeError("Cannot write to terminated process (exit code: -6)"),
    )
    assert "Claude process terminated: SIGABRT (signal 6)" in diagnostic
    assert "Claude stderr tail:\nfatal: Claude CLI aborted\ntransport closed" in diagnostic


def test_service_initiated_teardown_signal_is_not_reported_as_session_error() -> None:
    """A SIGKILL the service issued itself must not read as a backend crash.

    Cleanup escalates SIGTERM to SIGKILL, so the SDK surfaces ``exit code -9``
    for a process Avibe terminated deliberately. Without this containment the
    failure falls through to the generic branch and the user is told the
    session failed for an unknown reason.
    """
    controller = _Controller(platform="slack", dm_threads=False)
    controller.im_client = _FakeIM()
    handler = SessionHandler(controller)
    composite_key = "slack_C123:/tmp/workdir"
    cleanup_calls = []

    async def _cleanup_session(
        key: str,
        *,
        current_receiver_task=None,
        expected_client=None,
        reason=None,
    ) -> None:
        cleanup_calls.append((key, reason))

    handler.cleanup_session = _cleanup_session
    handler._mark_claude_teardown_intentional(composite_key, None)
    context = MessageContext(user_id="U123", channel_id="C123", platform="slack")

    asyncio.run(
        handler.handle_session_error(
            composite_key,
            context,
            RuntimeError("Command failed with exit code -9"),
        )
    )

    assert cleanup_calls == [(composite_key, "intentional_teardown_signal")]
    assert controller.im_client.sent_messages == []


def test_teardown_intent_does_not_suppress_errors_from_the_next_generation() -> None:
    controller = _Controller(platform="slack", dm_threads=False)
    controller.im_client = _FakeIM()
    handler = SessionHandler(controller)
    composite_key = "slack_C123:/tmp/workdir"

    async def _cleanup_session(
        key: str,
        *,
        current_receiver_task=None,
        expected_client=None,
        reason=None,
    ) -> None:
        return None

    handler.cleanup_session = _cleanup_session
    handler._mark_claude_teardown_intentional(composite_key, None)
    # A replacement client took the key, so the previous teardown says nothing
    # about this failure.
    handler._clear_claude_teardown_intent(composite_key)
    context = MessageContext(user_id="U123", channel_id="C123", platform="slack")

    asyncio.run(
        handler.handle_session_error(
            composite_key,
            context,
            RuntimeError("Command failed with exit code -9"),
        )
    )

    assert len(controller.im_client.sent_messages) == 1
    _, message = controller.im_client.sent_messages[0]
    assert "Command failed with exit code -9" in message


def test_unrelated_error_during_teardown_window_is_still_reported() -> None:
    controller = _Controller(platform="slack", dm_threads=False)
    controller.im_client = _FakeIM()
    handler = SessionHandler(controller)
    composite_key = "slack_C123:/tmp/workdir"

    async def _cleanup_session(
        key: str,
        *,
        current_receiver_task=None,
        expected_client=None,
        reason=None,
    ) -> None:
        return None

    handler.cleanup_session = _cleanup_session
    handler._mark_claude_teardown_intentional(composite_key, None)
    context = MessageContext(user_id="U123", channel_id="C123", platform="slack")

    asyncio.run(
        handler.handle_session_error(
            composite_key,
            context,
            RuntimeError("Command failed with exit code 1"),
        )
    )

    assert len(controller.im_client.sent_messages) == 1


def test_client_teardown_marker_suppresses_signal_error_without_key_record() -> None:
    """A client the service marked for teardown is authoritative on its own.

    The per-key record is dropped when a replacement client registers, but the
    marked client object still identifies exactly which generation was killed
    deliberately.
    """
    controller = _Controller(platform="slack", dm_threads=False)
    controller.im_client = _FakeIM()
    handler = SessionHandler(controller)
    composite_key = "slack_C123:/tmp/workdir"
    client = SimpleNamespace(
        _transport=SimpleNamespace(_process=SimpleNamespace(returncode=-9)),
        _vibe_intentional_teardown=True,
    )
    controller.claude_sessions[composite_key] = client

    async def _cleanup_session(
        key: str,
        *,
        current_receiver_task=None,
        expected_client=None,
        reason=None,
    ) -> None:
        return None

    handler.cleanup_session = _cleanup_session
    handler._clear_claude_teardown_intent(composite_key)
    context = MessageContext(user_id="U123", channel_id="C123", platform="slack")

    asyncio.run(
        handler.handle_session_error(
            composite_key,
            context,
            RuntimeError("Command failed with exit code -9"),
        )
    )

    assert controller.im_client.sent_messages == []


def test_teardown_containment_matches_colon_delimited_exit_code() -> None:
    """The SDK reports write failures as ``(exit code: -9)``.

    Once cleanup has popped the client there is no returncode to read, so the
    message text is the only signal and both SDK spellings must match.
    """
    controller = _Controller(platform="slack", dm_threads=False)
    controller.im_client = _FakeIM()
    handler = SessionHandler(controller)
    composite_key = "slack_C123:/tmp/workdir"

    async def _cleanup_session(
        key: str,
        *,
        current_receiver_task=None,
        expected_client=None,
        reason=None,
    ) -> None:
        return None

    handler.cleanup_session = _cleanup_session
    handler._mark_claude_teardown_intentional(composite_key, None)
    context = MessageContext(user_id="U123", channel_id="C123", platform="slack")

    contained = asyncio.run(
        handler.handle_session_error(
            composite_key,
            context,
            RuntimeError("Cannot write to terminated process (exit code: -9)"),
        )
    )

    assert contained is True
    assert controller.im_client.sent_messages == []


def test_reported_session_errors_are_not_marked_contained() -> None:
    controller = _Controller(platform="slack", dm_threads=False)
    controller.im_client = _FakeIM()
    handler = SessionHandler(controller)
    context = MessageContext(user_id="U123", channel_id="C123", platform="slack")

    contained = asyncio.run(
        handler.handle_session_error(
            "slack_C123:/tmp/workdir",
            context,
            RuntimeError("boom"),
        )
    )

    assert contained is False
    assert len(controller.im_client.sent_messages) == 1


def test_old_generation_teardown_is_contained_after_a_replacement_registers() -> None:
    """The caller's own client decides, not whatever now holds the key.

    A query from the torn-down generation can reach the handler after a
    replacement has registered and cleared the key record. The old client was
    already popped from ``claude_sessions``, so re-reading the map here would
    classify the delayed ``-9`` against the healthy replacement and report a
    deliberate teardown as a genuine backend failure.
    """
    controller = _Controller(platform="slack", dm_threads=False)
    controller.im_client = _FakeIM()
    handler = SessionHandler(controller)
    composite_key = "slack_C123:/tmp/workdir"
    torn_down = SimpleNamespace(
        _transport=SimpleNamespace(_process=SimpleNamespace(returncode=-9)),
        _vibe_intentional_teardown=True,
    )
    replacement = SimpleNamespace(
        _transport=SimpleNamespace(_process=SimpleNamespace(returncode=None)),
    )
    controller.claude_sessions[composite_key] = replacement

    async def _cleanup_session(
        key: str,
        *,
        current_receiver_task=None,
        expected_client=None,
        reason=None,
    ) -> None:
        return None

    handler.cleanup_session = _cleanup_session
    handler._clear_claude_teardown_intent(composite_key)
    context = MessageContext(user_id="U123", channel_id="C123", platform="slack")

    contained = asyncio.run(
        handler.handle_session_error(
            composite_key,
            context,
            RuntimeError("Command failed with exit code -9"),
            client=torn_down,
        )
    )

    assert contained is True
    assert controller.im_client.sent_messages == []


def test_claude_teardown_is_intentional_probes_the_callers_client() -> None:
    """The public probe answers before any backend-health evidence is recorded."""
    controller = _Controller(platform="slack", dm_threads=False)
    controller.im_client = _FakeIM()
    handler = SessionHandler(controller)
    composite_key = "slack_C123:/tmp/workdir"
    torn_down = SimpleNamespace(
        _transport=SimpleNamespace(_process=SimpleNamespace(returncode=-9)),
        _vibe_intentional_teardown=True,
    )
    healthy = SimpleNamespace(_transport=SimpleNamespace(_process=SimpleNamespace(returncode=None)))
    controller.claude_sessions[composite_key] = healthy
    error = RuntimeError("Command failed with exit code -9")

    assert handler.claude_teardown_is_intentional(composite_key, error, client=torn_down) is True
    assert handler.claude_teardown_is_intentional(composite_key, error, client=healthy) is False
    # No client named and nothing marked: an ordinary failure, not a teardown.
    assert handler.claude_teardown_is_intentional(composite_key, error) is False
