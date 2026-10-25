"""Which Claude models take adaptive thinking, and which take a token budget.

Adaptive thinking (``thinking: {type: "adaptive"}`` with ``output_config.effort``)
starts with Claude 4.6; Claude 4.5 and older take only a token budget
(``thinking: {type: "enabled", budget_tokens}``) and refuse ``adaptive``. Vibey's
Anthropic provider and the Model Hub engine registration both decide by this one
rule, so a model is never sent one kind by the agent and rewritten to the other on
the way (#2737).

The budget-only models are a closed set, every Claude release since 4.6 takes
adaptive thinking, so they are named positively by version. A Claude ID that names
no version (an alias such as ``claude-latest``, or a family released later) is
taken as adaptive.
"""

from __future__ import annotations

import re
from typing import Final

# A Claude version in a model ID: ``claude-sonnet-4-5-20250929``, ``claude-3-5-haiku``, ``claude-opus-4``.
_CLAUDE_VERSION: Final = re.compile(r"claude-(?:[a-z]+-)?(\d+)(?:-(\d{1,2}))?(?![0-9])")
_FIRST_ADAPTIVE_VERSION: Final = (4, 6)


def is_claude_model(model_id: str) -> bool:
    """Whether the model ID's final path segment names a Claude model."""

    normalized = model_id.lower().replace(".", "-")
    return normalized.rsplit("/", 1)[-1].startswith("claude-")


def uses_adaptive_thinking(model_id: str) -> bool:
    """Whether this Claude model takes adaptive thinking; False for a Claude version before 4.6 and for non-Claude."""

    normalized = model_id.lower().replace(".", "-")
    if "claude" not in normalized:
        return False
    version = _CLAUDE_VERSION.search(normalized)
    if version is None:
        return True
    return (int(version.group(1)), int(version.group(2) or 0)) >= _FIRST_ADAPTIVE_VERSION
