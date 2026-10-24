"""The one rule for which Claude models take adaptive thinking and which take a token budget (#2737)."""

from __future__ import annotations

import pytest

from core.agent_core.ai.claude_thinking import is_claude_model, uses_adaptive_thinking


@pytest.mark.parametrize(("model_id", "adaptive"), [
    # Claude 4.5 and older take a token budget, whatever the ID's date, separator or relay prefix.
    ("claude-sonnet-4-5-20250929", False),
    ("claude-haiku-4-5", False),
    ("claude-opus-4.5", False),
    ("claude-opus-4-1-20250805", False),
    ("claude-opus-4-20250514", False),
    ("claude-3-5-haiku-20241022", False),
    ("claude-3-opus-20240229", False),
    ("anthropic/claude-sonnet-4-5", False),
    # Claude 4.6 and later take adaptive thinking.
    ("claude-sonnet-4-6", True),
    ("claude-opus-4-7", True),
    ("claude-opus-5-5", True),
    ("claude-fable-5-1", True),
    ("claude-opus-4-6-thinking", True),
    # A Claude ID that names no version is a later release or an alias: adaptive, not guessed legacy.
    ("claude-latest", True),
    # Not a Claude model.
    ("kimi-k2", False),
])
def test_only_a_claude_version_before_4_6_takes_a_token_budget(model_id, adaptive):
    assert uses_adaptive_thinking(model_id) is adaptive


def test_claude_model_detection_uses_the_model_name_not_the_protocol() -> None:
    assert is_claude_model("claude-sonnet-4-5") is True
    assert is_claude_model("anthropic/claude-sonnet-4-5") is True
    assert is_claude_model("grok-4.7") is False
    assert is_claude_model("relay-claude-opus-5-5") is False
