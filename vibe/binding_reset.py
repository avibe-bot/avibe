"""Recover a locally confirmed reset. No issuer call and no browser-supplied target.

The reset marker is authoritative only for the config fingerprints and fixed data
root it names. Every destructive step runs under binding admission. Broken or
stale markers leave enrollment closed; never guess which config to clear.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
import time

from config.atomic_io import write_atomic
from config.v2_config import VibeCloudRemoteAccessConfig, config_file_lock
from vibe import binding_admission as gate, desktop_local_instance as local, remote_access as remote
from vibe.personal_claim_client import ClaimError


_PHASES = ('retire', 'clear', 'artifacts', 'address', 'health', 'done')


def _stamp(cloud):
    return remote._pairing_identity_fingerprint(remote._pairing_identity_from_cloud(cloud))


def _save(intent):
    write_atomic(gate.reset_intent_path(), json.dumps(intent, sort_keys=True))


_CLOUD_PHASES = ('cloud', 'repair', 'invalid_credential', 'ineligible')


def _read():
    from vibe import binding_unbind as unbind
    path = gate.reset_intent_path()
    if path.is_symlink() or not path.is_file():
        raise ClaimError('reset_repair_required')
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        common = {'schema', 'operation', 'phase', 'source', 'target', 'instance', 'registry', 'identity_file', 'pairing_file'}
        schema = record.get('schema') if isinstance(record, dict) else None
        fields = set(record) - {'connection_file'} if isinstance(record, dict) else set()
        legacy = (type(schema) is int and schema == 1 and fields == common
                  and record['phase'] in _PHASES and isinstance(record['operation'], str)
                  and re.fullmatch('[0-9a-f]{32}', record['operation']))
        hosted_fields = common | {'root', 'accepted', 'was_requested'}
        hosted = (type(schema) is int and schema == 2 and fields in (hosted_fields, hosted_fields | {'dispatched'})
                  and type(record.get('dispatched', False)) is bool
                  and record['phase'] in _CLOUD_PHASES + _PHASES and unbind.valid_operation(record['operation'])
                  and type(record['accepted']) is bool and type(record['was_requested']) is bool
                  and isinstance(record['root'], str) and re.fullmatch('[0-9a-f]{64}', record['root']))
        valid = ((legacy or hosted)
                 and (record.get('connection_file') is None or isinstance(record['connection_file'], str)
                      and re.fullmatch('[0-9a-f]{64}', record['connection_file']))
                 and all(isinstance(record[k], str) and re.fullmatch('[0-9a-f]{64}', record[k])
                         for k in ('source', 'target', 'registry'))
                 and all(record[k] is None or isinstance(record[k], str) and re.fullmatch('[0-9a-f]{64}', record[k])
                         for k in ('identity_file', 'pairing_file')) and isinstance(record['instance'], str))
    except (OSError, ValueError, RecursionError):
        valid = False
    if not valid:
        raise ClaimError('reset_repair_required')
    return record


def _blank(cloud):
    # Keep connector tuning, not credentials, direct grants, or a private issuer.
    return VibeCloudRemoteAccessConfig(**{name: getattr(cloud, name) for name in (
        'cloudflared_path', 'transport_protocol', 'auto_recovery', 'optimization_profile', 'edge_ip_version', 'edge_bind_address')})


def _file_stamp(path):
    """Identify even malformed enrollment files, without following links or saving their contents."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    digest = hashlib.sha256()
    digest.update(f'{info.st_dev}:{info.st_ino}:{info.st_mode}:{info.st_size}:{info.st_mtime_ns}:'.encode())
    if stat.S_ISREG(info.st_mode):
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or not os.path.samestat(info, opened)):
                raise ClaimError('local_binding_changed')
            # File identity and mtime fence replacement even for oversized damaged records.
            digest.update(stream.read(65536))
    elif not stat.S_ISLNK(info.st_mode):
        raise ClaimError('reset_repair_required')
    return digest.hexdigest()


def _file_stamps():
    return {'identity_file': _file_stamp(local._identity_path()), 'pairing_file': _file_stamp(remote._pending_pairing_path())}


def _new_record(config):
    cloud = config.remote_access.vibe_cloud
    return {'source': _stamp(cloud), 'target': _stamp(_blank(cloud)), 'instance': cloud.instance_id,
            'registry': remote.device_binding(config), **_file_stamps()}


def _unbind_candidate(config):
    from vibe import binding_health, binding_unbind as unbind
    if gate.reset_in_progress():
        raise ClaimError('operation_in_progress')
    cloud = config.remote_access.vibe_cloud
    if not cloud.instance_id or not cloud.instance_secret or cloud.backend_url.rstrip('/') != 'https://avibe.bot':
        raise ClaimError('not_bound')
    if binding_health.rejected_at(config) is not None:
        raise ClaimError('binding_rejected')
    pending, error = remote._read_pending_pairing_record()
    if error or (pending and not remote._issuer_replay_refused(pending)):
        raise ClaimError('operation_in_progress')
    return {'root': unbind.root_stamp(), **_new_record(config)}


