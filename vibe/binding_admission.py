"""Cross-process admission for changes to this machine's remote binding.

The admission lock is outermost: never wait for it with a config, address or DB
lock held. A live writer owns its OS lock; between worker steps (and after a
crash) the opposite operation's journal retains its reservation.
"""
from contextlib import contextmanager
from functools import wraps
import json
import os
import threading

from config import paths
from storage.lock import MigrationFileLock, MigrationLockTimeout


class OperationBusy(Exception):
    pass


_owner = threading.local()


def pairing_operation(operation):
    """Enter before selecting a journal or dispatching anything to the issuer."""
    @wraps(operation)
    def run(*args, **kwargs):
        try:
            with admitted('pairing'):
                return operation(*args, **kwargs)
        except OperationBusy:
            return {'ok': False, 'error': 'operation_in_progress', 'status': 409}
    return run


@contextmanager
def admitted(kind):
    if kind not in ('address', 'pairing'):
        raise ValueError('invalid operation kind')
    root = paths.get_state_dir()
    key = (os.path.realpath(root), kind)
    current = getattr(_owner, 'key', None)
    if current is not None:
        if current != key:
            raise OperationBusy()
        yield
        return
    lock = MigrationFileLock(root / 'remote-binding-admission.lock', timeout_seconds=0)
    try:
        lock.acquire()
    except MigrationLockTimeout:
        raise OperationBusy() from None
    try:
        if kind == 'address':
            from vibe.remote_access import _read_pending_pairing_record
            record, error = _read_pending_pairing_record()
            if record is not None or error is not None:
                raise OperationBusy()
        else:
            from vibe import instance_address
            path = instance_address._state_path()
            if path.is_symlink():
                raise OperationBusy()
            try:
                state = json.loads(path.read_text(encoding='utf-8'))
            except FileNotFoundError:
                pass
            except (OSError, ValueError, RecursionError):
                raise OperationBusy() from None
            else:
                if (not isinstance(state, dict) or state.get('schema_version') != instance_address._STATE_SCHEMA_VERSION
                        or 'journal' not in state or state['journal'] is not None):
                    raise OperationBusy()
        _owner.key = key
        try:
            yield
        finally:
            del _owner.key
    finally:
        lock.release()
