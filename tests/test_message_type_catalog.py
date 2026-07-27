from __future__ import annotations

import json
import re
from importlib import resources
from typing import Any

import pytest

from core import web_push_notifications
from storage import agent_activity_service, messages_service
from storage.models import messages
from vibe.message_identity import INPUT_TURN_AUTHOR_TYPES
from vibe.message_types import (
    build_partial_index_predicate,
    input_author_type_pairs,
    spec_for,
    types_with,
    types_without,
)


class _EmptyResult:
    def mappings(self) -> "_EmptyResult":
        return self

    def all(self) -> list[Any]:
        return []


class _CaptureConnection:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> _EmptyResult:
        self.statements.append(statement)
        return _EmptyResult()


def _catalog_document() -> dict[str, Any]:
    resource = resources.files("vibe").joinpath("message_types.json")
    assert resource.is_file()
    return json.loads(resource.read_text(encoding="utf-8"))


def _catalog_types() -> tuple[str, ...]:
    return tuple(_catalog_document()["types"])


def _sequence_params(statement: Any) -> set[tuple[str, ...]]:
    return {
        tuple(value)
        for value in statement.compile().params.values()
        if isinstance(value, (list, tuple))
    }


def _message_type_equality_values(statement: Any) -> tuple[str, ...]:
    compiled = statement.compile()
    parameter_names = re.findall(r"\bmessages\.type = :(type_\d+)", str(compiled))
    return tuple(compiled.params[name] for name in parameter_names)


def test_catalog_resource_and_defaults_are_cross_language_data() -> None:
    document = _catalog_document()
    expected_default = {
        name: tuple(value) if isinstance(value, list) else value
        for name, value in document["defaults"].items()
    }

    assert dict(spec_for("not_a_catalog_type")) == expected_default
    with pytest.raises(TypeError):
        spec_for("user")["transcript"] = False  # type: ignore[index]


def test_transcript_types_match_current_constant() -> None:
    assert types_with("transcript") == messages_service.TRANSCRIPT_TYPES


def test_searchable_types_match_current_default_query() -> None:
    connection = _CaptureConnection()
    messages_service.search_messages(connection, query="catalog-probe")

    assert _sequence_params(connection.statements[-1]) == {types_with("searchable")}


def test_inbox_activity_types_match_current_constant() -> None:
    assert types_without("inboxActivity") == messages_service.NON_CONVERSATION_TYPES


def test_inbox_preview_and_settlement_types_match_current_query() -> None:
    connection = _CaptureConnection()
    messages_service.list_inbox_sessions(connection)
    current_query_sets = _sequence_params(connection.statements[-1])

    assert current_query_sets == {
        messages_service.NON_CONVERSATION_TYPES,
        types_with("inboxPreview"),
        types_with("inboxSettlesReply"),
    }


@pytest.mark.parametrize(
    "query",
    [messages_service.unread_counts, messages_service.unread_counts_by_session],
)
def test_unread_types_match_current_queries(query: Any) -> None:
    connection = _CaptureConnection()
    query(connection)

    assert _message_type_equality_values(connection.statements[-1]) == types_with("unread")


def test_input_turn_pairs_match_current_constant() -> None:
    assert input_author_type_pairs() == INPUT_TURN_AUTHOR_TYPES


def test_activity_fetch_and_terminal_semantics_match_current_service() -> None:
    derived_relevant = {
        message_type
        for message_type in _catalog_types()
        if spec_for(message_type)["activityRole"] != "none"
        or spec_for(message_type)["terminalWhenEvents"]
    }
    assert derived_relevant == set(agent_activity_service._RELEVANT_MESSAGE_TYPES)

    for message_type in _catalog_types():
        spec = spec_for(message_type)
        for event in (None, "backend_failure", "other"):
            metadata = {"event": event} if event is not None else {}
            expected = (
                spec["activityRole"] == "terminal"
                or event in spec["terminalWhenEvents"]
            )
            assert (
                agent_activity_service._is_terminal(message_type, "agent", metadata)
                is expected
            )


def test_web_push_candidate_exact_and_unread_sets_match_current_service() -> None:
    assert set(types_with("webPush")) | set(
        types_with("webPushWhenEvents")
    ) == web_push_notifications._NOTIFIABLE_TYPES
    assert set(types_with("unread")) == web_push_notifications._UNREAD_GATED_TYPES

    for message_type in _catalog_types():
        spec = spec_for(message_type)
        for event in (None, "backend_failure", "other"):
            metadata = {"event": event} if event is not None else {}
            expected = spec["webPush"] or event in spec["webPushWhenEvents"]
            assert (
                web_push_notifications._is_notifiable_message(message_type, metadata)
                is expected
            )


@pytest.mark.parametrize(
    "index_name",
    [
        "ix_messages_inbox_activity",
        "ix_messages_inbox_agent_reply",
        "ix_messages_inbox_user_send",
    ],
)
def test_partial_index_predicates_match_current_model(index_name: str) -> None:
    index = next(item for item in messages.indexes if item.name == index_name)
    current_predicate = index.dialect_options["sqlite"]["where"]

    assert build_partial_index_predicate(index_name) == str(current_predicate)
