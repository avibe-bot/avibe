from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.schema import CreateIndex

from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import (
    agent_sessions,
    system_schedule_attempts,
)
from storage.system_schedule_service import (
    SystemScheduleServiceError,
    _canonical_receipt,
    _sticky_attempt,
    cancel_operation,
    claim_next,
    create_operation,
    get_operation,
    record_receipt,
)

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
FIRE_AT = "2026-11-16T16:00:00.123456Z"
CONTRACT_VECTORS = (
    Path(__file__).parent / "fixtures" / "system_schedule_contract_vectors.json"
)


def test_shared_contract_vectors_cover_closed_shapes_and_terminal_receipts():
    vectors = json.loads(CONTRACT_VECTORS.read_text())
    assert vectors["contract"] == 1
    assert {request["title"] for request in vectors["createRequests"]} == {
        "Take medicine",
        "吃药 💊",
    }
    create_keys = {
        "requestId",
        "operationId",
        "kind",
        "title",
        "fireAt",
        "timeZone",
        "recurrence",
        "instanceId",
        "sessionId",
        "expiresAt",
    }
    assert all(set(request) == create_keys for request in vectors["createRequests"])
    assert all(request["recurrence"] is None for request in vectors["createRequests"])
    assert all(
        set(request) == {"requestId", "operationId", "kind", "purpose"}
        for request in vectors["cancelRequests"]
    )
    assert {request["purpose"] for request in vectors["cancelRequests"]} == {
        "user",
    }
    parsed = [_canonical_receipt(receipt).payload for receipt in vectors["receipts"]]
    assert parsed == vectors["receipts"]
    assert {receipt["state"] for receipt in parsed} == {
        "scheduled",
        "user_denied",
        "permission_denied",
        "unsupported",
        "failed",
        "expired",
        "stale_context",
        "cancelled",
    }


@pytest.fixture()
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.insert().values(
                id="ses-schedule",
                scope_id=None,
                agent_id=None,
                agent_name="codex",
                agent_backend="codex",
                agent_variant="default",
                model=None,
                reasoning_effort=None,
                session_anchor="schedule-test",
                workdir="/tmp",
                native_session_id="native-schedule-test",
                title="Schedule test",
                status="active",
                visibility="foreground",
                pinned=0,
                agent_status="idle",
                composer_draft_text=None,
                composer_draft_updated_at=None,
                metadata_json="{}",
                created_at="2026-11-16T14:00:00Z",
                updated_at="2026-11-16T14:00:00Z",
                last_active_at="2026-11-16T14:00:00Z",
            )
        )
    return tmp_path


def _create(*, operation_id: str = "wake-up", now: datetime = NOW, expires_in: int = 300):
    return create_operation(
        session_id="ses-schedule",
        instance_id="instance-a",
        operation_id=operation_id,
        kind="alarm",
        title="Wake up",
        fire_at=FIRE_AT,
        time_zone="America/Los_Angeles",
        expires_in=expires_in,
        now=now,
    )


def _get(operation_id: str = "wake-up", *, now: datetime = NOW):
    return get_operation(
        session_id="ses-schedule",
        instance_id="instance-a",
        operation_id=operation_id,
        now=now,
    )


def _receipt(
    claim: dict,
    *,
    state: str,
    system_identifier: str | None = None,
    authorization: str = "authorized",
    error: dict | None = None,
) -> dict:
    request = claim["request"]
    return {
        "contract": 1,
        "requestId": request["requestId"],
        "operationId": request["operationId"],
        "kind": request["kind"],
        "state": state,
        "systemIdentifier": system_identifier,
        "fireAt": request.get("fireAt"),
        "timeZone": request.get("timeZone"),
        "authorization": authorization,
        "error": error,
    }


def _record(claim: dict, receipt: dict, *, now: datetime = NOW):
    return record_receipt(
        instance_id="instance-a",
        device_id="device-a",
        lease_token=claim["leaseToken"],
        receipt=receipt,
        now=now,
    )


