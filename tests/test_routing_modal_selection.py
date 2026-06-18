from modules.im.slack_modal import parse_routing_modal_selection


def test_parse_routing_modal_selection_uses_action_override():
    view = {
        "state": {
            "values": {
                "backend_block": {"backend_select": {"selected_option": {"value": "opencode"}}},
                "opencode_model_block": {"opencode_model_select": {"selected_option": {"value": "m1"}}},
            }
        }
    }
    action = {"action_id": "opencode_model_select", "selected_option": {"value": "m2"}}

    selection = parse_routing_modal_selection(view=view, action=action, fallback_selected_backend="claude")

    assert selection.selected_backend == "opencode"
    assert selection.selected_opencode_model == "m2"


def test_parse_routing_modal_selection_normalizes_default_values():
    view = {
        "state": {
            "values": {
                "codex_agent_block": {"codex_agent_select": {"selected_option": {"value": "__default__"}}},
                "claude_model_block": {"claude_model_select": {"selected_option": {"value": "__default__"}}},
                "claude_reasoning_block": {"claude_reasoning_select": {"selected_option": {"value": "__default__"}}},
                "codex_reasoning_block": {"codex_reasoning_select_1": {"selected_option": {"value": "__default__"}}},
            }
        }
    }

    selection = parse_routing_modal_selection(view=view, action={}, fallback_selected_backend="claude")

    assert selection.selected_backend == "claude"
    assert selection.selected_codex_agent is None
    assert selection.selected_claude_model is None
    assert selection.selected_claude_reasoning is None
    assert selection.selected_codex_reasoning is None


def test_parse_routing_modal_selection_applies_codex_action_override():
    view = {
        "state": {
            "values": {
                "backend_block": {"backend_select": {"selected_option": {"value": "codex"}}},
                "codex_agent_block": {"codex_agent_select": {"selected_option": {"value": "__default__"}}},
                "codex_model_block": {"codex_model_select": {"selected_option": {"value": "gpt-5.4-mini"}}},
            }
        }
    }
    action = {"action_id": "codex_agent_select", "selected_option": {"value": "reviewer"}}

    selection = parse_routing_modal_selection(view=view, action=action, fallback_selected_backend="claude")

    assert selection.selected_backend == "codex"
    assert selection.selected_codex_agent == "reviewer"
    assert selection.selected_codex_model == "gpt-5.4-mini"
