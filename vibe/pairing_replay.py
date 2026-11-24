"""Bounded delivery of an existing installation redemption. No new grants."""
from __future__ import annotations

import json
import logging
import threading
import time

from config import paths
from storage.lock import MigrationFileLock, MigrationLockTimeout
from vibe.pairing_decision import Evidence, decide, request_key

logger = logging.getLogger(__name__)
_now = time.time
_LOAD = object()


def _path():
    return paths.get_state_dir() / 'pairing-replay.json'


def _saved():
    try:
        value = json.loads(_path().read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('invalid schedule')
        return value
    except FileNotFoundError:
        return None


def decision(record, *, config=_LOAD, active_claim_id=None, live_receipt=False):
    """Collect evidence. Mutating callers hold C and re-read J before this call."""
    from vibe import desktop_local_instance as local, personal_claims, remote_access as ra
    try:
        schedule = _saved()
    except (OSError, ValueError, RecursionError):
        schedule = False  # unreadable, never absence
    marker, marker_error = personal_claims._read_marker()
    installation = binding = None
    if record:
        try:
            if record.get('desktop_enrollment') or record.get('personal_claim'):
                installation = local._installation_id()
            if config is _LOAD:
                config = local._config()
            if config is not None:
                binding = ra._pairing_identity_fingerprint(ra._pairing_identity_from_cloud(config.remote_access.vibe_cloud))
        except (OSError, ValueError, local.LocalInstanceError):
            pass
    return decide(Evidence(record, schedule, marker, marker_error, installation, binding, _now()),
                  active_claim_id=active_claim_id, live_receipt=live_receipt)


def _state(record):
    """Read the stored schedule for this exact redeem, without granting dispatch."""
    saved = _saved()
    return saved if saved is not None and saved.get('key') == request_key(record) else None


def retire_schedule(record):
    # Publish the tombstone before unlinking the terminal journal.
    if record.get('redeem_request') and record['phase'] in ('applied', 'retirement_pending', 'revoked'):
        saved = _state(record)
        if saved:
            _save({'key': saved['key'], 'mode': 'retired'})


def reconciliation_required():
    from vibe.support_contact import SUPPORT_EMAIL
    return {'ok': False, 'error': 'pairing_needs_reconciliation', 'support_email': SUPPORT_EMAIL,
            'pairing': {'ok': False, 'recoverable': False}, 'replay_state': 'needs_reconciliation'}


def local_busy():
    return {'ok': False, 'error': 'pairing_local_busy', 'accepted': False, 'status': 409}


def refusal(d):
    if d.kind == 'check':
        return reconciliation_required()
    if d.kind == 'unavailable':
        return {'ok': False, 'error': d.error}
    if d.kind == 'terminal':
        return {'ok': False, 'error': d.error, 'status': 400 if d.error == 'pairing_key_expired' else 409}
    if d.kind == 'wait':
        result = (_pending(d.wait_seconds) if d.schedule.get('wait_reason') == 'pending' else
                  {'ok': False, 'error': 'pairing_retry_wait', 'retry_after_seconds': d.wait_seconds})
        return {**result, **d.projection}
    return None


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
    try:
        with _send_lock():
            return callback(*args, **kwargs)
    except MigrationLockTimeout:
        return local_busy()
    except OSError:
        return {'ok': False, 'error': 'pairing_recovery_unavailable', 'status': 503}


def run(record, *, automatic=False, manual_only=False):
    from vibe import remote_access as ra, desktop_local_instance as local
    try:
        with _send_lock():
            with ra._pairing_persist_lock():
                current, error = ra._read_pending_pairing_record()
                if error or not current or local.claim_snapshot(current) != local.claim_snapshot(record):
                    return {'ok': False, 'error': 'local_binding_changed', 'status': 409}
                d = decision(current)
                if denied := refusal(d):
                    return denied
                if d.kind != 'send':
                    return {'ok': False, 'error': 'local_binding_changed', 'status': 409}
                if automatic and not d.automatic_due:
                    return {'ok': False, 'error': 'pairing_replay_not_due'}
                if manual_only and d.state == 'scheduled':
                    return {'ok': False, 'error': 'pairing_replay_scheduled', **d.projection}
                record = current
                s = dict(d.schedule)
                s['attempts'] += 1
                s['automatic'] += int(automatic)
                s['mode'] = 'unsettled'
                _save(s)
                if current.get('replay_budget_claimed') is not True:
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
                else:
                    s['mode'] = {'local_archive_failed': 'received_not_archived',
                                 'manual_retryable': 'manual_retryable'}.get(category, 'unsettled')
                with ra._pairing_persist_lock():
                    current, _ = ra._read_pending_pairing_record()
                    if current and ra._same_pairing_claim_request(current, record):
                        if current['phase'] == 'prepared':
                            _save(s)
                        settled = decision(current)
                        if not result.get('ok') and settled.error != 'pairing_key_expired':
                            result = {**result, **settled.projection}
                            next_at = settled.projection.get('next_retry_at')
                return result
            finally:
                logger.info('Pairing redeem attempt=%d result=%s next_retry_at=%s', s['attempts'], category, next_at)
    except MigrationLockTimeout:
        return local_busy()
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ra._PairingLockUnavailable, local.LocalInstanceError):
        return {'ok': False, 'error': 'pairing_recovery_unavailable', 'status': 503}


def tick(on_bound=None):
    from vibe import remote_access as ra
    record, _ = ra._read_pending_pairing_record()
    if record and decision(record).automatic_due and run(record, automatic=True).get('ok') and on_bound:
        on_bound()


def start_worker(on_bound):
    stop = threading.Event()

    def work():
        while not stop.wait(1):
            try:
                tick(on_bound)
            except Exception:
                logger.debug('Pairing replay journal unavailable')
    threading.Thread(target=work, daemon=True, name='pairing-replay').start()
    return stop
