"""Reader-first audit references preserve per-request history and historical ACLs.

The existing Vault tests only exercise inline snapshots. These cases protect the
new reference decoder, batching, and unchanged writer against premature cutover.
"""
from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import event, select

from storage import migrations, resource_access_service, vault_service as vs
from storage.db import create_sqlite_engine
from storage.migration_policy import classify_migration
from storage.models import metadata, vault_audit, vault_audit_access_snapshots, vault_secrets
from storage.vault_crypto import Sealed
from vibe import api


@pytest.fixture
def vault(tmp_path):
    engine = create_sqlite_engine(tmp_path / "audit.sqlite")
    metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _member():
    # Not the validated remote Editor fast path: this reader must use the
    # historical resource ACL rather than instance-wide audit administration.
    return resource_access_service.ResourceUserContext(
        subject="reader", organization_id="org", organization_member_id="member",
        organization_role="member", instance_role="editor", is_remote=False,
        instance_access_source="organization_group",
    )


def _secret_snapshot(conn, *, access_level="public"):
    vs.create_secret(conn, name="KEY", sealed=Sealed("ct", "nonce", "wrap"))
    resource_id = conn.execute(select(vault_secrets.c.id).where(vault_secrets.c.name == "KEY")).scalar_one()
    resource_access_service.ensure_resource_policy(
        conn, resource_kind="vault_secret", resource_id=resource_id,
        organization_id="org", owner_user_id="other", access_level=access_level,
        group_ids=[], policy_revision=1, last_applied_control_plane_revision=1,
    )
    return vs._build_audit_access_snapshot(conn, ["KEY"])


def _row(identifier, snapshot, *, event_name="proxied"):
    return {
        "id": identifier, "ts": "2026-11-12T01:02:03.456789+00:00",
        "event": event_name, "secret_name": "KEY", "requester": '{"session_id": "session"}',
        "delivery": json.dumps({"host": "example.test", "method": "POST", "status": 201,
                                "field": "value", "_resource_access_snapshot": snapshot}),
        "request_id": "request", "grant_id": "grant",
    }


def test_inline_and_reference_history_match_through_api_after_secret_recreation(vault, monkeypatch):
    with vault.begin() as conn:
        snapshot = _secret_snapshot(conn)
        private_snapshot = deepcopy(snapshot)
        private_snapshot["resources"][0]["policy"]["access_level"] = "private"
        conn.execute(vault_audit_access_snapshots.insert(), [
            {"id": 1, "snapshot": json.dumps(snapshot)},
            {"id": 2, "snapshot": json.dumps(private_snapshot)},
        ])
        rows = [_row("inline", snapshot), _row("reference", {"ref": 1}),
                _row("private-inline", private_snapshot), _row("private-reference", {"ref": 2})]
        conn.execute(vault_audit.insert(), rows)
        # Neither a changed current ACL, deletion, nor reuse of the name may
        # replace the authority captured when the original egress happened.
        vs.delete_secret(conn, "KEY")
        _secret_snapshot(conn, access_level="private")

    monkeypatch.setattr(api, "_vault_engine", lambda: vault)
    monkeypatch.setattr(api, "resolve_resource_access_context", _member)
    visible = api.get_vault_audit(secret_name="KEY")["events"]
    actual = {row["id"]: row for row in visible if row["event"] == "proxied"}
    assert set(actual) == {"inline", "reference"}
    expected_delivery = '{"host": "example.test", "method": "POST", "status": 201, "field": "value"}'
    for expected in rows[:2]:
        assert actual[expected["id"]] == {**expected, "delivery": expected_delivery}
    assert "_resource_access_snapshot" not in json.dumps(visible)
    with vault.connect() as conn:
        stored = [dict(row) for row in conn.execute(select(vault_audit).where(vault_audit.c.event == "proxied")).mappings()]
    assert stored == rows  # Reading/hydrating never rewrites persisted history.


@pytest.mark.parametrize("reference", [
    {"ref": 999}, {"ref": True}, {"ref": 0}, {"ref": -1}, {"ref": "1"},
    {"ref": 1.0}, {"ref": 2**63}, {"ref": None}, {"ref": []},
    {"ref": 1, "version": 1, "resources": []},
])
def test_missing_or_malformed_references_never_fall_back_to_current_acl(vault, reference):
    with vault.begin() as conn:
        snapshot = _secret_snapshot(conn)  # Current ACL would allow the reader.
        conn.execute(vault_audit_access_snapshots.insert().values(id=1, snapshot=json.dumps(snapshot)))
        conn.execute(vault_audit.insert().values(**_row("broken", reference)))
        assert not [row for row in vs.list_audit(conn, user_context=_member()) if row["id"] == "broken"]
        # Administrators retain the existing complete stream and redaction.
        admin = [row for row in vs.list_audit(conn) if row["id"] == "broken"]
        assert len(admin) == 1
        assert "_resource_access_snapshot" not in admin[0]["delivery"]


