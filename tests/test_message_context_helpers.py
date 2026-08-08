from core.message_context import (
    build_context_session_key,
    build_context_turn_sink_key,
    build_thread_session_anchor,
    build_thread_session_anchor_candidates,
    resolve_context_settings_key,
    thread_id_from_session_anchor,
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


def test_telegram_thread_anchor_candidates_include_legacy_shape():
    assert build_thread_session_anchor_candidates("telegram", "-100123", "42") == (
        "telegram_-100123_42",
        "telegram_42",
    )
    assert build_thread_session_anchor_candidates("slack", "C123", "171717.999") == (
        "slack_171717.999",
    )


def _telegram_topic(thread_id):
    return MessageContext(
        user_id="U1",
        channel_id="-1004266799216",
        platform="telegram",
        thread_id=thread_id,
        platform_specific={"platform": "telegram", "is_forum": True},
    )


def test_turn_sink_key_separates_forum_topics_that_share_a_session_key():
    """Two topics of one group share a session key but must NOT share a sink slot.

    The sink is a turn-concurrency slot: whoever holds it makes every other turn on
    the same key settle ``refused_concurrent_turn`` before reaching a backend. The
    channel-scoped session key made that refusal group-wide.
    """
    topic_843 = _telegram_topic("843")
    topic_847 = _telegram_topic("847")

    assert build_context_session_key(topic_843) == build_context_session_key(topic_847)
    assert build_context_turn_sink_key(topic_843) != build_context_turn_sink_key(topic_847)
    assert build_context_turn_sink_key(topic_847) == "telegram::-1004266799216::thread::847"


def test_turn_sink_key_is_stable_across_a_threads_turns():
    """The backend receiver resolves the sink from a stale per-turn context, so the
    key must depend only on the routing scope — never on per-turn state."""
    first = _telegram_topic("847")
    first.platform_specific = {**(first.platform_specific or {}), "turn_token": "tok-1"}
    second = _telegram_topic("847")
    second.platform_specific = {**(second.platform_specific or {}), "turn_token": "tok-2"}

    assert build_context_turn_sink_key(first) == build_context_turn_sink_key(second)


def test_turn_sink_key_scopes_non_telegram_threads_too():
    """``resolve_context_thread_id`` returns None off Telegram, so the fallback to
    ``context.thread_id`` is what keeps Slack/Discord threads independent."""
    thread_a = MessageContext(
        user_id="U1", channel_id="C1", platform="slack", thread_id="171717.111"
    )
    thread_b = MessageContext(
        user_id="U1", channel_id="C1", platform="slack", thread_id="171717.222"
    )

    assert build_context_turn_sink_key(thread_a) == "slack::C1::thread::171717.111"
    assert build_context_turn_sink_key(thread_a) != build_context_turn_sink_key(thread_b)


def test_turn_sink_key_falls_back_to_session_key_without_a_thread():
    """A channel-level (unthreaded) context keeps today's key verbatim."""
    context = MessageContext(user_id="U1", channel_id="C1", platform="slack")

    assert build_context_turn_sink_key(context) == build_context_session_key(context)


def test_thread_id_from_session_anchor_accepts_canonical_and_legacy_shapes():
    assert (
        thread_id_from_session_anchor(
            "telegram_-100123_42:runtime_abc",
            platform="telegram",
            channel_id="-100123",
        )
        == "42"
    )
    assert thread_id_from_session_anchor("telegram_42", platform="telegram", channel_id="-100123") == "42"
    assert thread_id_from_session_anchor("telegram_-100123", platform="telegram", channel_id="-100123") is None
