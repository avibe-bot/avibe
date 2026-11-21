"""Local interactive continuation of one durable operation; never create a claim."""
from __future__ import annotations

from config import paths
from config.v2_config import config_file_lock
from vibe import binding_admission as gate, binding_reset as reset, desktop_local_instance as local, remote_access as remote
from vibe.personal_claim_client import ClaimError


def _pair_reason(record, error, cfg):
    if error or not record:
        return 'pairing_record_unusable'
    if remote._issuer_replay_refused(record):
        return 'issuer_replay_refused'
    stamp = reset._stamp(cfg.remote_access.vibe_cloud)
    phase = record['phase']
    if phase in ('redeemed', 'applied') and (stamp == record['target_fingerprint'] or
            phase == 'redeemed' and stamp == record['source_fingerprint']):
        return 'exact_replay_pending'
    if phase == 'prepared' and stamp == record['source_fingerprint'] and record.get('redeem_request') and not record.get('desktop_failure'):
        enrollment = record.get('personal_claim') or record.get('desktop_enrollment')
        if enrollment and local._installation_id() == enrollment['installation_id']:
            return 'exact_replay_pending'
    return 'pairing_record_unusable'


def _run_pair(record):
    result = (remote._retry_installation_pairing(record) if record['phase'] == 'prepared' else
              remote._persist_pairing(None, device_name=record['device_name'], operation_id=record['operation_id']))
    if result.get('ok'):
        return {'state': 'bound'}
    if result.get('error') == 'operation_in_progress':
        return {'state': 'operation_in_progress'}
    current, error = remote._read_pending_pairing_record()
    if not current or current.get('operation_id') != record['operation_id']:
        return {'state': 'idle' if not error else 'pairing_recovery_required',
                **({'reason_code': 'pairing_record_unusable'} if error else {})}
    return {'state': 'pairing_recovery_required', 'reason_code': _pair_reason(current, error, local._config())}


def _repair_snapshot():
    """Under reset admission + config lock. Never derive a deletion path from a marker."""
    from storage.mobile_store import mobile_store_path
    root = paths.get_vibe_remote_dir().resolve(strict=True)
    marker = gate.reset_intent_path()
    # In particular do not replace a reset in a moved root or purge a linked
    # SQLite store. A missing config is not proof that this root is unbound.
    for path in (marker.parent, paths.get_config_path(), paths.get_sqlite_state_path(), mobile_store_path()):
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            raise ClaimError('reset_repair_required')
    if not marker.is_file() or marker.is_symlink() or not paths.get_config_path().is_file():
        raise ClaimError('reset_repair_required')
    cfg = local._config()
    reason = reset._repair_reason(cfg)
    if reason is None:
        raise ClaimError('operation_in_progress')
    record, error = remote._read_pending_pairing_record()
    if error:
        # A malformed pairing file cannot be declared terminal on behalf of the issuer.
        raise ClaimError('reset_repair_required')
    recoverable = _pair_reason(record, error, cfg) == 'exact_replay_pending'
    if record and not recoverable and not remote._issuer_replay_refused(record):
        raise ClaimError('reset_repair_required')
    root_stat = root.stat()
    state_stat = marker.parent.stat()
    stamps = (str(root), root_stat.st_dev, root_stat.st_ino, state_stat.st_dev, state_stat.st_ino,
              reset._file_stamp(paths.get_config_path()), reset._file_stamp(marker),
              reset._stamp(cfg.remote_access.vibe_cloud), reset._file_stamps(), reason, recoverable)
    return cfg, stamps, record, recoverable


def _consents(confirm):
    try:
        return confirm() is True
    except (EOFError, KeyboardInterrupt):
        return False


def _repair(*, confirm, keep_pairing):
    reset._require_local_terminal()
    with gate.admitted('reset'), config_file_lock():
        _, before, _, replay = _repair_snapshot()
        if replay != keep_pairing:
            raise ClaimError('pairing_recovery_required' if replay else 'reset_repair_required')
        if replay:
            gate.check_rename_admission()
    if not callable(confirm) or not _consents(confirm):
        return {'state': 'declined'}
    reset._require_local_terminal()
    with gate.admitted('reset'):
        with config_file_lock():
            cfg, after, record, replay = _repair_snapshot()
            if before != after:
                raise ClaimError('local_binding_changed')
            if replay:
                gate.check_rename_admission()
                # The unusable reset is withdrawn ONLY for this exact, still
                # durable request. A crash here leaves that request with its
                # normal recovery owner. The admission file lock never drops.
                gate.reset_intent_path().unlink()
            else:
                reset._seal(cfg, replace=True)
        if not replay:
            return _resume_reset()
        with gate.transfer_to_pairing():
            return _run_pair(record)


def _resume_reset():
    try:
        return reset.resume()
    except gate.OperationBusy:
        return {'state': 'operation_in_progress'}
    except Exception:
        try:
            with config_file_lock():
                reason = reset._repair_reason(local._config())
        except Exception:
            return {'state': 'reset_repair_required'}
        if reason is None:
            return {'state': 'resetting'}
        view = {'state': 'reset_repair_required', 'reason_code': reason}
        try:
            with config_file_lock():
                _, _, _, replay = _repair_snapshot()
            if replay:
                view['next_action'] = 'confirm_resume_pairing'
        except (ClaimError, OSError, ValueError, local.LocalInstanceError):
            pass
        return view


def resume_interactively(*, confirm_pairing=None):
    """Try one existing operation in this process. Busy/idle never mean success."""
    reset._require_local_terminal()
    try:
        if confirm_pairing is not None:
            result = _repair(confirm=confirm_pairing, keep_pairing=True)
            return result if result['state'] != 'declined' else {'state': 'reset_repair_required'}
        if gate.reset_in_progress():
            observed = reset._file_stamp(gate.reset_intent_path())
            with gate.admitted('reset'):
                if observed is None or reset._file_stamp(gate.reset_intent_path()) != observed:
                    return {'state': 'idle'}
                return _resume_reset()
        observed = reset._file_stamp(remote._pending_pairing_path())
        if observed is None:
            return {'state': 'idle'}
        with gate.admitted('pairing'):
            if reset._file_stamp(remote._pending_pairing_path()) != observed:
                return {'state': 'idle'}
            record, error = remote._read_pending_pairing_record()
            if not record and not error:
                return {'state': 'idle'}
            reason = _pair_reason(record, error, local._config())
            if reason == 'exact_replay_pending':
                return _run_pair(record)
            return {'state': 'pairing_recovery_required', 'reason_code': reason}
    except gate.OperationBusy:
        return {'state': 'operation_in_progress'}
    except (OSError, ValueError, local.LocalInstanceError, ClaimError):
        if gate.reset_in_progress():
            return {'state': 'reset_repair_required'}
        return {'state': 'pairing_recovery_required', 'reason_code': 'pairing_record_unusable'}