def test_reference_on_old_only_database_fails_closed_without_current_acl_fallback(vault):
    with vault.begin() as conn:
        _secret_snapshot(conn)
        vault_audit_access_snapshots.drop(conn)
        conn.execute(vault_audit.insert().values(**_row("old-only-reference", {"ref": 1})))
        assert vs.list_audit(conn, user_context=_member()) == []


@pytest.mark.parametrize("payload", [
    "not-json", "null", "[]", '{"ref": 2}', '{"version": 2, "resources": []}',
    '{"version": 1, "resources": []}', '{"version": 1, "resources": [null]}',
    '{"version": 1, "resources": [{"name": "KEY", "resource_id": "id", "policy": null}]}',
])
def test_corrupt_snapshot_payload_fails_closed_without_recursive_resolution(vault, payload):
    with vault.begin() as conn:
        snapshot = _secret_snapshot(conn)
        conn.execute(vault_audit_access_snapshots.insert(), [
            {"id": 1, "snapshot": payload}, {"id": 2, "snapshot": json.dumps(snapshot)},
        ])
        conn.execute(vault_audit.insert().values(**_row("broken", {"ref": 1})))
        assert not [row for row in vs.list_audit(conn, user_context=_member()) if row["id"] == "broken"]


@pytest.mark.parametrize("representation", ["inline", "reference", "tombstone-reference"])
@pytest.mark.parametrize("damage", [
    "bool-version", "float-version", "string-version", "missing-version",
    "nested-reference", "null-reference", "bool-reference", "missing-resources",
    "null-resources", "object-resources", "empty-resources", "non-object",
])
def test_snapshot_envelope_corruption_denies_history_without_mutating_admin_stream(vault, representation, damage):
    """Valid ACLs must not hide a malformed envelope or a reference chain.

    Existing corrupt-target fixtures have no otherwise-authorizing resources,
    so they cannot catch version coercion or a ref-bearing inline target.
    Tombstones exercise the separate legacy-history fallback using this decoder.
    """
    with vault.begin() as conn:
        snapshot = _secret_snapshot(conn)
        damaged = deepcopy(snapshot)
        if damage.endswith("-version"):
            if damage == "missing-version":
                damaged.pop("version")
            else:
                damaged["version"] = {"bool-version": True, "float-version": 1.0, "string-version": "1"}[damage]
        elif damage.endswith("-reference"):
            damaged["ref"] = {"nested-reference": 2, "null-reference": None, "bool-reference": True}[damage]
        elif damage == "missing-resources":
            damaged.pop("resources")
        elif damage.endswith("-resources"):
            damaged["resources"] = {"null-resources": None, "object-resources": {}, "empty-resources": []}[damage]
        else:
            damaged = [snapshot]
        conn.execute(vault_audit_access_snapshots.insert(), [
            {"id": 1, "snapshot": json.dumps(damaged)},
            {"id": 2, "snapshot": json.dumps(snapshot)},
        ])
        if representation == "tombstone-reference":
            # Remove current policy and the genuine tombstone. Only the malformed
            # historical snapshot could authorize this older snapshot-free event.
            vs.delete_secret(conn, "KEY")
            conn.execute(vault_audit.delete())
            conn.execute(vault_audit.insert().values(**_row("tombstone", {"ref": 1}, event_name="deleted")))
            row = _row("broken", None)
            row.update(request_id=None, grant_id=None, delivery='{"host": "example.test"}')
        else:
            row = _row("broken", damaged if representation == "inline" else {"ref": 1})
        conn.execute(vault_audit.insert().values(**row))
        before = [dict(item) for item in conn.execute(select(vault_audit).order_by(vault_audit.c.id)).mappings()]
        snapshots_before = conn.execute(select(vault_audit_access_snapshots)).all()
        assert not [item for item in vs.list_audit(conn, user_context=_member()) if item["id"] == "broken"]
        admin = {item["id"]: item for item in vs.list_audit(conn)}
        expected = {**row, "delivery": ('{"host": "example.test"}' if representation == "tombstone-reference" else
                    '{"host": "example.test", "method": "POST", "status": 201, "field": "value"}')}
        assert admin["broken"] == expected
        assert [dict(item) for item in conn.execute(select(vault_audit).order_by(vault_audit.c.id)).mappings()] == before
        assert conn.execute(select(vault_audit_access_snapshots)).all() == snapshots_before


