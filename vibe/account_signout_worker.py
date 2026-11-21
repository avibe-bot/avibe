"""Leased account sign-out outbox. No network under a DB lock or in the sign-out handler."""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert

from storage.db import get_cached_sqlite_engine
from storage.device_account_signout import RECEIPT_TTL
from storage.models import (remote_account_refs as refs, remote_account_revocations as jobs,
                            remote_account_pairings as pairings, remote_signout_items as items,
                            remote_signout_receipts as receipts)
from vibe.account_session_revoke import dispatch, pairing_epoch

MAX_AGE = 7 * 86400
MAX_TRIES = 32
LEASE = 60
logger = logging.getLogger(__name__)


def _finish(conn, predicate, status, reason, now):
    """Resolve attached items including shared refs; start receipt TTL on its last item."""
    from vibe.remote_access import OAUTH_HANDSHAKE_TTL_SECONDS
    selected = select(jobs.c.digest).where(predicate)
    affected = select(items.c.receipt).where(items.c.job.in_(selected))
    other = select(items.c.id).where(items.c.receipt == receipts.c.id, items.c.job.not_in(selected)).exists()
    conn.execute(receipts.update().where(receipts.c.id.in_(affected), ~other).values(expires_at=now + RECEIPT_TTL))
    if status != 'unconfirmed':
        # A sibling still has a local cookie, not another use for this raw ref.
        conn.execute(refs.update().where(refs.c.digest.in_(selected)).values(reference=None, status=status, reason=reason))
    conn.execute(items.update().where(items.c.job.in_(selected)).values(job=None, status=status, reason=reason))
    # Never erase the digest while pending/refused: the upstream login may still exist.
    forget = now + OAUTH_HANDSHAKE_TTL_SECONDS if status == 'accepted' else None
    conn.execute(jobs.update().where(predicate).values(reference=None, next_at=None, status=status, reason=reason,
                                                       forget_at=forget))


def _expire_page(conn, table, pk, date, now):
    page = select(pk).where(date <= now).order_by(date).limit(32)
    conn.execute(table.delete().where(pk.in_(page)))


def _retire_old_pairings(conn, epoch, now):
    # Range searches avoid scanning the live pairing's entire enrollment/fence history on idle ticks.
    for table in (refs, jobs, pairings):
        key = next(iter(table.primary_key.columns))
        for outside in (table.c.epoch < epoch, table.c.epoch > epoch):
            page = select(key).where(outside).limit(32)
            if table is jobs:
                keys = conn.execute(page).scalars().all()
                if not keys:
                    continue
                _finish(conn, key.in_(keys), 'not_applicable', 'binding_changed', now)
                page = keys
            conn.execute(table.delete().where(key.in_(page)))


def _claim(config, now):
    epoch = pairing_epoch(config)
    with get_cached_sqlite_engine().begin() as conn:
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        _expire_page(conn, refs, refs.c.sid, refs.c.expires_at, now)
        expired_receipts = select(receipts.c.id).where(receipts.c.expires_at <= now).order_by(receipts.c.expires_at).limit(32)
        conn.execute(items.delete().where(items.c.receipt.in_(expired_receipts)))
        conn.execute(receipts.delete().where(receipts.c.id.in_(expired_receipts)))
        _expire_page(conn, jobs, jobs.c.digest, jobs.c.forget_at, now)
        if config is not None:
            _retire_old_pairings(conn, epoch or '', now)
            from vibe.remote_access import device_binding
            cookie = device_binding(config)
            for retired in (refs.c.cookie_binding < cookie, refs.c.cookie_binding > cookie):
                conn.execute(refs.delete().where(refs.c.sid.in_(select(refs.c.sid).where(retired).limit(32))))
        if not epoch or not config.remote_access.vibe_cloud.runtime_credentials():
            return None  # A paused/missing config is not an attempt or rejection.
        limit = conn.execute(select(pairings.c.blocked_until, pairings.c.refused).where(pairings.c.epoch == epoch)).first()
        if limit and (limit.refused or limit.blocked_until > now):
            return None
        row = conn.execute(select(jobs).where(jobs.c.epoch == epoch, jobs.c.next_at <= now)
                           .order_by(jobs.c.next_at).limit(1)).mappings().first()
        if row is None:
            return None
        attempt = row['attempts'] + 1
        conn.execute(jobs.update().where(jobs.c.digest == row['digest']).values(attempts=attempt, next_at=now + LEASE))
        return row['digest'], row['reference'], row['epoch'], attempt


