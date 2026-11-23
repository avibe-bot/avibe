"""Bounded replay of a saved installation redemption (no new grants).

The pairing journal is the authority. The sidecar only schedules its exact
request, not an operation id alone (Desktop can renew a grant in-place).
It contains no redemption inputs and does not change the guarded journal
snapshot used by a fresh personal claim. All mutations take the redeem lock.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time

from config import paths
from storage.lock import MigrationFileLock, MigrationLockTimeout

logger = logging.getLogger(__name__)
WINDOW = 15 * 60
_now = time.time
LIMIT = 20


def _path():
    return paths.get_state_dir() / 'pairing-replay.json'


def _state(record):
    key = hashlib.sha256(json.dumps([record['operation_id'], record['redeem_request']['redeem_request_id']],
                                    separators=(',', ':')).encode()).hexdigest()
    try:
        saved = json.loads(_path().read_text(encoding='utf-8'))
    except FileNotFoundError:
        saved = {}
    # Invalid state must not create a fresh automatic budget.
    if saved.get('key') == key:
        return saved
    if 'replay_budget_claimed' in record and type(record['replay_budget_claimed']) is not bool:
        raise ValueError('invalid replay enrollment')
    claimed = record.get('replay_budget_claimed', 'redeem_prepared_at' in record)
    if claimed:
        raise ValueError('claimed replay budget unavailable')
    start = record.get('redeem_prepared_at', record.get('prepared_at', 0))
    return dict(key=key, deadline=start + WINDOW, attempts=0, automatic=0, failures=0,
                due=start + 2, not_before=0, mode='scheduled')


def _scheduled(state, now):
    return (state['mode'] == 'scheduled' and state['automatic'] < LIMIT
            and max(now, state['due']) < state['deadline'])


def _view(state, now):
    if _scheduled(state, now):
        return {'replay_state': 'scheduled', 'next_retry_at': state['due']}
    return {'replay_state': 'manual'}


def projection(record, eligible):
    if not eligible or record.get('desktop_failure'):
        return {'replay_state': 'terminated'}
    try:
        return _view(_state(record), _now())
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return {'replay_state': 'terminated'}


def _save(state):
    from vibe.remote_access import write_atomic
    write_atomic(_path(), json.dumps(state))


def _pending(seconds):
    return {'ok': False, 'error': 'pairing_redeem_pending', 'status': 409,
            'retry_after_seconds': seconds,
            'pairing': {'ok': False, 'recoverable': True, 'cause': 'pairing_redeem_pending'}}


def _result(result):
    code, status = result.get('error'), result.get('status')
    if result.get('ok'):
        return 'success', None
    if code == 'pairing_redeem_pending':
        return 'pending', result['retry_after_seconds']
    if code == 'pairing_redeem_indeterminate' and result.get('pairing', {}).get('recoverable') is False:
        return 'local_archive_failed', None
    if code == 'pairing_backend_unresolvable' or code == 'pairing_redeem_indeterminate' and status is None:
        return 'network', None
    if code == 'pairing_redeem_indeterminate' and status == 429:
        seconds = result.get('retry_after_seconds')
        return 'rate_limited', seconds if type(seconds) is int and seconds > 0 else None
    if code == 'pairing_redeem_indeterminate' and isinstance(status, int) and 500 <= status < 600:
        return 'server_error', None
    return 'stopped', None


def _send_lock():
    return MigrationFileLock(paths.get_state_dir() / 'pairing-replay.lock', timeout_seconds=0)


def with_send_right(callback, *args, **kwargs):
    # Take the same right BEFORE even preparing/replacing a grant. Re-entrant
    # for a guarded first send, never blocking behind a different operation.
    try:
        with _send_lock():
            return callback(*args, **kwargs)
    except MigrationLockTimeout:
        return _pending(1)
    except OSError:
        return {'ok': False, 'error': 'pairing_recovery_unavailable', 'status': 503}


def run(record, *, automatic=False):
    from vibe import remote_access as ra
    from vibe import desktop_local_instance as local
    try:
        with _send_lock():
            with ra._pairing_persist_lock():
                current, error = ra._read_pending_pairing_record()
                if error or current != record or current['phase'] != 'prepared' or not eligible(current, local._config()):
                    return {'ok': False, 'error': 'local_binding_changed', 'status': 409}
                if current.get('desktop_failure'):
                    return {'ok': False, 'error': current['desktop_failure'], 'status': 400}
                # Use the journal's representation even on the first send.
                record = current
                now = _now()
                s = _state(current)
                if s['mode'] == 'unsettled':
                    # The previous owner died or could not commit its receipt.
                    # The response could have arrived just before that happened.
                    # Start the maximum pending floor at *observation*, not send.
                    s.update(mode='manual', not_before=now + 60, due=now + 60)
                    _save(s)
                    return _pending(60)
                if automatic and (not _scheduled(s, now) or now < s['due']):
                    return {'ok': False, 'error': 'pairing_replay_not_due'}
                if now < s['not_before']:
                    return _pending(math.ceil(s['not_before'] - now))
                s['attempts'] += 1
                s['automatic'] += int(automatic)
                # Reserve before dispatch. A crash consumes this attempt and
                # leaves a delayed replay, never a whole new budget.
                s['mode'] = 'unsettled'
                _save(s)
                if current.get('replay_budget_claimed') is not True:
                    # An unclaimed record may initialize a sidecar; once a POST
                    # can leave, missing/mismatched state never buys new sends.
                    record = {**current, 'replay_budget_claimed': True}
                    ra._write_pending_pairing_record(record)
            category, next_at = 'local_error', None
            try:
                backend, error = ra._normalize_pairing_backend_url(record['backend_url'])
                result = ({'ok': False, 'error': error or 'invalid_pairing_backend_url'} if backend is None else
                          ra._redeem_pairing_claim(record, backend, record['redeem_request']))
                category, delay = _result(result)
                now = _now()
                s['not_before'] = 0
                if category in ('pending', 'network', 'rate_limited', 'server_error'):
                    s['mode'] = 'scheduled'
                    s['failures'] += 1
                    delay = delay or min(60, 1 << min(s['failures'], 6))
                    s['not_before'] = now + delay if category in ('pending', 'rate_limited') else 0
                    s['due'] = now + delay
                    if _scheduled(s, now):
                        next_at = s['due']
                else:
                    s['mode'] = 'manual'
                # A new grant can replace the record while the network is out;
                # the old completion must not write a schedule for its successor.
                with ra._pairing_persist_lock():
                    current, _ = ra._read_pending_pairing_record()
                    if current and current['phase'] == 'prepared' and ra._same_pairing_claim_request(current, record):
                        _save(s)
                    else:
                        next_at = None
                return result
            finally:
                logger.info('Pairing redeem attempt=%d result=%s next_retry_at=%s', s['attempts'], category, next_at)
    except MigrationLockTimeout:
        return _pending(1)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ra._PairingLockUnavailable, local.LocalInstanceError):
        return {'ok': False, 'error': 'pairing_recovery_unavailable', 'status': 503}


def eligible(record, config):
    from vibe import desktop_local_instance as local, remote_access as ra
    guard = record.get('personal_claim') or record.get('desktop_enrollment')
    return bool(config and guard and local._installation_id() == guard['installation_id'] and
                ra._pairing_identity_fingerprint(ra._pairing_identity_from_cloud(config.remote_access.vibe_cloud))
                == record['source_fingerprint'])


def tick(on_bound=None):
    from vibe import remote_access as ra
    record, _ = ra._read_pending_pairing_record()
    if record and record['phase'] == 'prepared' and record.get('redeem_request') and not record.get('desktop_failure'):
        try:
            s, now = _state(record), _now()
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            # A claimed but unreadable budget is not a new request.
            return
        if (s['mode'] == 'unsettled' or (_scheduled(s, now) and now >= s['due'])) and run(record, automatic=True).get('ok') and on_bound:
            on_bound()


def start_worker(on_bound):
    stop = threading.Event()

    def work():
        while not stop.wait(1):
            try:
                tick(on_bound)
            except Exception:
                # Never log exceptions containing request or upstream body.
                logger.debug('Pairing replay journal unavailable')
    threading.Thread(target=work, daemon=True, name='pairing-replay').start()
    return stop
