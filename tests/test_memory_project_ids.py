from __future__ import annotations

import pytest

from vibe.memory_project_ids import (
    DEFAULT_MEMORY_PROJECT_ID,
    MEMORY_SEARCH_ALL_PROJECTS,
    is_legacy_memory_project_id,
    is_named_memory_project_id,
    is_new_stored_memory_project_id,
    is_persisted_memory_project_id,
    is_writable_memory_project_id,
    omitted_project_to_default,
    parse_agent_search_project,
    parse_ui_search_project,
    parse_writable_memory_project,
)

LEGACY = "p-" + "a" * 32


@pytest.mark.parametrize(
    ("value", "writable", "agent_search", "ui_search", "persisted"),
    [
        (None, False, False, False, False),
        ("", False, False, False, False),
        ("   ", False, False, False, False),
        ("default", True, True, True, True),
        ("Default", False, False, False, False),
        ("billing", True, True, True, True),
        ("all", False, False, True, False),
        ("personal", False, False, False, False),
        (LEGACY, False, False, False, True),
        ("p-deadbeef", False, False, False, False),
        ("u-" + "1" * 32, False, False, False, False),
        ("BILLING", False, False, False, False),
    ],
)
def test_project_id_matrix(
    value: object,
    writable: bool,
    agent_search: bool,
    ui_search: bool,
    persisted: bool,
) -> None:
    assert is_writable_memory_project_id(value) is writable
    assert is_new_stored_memory_project_id(value) is agent_search
    assert (value == MEMORY_SEARCH_ALL_PROJECTS or is_new_stored_memory_project_id(value)) is ui_search
    assert is_persisted_memory_project_id(value) is persisted
    assert is_legacy_memory_project_id(value) is (value == LEGACY)


def test_omitted_project_becomes_default() -> None:
    assert omitted_project_to_default(None) == DEFAULT_MEMORY_PROJECT_ID
    assert omitted_project_to_default("billing") == "billing"


def test_parsers_reject_empty_and_mixed_case() -> None:
    """Scenario: MEMORY-SEARCH-002"""
    for parser in (
        parse_writable_memory_project,
        parse_agent_search_project,
        parse_ui_search_project,
    ):
        with pytest.raises(ValueError):
            parser("")
        with pytest.raises(ValueError):
            parser("Default")
        with pytest.raises(ValueError):
            parser(LEGACY)


def test_named_slug_rejects_p_prefix() -> None:
    assert is_named_memory_project_id("pricing") is True
    assert is_named_memory_project_id("p-foo") is False


def test_prompt_exclusions_are_rejected_by_parsers() -> None:
    """Scenario: MEMORY-SEARCH-004. Prompt examples must fail the shared parser."""

    from core.prompt_registry import prompt_text

    memory_prompt = prompt_text("memory-context-prompt")
    assert "cannot be `all`, `personal`" in memory_prompt
    assert "start with `p-` / `u-`" in memory_prompt
    for value in ("all", "personal", "p-deadbeef", "u-" + "1" * 32, "Billing", ""):
        with pytest.raises(ValueError):
            parse_writable_memory_project(value)
        with pytest.raises(ValueError):
            parse_agent_search_project(value)


def test_ui_parser_accepts_all_agent_does_not() -> None:
    """Scenario: MEMORY-SEARCH-003 MEMORY-SEARCH-004"""
    assert parse_ui_search_project("all") == "all"
    with pytest.raises(ValueError):
        parse_agent_search_project("all")
    with pytest.raises(ValueError):
        parse_writable_memory_project("all")
