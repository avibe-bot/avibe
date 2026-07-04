"""`$<NAME>` dynamic-ask extraction in core/reply_enhancer.py (P0 commit 5)."""

from __future__ import annotations

from core.reply_enhancer import process_reply


def _names(text: str) -> list[str]:
    return [r.name for r in process_reply(text).secret_requests]


def test_extracts_marker():
    assert _names("I need $<openAiKey> to continue.") == ["openAiKey"]


def test_marker_stays_in_text_for_frontend():
    # The marker is NOT stripped — the web transcript renders it as a card.
    reply = process_reply("Please provide $<STRIPE_KEY> now.")
    assert "$<STRIPE_KEY>" in reply.text


def test_dedupes_and_orders():
    assert _names("$<A_KEY> then $<B_KEY> then $<A_KEY>") == ["A_KEY", "B_KEY"]


def test_ignored_inside_inline_code():
    assert _names("use `$<NOT_A_REQUEST>` in your script") == []


def test_ignored_inside_fenced_code():
    text = "Example:\n```\nvibe vault request $<NOT_REAL>\n```\nbut I need $<REAL_KEY>."
    assert _names(text) == ["REAL_KEY"]


def test_invalid_names_not_matched():
    # Leading digits and dashes are not shell-style secret names.
    assert _names("$<lower> $<1LEAD> $<has-dash>") == ["lower"]


def test_none_when_absent():
    assert _names("just a normal reply with no markers") == []
