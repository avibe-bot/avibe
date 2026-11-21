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


def _read():
    path = gate.reset_intent_path()
    if path.is_symlink() or not path.is_file():
        raise ClaimError('reset_repair_required')
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        valid = (isinstance(record, dict) and set(record) == {'schema', 'operation', 'phase', 'source', 'target', 'instance', 'registry', 'identity_file', 'pairing_file'}
                 and record['schema'] == 1 and record['phase'] in _PHASES
                 and all(isinstance(record[k], str) and re.fullmatch('[0-9a-f]{64}', record[k])
                         for k in ('source', 'target', 'registry'))
                 and all(record[k] is None or isinstance(record[k], str) and re.fullmatch('[0-9a-f]{64}', record[k])
                         for k in ('identity_file', 'pairing_file')) and isinstance(record['instance'], str)
                 and isinstance(record['operation'], str) and re.fullmatch('[0-9a-f]{32}', record['operation']))
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


def _seal(config):
    """Caller owns reset admission and the config lock after verifying consent."""
    if gate.reset_in_progress():
        raise ClaimError('operation_in_progress')
    cloud = config.remote_access.vibe_cloud
    record = {'schema': 1, 'operation': secrets.token_hex(16), 'phase': 'retire', 'source': _stamp(cloud),
              'target': _stamp(_blank(cloud)), 'instance': cloud.instance_id, 'registry': remote.device_binding(config)}
    record.update(_file_stamps())
    _save(record)
    return record


def _verify_stage(record, config):
    fingerprint = _stamp(config.remote_access.vibe_cloud)
    allowed = ({record['source']} if record['phase'] == 'retire' else {record['source'], record['target']}
               if record['phase'] == 'clear' else {record['target']})
    if fingerprint not in allowed:
        raise ClaimError('local_binding_changed')
    for key, actual in _file_stamps().items():
        if record['phase'] in ('retire', 'clear'):
            matches = actual == record[key]
        elif record['phase'] == 'artifacts':
            matches = actual is None or actual == record[key]  # Crash partway through unlink.
        else:
            matches = actual is None
        if not matches:
            raise ClaimError('local_binding_changed')


def _advance(record):
    from storage import device_registry, instance_address_clearing, remote_access_authorization_service
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
            raise ClaimError('reset_repair_required')
        device_registry.retire_binding(record['registry'], now=int(time.time()))
        if record['instance']:
            remote_access_authorization_service.delete_for_instance(record['instance'])
    elif phase == 'clear':
        with config_file_lock():
            cfg = local._config()
            _verify_stage(record, cfg)
            cfg.remote_access.vibe_cloud = _blank(cfg.remote_access.vibe_cloud)
            cfg.save()
    elif phase == 'artifacts':
        with config_file_lock():
            for path in (remote._pending_pairing_path(), personal_claims._marker(), personal_claims._marker(True),
                         binding_health._path(), local._identity_path()):
                path.unlink(missing_ok=True)
    elif phase == 'address':
        instance_address._state_path().unlink(missing_ok=True)
        with get_cached_sqlite_engine().begin() as conn:
            instance_address_clearing.forget_except(conn, instance_id=record['instance'], keep_hostnames=())
    elif phase == 'health':
        if os.path.lexists(mobile_store_path()):
            if not MobileStore(existing_only=True).revoke(caller='binding-reset').get('purged'):
                raise ClaimError('reset_repair_required')
    else:
        gate.reset_intent_path().unlink()
        return False
    record['phase'] = _PHASES[_PHASES.index(phase) + 1]
    _save(record)
    return True


def resume():
    """Only replay a previously sealed intent. Absence is not authorization."""
    with gate.admitted('reset'):
        record = _read()
        while _advance(record):
            pass
        return {'state': 'unpaired'}
