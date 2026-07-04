"""enforce case-folded vault secret name uniqueness

Revision ID: 20260704_0027
Revises: 20260703_0026
Create Date: 2026-07-04
"""

from __future__ import annotations

from alembic import op

revision = "20260704_0027"
down_revision = "20260703_0026"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return bind.exec_driver_sql(
        "select 1 from sqlite_master where type = 'table' and name = ?",
        (name,),
    ).first() is not None


def _ensure_no_case_only_duplicates() -> None:
    bind = op.get_bind()
    duplicate = bind.exec_driver_sql(
        """
        select lower(name) as folded_name, group_concat(name, ', ') as names
        from vault_secrets
        group by lower(name)
        having count(*) > 1
        limit 1
        """
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "cannot add vault secret folded-name uniqueness index; "
            f"case-only duplicates already exist for {duplicate[0]!r}: {duplicate[1]}"
        )


def _ensure_no_pending_provision_case_only_duplicates() -> None:
    if not _table_exists("vault_requests"):
        return
    bind = op.get_bind()
    duplicate = bind.exec_driver_sql(
        """
        select lower(secret_name) as folded_name, group_concat(distinct secret_name) as names
        from vault_requests
        where request_type = 'provision'
          and status = 'pending'
          and secret_name is not null
        group by lower(secret_name)
        having count(distinct secret_name) > 1
        limit 1
        """
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "cannot add vault pending provision folded-name guard; "
            f"case-only pending requests already exist for {duplicate[0]!r}: {duplicate[1]}"
        )


def _create_pending_provision_case_triggers() -> None:
    if not _table_exists("vault_requests"):
        return
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        create trigger if not exists trg_vault_requests_pending_provision_name_case_insert
        before insert on vault_requests
        when new.request_type = 'provision'
          and new.status = 'pending'
          and new.secret_name is not null
          and exists (
            select 1
            from vault_requests
            where request_type = 'provision'
              and status = 'pending'
              and secret_name is not null
              and lower(secret_name) = lower(new.secret_name)
              and secret_name <> new.secret_name
          )
        begin
          select raise(abort, 'vault pending provision name case conflict');
        end
        """
    )
    bind.exec_driver_sql(
        """
        create trigger if not exists trg_vault_requests_pending_provision_name_case_update
        before update of request_type, status, secret_name on vault_requests
        when new.request_type = 'provision'
          and new.status = 'pending'
          and new.secret_name is not null
          and exists (
            select 1
            from vault_requests
            where id <> new.id
              and request_type = 'provision'
              and status = 'pending'
              and secret_name is not null
              and lower(secret_name) = lower(new.secret_name)
              and secret_name <> new.secret_name
          )
        begin
          select raise(abort, 'vault pending provision name case conflict');
        end
        """
    )


def upgrade() -> None:
    if not _table_exists("vault_secrets"):
        _ensure_no_pending_provision_case_only_duplicates()
        _create_pending_provision_case_triggers()
        return
    bind = op.get_bind()
    _ensure_no_case_only_duplicates()
    bind.exec_driver_sql("create unique index if not exists uq_vault_secrets_name_folded on vault_secrets (lower(name))")
    _ensure_no_pending_provision_case_only_duplicates()
    _create_pending_provision_case_triggers()


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("drop trigger if exists trg_vault_requests_pending_provision_name_case_update")
    bind.exec_driver_sql("drop trigger if exists trg_vault_requests_pending_provision_name_case_insert")
    if _table_exists("vault_secrets"):
        op.drop_index("uq_vault_secrets_name_folded", table_name="vault_secrets", if_exists=True)
