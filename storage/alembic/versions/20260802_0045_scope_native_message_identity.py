"""scope native message identity to its conversation

Revision ID: 20260802_0045
Revises: 20260801_0044
Create Date: 2026-08-02
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "20260802_0045"
down_revision = "20260801_0044"
branch_labels = None
depends_on = None

_SCOPED_INDEX = "uq_messages_platform_scope_native"
_UNSCOPED_INDEX = "uq_messages_platform_native_unscoped"
_OLD_CONSTRAINT = "uq_messages_platform_native"


def _scoped_delivery_key(platform: str, scope_id: str, native_id: str) -> str:
    return f"{platform}:scope:{len(scope_id)}:{scope_id}:{native_id}"


def _delivery_identities(bind) -> list[dict[str, object]]:
    rows = bind.execute(
        sa.text(
            """
            select d.id, d.dedupe_key, d.snapshot_json, d.message_id,
                   m.platform as message_platform,
                   m.scope_id as message_scope_id,
                   m.native_message_id as message_native_message_id
              from message_deliveries d
              left join messages m on m.id = d.message_id
             where d.dedupe_key is not null
            """
        )
    ).mappings()
    identities: list[dict[str, object]] = []
    for row in rows:
        snapshot: dict[str, object] = {}
        try:
            parsed = json.loads(str(row["snapshot_json"] or "{}"))
            if isinstance(parsed, dict):
                snapshot = parsed
        except (TypeError, ValueError, json.JSONDecodeError):
            snapshot = {}
        platform = str(snapshot.get("platform") or row["message_platform"] or "")
        scope_id = str(snapshot.get("scope_id") or row["message_scope_id"] or "")
        recorded_native_id = str(
            snapshot.get("native_message_id")
            or row["message_native_message_id"]
            or ""
        )
        current_key = str(row["dedupe_key"])
        scoped_prefix = (
            f"{platform}:scope:{len(scope_id)}:{scope_id}:"
            if platform and scope_id
            else ""
        )
        if scoped_prefix and current_key.startswith(scoped_prefix):
            native_id = current_key.removeprefix(scoped_prefix)
        elif platform and current_key.startswith(f"{platform}:"):
            native_id = current_key.removeprefix(f"{platform}:")
        else:
            native_id = recorded_native_id
        identities.append(
            {
                "id": str(row["id"]),
                "dedupe_key": current_key,
                "platform": platform,
                "scope_id": scope_id,
                "native_id": native_id,
            }
        )
    return identities


def _rewrite_delivery_keys(bind, *, scoped: bool) -> None:
    updates: list[dict[str, str]] = []
    seen: dict[str, str] = {}
    for identity in _delivery_identities(bind):
        delivery_id = str(identity["id"])
        current = str(identity["dedupe_key"])
        platform = str(identity["platform"])
        scope_id = str(identity["scope_id"])
        native_id = str(identity["native_id"])
        old_key = f"{platform}:{native_id}" if platform and native_id else current
        scoped_key = (
            _scoped_delivery_key(platform, scope_id, native_id)
            if platform and scope_id and native_id
            else old_key
        )
        if scoped:
            target = scoped_key if current == old_key else current
        else:
            target = old_key if current == scoped_key else current
        previous = seen.get(target)
        if previous is not None and previous != delivery_id:
            raise RuntimeError(
                "cannot downgrade 0045: conversation-scoped Delivery identities "
                "would collide"
            )
        seen[target] = delivery_id
        if target != current:
            updates.append({"id": delivery_id, "dedupe_key": target})
    if updates:
        bind.execute(
            sa.text(
                "update message_deliveries set dedupe_key=:dedupe_key where id=:id"
            ),
            updates,
        )


def _index_names(bind) -> set[str]:
    return {
        str(name)
        for name in bind.execute(
            sa.text(
                "select name from sqlite_master "
                "where type='index' and tbl_name='messages'"
            )
        ).scalars()
    }


def _has_old_constraint(bind) -> bool:
    return any(
        constraint.get("name") == _OLD_CONSTRAINT
        for constraint in sa.inspect(bind).get_unique_constraints("messages")
    )


def _snapshot_message_references(bind) -> None:
    bind.execute(
        sa.text(
            "create temporary table _0045_show_message_refs as "
            "select id, message_id from show_session_events where message_id is not null"
        )
    )
    bind.execute(
        sa.text(
            "create temporary table _0045_media_message_refs as "
            "select token, message_id from media_objects where message_id is not null"
        )
    )
    bind.execute(
        sa.text(
            "create temporary table _0045_delivery_message_refs as "
            "select id, message_id from message_deliveries where message_id is not null"
        )
    )
    bind.execute(sa.text("pragma ignore_check_constraints=on"))
    bind.execute(
        sa.text(
            "update message_deliveries set message_id=null where message_id is not null"
        )
    )


def _snapshot_message_indexes(bind) -> None:
    bind.execute(
        sa.text(
            "create temporary table _0045_message_indexes as "
            "select name, sql from sqlite_master "
            "where type='index' and tbl_name='messages' and sql is not null"
        )
    )


def _restore_message_indexes(bind) -> None:
    indexes = list(
        bind.execute(
            sa.text("select name, sql from _0045_message_indexes order by name")
        ).mappings()
    )
    for index in indexes:
        bind.execute(sa.text(f'drop index if exists "{index["name"]}"'))
        bind.execute(sa.text(str(index["sql"])))
    bind.execute(sa.text("drop table _0045_message_indexes"))


def _restore_message_references(bind) -> None:
    bind.execute(
        sa.text(
            "update show_session_events set message_id = ("
            "select message_id from _0045_show_message_refs refs "
            "where refs.id = show_session_events.id) "
            "where id in (select id from _0045_show_message_refs)"
        )
    )
    bind.execute(
        sa.text(
            "update media_objects set message_id = ("
            "select message_id from _0045_media_message_refs refs "
            "where refs.token = media_objects.token) "
            "where token in (select token from _0045_media_message_refs)"
        )
    )
    bind.execute(
        sa.text(
            "update message_deliveries set message_id = ("
            "select message_id from _0045_delivery_message_refs refs "
            "where refs.id = message_deliveries.id) "
            "where id in (select id from _0045_delivery_message_refs)"
        )
    )
    bind.execute(sa.text("pragma ignore_check_constraints=off"))
    bind.execute(sa.text("drop table _0045_show_message_refs"))
    bind.execute(sa.text("drop table _0045_media_message_refs"))
    bind.execute(sa.text("drop table _0045_delivery_message_refs"))


def upgrade() -> None:
    bind = op.get_bind()
    if _has_old_constraint(bind):
        _snapshot_message_references(bind)
        _snapshot_message_indexes(bind)
        try:
            with op.batch_alter_table("messages") as batch_op:
                batch_op.drop_constraint(_OLD_CONSTRAINT, type_="unique")
            _restore_message_indexes(bind)
            _restore_message_references(bind)
        except Exception:
            bind.execute(sa.text("pragma ignore_check_constraints=off"))
            raise
    indexes = _index_names(bind)
    if _SCOPED_INDEX not in indexes:
        op.create_index(
            _SCOPED_INDEX,
            "messages",
            ["platform", "scope_id", "native_message_id"],
            unique=True,
            sqlite_where=sa.text(
                "scope_id is not null and native_message_id is not null"
            ),
        )
    if _UNSCOPED_INDEX not in indexes:
        op.create_index(
            _UNSCOPED_INDEX,
            "messages",
            ["platform", "native_message_id"],
            unique=True,
            sqlite_where=sa.text(
                "scope_id is null and native_message_id is not null"
            ),
        )
    if sa.inspect(bind).has_table("message_deliveries"):
        _rewrite_delivery_keys(bind, scoped=True)


def downgrade() -> None:
    bind = op.get_bind()
    collision = bind.execute(
        sa.text(
            """
            select platform, native_message_id
              from messages
             where native_message_id is not null
             group by platform, native_message_id
            having count(*) > 1
             limit 1
            """
        )
    ).first()
    if collision is not None:
        raise RuntimeError(
            "cannot downgrade 0045: conversation-scoped Message identities would collide"
        )
    if sa.inspect(bind).has_table("message_deliveries"):
        _rewrite_delivery_keys(bind, scoped=False)
    indexes = _index_names(bind)
    if _UNSCOPED_INDEX in indexes:
        op.drop_index(_UNSCOPED_INDEX, table_name="messages")
    if _SCOPED_INDEX in indexes:
        op.drop_index(_SCOPED_INDEX, table_name="messages")
    _snapshot_message_references(bind)
    _snapshot_message_indexes(bind)
    try:
        with op.batch_alter_table("messages") as batch_op:
            batch_op.create_unique_constraint(
                _OLD_CONSTRAINT,
                ["platform", "native_message_id"],
            )
        _restore_message_indexes(bind)
        _restore_message_references(bind)
    except Exception:
        bind.execute(sa.text("pragma ignore_check_constraints=off"))
        raise