def _schedule(operation_id: str = "wake-up") -> dict:
    _create(operation_id=operation_id)
    claim = claim_next(instance_id="instance-a", device_id="device-a", now=NOW)
    _record(
        claim,
        _receipt(claim, state="scheduled", system_identifier=f"opaque-{operation_id}"),
    )
    return claim


def test_create_is_canonical_session_bound_idempotent_and_conflict_safe(isolated_state):
    created = _create()
    retried = _create()

    assert retried == created
    assert created["session_id"] == "ses-schedule"
    assert created["instance_id"] == "instance-a"
    assert created["state"] == "pending"
    assert created["fire_at"] == "2026-11-16T16:00:00.123Z"
    assert created["expires_at"] == "2026-11-16T15:05:00.000Z"
    assert created["create"]["generation"] == 1
    assert created["create"]["request_id"].startswith("ssr_")

    with pytest.raises(SystemScheduleServiceError, match="different system schedule") as exc_info:
        create_operation(
            session_id="ses-schedule",
            instance_id="instance-a",
            operation_id="wake-up",
            kind="reminder",
            title="Wake up",
            fire_at=FIRE_AT,
            time_zone="America/Los_Angeles",
            expires_in=300,
            now=NOW,
        )
    assert exc_info.value.code == "operation_conflict"

    with pytest.raises(SystemScheduleServiceError) as exc_info:
        get_operation(
            session_id="ses-schedule",
            instance_id="instance-b",
            operation_id="wake-up",
            now=NOW,
        )
    assert exc_info.value.code == "system_schedule_not_found"

    with pytest.raises(SystemScheduleServiceError) as exc_info:
        create_operation(
            session_id="missing-session",
            instance_id="instance-a",
            operation_id="missing",
            kind="alarm",
            title="Wake up",
            fire_at=FIRE_AT,
            time_zone="America/Los_Angeles",
            expires_in=300,
            now=NOW,
        )
    assert exc_info.value.code == "session_not_found"

    for operation_id in ("hidden\u007fcontrol", "hidden\u200eformat"):
        with pytest.raises(SystemScheduleServiceError) as exc_info:
            create_operation(
                session_id="ses-schedule",
                instance_id="instance-a",
                operation_id=operation_id,
                kind="alarm",
                title="Wake up",
                fire_at=FIRE_AT,
                time_zone="America/Los_Angeles",
                expires_in=300,
                now=NOW,
            )
        assert exc_info.value.code == "invalid_request"


# I3 review E8: an idempotent retry is resolved before first-create time guards.
def test_existing_operation_retry_precedes_first_create_time_validation(isolated_state):
    created = _create(operation_id="past-retry")

    retried = _create(
        operation_id="past-retry",
        now=NOW + timedelta(hours=2),
    )
    assert retried["operation_id"] == created["operation_id"]
    assert retried["state"] == "expired"
    assert retried["create"]["request_id"] == created["create"]["request_id"]

    with pytest.raises(SystemScheduleServiceError) as exc_info:
        _create(
            operation_id="new-past-operation",
            now=NOW + timedelta(hours=2),
        )
    assert exc_info.value.code == "fire_time_in_past"


def test_created_operation_survives_originating_session_deletion(isolated_state):
    _create(operation_id="survives-session")
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.delete().where(agent_sessions.c.id == "ses-schedule")
        )

    operation = _get("survives-session")
    assert operation["state"] == "pending"
    claim = claim_next(
        instance_id="instance-a",
        device_id="device-a",
        now=NOW,
    )
    assert claim["request"]["sessionId"] == "ses-schedule"


