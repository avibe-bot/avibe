"""Reconcile a personal pairing on the running UI service's existing worker."""
from __future__ import annotations

from contextlib import contextmanager
import logging
import threading

from config import paths
from vibe.desktop_bound_observation import service_generation
from vibe.personal_claim_client import ClaimError


_lifecycle = threading.Lock()
_current = None


class _Observer:
    def __init__(self, activate):
        self.activate = activate
        self.root = paths.physical_home(paths.get_vibe_remote_dir())
        info = self.root.stat()
        self.root_id = info.st_dev, info.st_ino
        self.accepted = None
        self.reading = threading.Lock()

    def reconcile(self):
        # A redirect completion can coincide with a periodic tick. Nothing is
        # owed by either caller; the next tick still observes the actual files.
        if not self.reading.acquire(blocking=False):
            return False
        try:
            if _current is not self:
                return False
            generation = service_generation(self.root, self.root_id)
            if generation is None or generation == self.accepted:
                return False
            if service_generation(self.root, self.root_id) != generation:
                return False
            with _lifecycle:
                if _current is not self:
                    return False
                # No config lock, config, target, credential or On request is handed
                # to the workers. They reload current state and own retry/Off policy.
                if self.activate():
                    self.accepted = generation
                    return True
        except (ClaimError, OSError):
            pass  # Includes a pending journal, a damaged marker and a replaced root.
        except Exception:
            # Optional adoption must neither terminate the settings recovery worker
            # nor turn a completed redirect into a new pairing attempt. No raw IO diagnostics.
            logging.getLogger(__name__).warning("Published binding adoption deferred")
        finally:
            self.reading.release()
        return False


def current():
    """Capture this lifetime, never dispatch a stale callback to its successor."""
    observer = _current
    return observer.reconcile if observer is not None else None


@contextmanager
def serving(activate):
    global _current
    try:
        observer = _Observer(activate)
    except (OSError, RuntimeError):
        observer = None  # Cannot anchor this service. Do not later follow a different home.
    with _lifecycle:
        _current = observer
    try:
        yield
    finally:
        with _lifecycle:
            if _current is observer:
                _current = None
