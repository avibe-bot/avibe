"""MESSAGE-DELIVERY-338: one admission entry for every input source."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from sqlalchemy import func, select

from core.input_admission import (
    DEFAULT_ADMISSION_POLICY,
    AdmissionDecision,
    InputEnvelope,
    InputProvenance,
    InputSender,
    admit_input,
)
from storage import message_deliveries
from storage.db import create_sqlite_engine
from storage.models import agent_sessions, metadata
from storage.models import message_deliveries as message_deliveries_table


SESSION_ID = "ses_admission"


def _envelope(source: str, **fields) -> InputEnvelope:
    provenance = {
        key: fields.pop(key) for key in ("trigger", "platform") if key in fields
    }
    harness = source in {"harness", "show_annotation", "agent_initiated"}
    return InputEnvelope(
        session_id=SESSION_ID,
        scope_id=None,
        sender=InputSender("harness" if harness else "user", "sender-id", "Sender"),
        kind="harness" if harness else "user",
        provenance=InputProvenance(
            source=source,
            origin="harness" if harness else "user",
            **provenance,
        ),
        dispatch_text="input",
        text="input",
        **fields,
    )


HARNESS_TRIGGERS = [
    ("scheduled", "p3"),
    ("task_run", "p3"),
    ("watch", "p1"),
    ("hook", "p1"),
    ("webhook", "p1"),
    ("agent_run", "p1"),
    ("callback", "p1"),
]

CASES = [
    ("web composer", _envelope("web_composer"), "p3", "source:web_composer"),
    ("im", _envelope("im", platform="slack", backend="codex"), "p1", "source:im"),
    (
        "im claude subagent prefix",
        _envelope("im", platform="slack", backend="claude", subagent="reviewer"),
        "p1",
        "source:im",
    ),
    (
        "im opencode",
        _envelope("im", platform="slack", backend="opencode"),
        "p1",
        "source:im",
    ),
    (
        "im opencode subagent prefix",
        _envelope("im", platform="slack", backend="opencode", subagent="reviewer"),
        "p3",
        "opencode_subagent",
    ),
    *[
        (f"harness {trigger}", _envelope("harness", trigger=trigger), priority, f"trigger:{trigger}")
        for trigger, priority in HARNESS_TRIGGERS
    ],
    (
        "agent run --queue",
        _envelope("harness", trigger="agent_run", requested_intent="queue"),
        "p3",
        "requested:queue",
    ),
    (
        "agent run steer",
        _envelope("harness", trigger="agent_run", requested_intent="steer"),
        "p1",
        "requested:steer",
    ),
    (
        "agent run legacy send_now",
        _envelope("harness", trigger="agent_run", requested_intent="send_now"),
        "p1",
        "requested:send_now",
    ),
    (
        "requested intent wins over the trigger",
        _envelope("harness", trigger="watch", requested_intent="queue"),
        "p3",
        "requested:queue",
    ),
    ("harness without trigger", _envelope("harness"), "p1", "harness_default"),
    ("show annotation", _envelope("show_annotation"), "p1", "source:show_annotation"),
    ("failure retry", _envelope("failure_retry"), "p3", "source:failure_retry"),
    ("agent initiated", _envelope("agent_initiated"), "p3", "source:agent_initiated"),
]


@pytest.fixture()
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "state.sqlite")
    metadata.create_all(engine)
    now = "2026-10-20T00:00:00+00:00"
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.insert().values(
                id=SESSION_ID,
                agent_backend="codex",
                agent_variant="default",
                session_anchor=SESSION_ID,
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
    yield engine
    engine.dispose()


def _admit(engine, envelope, **kwargs):
    with engine.begin() as conn:
        admitted = admit_input(
            conn,
            envelope,
            delivery_id=message_deliveries.new_delivery_id(),
            **kwargs,
        )
        row = message_deliveries.get_delivery(conn, admitted.delivery["id"])
    return admitted, row


@pytest.mark.parametrize(
    ("envelope", "priority", "reason"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_each_source_is_admitted_with_its_released_priority_and_records_the_decision(
    engine, envelope, priority, reason
) -> None:
    """MESSAGE-DELIVERY-338: one policy holds every source's released priority."""

    admitted, row = _admit(engine, envelope)

    assert (row["priority"], row["state"]) == (priority, "reserved")
    assert admitted.decision.priority == priority
    assert json.loads(row["delivery_history_json"])["events"] == [
        {
            "at": row["submitted_at"],
            "kind": "admission",
            "priority": priority,
            "state": "reserved",
            "policy": {"id": "default", "reason": reason},
        }
    ]
    assert row["snapshot_json"] == json.dumps(
        envelope.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def test_a_source_owned_admission_event_carries_the_decision(engine) -> None:
    """MESSAGE-DELIVERY-338: a failure retry and an agent-initiated Turn keep their own event."""

    binding = {"kind": "backend_failure_retry", "notice_id": "msg_notice"}
    _admitted, row = _admit(
        engine,
        _envelope("failure_retry"),
        history=binding,
        admission_context={"ignored": "the source event wins"},
    )

    (event,) = json.loads(row["delivery_history_json"])["events"]
    assert event == {
        "at": row["submitted_at"],
        **binding,
        "policy": {"id": "default", "reason": "source:failure_retry"},
    }


def test_an_input_that_already_has_a_delivery_gets_it_back(engine) -> None:
    """MESSAGE-DELIVERY-338: a duplicate native id reuses its Delivery, never a second one."""

    envelope = replace(
        _envelope("im", platform="slack", backend="codex"),
        provenance=InputProvenance(source="im", platform="slack", native_message_id="171.1"),
    )
    dedupe_key = message_deliveries.native_dedupe_key("slack", "171.1")
    first, _ = _admit(engine, envelope, dedupe_key=dedupe_key, reuse_existing=True)
    again, _ = _admit(engine, envelope, dedupe_key=dedupe_key, reuse_existing=True)

    assert again.delivery["id"] == first.delivery["id"]
    with engine.connect() as conn:
        assert conn.execute(
            select(func.count()).select_from(message_deliveries_table)
        ).scalar_one() == 1
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.insert().values(
                id="ses_other",
                agent_backend="codex",
                agent_variant="default",
                session_anchor="ses_other",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at="2026-10-20T00:00:00+00:00",
                updated_at="2026-10-20T00:00:00+00:00",
                last_active_at="2026-10-20T00:00:00+00:00",
            )
        )
    other = replace(envelope, session_id="ses_other")
    with pytest.raises(ValueError, match="belongs to another Session"):
        _admit(engine, other, dedupe_key=dedupe_key, reuse_existing=True)


def test_the_record_names_whichever_policy_decided(engine) -> None:
    """MESSAGE-DELIVERY-338: a policy that replaces or wraps the default is what gets recorded."""

    class QueueEverything:
        def decide(self, envelope):
            default = DEFAULT_ADMISSION_POLICY.decide(envelope)
            return AdmissionDecision("queue", "test-firewall", f"over:{default.reason}")

    admitted, row = _admit(
        engine,
        _envelope("im", platform="slack", backend="codex"),
        admission_context={"processing_indicator_message_id": "echo-1"},
        policy=QueueEverything(),
    )

    assert row["priority"] == "p3"
    (event,) = json.loads(row["delivery_history_json"])["events"]
    assert event["context"] == {"processing_indicator_message_id": "echo-1"}
    assert event["policy"] == {"id": "test-firewall", "reason": "over:source:im"}


def test_an_unknown_harness_trigger_is_refused_rather_than_guessed() -> None:
    """The trigger vocabulary stays closed now that the policy, not the scheduler, maps it."""

    with pytest.raises(ValueError, match="unsupported delivery trigger"):
        DEFAULT_ADMISSION_POLICY.decide(_envelope("harness", trigger="task_escalation"))
