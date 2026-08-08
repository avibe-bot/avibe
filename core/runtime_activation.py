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
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Generic, Iterator, TypeVar


_T = TypeVar("_T")
_RETIRED_DIAGNOSTIC_LIMIT = 64


@dataclass
class _GenerationBoundary:
    lock: threading.RLock = field(default_factory=threading.RLock)
    phase: str = "live"
    reservation_serial: int = 0


@dataclass(frozen=True)
class RuntimeActivationIdentity:
    """Opaque identity for one exact disposable backend resource generation."""

    backend: str
    resource_key: str
    generation: int
    _boundary: _GenerationBoundary = field(
        default_factory=_GenerationBoundary,
        compare=False,
        repr=False,
    )


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


@dataclass(frozen=True)
class RuntimeActivationRetirementReservation:
    """One exact in-progress retirement decision for a runtime generation."""

    identity: RuntimeActivationIdentity
    serial: int


class RuntimeActivationRegistry:
    """Serialize exact-generation owner commits against runtime reclamation.

    Each identity owns its boundary, so an old identity can never acquire a new
    generation's lock after replacement. Live targets are strongly referenced;
    retired identities are retained only in a fixed-size diagnostics cache.
    """

    def __init__(self) -> None:
        self._index_lock = threading.Lock()
        self._current: dict[
            tuple[str, str], RuntimeActivationIdentity
        ] = {}
        self._retired_diagnostics: OrderedDict[
            tuple[str, str], RuntimeActivationIdentity
        ] = OrderedDict()
        self._generations = itertools.count(1)

    @staticmethod
    def _target_key(backend: str, resource_key: str) -> tuple[str, str]:
        normalized_backend = str(backend or "").strip()
        normalized_resource_key = str(resource_key or "").strip()
        if not normalized_backend or not normalized_resource_key:
            raise ValueError("runtime activation target requires backend and resource_key")
        return normalized_backend, normalized_resource_key

    def attach(self, backend: str, resource_key: str) -> RuntimeActivationIdentity:
        """Install and return a fresh generation for one runtime resource target.

        The adapter must call this exactly once when a new disposable resource
        generation is installed or adopted, while holding its lifecycle lock.
        Repeated resource lookups must retain the returned identity instead of
        attaching another generation.
        """

        key = self._target_key(backend, resource_key)
        while True:
            with self._index_lock:
                previous = self._current.get(key)
                if previous is None:
                    identity = RuntimeActivationIdentity(
                        backend=key[0],
                        resource_key=key[1],
                        generation=next(self._generations),
                    )
                    self._current[key] = identity
                    self._retired_diagnostics.pop(key, None)
                    return identity

            with previous._boundary.lock:
                with self._index_lock:
                    if self._current.get(key) is not previous:
                        continue
                    if previous._boundary.phase == "retiring":
                        raise RuntimeError(
                            "cannot replace a runtime generation while it is retiring"
                        )
                    previous._boundary.phase = "retired"
                    self._remember_retired_locked(key, previous)
                    identity = RuntimeActivationIdentity(
                        backend=key[0],
                        resource_key=key[1],
                        generation=next(self._generations),
                    )
                    self._current[key] = identity
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
        while True:
            with self._index_lock:
                identity = self._current.get(key)
                if identity is None:
                    return (
                        self._retired_diagnostics.get(key)
                        if include_retired
                        else None
                    )
            with identity._boundary.lock:
                with self._index_lock:
                    if self._current.get(key) is not identity:
                        continue
                    if identity._boundary.phase == "live" or include_retired:
                        return identity
                    return None

    def is_current(self, identity: RuntimeActivationIdentity) -> bool:
        """Whether ``identity`` still names the live generation for its target."""

        key = self._target_key(identity.backend, identity.resource_key)
        with identity._boundary.lock:
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
        with identity._boundary.lock:
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
        with identity._boundary.lock:
            yield self._is_current_locked(key, identity)

    def reserve_retirement(
        self,
        identity: RuntimeActivationIdentity,
    ) -> RuntimeActivationRetirementReservation | None:
        """Block new admissions while an exact final snapshot is computed."""

        key = self._target_key(identity.backend, identity.resource_key)
        with identity._boundary.lock:
            if not self._is_current_locked(key, identity):
                return None
            identity._boundary.phase = "retiring"
            identity._boundary.reservation_serial += 1
            return RuntimeActivationRetirementReservation(
                identity=identity,
                serial=identity._boundary.reservation_serial,
            )

    def finish_retirement(
        self,
        reservation: RuntimeActivationRetirementReservation,
        *,
        retire: bool,
    ) -> bool:
        """Commit or abort one exact retirement reservation."""

        identity = reservation.identity
        key = self._target_key(identity.backend, identity.resource_key)
        with identity._boundary.lock:
            if (
                identity._boundary.phase != "retiring"
                or identity._boundary.reservation_serial != reservation.serial
            ):
                return False
            with self._index_lock:
                if self._current.get(key) is not identity:
                    return False
                if retire:
                    identity._boundary.phase = "retired"
                    self._current.pop(key, None)
                    self._remember_retired_locked(key, identity)
                else:
                    identity._boundary.phase = "live"
            return True

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

        reservation = self.reserve_retirement(identity)
        if reservation is None:
            return False
        try:
            should_retire = bool(final_predicate())
        except BaseException:
            self.finish_retirement(reservation, retire=False)
            raise
        if not should_retire:
            self.finish_retirement(reservation, retire=False)
            return False
        return self.finish_retirement(reservation, retire=True)

    def tracked_target_count(self) -> int:
        with self._index_lock:
            return len(self._current)

    def retired_diagnostic_count(self) -> int:
        with self._index_lock:
            return len(self._retired_diagnostics)

    def _is_current_locked(
        self,
        key: tuple[str, str],
        identity: RuntimeActivationIdentity,
    ) -> bool:
        with self._index_lock:
            return bool(
                self._current.get(key) is identity
                and identity._boundary.phase == "live"
            )

    def _remember_retired_locked(
        self,
        key: tuple[str, str],
        identity: RuntimeActivationIdentity,
    ) -> None:
        self._retired_diagnostics[key] = identity
        self._retired_diagnostics.move_to_end(key)
        while len(self._retired_diagnostics) > _RETIRED_DIAGNOSTIC_LIMIT:
            self._retired_diagnostics.popitem(last=False)
