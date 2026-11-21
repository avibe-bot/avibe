"""Cross-process admission for changes to this machine's remote binding.

The admission lock is outermost: never wait for it with a config, address or DB
lock held. A live writer owns its OS lock; between worker steps (and after a
crash) the opposite operation's journal retains its reservation.
"""
from contextlib import contextmanager
from functools import wraps
import os
import threading

from config import paths
from storage.lock import MigrationFileLock, MigrationLockTimeout, try_shared_file_lease


class OperationBusy(Exception):
    pass


_owner = threading.local()


def run_pairing_operation(kind, operation, *args, **kwargs):
    try:
        with admitted(kind):
            return operation(*args, **kwargs)
    except OperationBusy:
        return {'ok': False, 'error': 'operation_in_progress', 'status': 409}


def pairing_operation(operation):
    @wraps(operation)
    def run(*args, **kwargs):
        return run_pairing_operation('pairing', operation, *args, **kwargs)
    return run


def publication_operation(operation):
    @wraps(operation)
    def run(*args, **kwargs):
        return run_pairing_operation('publication', operation, *args, **kwargs)
    return run


def publication_lease_path():
    # Never unlink: readers in the UI and controller must lock the same inode.
    return paths.get_state_dir() / 'address-cleanup-responses.lock'


def reset_intent_path():
    return paths.get_state_dir() / 'remote-binding-reset.json'


def reset_in_progress():
    # A malformed marker, including a dangling link, is never permission to
    # start another writer. Recovery must validate its contents separately.
    return os.path.lexists(reset_intent_path())


def check_rename_admission():
    from vibe import instance_address
    state = instance_address._state_path()
    try:
        state.lstat()
    except FileNotFoundError:
        return  # No rename to exclude. Pairing still checks config before redeem.
    except OSError:
        raise OperationBusy() from None
    if state.is_symlink():
        raise OperationBusy()
    binding = instance_address.current_binding()
    if binding is None and paths.get_config_path().exists():
        raise OperationBusy()
    if binding and instance_address._journal(binding) is not None:
        raise OperationBusy()


@contextmanager
def transfer_to_pairing():
    """The outer reset owner keeps the OS lock while completing its chosen handoff."""
    root = os.path.realpath(paths.get_state_dir())
    if getattr(_owner, 'key', None) != (root, 'reset') or reset_in_progress():
        raise OperationBusy()
    check_rename_admission()
    _owner.key = (root, 'pairing')
    try:
        yield
    finally:
        _owner.key = (root, 'reset')


@contextmanager
def _binding_lease(path, desktop):
    # Only guarded Desktop callers can share admission. Their reservation and
    # exact request CAS live under the config lock; a lost reply must not keep
    # the client from retrying that request while its old handler finishes.
    if desktop:
        handle = try_shared_file_lease(path)
        if handle is None:
            raise OperationBusy()
        with handle:
            yield
    else:
        lock = MigrationFileLock(path, timeout_seconds=0)
        try:
            lock.acquire()
        except MigrationLockTimeout:
            raise OperationBusy() from None
        try:
            yield
        finally:
            lock.release()


@contextmanager
def admitted(kind):
    if kind not in ('address', 'pairing', 'desktop', 'publication', 'reset'):
        raise ValueError('invalid operation kind')
    root = paths.get_state_dir()
    key = (os.path.realpath(root), kind)
    current = getattr(_owner, 'key', None)
    if current is not None:
        # A shared Desktop lease can publish its CAS-guarded result, but may
        # never be promoted to ordinary key pairing or another exclusive writer.
        nested = (current[0] == key[0] and (
            current[1] == 'pairing' and kind in ('desktop', 'publication') or
            current[1] == 'desktop' and kind == 'publication'))
        if current != key and not nested:
            raise OperationBusy()
        yield
        return
    with _binding_lease(root / 'remote-binding-admission.lock', kind == 'desktop'):
        if kind != 'reset' and reset_in_progress():
            raise OperationBusy()
        if kind == 'address':
            from vibe.remote_access import _issuer_replay_refused, _read_pending_pairing_record
            record, error = _read_pending_pairing_record()
            if error is not None or (record is not None and not _issuer_replay_refused(record)):
                raise OperationBusy()
        elif kind in ('pairing', 'desktop', 'publication'):
            check_rename_admission()
        _owner.key = key
        try:
            yield
        finally:
            del _owner.key
