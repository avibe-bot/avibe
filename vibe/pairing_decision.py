"""Pure recovery policy. No file access, config loading, clocks or transport here."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re

WINDOW = 15 * 60
LIMIT = 20


def request_key(record):
    ids = [record['operation_id'], record['redeem_request']['redeem_request_id']]
    return hashlib.sha256(json.dumps(ids, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class Evidence:
    journal: dict | None
    schedule: object
    marker: dict
    marker_error: str | None
    installation: str | None
    binding: str | None
    now: float


@dataclass(frozen=True)
class Decision:
    kind: str
    state: str | None = None
    error: str | None = None
    schedule: dict | None = None
    automatic_due: bool = False
    replaceable: bool = False
    wait_seconds: int = 0

    @property
    def projection(self):
        result = {'replay_state': self.state} if self.state else {}
        if self.state == 'scheduled':
            result['next_retry_at'] = self.schedule['due']
        return result

    @property
    def can_resume(self):
        return self.kind in ('send', 'wait', 'archive', 'retire')


CHECK = Decision('check', 'needs_reconciliation', 'pairing_needs_reconciliation')


def _schedule(record, saved):
    key = request_key(record)
    if saved is not None and not isinstance(saved, dict):
        raise ValueError
    if saved is not None and saved.get('key') == key:
        if any(type(saved.get(k)) is not int or saved[k] < 0 for k in ('attempts', 'automatic', 'failures')):
            raise ValueError
        if max(saved['automatic'], saved['failures']) > saved['attempts']:
            raise ValueError
        if any(type(saved.get(k)) not in (int, float) or not math.isfinite(saved[k]) or saved[k] < 0
               for k in ('due', 'deadline', 'not_before')):
            raise ValueError
        return saved
    if record.get('replay_budget_claimed') is not False:
        raise ValueError
    start = record['redeem_prepared_at']
    return dict(key=key, deadline=start + WINDOW, attempts=0, automatic=0, failures=0,
                due=start + 2, not_before=0, mode='scheduled')


def decide(e: Evidence, *, active_claim_id=None, live_receipt=False) -> Decision:
    """active_claim_id is supplied only by the lock-held personal accept/handoff."""
    j = e.journal
    personal = (j or {}).get('personal_claim') or {}
    marked = e.marker.get('claim_id')
    if marked is not None and marked not in (personal.get('claim_id'), active_claim_id):
        return CHECK
    phase = (j or {}).get('phase')
    sealed = phase in ('redeemed', 'applied')  # journal reader verifies the protected receipt
    if e.marker_error and (not sealed or e.marker_error != 'pairing_needs_reconciliation'):
        return CHECK
    clean_schedule = e.schedule is None or (
        isinstance(e.schedule, dict) and e.schedule.get('mode') == 'retired'
        and isinstance(e.schedule.get('key'), str) and re.fullmatch('[0-9a-f]{64}', e.schedule['key']) is not None
    )
    if not j:
        if not clean_schedule:
            return CHECK
        return Decision('fresh', replaceable=True)
    guard = personal or j.get('desktop_enrollment')
    installed = not guard or guard['installation_id'] == e.installation
    source = e.binding is not None and e.binding == j['source_fingerprint']
    if e.binding is None and (sealed or live_receipt or j.get('redeem_request')):
        return CHECK if e.marker_error else Decision('unavailable', error='pairing_recovery_unavailable')
    if live_receipt:
        # The caller owns the send and matched its response to this exact J under C.
        # An unsettled reservation is expected here; this path can never dispatch.
        if phase == 'prepared' and installed and source:
            return Decision('publish')
        return Decision('terminal', error='pairing_superseded_after_redeem')
    if sealed:
        if not installed or not (e.binding == j['target_fingerprint'] or phase == 'redeemed' and source):
            if e.marker_error:
                return CHECK
            error = ('local_binding_changed' if not installed else 'pairing_superseded_after_redeem'
                     if phase == 'redeemed' else 'pairing_reconciliation_failed')
            return Decision('terminal', error=error)
        return Decision('archive', 'local_archive')
    if phase == 'retirement_pending':
        if not installed:
            return Decision('terminal', error='local_binding_changed')
        return Decision('retire', 'local_archive', replaceable=True)
    if phase == 'revoked':
        return Decision('terminal', error='pairing_recovery_revoked', replaceable=True)
    if phase != 'prepared':
        return CHECK
    if not j.get('redeem_request'):
        if guard and not clean_schedule:
            return CHECK
        return Decision('reservation' if guard else 'legacy', replaceable=bool(j.get('desktop_enrollment')))
    if 'desktop_failure' in j:
        if not j.get('desktop_enrollment') or j['desktop_failure'] != 'pairing_key_expired':
            return CHECK
        if installed and source:
            return Decision('terminal', 'terminated', j['desktop_failure'], replaceable=True)
    try:
        s = _schedule(j, e.schedule)
        if s['mode'] not in ('scheduled', 'manual_retryable'):
            return CHECK
        scheduled = s['mode'] == 'scheduled' and s['automatic'] < LIMIT and max(e.now, s['due']) < s['deadline']
        wait = max(0, math.ceil(s['not_before'] - e.now))
    except (ValueError, TypeError, KeyError, OverflowError):
        return CHECK
    if not (guard and installed and source):
        return Decision('terminal', 'terminated', 'local_binding_changed', replaceable=not wait)
    return Decision('wait' if wait else 'send', 'scheduled' if scheduled else 'manual', schedule=s,
                    automatic_due=scheduled and not wait and e.now >= s['due'], replaceable=not wait, wait_seconds=wait)
