from core.message_context import (
    build_context_session_key,
    build_thread_session_anchor,
    resolve_context_settings_key,
)
from modules.im import MessageContext


def test_chat_id_equals_user_id_dm_gets_typed_user_session_key():
    context = MessageContext(
        user_id="58181121",
        channel_id="58181121",
        platform="telegram",
        platform_specific={"platform": "telegram", "is_dm": True},
    )

    assert resolve_context_settings_key(context) == "58181121"
    assert build_context_session_key(context) == "telegram::user::58181121"


def test_distinct_dm_channel_keeps_legacy_session_key():
    context = MessageContext(
        user_id="U123",
        channel_id="D456",
        platform="slack",
        platform_specific={"platform": "slack", "is_dm": True},
    )

    assert resolve_context_settings_key(context) == "U123"
    assert build_context_session_key(context, settings_key="U123") == "slack::U123"


def test_telegram_thread_anchor_includes_chat_id():
    assert build_thread_session_anchor("telegram", "-100123", "42") == "telegram_-100123_42"
    assert build_thread_session_anchor("telegram", "-100456", "42") == "telegram_-100456_42"


def test_non_telegram_thread_anchor_keeps_existing_shape():
    assert build_thread_session_anchor("slack", "C123", "171717.999") == "slack_171717.999"
