from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.agent_input import AgentInputMetadata, without_legacy_metadata
from vibe.i18n import get_supported_languages, get_translator


def test_clock_and_switches_are_evaluated_for_each_native_input():
    metadata = AgentInputMetadata(user_id="U1", user_name="Alex")
    config = SimpleNamespace(include_time_info=True, include_user_info=True)
    original = "hello\n```text\n[Now: literal user content]\n```"
    first = datetime(2026, 9, 6, 11, tzinfo=timezone(timedelta(hours=8)))
    second = first + timedelta(minutes=10)
    with patch("core.agent_input.datetime") as clock:
        clock.now.return_value.astimezone.return_value = first
        assert metadata.render(original, config) == f"[Now: 2026-09-06 11:00:00 UTC+08:00]\n[Alex<U1>]\n{original}"
        clock.now.return_value.astimezone.return_value = second
        assert metadata.render(original, config) == f"[Now: 2026-09-06 11:10:00 UTC+08:00]\n[Alex<U1>]\n{original}"
        config.include_time_info = False
        config.include_user_info = False
        assert metadata.render(original, config) == original
    assert metadata == AgentInputMetadata(user_id="U1", user_name="Alex")


@pytest.mark.parametrize("include_time", [False, True])
@pytest.mark.parametrize("include_user", [False, True])
def test_switches_do_not_hide_harness_source(include_time, include_user):
    config = SimpleNamespace(include_time_info=include_time, include_user_info=include_user)
    metadata = AgentInputMetadata(source_session_id="ses-source")
    rendered = metadata.render("work", config, now=datetime(2026, 9, 6, tzinfo=timezone.utc))
    assert rendered.endswith("From: #ses-source\nwork")
    assert ("[Now:" in rendered) is include_time
    assert "<" not in rendered


@pytest.mark.parametrize("time_prefix", ["", "[Current Time: 2026-08-02 11:00:00 UTC+08:00]\n"])
@pytest.mark.parametrize("identity_prefix", ["", "[Alex<U1>]\n"])
@pytest.mark.parametrize("suffix", ["", "\n\n[Attachment Download Errors]\nreport.pdf unavailable"])
def test_legacy_prefix_removal_requires_the_original_body(time_prefix, identity_prefix, suffix):
    original = "hello\n[Current Time: this is the user's own example]"
    decorated = time_prefix + identity_prefix + original + suffix
    assert without_legacy_metadata(decorated, original=original, user_id="U1") == original + suffix
    assert without_legacy_metadata(decorated, original=decorated, user_id="U1") == decorated


def test_legacy_metadata_never_strips_a_different_body_or_sender():
    decorated = "[Current Time: 2026-08-02 11:00:00 UTC+08:00]\n[Alex<U1>]\nhello"
    assert without_legacy_metadata(decorated, original="other", user_id="U1") == decorated
    assert without_legacy_metadata(decorated, original="hello", user_id="U2") == decorated


@pytest.mark.parametrize("language", get_supported_languages())
@pytest.mark.parametrize("original", ["", " \t\n", "hello", "[Attachment Download Errors]\n- user example"])
@pytest.mark.parametrize("prefix", [
    "[Current Time: 2026-08-02 11:00:00 UTC+08:00]\n",
    "[Alex<U1>]\n",
    "[Current Time: 2026-08-02 11:00:00 UTC+08:00]\n[Alex<U1>]\n",
])
@pytest.mark.parametrize("append_first", [False, True])
def test_legacy_attachment_blocks_match_the_released_producer(language, original, prefix, append_first):
    from core.handlers.message_handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler._t = get_translator(language)
    errors = ["report.pdf unavailable", "image.png unavailable"]
    if append_first:
        body = handler._append_attachment_errors(original, errors)
        decorated = prefix + body
    else:
        decorated = handler._append_attachment_errors(prefix + original, errors)
        body = decorated[len(prefix):]
    assert without_legacy_metadata(decorated, original=original, user_id="U1") == body
    assert without_legacy_metadata(decorated, original=decorated, user_id="U1") == decorated


def test_legacy_attachment_matching_does_not_accept_arbitrary_bracketed_suffixes():
    decorated = "[Alex<U1>]\nhello\n\n[Another block]\n- not a released error"
    assert without_legacy_metadata(decorated, original="hello", user_id="U1") == decorated
