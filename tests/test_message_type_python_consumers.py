from __future__ import annotations

from core import backend_failure, internal_server, show_git
from core.services import session_fork
from vibe import ui_server
from vibe.message_types import spec_for, types_with


def test_backend_failure_predicate_matches_legacy_type_and_event_pair() -> None:
    for message_type in (
        "user",
        "harness",
        "result",
        "notify",
        "error",
        "assistant",
        "tool_call",
        "queued",
        "draft",
        "pending",
        "harness_dedupe",
        "silent",
        "unknown",
    ):
        for event in (None, "backend_failure", "other"):
            metadata = {"event": event} if event is not None else {}
            legacy_expected = message_type == "notify" and event == "backend_failure"
            catalog_expected = (
                event == backend_failure.BACKEND_FAILURE_EVENT
                and event in spec_for(message_type)["terminalWhenEvents"]
            )
            assert catalog_expected is legacy_expected
            assert (
                backend_failure.is_backend_failure_notification(message_type, metadata)
                is legacy_expected
            )
    assert backend_failure.is_backend_failure_notification(
        " notify ",
        {"event": "backend_failure"},
    )


def test_reservation_acceptance_matches_legacy_consumers() -> None:
    expected = {"user", "harness", "queued"}
    assert set(types_with("acceptedReservation")) == expected
    assert internal_server._ACCEPTED_RESERVATION_TYPES == expected
    assert ui_server._ACCEPTED_RESERVATION_TYPES == expected


def test_fork_activity_sets_match_legacy_values() -> None:
    assert session_fork.TERMINAL_AGENT_OUTPUT_TYPES == {
        "result",
        "error",
        "silent",
    }
    assert session_fork.SOURCE_PROGRESS_AGENT_OUTPUT_TYPES == {
        "assistant",
        "result",
        "error",
        "silent",
    }
    assert set(session_fork._FORK_ANCHOR_TYPES) == {
        "user",
        "harness",
        "result",
        "notify",
        "error",
        "silent",
    }


def test_show_git_input_message_types_match_legacy_values() -> None:
    assert show_git._INPUT_TURN_MESSAGE_TYPES == ("user", "harness")


def test_mirror_catalog_roles_match_legacy_type_sets() -> None:
    assert set(types_with("inboxPreview")) == {"result", "notify", "error"}
    assert {
        message_type
        for message_type in types_with("activityRole")
        if spec_for(message_type)["activityRole"] == "activity"
    } == {"assistant"}
