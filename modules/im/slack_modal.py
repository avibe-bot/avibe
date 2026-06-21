"""Slack-specific modal parsing helpers."""

from __future__ import annotations

from typing import Optional

from core.modals import RoutingModalSelection


def _extract_selected_value(values: dict, block_id: str, action_id: str) -> Optional[str]:
    data = values.get(block_id, {}).get(action_id, {})
    return data.get("selected_option", {}).get("value")


def _extract_prefixed_selected_value(values: dict, block_id: str, action_prefix: str) -> Optional[str]:
    block = values.get(block_id, {})
    if not isinstance(block, dict):
        return None
    for dynamic_action_id, action_data in block.items():
        if (
            isinstance(dynamic_action_id, str)
            and dynamic_action_id.startswith(action_prefix)
            and isinstance(action_data, dict)
        ):
            return action_data.get("selected_option", {}).get("value")
    return None


def _normalize_default(value: Optional[str]) -> Optional[str]:
    if value == "__default__":
        return None
    return value


def parse_routing_modal_selection(
    *,
    view: dict,
    action: dict,
    fallback_selected_backend: str,
) -> RoutingModalSelection:
    """Parse Slack routing modal state into normalized selection model."""
    values = view.get("state", {}).get("values", {})

    selected_backend = _extract_selected_value(values, "backend_block", "backend_select") or fallback_selected_backend
    selected_action_id = action.get("action_id")

    selected_value = None
    selected_option = action.get("selected_option")
    if isinstance(selected_option, dict):
        selected_value = selected_option.get("value")

    oc_agent = _extract_selected_value(values, "opencode_agent_block", "opencode_agent_select")
    oc_model = _extract_selected_value(values, "opencode_model_block", "opencode_model_select")
    oc_reasoning = _extract_prefixed_selected_value(values, "opencode_reasoning_block", "opencode_reasoning_select")

    claude_agent = _extract_selected_value(values, "claude_agent_block", "claude_agent_select")
    claude_model = _extract_selected_value(values, "claude_model_block", "claude_model_select")
    claude_reasoning = _extract_selected_value(values, "claude_reasoning_block", "claude_reasoning_select")
    codex_agent = _extract_selected_value(values, "codex_agent_block", "codex_agent_select")
    codex_model = _extract_selected_value(values, "codex_model_block", "codex_model_select")
    codex_reasoning = _extract_prefixed_selected_value(values, "codex_reasoning_block", "codex_reasoning_select")

    if isinstance(selected_action_id, str) and isinstance(selected_value, str):
        if selected_action_id == "opencode_agent_select":
            oc_agent = selected_value
        elif selected_action_id == "opencode_model_select":
            oc_model = selected_value
        elif selected_action_id.startswith("opencode_reasoning_select"):
            oc_reasoning = selected_value
        elif selected_action_id == "claude_agent_select":
            claude_agent = selected_value
        elif selected_action_id == "claude_model_select":
            claude_model = selected_value
        elif selected_action_id == "claude_reasoning_select":
            claude_reasoning = selected_value
        elif selected_action_id == "codex_agent_select":
            codex_agent = selected_value
        elif selected_action_id == "codex_model_select":
            codex_model = selected_value
        elif selected_action_id.startswith("codex_reasoning_select"):
            codex_reasoning = selected_value

    return RoutingModalSelection(
        selected_backend=selected_backend,
        selected_opencode_agent=_normalize_default(oc_agent),
        selected_opencode_model=_normalize_default(oc_model),
        selected_opencode_reasoning=_normalize_default(oc_reasoning),
        selected_claude_agent=_normalize_default(claude_agent),
        selected_claude_model=_normalize_default(claude_model),
        selected_claude_reasoning=_normalize_default(claude_reasoning),
        selected_codex_agent=_normalize_default(codex_agent),
        selected_codex_model=_normalize_default(codex_model),
        selected_codex_reasoning=_normalize_default(codex_reasoning),
    )