def test_unclaimed_create_expires_but_claimed_create_awaits_device(isolated_state):
    _create(operation_id="unclaimed", expires_in=30)
    assert _get("unclaimed", now=NOW + timedelta(seconds=31))["state"] == "expired"
    assert claim_next(
        instance_id="instance-a",
        device_id="device-a",
        now=NOW + timedelta(seconds=31),
    ) is None

    _create(operation_id="claimed", expires_in=30)
    claim = claim_next(instance_id="instance-a", device_id="device-a", now=NOW)
    waiting = _get("claimed", now=NOW + timedelta(seconds=31))
    assert waiting["state"] == "awaiting_device"
    assert waiting["status_message"] == (
        "Waiting for your device to return the result."
    )
    assert waiting["create"]["terminal_state"] is None
    assert claim_next(
        instance_id="instance-a",
        device_id="device-b",
        now=NOW + timedelta(seconds=31),
    ) is None

    scheduled = _record(
        claim,
        _receipt(claim, state="scheduled", system_identifier="opaque-claimed"),
        now=NOW + timedelta(seconds=32),
    )
    assert scheduled["operation"]["state"] == "scheduled"
    assert scheduled["operation"]["cleanup"] is None


def test_device_binding_is_sticky_and_exclusions_allow_cancel_to_pass_create(
    isolated_state,
):
    _create(expires_in=300)
    create_claim = claim_next(instance_id="instance-a", device_id="device-a", now=NOW)
    assert claim_next(instance_id="instance-a", device_id="device-b", now=NOW) is None
    assert claim_next(instance_id="instance-a", device_id="device-a", now=NOW) == create_claim
    assert claim_next(
        instance_id="instance-a",
        device_id="device-a",
        exclude_lease_tokens=[create_claim["leaseToken"]],
        allow_create=False,
        kinds=[],
        now=NOW,
    ) is None

    cancelled = cancel_operation(
        session_id="ses-schedule",
        instance_id="instance-a",
        operation_id="wake-up",
        now=NOW + timedelta(seconds=1),
    )
    assert cancelled["state"] == "cancelled"
    cancel_claim = claim_next(
        instance_id="instance-a",
        device_id="device-a",
        exclude_lease_tokens=[create_claim["leaseToken"]],
        allow_create=False,
        kinds=[],
        now=NOW + timedelta(seconds=1),
    )
    assert cancel_claim["action"] == "cancel"
    assert cancel_claim["generation"] == 1
    assert cancel_claim["purpose"] == "user"
    assert claim_next(
        instance_id="instance-a",
        device_id="device-b",
        now=NOW + timedelta(seconds=1),
    ) is None


# I3 review C1: a terminal create attempt is never sticky claim work again.
def test_cancelled_claimed_create_is_never_redispatched(isolated_state):
    for suffix, cancel_state in (
        ("inflight", None),
        ("failed", "failed"),
        ("settled", "cancelled"),
    ):
        operation_id = f"cancelled-sticky-{suffix}"
        _create(operation_id=operation_id)
        create_claim = claim_next(
            instance_id="instance-a",
            device_id="device-a",
            now=NOW,
        )
        cancel_operation(
            session_id="ses-schedule",
            instance_id="instance-a",
            operation_id=operation_id,
            now=NOW + timedelta(seconds=1),
        )
        cancel_claim = claim_next(
            instance_id="instance-a",
            device_id="device-a",
            exclude_lease_tokens=[create_claim["leaseToken"]],
            now=NOW + timedelta(seconds=1),
        )
        assert cancel_claim["action"] == "cancel"
        if cancel_state is not None:
            error = None
            if cancel_state == "failed":
                error = {"code": "remove_failed", "message": "Try again."}
            _record(
                cancel_claim,
                _receipt(cancel_claim, state=cancel_state, error=error),
                now=NOW + timedelta(seconds=2),
            )
        excluded = [cancel_claim["leaseToken"]] if cancel_state is None else []
        assert claim_next(
            instance_id="instance-a",
            device_id="device-a",
            exclude_lease_tokens=excluded,
            now=NOW + timedelta(seconds=3),
        ) is None
        if cancel_state is None:
            _record(
                cancel_claim,
                _receipt(cancel_claim, state="cancelled"),
                now=NOW + timedelta(seconds=4),
            )


