from __future__ import annotations

import pytest

from core.memory.types import RecallPolicy


def test_non_agentic_policy_defaults_only_an_omitted_limit() -> None:
    assert RecallPolicy.from_payload({}).max_results == 8
    with pytest.raises(ValueError):
        RecallPolicy.from_payload({"mode": "keyword", "max_results": None})


def test_policy_matches_everos_result_limit() -> None:
    assert RecallPolicy(mode="hybrid", max_results=100).max_results == 100
    with pytest.raises(ValueError):
        RecallPolicy(mode="hybrid", max_results=101)


@pytest.mark.parametrize(
    "field",
    ["timeout_seconds", "max_model_calls", "cost_budget_tokens"],
)
def test_non_agentic_policy_rejects_agentic_budget_fields(field: str) -> None:
    with pytest.raises(ValueError):
        RecallPolicy.from_payload({"mode": "hybrid", field: None})


def test_agentic_policy_requires_all_explicit_positive_budgets() -> None:
    payload = {
        "mode": "agentic",
        "max_results": 12,
        "timeout_seconds": 10,
        "max_model_calls": 2,
        "cost_budget_tokens": 8_000,
    }

    policy = RecallPolicy.from_payload(payload)

    assert policy.payload() == {
        **payload,
        "include_profile": True,
        "include_current_session": False,
    }
    for field in (
        "max_results",
        "timeout_seconds",
        "max_model_calls",
        "cost_budget_tokens",
    ):
        incomplete = dict(payload)
        incomplete.pop(field)
        with pytest.raises(ValueError):
            RecallPolicy.from_payload(incomplete)


def test_non_agentic_wire_payload_omits_unsupported_budget_fields() -> None:
    assert RecallPolicy(mode="keyword").payload() == {
        "mode": "keyword",
        "max_results": 8,
        "include_profile": True,
        "include_current_session": False,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "agentic", "max_results": 0, "timeout_seconds": 1, "max_model_calls": 1, "cost_budget_tokens": 1},
        {"mode": "agentic", "max_results": 1, "timeout_seconds": 0, "max_model_calls": 1, "cost_budget_tokens": 1},
        {"mode": "agentic", "max_results": 1, "timeout_seconds": 1, "max_model_calls": 0, "cost_budget_tokens": 1},
        {"mode": "agentic", "max_results": 1, "timeout_seconds": 1, "max_model_calls": 1, "cost_budget_tokens": 0},
    ],
)
def test_agentic_policy_rejects_zero_budget(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RecallPolicy.from_payload(payload)


def test_policy_rejects_unknown_wire_fields() -> None:
    with pytest.raises(ValueError):
        RecallPolicy.from_payload({"mode": "keyword", "filters": {}})
