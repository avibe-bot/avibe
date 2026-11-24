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


def _request_key(record):
    return hashlib.sha256(json.dumps([record['operation_id'], record['redeem_request']['redeem_request_id']],
                                     separators=(',', ':')).encode()).hexdigest()


def _saved():
    try:
        return json.loads(_path().read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None


def accept_conflict(record, *, active_claim_id=None, sealed_archive=False):
    """An issued acceptance belongs to its journal or the lock-held live handoff only."""
    from vibe import personal_claims
    journal_id = ((record or {}).get('personal_claim') or {}).get('claim_id')
    try:
        sealed_id = (journal_id or '') if sealed_archive else None
        marked = personal_claims._outcome(sealed_claim_id=sealed_id).get('claim_id')
    except personal_claims.ClaimError:
        return True
    return bool(marked and marked not in (journal_id, active_claim_id))


def orphaned_delivery(*, active_claim_id=None):
    """No current grant: an outstanding send or accept is not a clean start."""
    from vibe import personal_claims
    try:
        saved = _saved()
        if saved is not None and saved.get('mode') != 'retired':
            return True
        return accept_conflict(None, active_claim_id=active_claim_id)
    except (OSError, ValueError, AttributeError, RecursionError, personal_claims.ClaimError):
        return True


def retire_schedule(record):
    # Called under C while a protected terminal journal is still on disk.
    # Publish the tombstone first: either side of an interrupted unlink is safe.
    if record.get('redeem_request') and record['phase'] in ('applied', 'retirement_pending', 'revoked'):
        saved = _saved()
        if saved and saved.get('key') == _request_key(record):
            _save({'key': saved['key'], 'mode': 'retired'})


def _state(record):
    key = _request_key(record)
    saved = _saved() or {}
    # Invalid state must not create a fresh automatic budget.
    if saved.get('key') == key:
        if any(type(saved.get(k)) is not int or saved[k] < 0 for k in ('attempts', 'automatic', 'failures')):
            raise ValueError('invalid replay counters')
        if not (saved['automatic'] <= saved['attempts'] and saved['failures'] <= saved['attempts']):
            raise ValueError('inconsistent replay counters')
        if any(type(saved.get(k)) not in (float, int) or not math.isfinite(saved[k]) or saved[k] < 0
               for k in ('due', 'not_before', 'deadline')):
            raise ValueError('invalid replay timing')
        return saved
    if 'replay_budget_claimed' in record and type(record['replay_budget_claimed']) is not bool:
        raise ValueError('invalid replay enrollment')
    claimed = record.get('replay_budget_claimed', 'redeem_prepared_at' in record)
    if claimed:
        raise ValueError('claimed replay budget unavailable')
    start = record.get('redeem_prepared_at', record.get('prepared_at', 0))
    return dict(key=key, deadline=start + WINDOW, attempts=0, automatic=0, failures=0,
                due=start + 2, not_before=0,
                mode='scheduled' if record.get('replay_budget_claimed') is False else 'unsettled')


def _scheduled(state, now):
    return (state['mode'] == 'scheduled' and state['automatic'] < LIMIT
            and max(now, state['due']) < state['deadline'])


def _view(state, now):
    if state['mode'] not in ('scheduled', 'manual_retryable'):
        # In particular, a legacy unqualified 'manual' might be a lost 200.
        return {'replay_state': 'needs_reconciliation'}
    if _scheduled(state, now):
        return {'replay_state': 'scheduled', 'next_retry_at': state['due']}
    return {'replay_state': 'manual'}


def projection(record, eligible):
    if accept_conflict(record):
        return {'replay_state': 'needs_reconciliation'}
    if not eligible or record.get('desktop_failure'):
        return {'replay_state': 'terminated'}
    try:
        return _view(_state(record), _now())
    except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError):
        return {'replay_state': 'needs_reconciliation'}


def reconciliation_required():
    from vibe.support_contact import SUPPORT_EMAIL
    return {'ok': False, 'error': 'pairing_needs_reconciliation', 'support_email': SUPPORT_EMAIL,
            'pairing': {'ok': False, 'recoverable': False}, 'replay_state': 'needs_reconciliation'}


def local_busy():
    return {'ok': False, 'error': 'pairing_local_busy', 'accepted': False, 'status': 409}