def unbind_snapshot():
    """Server-private preview evidence. Never serialize this as the confirmation token."""
    with gate.admitted('reset'):
        gate.check_rename_admission()
        with config_file_lock():
            return _unbind_candidate(local._config())


def seal_unbind(expected):
    """Trusted adapters call only after authenticating and consuming explicit consent."""
    from uuid import uuid4
    with gate.admitted('reset'):
        gate.check_rename_admission()
        with remote._connector_lifecycle(timeout=0), config_file_lock():
            current = _unbind_candidate(local._config())
            if current != expected:
                raise ClaimError('local_binding_changed')
            record = {**current, 'schema': 2, 'operation': str(uuid4()), 'phase': 'cloud',
                      'accepted': False, 'dispatched': False,
                      'was_requested': remote._connector_requested_path().is_file()}
            _save(record)
            return record['operation']


def stop_sealed_unbind():
    with gate.admitted('reset'):
        with config_file_lock():
            cfg = local._config()
            record = _read()
            if record['phase'] != 'cloud':
                raise ClaimError('operation_in_progress')
            _verify_stage(record, cfg)
        # Sealed means accepted. If this stop fails, the worker stops again before any send.
        try:
            remote._stop_direct_lifecycle_before_pairing_discard(cfg)
            remote.stop(cfg)
        except Exception:
            pass
        return {'state': 'unbinding'}


def accept_unbind(expected):
    """Seal and quiesce locally under one admission; the scheduled worker owns all hosted I/O."""
    with gate.admitted('reset'):
        seal_unbind(expected)
        return stop_sealed_unbind()


def _seal(config, *, replace=False):
    """Caller owns reset admission and the config lock after verifying consent."""
    if gate.reset_in_progress() != replace:
        raise ClaimError('operation_in_progress')
    record = {**_new_record(config), 'schema': 1, 'operation': secrets.token_hex(16), 'phase': 'retire'}
    _save(record)
    return record


def _verify_stage(record, config):
    if record.get('schema') == 2:
        from vibe.binding_unbind import root_stamp
        if record['root'] != root_stamp() or record['phase'] == 'repair':
            raise ClaimError('reset_repair_required')
    fingerprint = _stamp(config.remote_access.vibe_cloud)
    allowed = ({record['source']} if record['phase'] in ('retire', *_CLOUD_PHASES) else {record['source'], record['target']}
               if record['phase'] == 'clear' else {record['target']})
    if fingerprint not in allowed:
        raise ClaimError('local_binding_changed')
    for key, actual in _file_stamps().items():
        if record['phase'] in ('retire', 'clear', *_CLOUD_PHASES):
            matches = actual == record[key]
        elif record['phase'] == 'artifacts':
            matches = actual is None or actual == record[key]  # Crash partway through unlink.
        else:
            matches = actual is None
        if not matches:
            raise ClaimError('local_binding_changed')


def _repair_reason(config):
    try:
        record = _read()
    except ClaimError:
        return 'reset_record_damaged'
    try:
        _verify_stage(record, config)
    except ClaimError:
        if record['schema'] == 2:
            return 'unbind_requires_attention'
        allowed = ({record['source']} if record['phase'] in ('retire', *_CLOUD_PHASES) else {record['source'], record['target']}
                   if record['phase'] == 'clear' else {record['target']})
        return 'binding_changed' if _stamp(config.remote_access.vibe_cloud) not in allowed else 'files_changed'
    return None


