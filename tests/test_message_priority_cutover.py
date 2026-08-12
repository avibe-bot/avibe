from __future__ import annotations

import pytest

from core.scheduled_tasks import ParsedSessionKey, ResolvedSessionIdTarget


@pytest.mark.parametrize(
    ("trigger_kind", "expected_intent"),
    [
        ("im", "steer"),
        ("scheduled", "queue"),
        ("task_run", "queue"),
        ("watch", "steer"),
        ("hook", "steer"),
        ("webhook", "steer"),
        ("agent_run", "steer"),
        ("show_annotation", "steer"),
        ("callback", "steer"),
    ],
)
def test_source_policy_has_one_explicit_delivery_intent(
    trigger_kind: str,
    expected_intent: str,
) -> None:
    from core.message_priority import delivery_intent_for_trigger

    assert delivery_intent_for_trigger(trigger_kind) == expected_intent


@pytest.mark.parametrize(
    ("intent", "expected_priority"),
    [
        ("replace", "p0"),
        ("steer", "p1"),
        ("send_now", "p1"),
        ("queue", "p3"),
    ],
)
def test_delivery_intent_maps_to_exact_priority(
    intent: str,
    expected_priority: str,
) -> None:
    from core.message_priority import priority_for_delivery_intent

    assert priority_for_delivery_intent(intent) == expected_priority


@pytest.mark.parametrize(
    ("priority", "expected_intent"),
    [("p0", "replace"), ("p1", "steer"), ("p3", "queue")],
)
def test_delivery_priority_maps_to_exact_intent(
    priority: str,
    expected_intent: str,
) -> None:
    from core.message_priority import delivery_intent_for_priority

    assert delivery_intent_for_priority(priority) == expected_intent


def test_agent_run_queue_intent_is_not_normalized_back_to_steer() -> None:
    from core.scheduled_tasks import normalize_agent_run_delivery_intent

    assert normalize_agent_run_delivery_intent("queue") == "queue"


def test_legacy_content_send_now_is_normalized_to_steer() -> None:
    from core.scheduled_tasks import normalize_agent_run_delivery_intent

    assert normalize_agent_run_delivery_intent("send_now") == "steer"


def test_session_context_rebuilds_an_im_target_from_durable_scope(monkeypatch) -> None:
    from core import internal_server
    from core import scheduled_tasks

    target = ResolvedSessionIdTarget(
        session_id="ses_im",
        session_key=ParsedSessionKey(
            platform="slack",
            scope_type="channel",
            scope_id="C123",
            thread_id="171.2",
        ),
        agent_backend="codex",
        agent_variant="default",
        native_session_id="native-1",
        scope_id="scp_1",
        agent_name="codex",
        session_anchor="slack_C123_171.2",
    )
    monkeypatch.setattr(
        scheduled_tasks,
        "resolve_session_id_target",
        lambda _session_id: target,
    )
    monkeypatch.setattr(
        internal_server,
        "_lookup_session",
        lambda _session_id: {
            "id": "ses_im",
            "agent_id": None,
            "agent_name": "codex",
            "agent_backend": "codex",
            "agent_variant": "default",
            "model": None,
            "reasoning_effort": None,
            "native_session_id": "native-1",
            "workdir": "/tmp/project",
            "metadata": {},
            "session_anchor": "slack_C123_171.2",
            "visibility": "foreground",
        },
    )

    context = internal_server._build_session_context("ses_im")

    assert context.platform == "slack"
    assert context.channel_id == "C123"
    assert context.thread_id == "171.2"
    assert context.platform_specific["agent_session_id"] == "ses_im"
    assert "workbench_session_id" not in context.platform_specific