def replacement_blocked(record, *, active_claim_id=None):
    """Called under the send right, before any new input can replace evidence."""
    if not record:
        return orphaned_delivery(active_claim_id=active_claim_id)
    if accept_conflict(record, active_claim_id=active_claim_id):
        return True
    if record['phase'] in ('redeemed', 'applied'):
        return True
    if record['phase'] != 'prepared' or record.get('desktop_failure') == 'pairing_key_expired':
        return False
    if not record.get('redeem_request'):
        return not record.get('desktop_enrollment')  # bare reservation is not a delivery
    try:
        s = _state(record)
        return (_view(s, _now())['replay_state'] == 'needs_reconciliation'
                or _now() < s['not_before'])
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return True


def wait_result(state, now):
    if now >= state['not_before']:
        return None
    seconds = math.ceil(state['not_before'] - now)
    result = (_pending(seconds) if state.get('wait_reason') == 'pending' else
              {'ok': False, 'error': 'pairing_retry_wait', 'retry_after_seconds': seconds})
    return {**result, **_view(state, now)}


def duplicate_hold(record):
    if accept_conflict(record):
        return reconciliation_required()
    try:
        state, now = _state(record), _now()
        if _view(state, now)['replay_state'] == 'needs_reconciliation':
            return reconciliation_required()
        return wait_result(state, now)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError):
        return reconciliation_required()


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
    # These are actual server replies to the request, unlike an abandoned
    # send reservation. Keep manual retry restricted to known contracts.
    cause = result.get('pairing', {}).get('cause')
    if code == 'pairing_redeem_indeterminate' and (
        (status == 409 and cause == 'registration_indeterminate') or
        (status in (400, 401, 403, 404, 409, 410, 422) and cause == 'pairing_request_failed')
    ):
        return 'manual_retryable', None
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
        return local_busy()
    except OSError:
        return {'ok': False, 'error': 'pairing_recovery_unavailable', 'status': 503}


def run(record, *, automatic=False, manual_only=False):
    from vibe import remote_access as ra
    from vibe import desktop_local_instance as local
    try:
        with _send_lock():
            with ra._pairing_persist_lock():
                current, error = ra._read_pending_pairing_record()
                if (error or not current or local.claim_snapshot(current) != local.claim_snapshot(record)
                        or current['phase'] != 'prepared' or not eligible(current, local._config())):
                    return {'ok': False, 'error': 'local_binding_changed', 'status': 409}
                if accept_conflict(current):
                    return reconciliation_required()
                if current.get('desktop_failure'):
                    return {'ok': False, 'error': current['desktop_failure'], 'status': 400}
                # Use the journal's representation even on the first send.
                record = current
                now = _now()
                try:
                    s = _state(current)
                except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError):
                    return reconciliation_required()
                if _view(s, now)['replay_state'] == 'needs_reconciliation':
                    return reconciliation_required()
                if automatic and (not _scheduled(s, now) or now < s['due']):
                    return {'ok': False, 'error': 'pairing_replay_not_due'}
                view = _view(s, now)
                if wait := wait_result(s, now):
                    return wait
                if manual_only and view['replay_state'] == 'scheduled':
                    return {'ok': False, 'error': 'pairing_replay_scheduled', **view}
                s['attempts'] += 1
                s['automatic'] += int(automatic)
                # Reserve before dispatch: without a durable outcome, elapsed time
                # cannot authorize another delivery.
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
                    s['wait_reason'] = category
                    s['not_before'] = now + delay if category in ('pending', 'rate_limited') else 0
                    s['due'] = now + delay
                    if _scheduled(s, now):
                        next_at = s['due']
                else:
                    s['mode'] = {'local_archive_failed': 'received_not_archived',
                                 'manual_retryable': 'manual_retryable'}.get(category, 'unsettled')
                # A new grant can replace the record while the network is out;
                # the old completion must not write a schedule for its successor.
                with ra._pairing_persist_lock():
                    current, _ = ra._read_pending_pairing_record()
                    if current and ra._same_pairing_claim_request(current, record):
                        if current['phase'] == 'prepared':
                            _save(s)
                            # Bind the action in the response to this request, not a
                            # later read of whichever operation is now on this host.
                            if not result.get('ok') and not current.get('desktop_failure'):
                                result = {**result, **_view(s, now)}
                        elif current['phase'] in ('redeemed', 'applied'):
                            result = {**result, 'replay_state': 'local_archive'}
                            next_at = None
                    else:
                        next_at = None
                return result
            finally:
                logger.info('Pairing redeem attempt=%d result=%s next_retry_at=%s', s['attempts'], category, next_at)
    except MigrationLockTimeout:
        return local_busy()
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
        if _scheduled(s, now) and now >= s['due'] and run(record, automatic=True).get('ok') and on_bound:
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