def _advance(record):
    from storage import device_registry
    from storage.models import (instance_address_clearings, instance_address_clearing_receipts,
                                remote_access_authorizations, remote_device_sessions)
    from sqlalchemy import select
    from storage.db import get_cached_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.mobile_store import MobileStore, mobile_store_path
    from vibe import binding_health, instance_address, personal_claims

    phase = record['phase']
    with config_file_lock():
        cfg = local._config()
        _verify_stage(record, cfg)
    if phase == 'retire':
        ensure_sqlite_state()
        instance_address.drain_for_reset()
        remote._stop_direct_lifecycle_before_pairing_discard(cfg)
        from vibe.direct_certificates import discard_direct_certificates
        discard_direct_certificates()
        if not remote.stop(cfg).get('ok'):
            raise ClaimError('reset_in_progress')  # transient failure; the same sealed phase can retry
        # Explicit reset detaches this entire data root, not just the instance ID
        # still readable in its config. In particular repair can start after clear.
        with get_cached_sqlite_engine().begin() as conn:
            generations = set(conn.scalars(select(remote_device_sessions.c.binding).distinct()))
            conn.execute(remote_access_authorizations.delete())
        for generation in generations | {record['registry']}:
            device_registry.retire_binding(generation, now=int(time.time()))
    elif phase == 'clear':
        with remote._connector_lifecycle(timeout=0), config_file_lock():
            cfg = local._config()
            _verify_stage(record, cfg)
            from vibe.connection_settings import journal
            # On replay of an already published config, never capture a newer
            # journal. A reset of an already blank config uses the saved token.
            if _stamp(cfg.remote_access.vibe_cloud) == record['source']:
                version, pins = journal.for_binding_cutover(cfg)
                if record['source'] != record['target'] or 'connection_file' not in record:
                    record['connection_file'] = version
                    _save(record)
                cleared = _blank(cfg.remote_access.vibe_cloud)
                for field, restored in pins.items():
                    setattr(cleared, field, restored)
                cfg.remote_access.vibe_cloud = cleared
                cfg.save()
            journal.retire_version(record.get('connection_file'))
    elif phase == 'artifacts':
        with config_file_lock():
            for path in (remote._pending_pairing_path(), personal_claims._marker(), personal_claims._marker(True),
                         binding_health._path(), local._identity_path()):
                path.unlink(missing_ok=True)
    elif phase == 'address':
        instance_address._state_path().unlink(missing_ok=True)
        with get_cached_sqlite_engine().begin() as conn:
            conn.execute(instance_address_clearing_receipts.delete())
            conn.execute(instance_address_clearings.delete())
    elif phase == 'health':
        if os.path.lexists(mobile_store_path()):
            if not MobileStore(existing_only=True).revoke(caller='binding-reset').get('purged'):
                raise ClaimError('reset_repair_required')
    elif phase == 'done':
        if record['schema'] == 2:
            from vibe import binding_unbind
            binding_unbind.save_result(record['target'], 'unbound')
        gate.reset_intent_path().unlink()
        return False
    else:
        raise ClaimError('reset_repair_required')
    record['phase'] = _PHASES[_PHASES.index(phase) + 1]
    _save(record)
    return True


def _require_local_terminal():
    from vibe.authorization import current_invocation_authority
    from vibe.cli import _cli_invocation_authority
    from vibe.ui_request_context import has_request_context

    if (current_invocation_authority() is not None or has_request_context() or _cli_invocation_authority() is not None
            or not sys.stdin.isatty() or not sys.stdout.isatty()):
        raise ClaimError('local_operator_required')


def _cli_snapshot():
    if gate.reset_in_progress():
        raise ClaimError('operation_in_progress')
    config = local._config()
    cloud = config.remote_access.vibe_cloud
    files = _file_stamps()
    if not any((cloud.instance_id, cloud.instance_secret, cloud.tunnel_token, cloud.client_id, cloud.session_secret,
                files['identity_file'], files['pairing_file'])):
        raise ClaimError('not_bound')
    from vibe.binding_recovery import _pair_reason
    record, error = remote._read_pending_pairing_record()
    if _pair_reason(record, error, config) == 'exact_replay_pending':
        raise ClaimError('pairing_recovery_required')
    return config, (_stamp(cloud), files)


def reset_interactively(*, confirm, repair=False):
    """CLI adapter only. A trusted prompt returns literal True after explicit consent.

    Do not hold a config lock across input, or accept a serialized confirmation
    from an HTTP/Harness caller. Recheck the generation and both authorities
    before sealing. The ordinary link claim is a separate, later operation.
    """
    _require_local_terminal()
    if not callable(confirm):
        raise ClaimError('invalid_request')
    if repair:
        from vibe.binding_recovery import _repair
        return _repair(confirm=confirm, keep_pairing=False)
    with gate.admitted('reset'), config_file_lock():
        _, before = _cli_snapshot()
    try:
        decision = confirm()
    except (EOFError, KeyboardInterrupt):
        decision = False
    if decision is not True:
        return {'state': 'declined'}
    _require_local_terminal()
    with gate.admitted('reset'):
        with config_file_lock():
            cfg, after = _cli_snapshot()
            if before != after:
                raise ClaimError('local_binding_changed')
            _seal(cfg)
        from vibe.binding_recovery import _resume_reset
        return _resume_reset()


def dispatch_evidence():
    """What a terminal may say about an unbind request this computer made. Read-only.

    ``received``: avibe.bot answered the exact pending receipt. ``maybe_sent``: a
    send may have happened without an exact answer. None: none was sent, or no
    hosted unbind is recorded. Never a reason to resend or to clear anything.
    """
    try:
        record = _read()
    except ClaimError:
        return None
    if record['schema'] != 2:
        return None
    if record['accepted']:
        return 'received'
    return 'maybe_sent' if record.get('dispatched', True) else None