def test_cancel_and_create_receipts_can_arrive_in_either_order(isolated_state):
    for suffix, cancel_first in (("a", True), ("b", False)):
        operation_id = f"race-{suffix}"
        _create(operation_id=operation_id)
        create_claim = claim_next(
            instance_id="instance-a",
            device_id="device-a",
            now=NOW,
        )
        cancel_operation(
            session_id="ses-schedule",
            instance_id="instance-a",
            operation_id=operation_id,
            now=NOW + timedelta(seconds=1),
        )
        cancel_claim = claim_next(
            instance_id="instance-a",
            device_id="device-a",
            exclude_lease_tokens=[create_claim["leaseToken"]],
            now=NOW + timedelta(seconds=1),
        )
        create_receipt = _receipt(
            create_claim,
            state="scheduled",
            system_identifier=f"opaque-{suffix}",
        )
        cancel_receipt = _receipt(
            cancel_claim,
            state="cancelled",
            system_identifier=f"opaque-{suffix}",
        )
        ordered = (
            ((cancel_claim, cancel_receipt), (create_claim, create_receipt))
            if cancel_first
            else ((create_claim, create_receipt), (cancel_claim, cancel_receipt))
        )
        for offset, (claim, receipt) in enumerate(ordered, start=2):
            _record(claim, receipt, now=NOW + timedelta(seconds=offset))
        operation = _get(operation_id, now=NOW + timedelta(seconds=5))
        assert operation["state"] == "cancelled"
        assert operation["create"]["terminal_state"] == "cancelled"
        assert operation["create"]["receipt"]["state"] == "scheduled"
        assert operation["cancel"]["terminal_state"] == "cancelled"
        assert operation["cleanup"] is None


# I3 review D1/E3: receipt order is evidence only and cannot synthesize cleanup.
def test_late_side_effect_and_cancel_receipt_order_never_generates_cleanup_attempt(
    isolated_state,
):
    for suffix, create_first in (("create-first", True), ("cancel-first", False)):
        operation_id = f"no-cleanup-{suffix}"
        _create(operation_id=operation_id)
        create_claim = claim_next(
            instance_id="instance-a",
            device_id="device-a",
            now=NOW,
        )
        cancel_operation(
            session_id="ses-schedule",
            instance_id="instance-a",
            operation_id=operation_id,
            now=NOW + timedelta(seconds=1),
        )
        cancel_claim = claim_next(
            instance_id="instance-a",
            device_id="device-a",
            exclude_lease_tokens=[create_claim["leaseToken"]],
            now=NOW + timedelta(seconds=1),
        )
        create_receipt = _receipt(
            create_claim,
            state="scheduled",
            system_identifier=f"opaque-{suffix}",
        )
        cancel_receipt = _receipt(
            cancel_claim,
            state="failed",
            error={"code": "lookup_unknown", "message": "Could not inspect the item."},
        )
        ordered = (
            ((create_claim, create_receipt), (cancel_claim, cancel_receipt))
            if create_first
            else ((cancel_claim, cancel_receipt), (create_claim, create_receipt))
        )
        for offset, (claim, receipt) in enumerate(ordered, start=2):
            _record(claim, receipt, now=NOW + timedelta(seconds=offset))

        operation = _get(operation_id, now=NOW + timedelta(seconds=5))
        assert operation["cleanup"] is None
        assert claim_next(
            instance_id="instance-a",
            device_id="device-a",
            now=NOW + timedelta(seconds=5),
        ) is None


