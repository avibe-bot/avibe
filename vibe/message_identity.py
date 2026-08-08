"""Lightweight message authorship and input-turn semantics."""

from __future__ import annotations

from typing import Optional

from vibe.message_types import input_author_type_pairs

HARNESS_TYPE = "harness"
INPUT_TURN_AUTHOR_TYPES = input_author_type_pairs()


def is_input_turn(author: Optional[str], message_type: Optional[str]) -> bool:
    """Return whether a transcript row starts human or harness agent work."""

    return (author, message_type) in INPUT_TURN_AUTHOR_TYPES
