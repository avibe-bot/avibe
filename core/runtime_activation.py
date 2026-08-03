"""In-process interlock between runtime admission and reclamation.

Durable Delivery, Turn, Activity, and Run rows remain the execution owners. This
registry only prevents an owner commit for an observed backend generation from
racing that exact generation's reclamation.

Backend lifecycle locks remain outermost. Callers attach, replace, or retire a
generation while holding the adapter's lifecycle lock, then use the short
synchronous boundary here for the final predicate or authoritative commit. No
cleanup await belongs inside this boundary.
"""

from __future__ import annotations

import itertools
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Generic, Iterator, TypeVar


_T = TypeVar("_T")


@dataclass(frozen=True)
class RuntimeActivationIdentity:
    """Opaque identity for one exact disposable backend resource generation."""

    backend: str
    resource_key: str
    generation: int


@dataclass(frozen=True)
class RuntimeActivationCommit(Generic[_T]):
    """Result of an owning commit attempted against an observed generation."""

    admitted: bool
    value: _T | None = None


@dataclass(frozen=True)
class RuntimeActivationResolution:
    """Tri-state exact-target lookup used by durable admission owners."""

    authoritative: bool
    identity: RuntimeActivationIdentity | None = None


@dataclass
class _GenerationState:
    identity: RuntimeActivationIdentity
    retired: bool = False


class RuntimeActivationRegistry:
    """Serialize exact-generation owner commits against runtime reclamation.

    Locks are retained for the registry lifetime so an old identity can never
    acquire a different lock after a target is replaced. The number of runtime
    resource targets is naturally bounded by the controller's adapter caches.
    """

    def __init__(self) -> None:
        self._index_lock = threading.Lock()
        self._boundaries: dict[tuple[str, str], threading.RLock] = {}
        self._states: dict[tuple[str, str], _GenerationState] = {}
        self._generations = itertools.count(1)

    @staticmethod
    def _target_key(backend: str, resource_key: str) -> tuple[str, str]:
        normalized_backend = str(backend or "").strip()
        normalized_resource_key = str(resource_key or "").strip()
        if not normalized_backend or not normalized_resource_key:
            raise ValueError("runtime activation target requires backend and resource_key")
        return normalized_backend, normalized_resource_key

    def _boundary(self, key: tuple[str, str]) -> threading.RLock:
        with self._index_lock:
            return self._boundaries.setdefault(key, threading.RLock())

    def attach(self, backend: str, resource_key: str) -> RuntimeActivationIdentity:
        """Install and return a fresh generation for one runtime resource target.

        The adapter must call this exactly once when a new disposable resource
        generation is installed or adopted, while holding its lifecycle lock.
        Repeated resource lookups must retain the returned identity instead of
        attaching another generation.
        """

        key = self._target_key(backend, resource_key)
        boundary = self._boundary(key)
        with boundary:
            identity = RuntimeActivationIdentity(
                backend=key[0],
                resource_key=key[1],
                generation=next(self._generations),
            )
            self._states[key] = _GenerationState(identity=identity)
            return identity

    def current(
        self,
        backend: str,
        resource_key: str,
        *,
        include_retired: bool = False,
    ) -> RuntimeActivationIdentity | None:
        """Return the current target identity without creating a generation."""

        key = self._target_key(backend, resource_key)
        boundary = self._boundary(key)
        with boundary:
            state = self._states.get(key)
            if state is None or (state.retired and not include_retired):
                return None
            return state.identity

    def is_current(self, identity: RuntimeActivationIdentity) -> bool:
        """Whether ``identity`` still names the live generation for its target."""

        key = self._target_key(identity.backend, identity.resource_key)
        boundary = self._boundary(key)
        with boundary:
            return self._is_current_locked(key, identity)

    def commit_if_current(
        self,
        identity: RuntimeActivationIdentity,
        commit: Callable[[], _T],
    ) -> RuntimeActivationCommit[_T]:
        """Run one authoritative synchronous owner commit if generation is live.

        Generation validation and ``commit`` execute under the same target
        boundary. A cleanup-first loser receives ``admitted=False`` and must use
        its existing exact recovery path; ``commit`` is never called.
        """

        key = self._target_key(identity.backend, identity.resource_key)
        boundary = self._boundary(key)
        with boundary:
            if not self._is_current_locked(key, identity):
                return RuntimeActivationCommit(admitted=False)
            return RuntimeActivationCommit(admitted=True, value=commit())

    @contextmanager
    def hold_if_current(
        self,
        identity: RuntimeActivationIdentity,
    ) -> Iterator[bool]:
        """Hold one generation boundary across an externally owned commit.

        SQLite commits only when its transaction context exits. This form lets
        callers acquire the activation boundary before opening that transaction
        and release it only after commit. A false value means cleanup already
        retired or replaced the observed generation; callers may persist a
        queued/waiting owner, but must not promote it to starting or active.
        """

        key = self._target_key(identity.backend, identity.resource_key)
        boundary = self._boundary(key)
        with boundary:
            yield self._is_current_locked(key, identity)

    def retire_if_current(
        self,
        identity: RuntimeActivationIdentity,
        final_predicate: Callable[[], bool],
    ) -> bool:
        """Retire one generation only if its final locked predicate still allows it.

        The reclaimer holds its backend lifecycle lock before entering here.
        ``final_predicate`` performs the final ownership/progress snapshot under
        this boundary. On success the generation is marked retired before the
        boundary is released; process stop, receiver joins, and other awaits then
        happen outside this registry.
        """

        key = self._target_key(identity.backend, identity.resource_key)
        boundary = self._boundary(key)
        with boundary:
            if not self._is_current_locked(key, identity):
                return False
            if not final_predicate():
                return False
            self._states[key].retired = True
            return True

    def _is_current_locked(
        self,
        key: tuple[str, str],
        identity: RuntimeActivationIdentity,
    ) -> bool:
        state = self._states.get(key)
        return bool(
            state is not None
            and not state.retired
            and state.identity == identity
        )
