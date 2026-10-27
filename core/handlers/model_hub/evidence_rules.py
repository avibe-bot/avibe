"""Narrow rules that learn negative capability evidence from real upstream errors.

``model-hub-capability-model.md`` §3.2: a rule maps an upstream error to exactly
one (Source, model, feature), and only when the error names that feature.
Generic errors (5xx, overload, rate limits, auth, any 400 that names nothing
the request sent) never create evidence. The rules read only the closed tokens
L1 put in ``UpstreamRejection`` and the request the gateway sent; they never
change how the request itself is classified or failed over.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from config.v2_config import ModelHubSourceConfig
from vibe.backend_model_catalog import OVERLAY_SEARCH_TOOLS

from .adapter import UpstreamRejection
from .capability_evidence import EvidenceFeature
from .request import declared_tool_types, replays_history_items

# Where each request protocol carries its reasoning effort on the wire.
_EFFORT_PATHS: Mapping[str, tuple[str, ...]] = {
    "openai_responses": ("reasoning", "effort"),
    "openai_chat": ("reasoning_effort",),
    "anthropic": ("output_config", "effort"),
}
_EFFORT_PARAMS = frozenset({"reasoning.effort", "reasoning_effort", "output_config.effort"})

# The hosted result an "Invalid encrypted_content in <kind> block" message names.
_ENCRYPTED_BLOCK_TOOLS: Mapping[str, str] = {"search_result": "web_search"}
# Request items that replay a hosted tool's native result, per tool.
_REPLAYED_RESULT_TYPES: Mapping[str, frozenset[str]] = {
    "web_search": frozenset({"web_search_tool_result", "web_search_call"}),
}

SpecLookup = Callable[[str, str, str, str], Any]


@dataclass(frozen=True)
class LearnedEvidence:
    feature: EvidenceFeature
    detail: str


def wire_effort(request: Mapping[str, Any], protocol: str) -> Optional[str]:
    """The reasoning effort this request sends, as the wire spells it: the token ``registry.sent_effort`` names."""

    if protocol == "google":
        from core.agent_core.ai.google import wire_effort as google_wire_effort

        return google_wire_effort(request)
    value: Any = request
    for key in _EFFORT_PATHS.get(protocol, ()):
        value = value.get(key) if isinstance(value, Mapping) else None
    return value if isinstance(value, str) and value else None


def _detail(rejection: UpstreamRejection) -> str:
    return f"{rejection.status} {rejection.code or rejection.error_type or 'error'}"[:64]


def learn(
    request: Mapping[str, Any],
    rejection: UpstreamRejection,
    *,
    request_protocol: str,
    source: ModelHubSourceConfig,
    model_id: str,
    spec_for: SpecLookup,
) -> Optional[LearnedEvidence]:
    """The one feature this rejection names for this request, if any."""

    effort = EvidenceFeature.effort_or_none(wire_effort(request, request_protocol))
    names_effort_field = rejection.param is None or rejection.param in _EFFORT_PARAMS
    if effort is not None and names_effort_field and (
        rejection.invalid_value == effort.name
        or (
            rejection.invalid_value is None
            and rejection.param in _EFFORT_PARAMS
            and request_protocol == source.protocol
        )
    ):
        return LearnedEvidence(effort, _detail(rejection))

    rejected_type = rejection.unsupported_tool_type
    if rejected_type is not None and rejected_type in declared_tool_types(request):
        for tool in OVERLAY_SEARCH_TOOLS:
            spec = spec_for(source.vendor, source.protocol, model_id, tool)
            if spec is not None and spec.declaration_type == rejected_type:
                return LearnedEvidence(EvidenceFeature.hosted(tool, "call"), _detail(rejection))

    tool = _ENCRYPTED_BLOCK_TOOLS.get(rejection.invalid_encrypted_content_block or "")
    if tool is not None and replays_history_items(request, _REPLAYED_RESULT_TYPES[tool]):
        return LearnedEvidence(EvidenceFeature.hosted(tool, "replay"), _detail(rejection))
    return None
