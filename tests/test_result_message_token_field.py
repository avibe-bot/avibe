from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.im.formatters.slack_formatter import SlackFormatter


def test_result_message_uses_status_emoji_and_token_field() -> None:
    formatter = SlackFormatter()

    rendered = formatter.format_result_message(
        "success",
        duration_ms=144_000,
        result=None,
        show_duration=True,
        token_field="240k tok",
    )

    assert rendered == "✅ ⏱️ 2m 24s · 🪙 240k tok"


def test_result_message_omits_token_field_when_empty() -> None:
    formatter = SlackFormatter()

    rendered = formatter.format_result_message(
        "success",
        duration_ms=5_000,
        result=None,
        show_duration=True,
        token_field="",
    )

    assert rendered == "✅ ⏱️ 5s"


def test_result_message_shows_token_field_without_duration() -> None:
    formatter = SlackFormatter()

    rendered = formatter.format_result_message(
        "success",
        duration_ms=0,
        result=None,
        show_duration=True,
        token_field="12.3k tok",
    )

    assert rendered == "✅ 🪙 12.3k tok"


def test_result_message_bare_marker_when_nothing_else() -> None:
    formatter = SlackFormatter()

    rendered = formatter.format_result_message(
        "success",
        duration_ms=0,
        result=None,
        show_duration=False,
        token_field="",
    )

    assert rendered == "✅"


def test_result_message_error_and_warning_markers() -> None:
    formatter = SlackFormatter()

    error = formatter.format_result_message(
        "error_max_turns", duration_ms=5_000, show_duration=True
    )
    warning = formatter.format_result_message(
        "warning", duration_ms=5_000, show_duration=True
    )

    assert error == "❌ ⏱️ 5s"
    assert warning == "⚠️ ⏱️ 5s"


def test_result_message_token_field_precedes_result_body() -> None:
    formatter = SlackFormatter()

    rendered = formatter.format_result_message(
        "success",
        duration_ms=144_000,
        result="all done",
        show_duration=True,
        token_field="240k tok",
    )

    assert rendered == "✅ ⏱️ 2m 24s · 🪙 240k tok\n\nall done"
