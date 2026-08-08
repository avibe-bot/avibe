from __future__ import annotations

import json
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


def _message_type_sequences(statement: Any) -> set[tuple[str, ...]]:
    """Every message-TYPE set a query binds, i.e. each expanding IN/NOT IN over
    ``messages.c.type``.

    Scoped to that column by bound-parameter NAME: SQLAlchemy names an expanding
    parameter after the column it compares, so ``messages.c.type.in_(...)`` binds
    ``type_1``, ``type_2``, … and no other column in these queries compiles to that
    prefix. The scoping is the point — this module's contract is the message-type
    catalog, and these queries also bind sequences over OTHER columns whose contents
    are somebody else's decision:

    ``list_inbox_sessions`` / ``unread_counts`` / ``unread_counts_by_session`` now also
    bind ``agent_sessions.c.visibility`` (``INBOX_SESSION_VISIBILITIES`` ==
    ``('foreground', 'system')``) since the reserved workspace-notifications row became
    a ``system`` surface — a session-projection decision pinned by
    ``tests/test_workspace_system_session.py``. Collecting every sequence indiscriminately
    made that legitimate change fail three catalog assertions, which says the filter
    belongs here rather than in the expectations: adding the visibility tuple to each
    expected set would have this module re-pin a contract it does not own (and go red
    again on the next non-type IN-list).
    """
    return {
        tuple(value)
        for name, value in statement.compile().params.items()
        if isinstance(value, (list, tuple)) and (name == "type" or name.startswith("type_"))
    }


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
    expected = (
        "user",
        "harness",
        "annotation",
        "output",
        "result",
        "notify",
        "error",
    )
    assert types_with("transcript") == expected
    assert messages_service.TRANSCRIPT_TYPES == expected


def test_searchable_types_match_current_default_query() -> None:
    connection = _CaptureConnection()
    messages_service.search_messages(connection, query="catalog-probe")

    expected = ("user", "harness", "annotation", "output", "result")
    assert types_with("searchable") == expected
    assert _message_type_sequences(connection.statements[-1]) == {expected}


def test_inbox_activity_types_match_current_constant() -> None:
    expected = (
        "user",
        "harness",
        "agent_initiated",
        "annotation",
        "output",
        "result",
        "notify",
        "error",
        "assistant",
    )
    assert types_with("inboxActivity") == expected
    assert messages_service.INBOX_ACTIVITY_TYPES == expected


def test_inbox_preview_and_settlement_types_match_current_query() -> None:
    connection = _CaptureConnection()
    messages_service.list_inbox_sessions(connection)
    current_query_sets = _message_type_sequences(connection.statements[-1])

    expected_preview = ("output", "result", "notify", "error")
    expected_settlement = ("result", "notify", "error")
    expected_unread = ("result",)
    assert types_with("inboxPreview") == expected_preview
    assert types_with("inboxSettlesReply") == expected_settlement
    assert types_with("unread") == expected_unread
    assert current_query_sets == {
        messages_service.INBOX_ACTIVITY_TYPES,
        expected_preview,
        expected_settlement,
        expected_unread,
    }


@pytest.mark.parametrize(
    "query",
    [messages_service.unread_counts, messages_service.unread_counts_by_session],
)
def test_unread_types_match_current_queries(query: Any) -> None:
    connection = _CaptureConnection()
    query(connection)

    expected = ("result",)
    assert types_with("unread") == expected
    assert _message_type_sequences(connection.statements[-1]) == {expected}


def test_input_turn_pairs_match_current_constant() -> None:
    expected = (
        ("user", "user"),
        ("harness", "harness"),
        ("harness", "agent_initiated"),
        ("harness", "annotation"),
    )
    assert input_author_type_pairs() == expected
    assert INPUT_TURN_AUTHOR_TYPES == expected


def test_annotation_catalog_contract_is_explicit() -> None:
    assert dict(spec_for("annotation")) == {
        "transcript": True,
        "searchable": True,
        "inputAuthors": ("harness",),
        "inboxActivity": True,
        "inboxPreview": False,
        "inboxSettlesReply": False,
        "activityRole": "none",
        "terminalWhenEvents": (),
        "unread": False,
        "webPush": False,
        "webPushWhenEvents": (),
        "render": "annotation",
    }


def test_activity_fetch_and_terminal_semantics_match_current_service() -> None:
    assert spec_for("output")["activityRole"] == "boundary"
    derived_relevant = {
        message_type
        for message_type in _catalog_types()
        if spec_for(message_type)["activityRole"] != "none"
        or spec_for(message_type)["terminalWhenEvents"]
    }
    expected_relevant = (
        "user",
        "harness",
        "agent_initiated",
        "output",
        "result",
        "notify",
        "error",
        "assistant",
    )
    assert derived_relevant == set(expected_relevant)
    assert set(agent_activity_service._RELEVANT_MESSAGE_TYPES) == set(expected_relevant)

    for message_type in _catalog_types():
        spec = spec_for(message_type)
        for event in (None, "backend_failure", "other"):
            metadata = {"event": event} if event is not None else {}
            legacy_expected = (
                message_type in {"result", "error", "silent"}
                or (message_type == "notify" and event == "backend_failure")
            )
            assert (
                spec["activityRole"] == "terminal"
                or event in spec["terminalWhenEvents"]
            ) is legacy_expected
            assert (
                agent_activity_service._is_terminal(message_type, "agent", metadata)
                is legacy_expected
            )
    assert not agent_activity_service._is_terminal(" result ", "agent", {})
    assert (
        agent_activity_service._terminal_status(
            " notify ",
            {"event": "backend_failure"},
        )
        == "done"
    )


def test_web_push_candidate_exact_and_unread_sets_match_current_service() -> None:
    expected_candidates = {"result", "error", "notify"}
    expected_unread = {"result"}
    assert set(types_with("webPush")) | set(
        types_with("webPushWhenEvents")
    ) == expected_candidates
    assert web_push_notifications._NOTIFIABLE_TYPES == expected_candidates
    assert set(types_with("unread")) == expected_unread
    assert web_push_notifications._UNREAD_GATED_TYPES == expected_unread

    for message_type in _catalog_types():
        spec = spec_for(message_type)
        for event in (None, "backend_failure", "other"):
            metadata = {"event": event} if event is not None else {}
            legacy_expected = message_type in {"result", "error"} or (
                message_type == "notify" and event == "backend_failure"
            )
            assert (
                spec["webPush"] or event in spec["webPushWhenEvents"]
            ) is legacy_expected
            assert (
                web_push_notifications._is_notifiable_message(message_type, metadata)
                is legacy_expected
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
