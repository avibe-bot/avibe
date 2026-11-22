"""Fence anonymous push grants at the same config/SQLite publication as binding.

Only the serving UI knows its proxy environment. Other publishers leave the
previous grant set intact and invalidate its receipt. Senders never derive a
policy from their own environment or acquire the config lock under SQLite.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import logging
import os
import threading
import time

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import SQLAlchemyError

from config.v2_config import V2Config, config_file_lock, config_recovery_refusal
from storage.db import get_cached_sqlite_engine
from storage.models import state_meta, web_push_subscriptions as push
from storage.web_push_service import RETIRED_LOCAL, _utc_now_iso, get_deliverable_by_endpoint
from vibe import declared_proxy
from vibe.network_position import BindingState, binding_state, position_snapshot

_RECEIPT = 'web_push.local_binding.v1'
_listener_pid = None
_POLICY_CHECK_INTERVAL = 1.0
_UNREADABLE = object()


def _is_digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


@contextmanager
def listener_publications():
    """Enter after acquiring listening sockets, leave before a replacement. Not inherited by a child."""
    global _listener_pid
    previous, _listener_pid = _listener_pid, os.getpid()
    try:
        yield
    finally:
        _listener_pid = previous


def _digest(values):
    return hashlib.sha256(json.dumps(values, separators=(',', ':')).encode()).hexdigest()


def _generation(config):
    cloud = config.remote_access.vibe_cloud
    return _digest([cloud.backend_url, cloud.instance_id, cloud.instance_secret])


def _stamp(config):
    # Only file evidence, never an environment variable. A changed declaration
    # pauses delivery even if an OS writer bypassed publishing(). A change in
    # spelling/order is harmless: normalization is shared with the listener.
    cloud = config.remote_access.vibe_cloud
    origins = sorted({origin for value in declared_proxy.origin_entries(config, '')
                      if (origin := declared_proxy.normalize_origin(value))})
    return _digest([_generation(config), cloud.enabled, cloud.public_url, origins])


def _raw(conn):
    return conn.execute(select(state_meta.c.value_json).where(state_meta.c.key == _RECEIPT)).scalar_one_or_none()


def _put(conn, record):
    row = insert(state_meta).values(key=_RECEIPT, value_json=json.dumps(record, sort_keys=True), updated_at=_utc_now_iso())
    conn.execute(row.on_conflict_do_update(index_elements=['key'], set_={
        'value_json': row.excluded.value_json, 'updated_at': row.excluded.updated_at,
    }))


@dataclass(frozen=True)
class _Evidence:
    stamp: str | None
    generation: str
    origins: frozenset[str]
    peers: tuple


def _evidence(record):
    if record is None:
        return None
    if _is_digest(record):
        # Original receipt: retirement already ran. Preserve subsequently
        # repaired host grants but require the first listener attestation.
        return _Evidence(None, record, frozenset(), ())
    if not isinstance(record, dict) or record.get('version') != 2:
        raise ValueError('invalid anonymous push receipt')
    stamp, generation = record.get('stamp'), record.get('generation')
    origins, peers = record.get('origins'), record.get('peers')
    if (not _is_digest(stamp) or not _is_digest(generation) or not isinstance(origins, list)
            or not isinstance(peers, list) or bool(origins) != bool(peers)
            or any(not isinstance(s, str) or declared_proxy.normalize_origin(s) != s for s in origins)
            or any(not isinstance(s, str) for s in peers)):
        raise ValueError('invalid anonymous push receipt')
    return _Evidence(stamp, generation, frozenset(origins), tuple(ipaddress.ip_network(s) for s in peers))


def _prior(record):
    if isinstance(record, dict) and record.get('version') == 2 and 'pending' in record:
        return record['pending']
    return record


def _record(raw):
    """A malformed existing row is not evidence of a fresh install."""
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        if isinstance(value, dict) and 'pending' in value:
            if (value.get('version') != 2 or set(value) != {'version', 'pending', 'carry_to'}
                    or (value['carry_to'] is not None and not _is_digest(value['carry_to']))):
                return _UNREADABLE
            _evidence(value['pending'])
        elif value is None or _evidence(value) is None:
            return _UNREADABLE
        return value
    except (ValueError, TypeError, RecursionError):
        return _UNREADABLE


def _target(config, policy):
    return {'version': 2, 'stamp': _stamp(config), 'generation': _generation(config),
            'origins': list(policy.origins), 'peers': [str(net) for net in policy.networks]}


def _attest(conn, config, policy, *, first=False, predecessor=None):
    target = _target(config, policy)
    generation = target['generation']
    record = _record(_raw(conn))
    if record == target and not first:
        return
    # A verified listener can recover missing history only by withdrawing the
    # mixed grants. Retirement and the replacement receipt commit together.
    unreadable = record is _UNREADABLE
    before = None if unreadable else _evidence(_prior(record))
    carry = (before is not None and not first and (
        before.generation == generation or (predecessor is not None and before.generation == _generation(predecessor))
        or (isinstance(record, dict) and record.get('carry_to') == generation)))
    # Neither adding a declaration nor repeating an attestation enables rows.
    # A narrow/withdrawn policy cannot distinguish the remaining browsers.
    withdrawn = before is not None and (not before.origins.issubset(policy.origins) or any(
        not any(old.version == new.version and old.subnet_of(new) for new in policy.networks) for old in before.peers))
    if unreadable or withdrawn or (not policy.origins and not carry):
        conn.execute(push.update().where(push.c.user_key == 'local',
            (push.c.enabled == 1) | push.c.provider_invalidated_at.is_not(None)).values(
                user_key=RETIRED_LOCAL, enabled=0, provider_invalidated_at=None, updated_at=_utc_now_iso()))
    _put(conn, target)


@contextmanager
def publishing(previous, current):
    """Caller holds config lock. Roll back the decision if file replacement fails."""
    old_bound = binding_state(previous) is BindingState.BOUND
    if binding_state(current) is not BindingState.BOUND or (old_bound and _stamp(previous) == _stamp(current)):
        yield
        return
    listener = _listener_pid == os.getpid()
    policy = declared_proxy.listener_policy(current) if listener else None
    with get_cached_sqlite_engine().begin() as conn:
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        if policy is not None:
            _attest(conn, current, policy, first=not old_bound, predecessor=previous if old_bound else None)
        else:
            record = _record(_raw(conn))
            if record is not _UNREADABLE:
                before = _evidence(_prior(record))
                carry = old_bound and before is not None and (before.generation == _generation(previous)
                    or (isinstance(record, dict) and record.get('carry_to') == _generation(previous)))
                _put(conn, {'version': 2, 'pending': _prior(record) if old_bound else None,
                            'carry_to': _generation(current) if carry else None})
        yield


def reconcile_startup(config):
    """Serving UI startup and periodic check. Recheck the file under the writer lock."""
    if _listener_pid != os.getpid() or binding_state(config) is not BindingState.BOUND:
        return
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    policy = declared_proxy.listener_policy(config)
    if policy is not None:
        # Steady-state ticks need only a read. Do not take either writer lock
        # unless something changed. This path never grants or modifies authority.
        with engine.connect() as conn:
            raw = _raw(conn)
            if raw == json.dumps(_target(config, policy), sort_keys=True):
                return
    with config_file_lock():
        current = V2Config.load()
        if binding_state(current) is not BindingState.BOUND:
            return
        policy = declared_proxy.listener_policy(current)
        if policy is None:
            return
        with engine.begin() as conn:
            conn.exec_driver_sql('BEGIN IMMEDIATE')
            _attest(conn, current, policy)


@contextmanager
def serving_listener():
    """Scope one checker to the UI's listening sockets, including a first run with no config."""
    stop = threading.Event()

    def check():
        config, _ = position_snapshot()
        reconcile_startup(config)

    def poll():
        deadline = time.monotonic() + _POLICY_CHECK_INTERVAL
        failing = False
        while not stop.wait(max(0., deadline - time.monotonic())):
            deadline = time.monotonic() + _POLICY_CHECK_INTERVAL
            try:
                check()
                failing = False
            except Exception:
                # A temporary read/lock/import failure must not silently kill the
                # only process that can release the pause. Log once per outage.
                if not failing:
                    logging.getLogger(__name__).warning('Could not reconcile anonymous push policy', exc_info=True)
                failing = True

    with listener_publications():
        check()  # Before accepting requests with this listener's environment.
        worker = threading.Thread(target=poll, name='ui-push-policy', daemon=True)
        worker.start()
        try:
            yield
        finally:
            stop.set()
            worker.join()


def receipt_current(conn, config):
    if config_recovery_refusal(config, 'ui', 'remote_access'):
        return False
    record = _record(_raw(conn))
    return (isinstance(record, dict) and 'pending' not in record and record['stamp'] == _stamp(config)
            and record['generation'] == _generation(config))


def may_dispatch(engine, subscription):
    """Last check on the current grant, not a stale queue snapshot. No lock over I/O."""
    if subscription['user_key'] != 'local':
        return subscription['user_key'] != RETIRED_LOCAL
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql('BEGIN')
            current = get_deliverable_by_endpoint(conn, endpoint=subscription['endpoint'], user_key='local')
            if not current or any(current[k] != subscription[k] for k in ('id', 'auth', 'p256dh')):
                return False
            config, state = position_snapshot()
            return state is BindingState.UNBOUND or (state is BindingState.BOUND and receipt_current(conn, config))
    except (OSError, SQLAlchemyError):
        return False
