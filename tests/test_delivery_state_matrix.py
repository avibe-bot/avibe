from __future__ import annotations

import re

from storage.delivery_states import (
    ADMITTED_DELIVERY_STATES,
    CLAIMABLE_QUEUE_STATES,
    DELIVERY_STATE_MATRIX,
    DELIVERY_STATES,
    FENCE_STATES,
    RUN_CANCEL_RETIRE_STATES,
)
from storage.models import message_deliveries


def test_delivery_state_matrix_matches_database_state_constraint() -> None:
    constraint = next(
        item
        for item in message_deliveries.constraints
        if item.name == "ck_message_deliveries_state"
    )
    database_states = set(re.findall(r"'([^']+)'", str(constraint.sqltext)))

    assert database_states == set(DELIVERY_STATE_MATRIX)
    assert tuple(DELIVERY_STATE_MATRIX) == DELIVERY_STATES


def test_delivery_state_matrix_matches_migrated_sqlite_constraint(
    monkeypatch,
    tmp_path,
) -> None:
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    with create_sqlite_engine().connect() as conn:
        ddl = conn.exec_driver_sql(
            "select sql from sqlite_master where type='table' and name='message_deliveries'"
        ).scalar_one()
    match = re.search(
        r"CONSTRAINT ck_message_deliveries_state CHECK \(state in \((.*?)\)\)",
        ddl,
        flags=re.DOTALL,
    )

    assert match is not None
    assert set(re.findall(r"'([^']+)'", match.group(1))) == set(DELIVERY_STATE_MATRIX)


def test_delivery_state_matrix_derives_every_cross_cutting_state_set() -> None:
    assert CLAIMABLE_QUEUE_STATES == tuple(
        state
        for state, policy in DELIVERY_STATE_MATRIX.items()
        if policy.ordering == "claimable"
    )
    assert FENCE_STATES == tuple(
        state
        for state, policy in DELIVERY_STATE_MATRIX.items()
        if policy.ordering == "fence"
    )
    assert RUN_CANCEL_RETIRE_STATES == tuple(
        state
        for state, policy in DELIVERY_STATE_MATRIX.items()
        if policy.run_cancel == "retire"
    )
    assert ADMITTED_DELIVERY_STATES == tuple(
        state
        for state, policy in DELIVERY_STATE_MATRIX.items()
        if policy.submission == "admitted"
    )

    for policy in DELIVERY_STATE_MATRIX.values():
        if policy.run_cancel == "retire":
            assert policy.native_effect == "none"

