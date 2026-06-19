from core.message_context import build_context_session_key, resolve_context_settings_key
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
