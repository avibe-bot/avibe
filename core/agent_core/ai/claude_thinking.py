"""Which Claude models take adaptive thinking, and which take a token budget.

Adaptive thinking (``thinking: {type: "adaptive"}`` with ``output_config.effort``)
starts with the Claude 4.6 families; Claude 4.5 and older take only a token budget
(``thinking: {type: "enabled", budget_tokens}``) and refuse ``adaptive``. Vibey's
Anthropic provider and the Model Hub engine registration both decide by this one
rule, so a model is never sent one kind by the agent and rewritten to the other on
the way (#2737).
"""

from __future__ import annotations

from typing import Final

_ADAPTIVE_THINKING_FAMILIES: Final = (
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-sonnet-5",
    "claude-opus-5",
    "claude-fable-5",
    "claude-mythos-5",
    "claude-haiku-5",
)


def uses_adaptive_thinking(model_id: str) -> bool:
    normalized = model_id.lower().replace(".", "-")
    return any(family in normalized for family in _ADAPTIVE_THINKING_FAMILIES)