def test_reference_lookups_are_bounded_batches_using_primary_key(vault, monkeypatch):
    monkeypatch.setattr(vs, "_REMOTE_AUDIT_MIN_BATCH_SIZE", 2)
    monkeypatch.setattr(vs, "_REMOTE_AUDIT_MAX_BATCH_SIZE", 2)
    with vault.begin() as conn:
        snapshot = _secret_snapshot(conn)
        conn.execute(vault_audit.delete())
        conn.execute(vault_audit_access_snapshots.insert().values(id=1, snapshot=json.dumps(snapshot)))
        conn.execute(vault_audit.insert(), [_row(str(i), {"ref": 1}) for i in range(5)])

    reads = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and "FROM vault_audit_access_snapshots" in statement:
            reads.append((statement, parameters))

    event.listen(vault, "before_cursor_execute", capture)
    try:
        with vault.connect() as conn:
            assert len(vs.list_audit(conn, limit=5, user_context=_member())) == 5
            assert len(reads) == 3
            for statement, parameters in reads:
                assert parameters == (1,)  # Deduplicated within each batch.
                plan = conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters).all()
                assert any("SEARCH vault_audit_access_snapshots USING INTEGER PRIMARY KEY" in row[3] for row in plan)
                assert all("SCAN" not in row[3] and "TEMP B-TREE" not in row[3] for row in plan)
            reads.clear()
            assert len(vs.list_audit(conn, limit=5)) == 5
            assert reads == []  # Owner fast path does not need private ACL data.
    finally:
        event.remove(vault, "before_cursor_execute", capture)


@pytest.mark.parametrize("with_snapshot_table", [False, True])
def test_all_audit_writes_stay_byte_identical_and_inline(vault, monkeypatch, with_snapshot_table):
    monkeypatch.setattr(vs, "_now", lambda: "2026-11-12T01:02:03.456789+00:00")
    monkeypatch.setattr(vs, "_id", lambda prefix: "vau-fixed")
    with vault.begin() as conn:
        if not with_snapshot_table:
            vault_audit_access_snapshots.drop(conn)
        snapshot = {"version": 1, "resources": [{"name": "KEY", "resource_id": "id", "policy": None}]}
        for event_name in ("proxied", "created", "updated", "deleted", "delivered", "denied", "granted",
                           "signed", "revealed", "access-requested", "future-event"):
            conn.execute(vault_audit.delete())
            vs.audit(conn, event_name, secret_name="KEY", requester={"session_id": "session"},
                     delivery={"host": "example.test", "method": "POST", "status": 201, "field": "value"},
                     request_id="request", grant_id="grant", access_snapshot=snapshot)
            stored = dict(conn.execute(select(vault_audit)).mappings().one())
            assert stored == {
                "id": "vau-fixed", "ts": "2026-11-12T01:02:03.456789+00:00", "event": event_name,
                "secret_name": "KEY", "requester": '{"session_id": "session"}',
                "delivery": '{"host": "example.test", "method": "POST", "status": 201, "field": "value", '
                            '"_resource_access_snapshot": {"version": 1, "resources": [{"name": "KEY", '
                            '"resource_id": "id", "policy": null}]}}',
                "request_id": "request", "grant_id": "grant",
            }
            assert len(vs.list_audit(conn)) == 1
            assert vs.list_audit(conn, user_context=_member()) == []
        if with_snapshot_table:
            assert conn.execute(select(vault_audit_access_snapshots)).all() == []


def test_additive_migration_preserves_audit_bytes_and_epoch(tmp_path):
    db = tmp_path / "migration.sqlite"
    migrations.run_migrations(db, revision="20261108_0083")
    original = _row("old", {"version": 1, "resources": []})
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"INSERT INTO vault_audit ({', '.join(original)}) VALUES ({', '.join('?' for _ in original)})",
            tuple(original.values()),
        )
        before = conn.execute("SELECT * FROM vault_audit").fetchall()
        epoch = conn.execute("SELECT * FROM state_meta WHERE key = 'compatibility_epoch'").fetchall()
    migrations.run_migrations(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM vault_audit").fetchall() == before
        assert conn.execute("SELECT * FROM state_meta WHERE key = 'compatibility_epoch'").fetchall() == epoch
        assert conn.execute("SELECT * FROM vault_audit_access_snapshots").fetchall() == []
        # Old omitted-column writes still work after upgrading (no restamping).
        conn.execute("INSERT INTO vault_audit (id, ts, event) VALUES ('old-writer', 'timestamp', 'proxied')")
    source = Path(migrations.__file__).parent / "alembic/versions/20261112_0087_vault_audit_snapshots.py"
    assert classify_migration(source).permitted == "additive"


def test_snapshot_migration_downgrade_preserves_audit_rows_and_recreates_empty_table(tmp_path):
    db = tmp_path / "downgrade.sqlite"
    migrations.run_migrations(db, revision="20261112_0086")
    original = _row("downgrade-audit", {"version": 1, "resources": []})
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"INSERT INTO vault_audit ({', '.join(original)}) VALUES ({', '.join('?' for _ in original)})",
            tuple(original.values()),
        )
        before = conn.execute("SELECT * FROM vault_audit").fetchall()

    migrations.run_migrations(db, revision="20261112_0087")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO vault_audit_access_snapshots (id, snapshot) VALUES (1, ?)",
            (json.dumps({"version": 1, "resources": []}),),
        )
    command.downgrade(migrations.alembic_config(db), "20261112_0086")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM vault_audit").fetchall() == before
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", ("vault_audit_access_snapshots",)).fetchone() is None

    migrations.run_migrations(db, revision="20261112_0087")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM vault_audit").fetchall() == before
        assert conn.execute("SELECT * FROM vault_audit_access_snapshots").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
