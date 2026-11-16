"""The persisted SQLite reader/writer contract, independent of Alembic ordering.

One JSON integer in state_meta. A supported epoch permits an older binary to
open a newer exact revision, but never to migrate, repair or import it. Raising
this number is a release boundary, not an application write or feature toggle.
"""
from __future__ import annotations

import json
import sqlite3

from sqlalchemy import Connection

COMPATIBILITY_EPOCH_KEY = "compatibility_epoch"
# Epoch 2 activates C-alt's projection-only tool steps. RC19 shipped support for
# both epochs before the representation transition; older readers must refuse.
VIBEY_TOOL_PROJECTION_EPOCH = 2
# 3.2.6 shipped the epoch-3 reader bridge before 3.2.7's coordinated activation.
# Reader admission remains independent of the writer capability below.
READER_COMPATIBILITY_EPOCH = 3
SUPPORTED_COMPATIBILITY_EPOCHS = frozenset({1, VIBEY_TOOL_PROJECTION_EPOCH, READER_COMPATIBILITY_EPOCH})
BRIDGE_REVISION = "20261108_0076"
PRE_BRIDGE_REVISION = "20261107_0075"
# The actual 3.2.6 wheel passed the epoch-3 read/write/cleanup round trip,
# including existing tool projection. CI rechecks; the publisher independently
# fills this literal before packaging. None keeps unverified builds conservative.
# This is binary capability, not a persisted DB marker/version gate.
VERIFIED_PREDECESSOR_EPOCH: int | None = 3
COORDINATED_WRITER_EPOCH = 3


def permits_compact_writes(conn: Connection) -> bool:
    """All three writers share the same released-reader proof and DB boundary."""
    return (
        VERIFIED_PREDECESSOR_EPOCH == COORDINATED_WRITER_EPOCH
        and read_compatibility_epoch(conn.connection.driver_connection) == COORDINATED_WRITER_EPOCH
    )


def permits_tool_trace_suppression(conn: Connection) -> bool:
    """Writer gate, after verifying the exact committed response/call projection.

    The build's released-reader proof and this transaction's database marker
    must both authorize the representation. Never infer it from Session.backend.
    """
    if VERIFIED_PREDECESSOR_EPOCH not in (VIBEY_TOOL_PROJECTION_EPOCH, COORDINATED_WRITER_EPOCH):
        return False
    epoch = read_compatibility_epoch(conn.connection.driver_connection)
    return epoch == VIBEY_TOOL_PROJECTION_EPOCH or (
        epoch == COORDINATED_WRITER_EPOCH and VERIFIED_PREDECESSOR_EPOCH == COORDINATED_WRITER_EPOCH
    )


class CompatibilityEpochError(ValueError):
    """The marker is malformed, unsupported, or would move backwards."""


def parse_compatibility_epoch(value: object) -> int:
    """Read a strict positive JSON integer; parsing is not reader compatibility."""
    try:
        epoch = json.loads(value) if isinstance(value, (str, bytes)) else None
    except (ValueError, TypeError) as exc:
        raise CompatibilityEpochError("malformed compatibility_epoch (expected a JSON integer)") from exc
    if type(epoch) is not int or epoch < 1:
        raise CompatibilityEpochError(f"malformed compatibility_epoch {epoch!r}; expected a positive JSON integer")
    return epoch


def require_supported_compatibility_epoch(epoch: int) -> None:
    """Admission for application reads/writes, not for a known forward migration."""
    if epoch not in SUPPORTED_COMPATIBILITY_EPOCHS:
        raise CompatibilityEpochError(
            f"unsupported compatibility_epoch {epoch}; supported: {sorted(SUPPORTED_COMPATIBILITY_EPOCHS)}"
        )


def read_compatibility_epoch(conn: sqlite3.Connection) -> int | None:
    """Read on the caller's snapshot. Missing marker means pre-protocol, not epoch 0."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'state_meta'").fetchone():
        return None
    columns = {row[1] for row in conn.execute('PRAGMA table_info("state_meta")')}
    if not {"key", "value_json"}.issubset(columns):
        raise CompatibilityEpochError("compatibility_epoch metadata schema is incomplete")
    row = conn.execute("SELECT value_json FROM state_meta WHERE key = ?", (COMPATIBILITY_EPOCH_KEY,)).fetchone()
    return None if row is None else parse_compatibility_epoch(row[0])


def advance_compatibility_epoch(conn: Connection, epoch: int) -> None:
    """Migration-only: commit an epoch with the caller's explicit schema transaction.

    A semantic representation change needs a breaking migration even without DDL.
    This helper cannot lower a marker or run outside a real SQLite transaction.
    """
    parse_compatibility_epoch(json.dumps(epoch))
    if not conn.connection.driver_connection.in_transaction:
        raise CompatibilityEpochError("epoch changes require an explicit SQLite transaction")
    row = conn.exec_driver_sql(
        "SELECT value_json FROM state_meta WHERE key = ?", (COMPATIBILITY_EPOCH_KEY,)
    ).first()
    if row is not None and parse_compatibility_epoch(row[0]) > epoch:
        raise CompatibilityEpochError("refusing to lower compatibility_epoch; use an explicit verified restore")
    conn.exec_driver_sql(
        "INSERT INTO state_meta(key, value_json, updated_at) VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) "
        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
        (COMPATIBILITY_EPOCH_KEY, json.dumps(epoch)),
    )
