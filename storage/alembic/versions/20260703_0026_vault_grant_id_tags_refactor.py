"""vault grant-id and tag selector refactor

Revision ID: 20260703_0026
Revises: 20260627_0025
Create Date: 2026-07-03
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "20260703_0026"
down_revision = "20260627_0025"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return bind.exec_driver_sql(
        "select 1 from sqlite_master where type = 'table' and name = ?",
        (name,),
    ).first() is not None


def _columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.exec_driver_sql(f'pragma table_info("{table_name}")')}


def _create_vault_secrets(table_name: str = "vault_secrets") -> None:
    op.create_table(
        table_name,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("tags", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False, server_default="static"),
        sa.Column("protection", sa.String(), nullable=False, server_default="standard"),
        sa.Column("signer_kind", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="manual"),
        sa.Column("ciphertext", sa.Text(), nullable=True),
        sa.Column("nonce", sa.Text(), nullable=True),
        sa.Column("wrap_meta", sa.Text(), nullable=True),
        sa.Column("public_meta", sa.Text(), nullable=True),
        sa.Column("policy", sa.Text(), nullable=True),
        sa.Column("last_used_at", sa.String(), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.UniqueConstraint("name", name=f"uq_{table_name}_name" if table_name != "vault_secrets" else "uq_vault_secrets_name"),
    )


def _create_vault_grants() -> None:
    op.create_table(
        "vault_grants",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("member_snapshot", sa.Text(), nullable=False),
        sa.Column("source_selector", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("purpose", sa.String(), nullable=False, server_default="run"),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("one_shot", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=False),
        sa.Column("revoked_at", sa.String(), nullable=True),
        sa.Column("agent_ready", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("agent_ready_at", sa.String(), nullable=True),
    )


def _merge_json_tags(raw: str | None, added_tags: set[str]) -> str:
    try:
        current = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        current = []
    tags = [str(tag) for tag in current if isinstance(tag, str) and tag]
    seen = set(tags)
    for tag in sorted(added_tags):
        if tag not in seen:
            tags.append(tag)
            seen.add(tag)
    return json.dumps(tags)


def _preserve_skill_links_as_tags() -> None:
    if not _table_exists("vault_links") or not _table_exists("vault_secrets") or "tags" not in _columns("vault_secrets"):
        return
    bind = op.get_bind()
    links: dict[str, set[str]] = {}
    for row in bind.exec_driver_sql("select secret_name, skill_name from vault_links").mappings():
        secret_name = str(row.get("secret_name") or "").strip()
        skill_name = str(row.get("skill_name") or "").strip()
        if not secret_name or not skill_name:
            continue
        tag = skill_name if skill_name.startswith("skill:") else f"skill:{skill_name}"
        links.setdefault(secret_name, set()).add(tag)
    for secret_name, added_tags in links.items():
        current = bind.exec_driver_sql("select tags from vault_secrets where name = ?", (secret_name,)).first()
        if current is None:
            continue
        bind.exec_driver_sql(
            "update vault_secrets set tags = ? where name = ?",
            (_merge_json_tags(current[0], added_tags), secret_name),
        )


def _pending_access_card_is_legacy(delivery_raw: str | None) -> bool:
    try:
        delivery = json.loads(delivery_raw) if delivery_raw else {}
    except json.JSONDecodeError:
        return True
    if not isinstance(delivery, dict):
        return True
    card = delivery.get("card")
    if not isinstance(card, dict):
        return True
    if "scope_options" in card:
        return True
    options = card.get("grant_options")
    if not isinstance(options, list) or len(options) != 1 or not isinstance(options[0], dict):
        return True
    return not bool(str(options[0].get("grant_id") or "").strip())


def _retire_legacy_pending_access_requests() -> None:
    if not _table_exists("vault_requests"):
        return
    bind = op.get_bind()
    now = datetime.now(timezone.utc).isoformat()
    legacy_ids = [
        str(row["id"])
        for row in bind.exec_driver_sql(
            """
            select id, delivery
            from vault_requests
            where request_type = 'access' and status = 'pending'
            """
        ).mappings()
        if _pending_access_card_is_legacy(row.get("delivery"))
    ]
    for request_id in legacy_ids:
        bind.exec_driver_sql(
            "update vault_requests set status = 'expired', decided_at = ? where id = ? and status = 'pending'",
            (now, request_id),
        )


def upgrade() -> None:
    bind = op.get_bind()

    _retire_legacy_pending_access_requests()

    if _table_exists("vault_links"):
        _preserve_skill_links_as_tags()
        op.drop_index("ix_vault_links_skill", table_name="vault_links", if_exists=True)
        op.drop_table("vault_links")

    if _table_exists("vault_secrets") and "group_name" in _columns("vault_secrets"):
        op.drop_index("ix_vault_secrets_group", table_name="vault_secrets", if_exists=True)
        _create_vault_secrets("vault_secrets_new")
        bind.exec_driver_sql(
            """
            insert into vault_secrets_new (
                id, name, tags, kind, protection, signer_kind, source,
                ciphertext, nonce, wrap_meta, public_meta, policy,
                last_used_at, use_count, created_at, updated_at
            )
            select
                id, name, tags, kind, protection, signer_kind, source,
                ciphertext, nonce, wrap_meta, public_meta, policy,
                last_used_at, use_count, created_at, updated_at
            from vault_secrets
            """
        )
        op.drop_table("vault_secrets")
        op.rename_table("vault_secrets_new", "vault_secrets")

    if _table_exists("vault_secrets"):
        op.create_index("ix_vault_secrets_name_kind", "vault_secrets", ["name", "kind"], if_not_exists=True)

    if _table_exists("vault_groups"):
        op.drop_table("vault_groups")

    if _table_exists("vault_grants"):
        grant_cols = _columns("vault_grants")
        if {"scope_type", "scope_ref"} & grant_cols or "request_id" not in grant_cols:
            op.drop_index("ix_vault_grants_status_expires", table_name="vault_grants", if_exists=True)
            op.drop_table("vault_grants")
            _create_vault_grants()
    else:
        _create_vault_grants()

    op.create_index("ix_vault_grants_status_expires", "vault_grants", ["status", "expires_at"], if_not_exists=True)
    op.create_index("ix_vault_grants_request", "vault_grants", ["request_id"], if_not_exists=True)
    op.create_index("ix_vault_grants_session_purpose", "vault_grants", ["session_id", "purpose"], if_not_exists=True)


def downgrade() -> None:
    # Vaults has not launched; keep the forward-only refactor simple.
    return None