# I3 review E2: an expired user cancel plus late create receipt creates no Runtime compensation.
def test_expired_user_cancel_then_late_create_receipt_never_generates_cleanup_attempt(
    isolated_state,
):
    _create()
    create_claim = claim_next(instance_id="instance-a", device_id="device-a", now=NOW)
    cancel_operation(
        session_id="ses-schedule",
        instance_id="instance-a",
        operation_id="wake-up",
        expires_in=30,
        now=NOW + timedelta(seconds=1),
    )

    operation = _record(
        create_claim,
        _receipt(create_claim, state="scheduled", system_identifier="opaque-late"),
        now=NOW + timedelta(seconds=32),
    )["operation"]
    assert operation["cancel"]["terminal_state"] == "expired"
    assert operation["cleanup"] is None
    assert claim_next(
        instance_id="instance-a",
        device_id="device-a",
        now=NOW + timedelta(seconds=32),
    ) is None


def test_claimed_cancel_survives_deadline_and_retry_uses_new_generation(isolated_state):
    _schedule()
    cancel_operation(
        session_id="ses-schedule",
        instance_id="instance-a",
        operation_id="wake-up",
        expires_in=30,
        now=NOW + timedelta(seconds=1),
    )
    first = claim_next(
        instance_id="instance-a",
        device_id="device-a",
        now=NOW + timedelta(seconds=2),
    )
    assert claim_next(
        instance_id="instance-a",
        device_id="device-a",
        now=NOW + timedelta(seconds=32),
    ) == first
    _record(
        first,
        _receipt(
            first,
            state="failed",
            error={"code": "remove_failed", "message": "Try again."},
        ),
        now=NOW + timedelta(seconds=33),
    )
    retried = cancel_operation(
        session_id="ses-schedule",
        instance_id="instance-a",
        operation_id="wake-up",
        expires_in=30,
        now=NOW + timedelta(seconds=34),
    )
    assert retried["cancel"]["generation"] == 2
    assert retried["cancel"]["state"] == "pending"


def test_unclaimed_cancel_expires_without_rewriting_scheduled_create(isolated_state):
    _schedule()
    cancel_operation(
        session_id="ses-schedule",
        instance_id="instance-a",
        operation_id="wake-up",
        expires_in=30,
        now=NOW + timedelta(seconds=1),
    )
    operation = _get(now=NOW + timedelta(seconds=32))
    assert operation["state"] == "scheduled"
    assert operation["cancel"]["terminal_state"] == "expired"


def test_receipts_are_strict_idempotent_and_bound_to_exact_attempt(isolated_state):
    _create()
    claim = claim_next(instance_id="instance-a", device_id="device-a", now=NOW)
    receipt = _receipt(
        claim,
        state="permission_denied",
        authorization="denied",
        error={"code": "permission_denied", "message": "Permission was denied."},
    )

    mismatched = dict(receipt)
    mismatched["fireAt"] = "2026-11-16T16:00:00.124Z"
    with pytest.raises(SystemScheduleServiceError) as exc_info:
        _record(claim, mismatched)
    assert exc_info.value.code == "receipt_mismatch"

    first = _record(claim, receipt)
    duplicate = _record(claim, receipt, now=NOW + timedelta(seconds=1))
    assert duplicate["operation"]["create"]["receipt"] == first["operation"]["create"]["receipt"]

    conflicting = dict(receipt)
    conflicting["error"] = {"code": "permission_denied", "message": "Changed."}
    with pytest.raises(SystemScheduleServiceError) as exc_info:
        _record(claim, conflicting, now=NOW + timedelta(seconds=2))
    assert exc_info.value.code == "receipt_conflict"

    with pytest.raises(SystemScheduleServiceError) as exc_info:
        record_receipt(
            instance_id="instance-a",
            device_id="device-b",
            lease_token=claim["leaseToken"],
            receipt=receipt,
            now=NOW + timedelta(seconds=2),
        )
    assert exc_info.value.code == "receipt_mismatch"


