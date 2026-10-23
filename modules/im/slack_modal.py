"""Slack-specific modal parsing helpers."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from core.modals import DEFAULT_OPTION_VALUE, RouteDraft, RoutingModalSelection


def _selected_value(block: object) -> Optional[str]:
    if not isinstance(block, dict):
        return None
    for action_data in block.values():
        if isinstance(action_data, dict):
            value = (action_data.get("selected_option") or {}).get("value")
            if isinstance(value, str):
                return value
    return None


def _choice(value: Optional[str]) -> Optional[str]:
    return None if value in (None, DEFAULT_OPTION_VALUE) else value


def parse_routing_modal_selection(*, view: dict, action: dict) -> RoutingModalSelection:
    """Parse the routing modal's state into the shown backend and its route choices.

    Route blocks are ``route_<field>:<backend>[:...]`` (see ``SlackBot._route_section_blocks``). The draft is
    ``None`` when the section on screen is not the selected backend's, as right after a backend switch.
    """
    values = view.get("state", {}).get("values", {})
    selected_backend = _selected_value(values.get("backend_block")) or ""
    fields: dict[str, Optional[str]] = {}
    section_backend = None
    for block_id, block in values.items():
        if not isinstance(block_id, str) or not block_id.startswith("route_"):
            continue
        field, _, rest = block_id.partition(":")
        section_backend = rest.split(":", 1)[0]
        fields[field] = _choice(_selected_value(block))

    block_id = str(action.get("block_id") or "")
    value = (action.get("selected_option") or {}).get("value") if isinstance(action, dict) else None
    if action.get("action_id") == "backend_select" and isinstance(value, str):
        selected_backend = value
    if not selected_backend or section_backend != selected_backend:
        return RoutingModalSelection(selected_backend=selected_backend)

    draft = RouteDraft(
        variant=fields.get("route_variant"),
        model=fields.get("route_model"),
        reasoning_effort=fields.get("route_effort"),
    )
    if block_id.startswith("route_model:") and isinstance(value, str):
        # The state already holds the new model, so the model shown before the change is the one the view was
        # rendered with; only a different model clears the effort.
        shown = next(
            (
                (block.get("element") or {}).get("initial_option", {}).get("value")
                for block in view.get("blocks", [])
                if str(block.get("block_id") or "").startswith("route_model:")
            ),
            None,
        )
        draft = replace(draft, model=_choice(shown)).with_model(_choice(value))
    return RoutingModalSelection(selected_backend=selected_backend, draft=draft)