def unbind_interactively(*, confirm):
    """Terminal "bind as a new computer" on a bound computer: delete first.

    The question is asked with no lock held. The answer seals the same hosted
    intent as the page, then one bounded attempt runs here; later attempts belong
    to the scheduled worker. Raises ``binding_rejected``/``not_bound`` before
    asking when this computer has no deletable binding (the caller keeps D1).
    """
    _require_local_terminal()
    if not callable(confirm):
        raise ClaimError('invalid_request')
    with gate.admitted('reset'):
        gate.check_rename_admission()
        with config_file_lock():
            expected = _unbind_candidate(local._config())
    try:
        decision = confirm()
    except (EOFError, KeyboardInterrupt):
        decision = False
    if decision is not True:
        return {'state': 'declined'}
    _require_local_terminal()
    with gate.admitted('reset'):
        accept_unbind(expected)
        operation = _read()['operation']
        from vibe.binding_recovery import _resume_reset
        view = _resume_reset()
    return {**view, 'operation': operation}


def reset_after_refusal_interactively(operation, *, confirm):
    """The separate local-only consent after this run's exact 401. Sends nothing."""
    from vibe import binding_unbind
    _require_local_terminal()
    with gate.admitted('reset'), config_file_lock():
        if binding_unbind.last_result(local._config(), operation=operation) != {
                'state': 'not_unbound', 'reason_code': 'invalid_credential'}:
            raise ClaimError('local_binding_changed')
    # reset_interactively re-snapshots the generation around the question.
    return reset_interactively(confirm=confirm)


def reset_local_only_interactively(*, confirm):
    """Local TTY escape, never an issuer request or an ordinary claim start."""
    _require_local_terminal()
    if not gate.reset_in_progress():
        return reset_interactively(confirm=confirm)
    from vibe.binding_recovery import _repair, _resume_reset
    with gate.admitted('reset'):
        try:
            record = _read()
            with config_file_lock():
                _verify_stage(record, local._config())
        except ClaimError:
            pass  # Invalid or stale intent needs a fresh, independently scoped TTY decision.
        else:
            if record['phase'] in _PHASES:
                # Consent is already durable; these phases cannot issue a cloud request.
                return _resume_reset()
    return _repair(confirm=confirm, keep_pairing=False, allow_cloud=True)


def _resume_cloud(record):
    """One dispatch under reset admission, without a config or SQLite lock during I/O."""
    from vibe import binding_unbind
    with config_file_lock():
        cfg = local._config()
        _verify_stage(record, cfg)
    remote._stop_direct_lifecycle_before_pairing_discard(cfg)
    if not remote.stop(cfg).get('ok'):
        return {'state': 'unbinding'}
    if record.get('dispatched') is False:
        with config_file_lock():
            if _read() != record:
                raise ClaimError('local_binding_changed')
            record['dispatched'] = True  # Before the first byte: from now on it may have been sent.
            _save(record)
    outcome = binding_unbind.send_original(cfg, record['operation'], accepted=record['accepted'])
    with config_file_lock():
        if _read() != record:
            raise ClaimError('local_binding_changed')
        _verify_stage(record, local._config())
        if outcome == 'retry':
            return {'state': 'unbinding'}
        if outcome == 'pending':
            if not record['accepted']:
                record['accepted'] = True
                _save(record)
            return {'state': 'unbinding'}
        record['phase'] = {'unbound': 'retire', 'ineligible': 'ineligible',
                           'invalid_credential': 'invalid_credential'}.get(outcome, 'repair')
        _save(record)
    return None


def _finish_denial(record):
    from vibe import binding_unbind as unbind
    with remote._connector_lifecycle(timeout=0), config_file_lock():
        if record != _read() or record['accepted']:
            raise ClaimError('reset_repair_required')
        _verify_stage(record, local._config())
        request = remote._connector_requested_path()
        if record['was_requested']:
            request.parent.mkdir(parents=True, exist_ok=True)
            request.touch(exist_ok=True)
        else:
            request.unlink(missing_ok=True)
        reason = record['phase']
        unbind.save_result(record['source'], 'not_unbound', reason, operation=record['operation'])
        gate.reset_intent_path().unlink()
        cfg = local._config()
    remote._release_direct_lifecycle_after_refused_change(cfg)
    return {'state': 'not_unbound', 'reason_code': reason, 'operation': record['operation']}


def resume():
    """Only replay a previously sealed intent. Absence is not authorization."""
    with gate.admitted('reset'):
        record = _read()
        if record['phase'] == 'cloud':
            waiting = _resume_cloud(record)
            if waiting is not None:
                return waiting
        if record['phase'] in ('ineligible', 'invalid_credential'):
            return _finish_denial(record)
        while _advance(record):
            pass
        return {'state': 'unpaired'}