def test_every_observable_entry_advances_unclaimed_expiry(isolated_state):
    entry_times = [
        ("status", 0),
        ("cancel", 1),
        ("claim", 2),
        ("create", 3),
        ("receipt", 4),
    ]
    for label, offset in entry_times:
        operation_id = f"due-{label}"
        _create(operation_id=operation_id, expires_in=30)
        later = NOW + timedelta(seconds=31 + offset)
        if label == "status":
            _get(operation_id, now=later)
        elif label == "cancel":
            cancel_operation(
                session_id="ses-schedule",
                instance_id="instance-a",
                operation_id=operation_id,
                now=later,
            )
        elif label == "claim":
            claim_next(instance_id="instance-a", device_id="device-a", now=later)
        elif label == "create":
            _create(operation_id=f"trigger-{label}", now=later)
        else:
            _create(
                operation_id="receipt-trigger",
                now=NOW - timedelta(seconds=1),
            )
            claim = claim_next(instance_id="instance-a", device_id="device-a", now=NOW)
            _record(
                claim,
                _receipt(
                    claim,
                    state="permission_denied",
                    authorization="denied",
                    error={"code": "permission_denied", "message": "Denied."},
                ),
                now=later,
            )
        assert _get(operation_id, now=later)["state"] == "expired"


@pytest.mark.parametrize("state", [
    "scheduled", "user_denied", "permission_denied", "unsupported",
    "failed", "expired", "stale_context", "cancelled",
])
def test_receipted_attempts_leave_no_nonterminal_sticky_candidate(isolated_state, state):
    _create()
    claim = claim_next(instance_id="instance-a", device_id="device-a", now=NOW)
    _record(
        claim,
        _receipt(
            claim, state=state,
            system_identifier="opaque-scheduled" if state == "scheduled" else None,
        ),
    )
    engine = create_sqlite_engine()
    with engine.connect() as conn:
        attempt = conn.execute(select(system_schedule_attempts)).mappings().one()
        assert attempt["claim_state"] == "settled"
        assert attempt["terminal_state"] == state
        assert attempt["receipt_json"] is not None
        assert _sticky_attempt(
            conn, action="create", instance_id="instance-a", device_id="device-a",
            excluded=(),
        ) is None


