"""Retire anonymous push grants at publication, or on first start of a bound upgrade.

The retired owner key is not a user: its rows are disabled, cannot deliver or
rotate in the background, and let a foreground host recognize its old grant
without conflating it with an explicit opt-out. No schema upgrade is needed.
"""

from __future__ import annotations

from contextlib import contextmanager
import json

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from config.v2_config import V2Config, config_file_lock
from storage.db import get_cached_sqlite_engine
from storage.models import state_meta, web_push_subscriptions as push
from storage.web_push_service import RETIRED_LOCAL, _utc_now_iso, get_deliverable_by_endpoint
from vibe.device_metadata import binding_key
from vibe.network_position import BindingState, binding_state, position_snapshot

_RECEIPT = "web_push.local_binding.v1"


def _generation(config):
    cloud = config.remote_access.vibe_cloud
    return json.dumps(binding_key(cloud.instance_id, cloud.session_secret))


def _receipt(conn):
    return conn.execute(select(state_meta.c.value_json).where(state_meta.c.key == _RECEIPT)).scalar_one_or_none()


def _reconcile(conn, config, *, predecessor=None, first_bind=False):
    generation = _generation(config)
    previous = _receipt(conn)
    if previous == generation and not first_bind:
        return
    # Rebinding a computer that already retired its old setup grants does
    # not throw away its subsequently proven host registrations.
    if first_bind or predecessor is None or previous != _generation(predecessor):
        conn.execute(
            push.update()
            .where(push.c.user_key == "local", (push.c.enabled == 1) | push.c.provider_invalidated_at.is_not(None))
            .values(user_key=RETIRED_LOCAL, enabled=0, provider_invalidated_at=None, updated_at=_utc_now_iso())
        )
    row = insert(state_meta).values(key=_RECEIPT, value_json=generation, updated_at=_utc_now_iso())
    conn.execute(
        row.on_conflict_do_update(
            index_elements=["key"],
            set_={
                "value_json": row.excluded.value_json,
                "updated_at": row.excluded.updated_at,
            },
        )
    )


@contextmanager
def publishing(previous, current):
    """Caller owns the config-file lock. Only the actual file write is inside."""
    old_bound = binding_state(previous) is BindingState.BOUND
    if binding_state(current) is not BindingState.BOUND or (
        old_bound and _generation(previous) == _generation(current)
    ):
        yield
        return
    with get_cached_sqlite_engine().begin() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        _reconcile(conn, current, predecessor=previous if old_bound else None, first_bind=not old_bound)
        yield


def reconcile_startup(config):
    if binding_state(config) is not BindingState.BOUND:
        return
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()
    with config_file_lock():
        current = V2Config.load()
        if binding_state(current) is not BindingState.BOUND:
            return
        with get_cached_sqlite_engine().begin() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            _reconcile(conn, current)


def receipt_current(conn, config):
    """An interrupted file publication with an uncommitted SQLite receipt is closed."""
    return _receipt(conn) == _generation(config)


def may_dispatch(engine, subscription):
    """Final admission, not a check on an old queue snapshot. No lock over I/O.

    Freeze the DB read view before loading the atomic config: a reader overlapping
    file publication either sees the retired DB row or lacks the new receipt.
    Signed grants have their own session, ACL and per-device delivery policy.
    """
    if subscription["user_key"] != "local":
        return True
    with engine.connect() as conn:
        conn.exec_driver_sql("BEGIN")
        current = get_deliverable_by_endpoint(conn, endpoint=subscription["endpoint"], user_key="local")
        if not current or any(current[k] != subscription[k] for k in ("id", "auth", "p256dh")):
            return False
        config, state = position_snapshot()
        return state is BindingState.UNBOUND or (state is BindingState.BOUND and receipt_current(conn, config))
