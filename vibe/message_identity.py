"""Lightweight message authorship and input-turn semantics."""

from __future__ import annotations

from typing import Optional

HARNESS_TYPE = "harness"
INPUT_TURN_AUTHOR_TYPES = (("user", "user"), ("harness", HARNESS_TYPE))


def is_input_turn(author: Optional[str], message_type: Optional[str]) -> bool:
    """Return whether a transcript row starts human or harness agent work."""

    return (author, message_type) in INPUT_TURN_AUTHOR_TYPES