def test_sticky_poll_work_is_bounded_by_live_attempts_not_cancelled_history(
    isolated_state,
):
    _create()
    claim_next(instance_id="instance-a", device_id="device-a", now=NOW)
    cancel_operation(
        session_id="ses-schedule",
        instance_id="instance-a",
        operation_id="wake-up",
        now=NOW + timedelta(seconds=1),
    )
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        index_name = "idx_system_schedule_attempts_sticky_claim"
        migrated_ddl = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).scalar_one()
        model_index = next(
            index for index in system_schedule_attempts.indexes if index.name == index_name
        )
        model_ddl = str(CreateIndex(model_index).compile(dialect=engine.dialect))
        def normalize(ddl):
            return " ".join(ddl.replace("IF NOT EXISTS ", "").split()).lower()

        assert normalize(migrated_ddl) == normalize(model_ddl)
        assert "WHERE" not in migrated_ddl.upper()
        assert [column.name for column in model_index.columns] == [
            "action", "claim_state", "instance_id", "device_id",
            "terminal_state", "updated_at", "request_id",
        ]
        history = dict(
            conn.execute(
                select(system_schedule_attempts).where(
                    system_schedule_attempts.c.action == "create"
                )
            ).mappings().one()
        )
        assert history["claim_state"] == "claimed"
        assert history["terminal_state"] == "cancelled"
        assert history["receipt_json"] is None
        raw = conn.connection.driver_connection
        query_plans = []

        def measure(*, instance_id="instance-a", device_id="device-a", excluded=()):
            steps = 0
            statements = []

            def progress():
                nonlocal steps
                steps += 1
                return 0

            def capture(_conn, _cursor, statement, parameters, _context, _many):
                statements.append((statement, parameters))

            event.listen(conn, "before_cursor_execute", capture)
            raw.set_progress_handler(progress, 1)
            try:
                result = _sticky_attempt(
                    conn,
                    action="create",
                    instance_id=instance_id,
                    device_id=device_id,
                    excluded=excluded,
                )
            finally:
                raw.set_progress_handler(None, 0)
                event.remove(conn, "before_cursor_execute", capture)
            assert len(statements) == 1
            statement, parameters = statements[0]
            assert "INDEXED BY" not in statement
            plan = " ".join(
                row[3]
                for row in conn.exec_driver_sql(
                    f"EXPLAIN QUERY PLAN {statement}", parameters
                )
            )
            assert "SEARCH system_schedule_attempts" in plan
            assert index_name in plan
            assert "terminal_state=?" in plan
            assert "SCAN system_schedule_attempts" not in plan
            assert "USE TEMP B-TREE" not in plan
            query_plans.append(plan)
            return result, steps

        rows = []
        for index in range(10_009):
            row = dict(history)
            row.update(
                operation_id=f"history-{index}",
                attempt_key=f"history-{index}:create:0",
                request_id=f"history-request-{index}",
                lease_token=f"history-lease-{index}",
            )
            if index % 3 == 0:
                row.update(
                    claim_state="settled",
                    terminal_state="scheduled",
                    receipt_json='{"state":"scheduled"}',
                )
            elif index % 3 == 1:
                row["terminal_state"] = "failed"
            rows.append(row)
        empty, single_steps = measure()
        assert empty is None
        conn.execute(system_schedule_attempts.insert(), rows[:9])
        empty, small_steps = measure()
        assert empty is None
        conn.execute(system_schedule_attempts.insert(), rows[9:])
        empty, large_steps = measure()
        assert empty is None
        assert large_steps <= small_steps + 200, (small_steps, large_steps)
        assert large_steps <= single_steps + 200, (single_steps, large_steps)
        for suffix, instance_id, device in [
            ("a", "instance-a", "device-a"),
            ("b", "instance-a", "device-a"),
            ("foreign", "instance-a", "device-b"),
            ("other-instance", "instance-b", "device-a"),
        ]:
            live = dict(history)
            live.update(
                operation_id=f"live-{suffix}",
                attempt_key=f"live-{suffix}:create:0",
                request_id=f"live-{suffix}",
                lease_token=f"live-lease-{suffix}",
                instance_id=instance_id,
                device_id=device,
                terminal_state=None,
            )
            conn.execute(system_schedule_attempts.insert(), live)
        result, live_steps = measure()
        assert result["request_id"] == "live-a"
        assert live_steps <= small_steps + 300, (small_steps, live_steps)
        second, _ = measure(excluded=("live-lease-a",))
        assert second["request_id"] == "live-b"
        assert measure(excluded=("live-lease-a", "live-lease-b"))[0] is None
        foreign, _ = measure(device_id="device-b")
        assert foreign["request_id"] == "live-foreign"
        other, _ = measure(instance_id="instance-b")
        assert other["request_id"] == "live-other-instance"
        print(
            f"sticky actual query: history=1/10/10010 steps={single_steps}/{small_steps}/{large_steps} "
            f"live_steps={live_steps} plans={query_plans} migrated_ddl={migrated_ddl}"
        )