def _pairing_gate(conn, epoch, *, until=0, refused=0):
    stmt = insert(pairings).values(epoch=epoch, blocked_until=until, refused=refused)
    conn.execute(stmt.on_conflict_do_update(index_elements=['epoch'], set_={
        'blocked_until': func.max(pairings.c.blocked_until, until), 'refused': func.max(pairings.c.refused, refused)}))


def _settle(digest, attempt, code, now):
    with get_cached_sqlite_engine().begin() as conn:
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        row = conn.execute(select(jobs.c.created_at, jobs.c.epoch, jobs.c.answers).where(
            jobs.c.digest == digest, jobs.c.attempts == attempt, jobs.c.next_at.is_not(None))).first()
        if row is None:  # A stale worker cannot overwrite the next lease's answer.
            return
        target = jobs.c.digest == digest
        if code == 204:
            _finish(conn, target, 'accepted', None, now)
        elif code == 403:
            _finish(conn, target, 'not_applicable', 'not_eligible', now)
        elif code == 401:
            _pairing_gate(conn, row.epoch, refused=1)
            conn.execute(refs.update().where(refs.c.epoch == row.epoch, refs.c.reference.is_not(None))
                         .values(reference=None, status='refused', reason='credential'))
            _finish(conn, (jobs.c.epoch == row.epoch) & jobs.c.next_at.is_not(None), 'refused', 'credential', now)
        elif code is not None and 300 <= code < 500 and code != 429:
            _finish(conn, target, 'refused', 'protocol', now)
        else:
            delay = min(3600, 5 * 2 ** min(10, attempt - 1))
            if code == 429:
                _pairing_gate(conn, row.epoch, until=now + 600)
                delay = max(delay, 600)
            answers = row.answers + int(code is not None)
            if answers >= MAX_TRIES or now >= row.created_at + MAX_AGE:
                _finish(conn, target, 'unconfirmed', 'retry_exhausted', now)
            else:
                conn.execute(jobs.update().where(target).values(next_at=now + delay, answers=answers, reason='retrying'))
                conn.execute(items.update().where(items.c.job == digest).values(reason='retrying'))


def run_once(load_config):
    from config.v2_config import config_file_lock
    from vibe.binding_admission import reset_in_progress
    # Retiring other epochs is destructive. Keep the config read and its DB
    # write on the same side of publication, never around the issuer request.
    with config_file_lock():
        if reset_in_progress():
            return False
        config = load_config()
        job = _claim(config, int(time.time()))
    if job is None:
        return False
    digest, reference, epoch, attempt = job
    # Snapshot credentials, never re-read them inside the send. The issuer revalidates current pairing.
    config = load_config()
    credentials = config.remote_access.vibe_cloud.runtime_credentials() if pairing_epoch(config) == epoch else None
    if not credentials or reset_in_progress():
        return False  # Nothing was sent. Let the next claimant recheck this binding.
    code = dispatch(credentials, reference)
    _settle(digest, attempt, code, int(time.time()))
    return True


async def _poll(operation, load_config, label):
    while True:
        try:
            busy = await asyncio.to_thread(operation, load_config)
        except Exception:
            # Neither a transport exception nor a query parameter belongs in the service log.
            logger.warning('%s worker unavailable', label)
            busy = False
        await asyncio.sleep(0.25 if busy else 3)


async def serve(load_config):
    from storage import device_health_owner_checks
    await asyncio.gather(_poll(run_once, load_config, 'Device account sign-out'),
                         _poll(device_health_owner_checks.run_once, load_config, 'Device Health owner proof'))