def test_poll_and_expiry_queries_search_without_forcing_an_index(isolated_state):
    engine = create_sqlite_engine()
    queries = {
        "idx_system_schedule_attempts_expiry": (
            "SELECT request_id FROM system_schedule_attempts WHERE action='create' "
            "AND claim_state='pending' AND terminal_state IS NULL AND deadline <= ?"
        ),
        "idx_system_schedule_attempts_pending_claim": (
            "SELECT request_id FROM system_schedule_attempts WHERE action='create' "
            "AND claim_state='pending' AND terminal_state IS NULL AND instance_id=? "
            "AND device_id IS NULL ORDER BY created_at, request_id LIMIT 1"
        ),
        "idx_system_schedule_attempts_sticky_claim": (
            "SELECT request_id FROM system_schedule_attempts WHERE action='cancel' "
            "AND claim_state='claimed' AND instance_id=? AND device_id=? "
            "AND terminal_state IS NULL AND receipt_json IS NULL "
            "ORDER BY updated_at, request_id LIMIT 1"
        ),
    }
    with engine.begin() as conn:
        names = {
            row["name"]
            for row in conn.execute(
                select(text("name")).select_from(text("pragma_index_list('system_schedule_attempts')"))
            ).mappings()
        }
        assert set(queries) <= names
        for index_name, sql in queries.items():
            params = (
                ("2026-11-16T15:00:00.000Z",)
                if index_name.endswith("expiry")
                else (("instance-a", "device-a") if index_name.endswith("sticky_claim") else ("instance-a",))
            )
            plan = " ".join(
                row[3]
                for row in conn.exec_driver_sql(
                    f"EXPLAIN QUERY PLAN {sql}",
                    params,
                )
            )
            assert "SEARCH system_schedule_attempts" in plan
            assert "SCAN system_schedule_attempts" not in plan
            assert "USE TEMP B-TREE" not in plan


def test_attempt_rows_keep_every_cancel_generation(isolated_state):
    _schedule()
    for generation in (1, 2):
        cancel_operation(
            session_id="ses-schedule",
            instance_id="instance-a",
            operation_id="wake-up",
            expires_in=30,
            now=NOW + timedelta(seconds=generation),
        )
        claim = claim_next(
            instance_id="instance-a",
            device_id="device-a",
            now=NOW + timedelta(seconds=generation),
        )
        _record(
            claim,
            _receipt(
                claim,
                state="failed",
                error={"code": "remove_failed", "message": "Try again."},
            ),
            now=NOW + timedelta(seconds=generation, milliseconds=1),
        )
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            select(
                system_schedule_attempts.c.generation,
                system_schedule_attempts.c.terminal_state,
                system_schedule_attempts.c.attempt_key,
            )
            .where(system_schedule_attempts.c.operation_id == "wake-up")
            .where(system_schedule_attempts.c.action == "cancel")
            .order_by(system_schedule_attempts.c.generation)
        ).all()
    assert rows == [
        (1, "failed", '["wake-up","cancel",1]'),
        (2, "failed", '["wake-up","cancel",2]'),
    ]


@pytest.mark.parametrize("vector", json.loads(CONTRACT_VECTORS.read_text())["claimFilters"], ids=lambda vector: vector["name"])
def test_claim_kinds_filter_only_unbound_creates(isolated_state, vector):
    kinds, expected = vector["kinds"], vector["firstKind"]
    for kind in ("reminder", "alarm"):
        create_operation(
            session_id="ses-schedule", instance_id="instance-a", operation_id=f"kind-{kind}",
            kind=kind, title=kind, fire_at=FIRE_AT, time_zone="America/Los_Angeles", now=NOW - timedelta(seconds=1) if kind == "reminder" else NOW,
        )
    claim = claim_next(instance_id="instance-a", device_id="device-a", kinds=kinds, now=NOW)
    assert (claim["request"]["kind"] if claim else None) == expected
    if claim:
        assert claim_next(instance_id="instance-a", device_id="device-a", kinds=[], now=NOW) == claim


@pytest.mark.parametrize("kinds", ["alarm", ["calendar"], ["alarm", "alarm"], [None], {}])
def test_claim_rejects_invalid_kinds(isolated_state, kinds):
    with pytest.raises(SystemScheduleServiceError) as caught:
        claim_next(instance_id="instance-a", device_id="device-a", kinds=kinds, now=NOW)
    assert caught.value.code == "invalid_kinds"
