"""Confined deletion primitive for Memory factory reset.

The Controller owns lifecycle and aggregate replacement.  This module only
knows the two mutable roots that factory reset is allowed to remove and
returns one truthful outcome for each root.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from config import paths
from core.memory.confined_filesystem import remove_confined_path


@dataclass(frozen=True, slots=True)
class FactoryResetRootOutcome:
    """Deletion result for one exact effective-home-relative root."""

    relative_path: str
    existed: bool
    deleted: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class FactoryResetDeletionResult:
    """Truthful result for both mutable Memory roots."""

    roots: tuple[FactoryResetRootOutcome, FactoryResetRootOutcome]

    @property
    def data_deleted(self) -> bool:
        return any(root.deleted for root in self.roots)

    @property
    def data_remaining(self) -> bool:
        return any(root.existed and not root.deleted for root in self.roots)

    def payload(self) -> dict[str, object]:
        return {
            "data_deleted": self.data_deleted,
            "data_remaining": self.data_remaining,
            "roots": [
                {
                    "path": root.relative_path,
                    "existed": root.existed,
                    "deleted": root.deleted,
                    **({"error": root.error} if root.error else {}),
                }
                for root in self.roots
            ],
        }


_ROOT_RELATIVE_PATHS = ("memory", "state/memory")


def _entry_state(path: Path) -> tuple[bool, str | None]:
    """Observe an entry without following a symlink, preserving lstat errors."""

    try:
        os.lstat(path)
    except FileNotFoundError:
        return False, None
    except OSError as error:
        # An unreadable root is conservatively treated as remaining. The reset
        # envelope must still include an outcome for this root so a retry can
        # report the same closed shape.
        return True, type(error).__name__
    return True, None


def delete_memory_roots(effective_home: Path) -> FactoryResetDeletionResult:
    """Remove exactly ``memory`` and ``state/memory`` beneath *effective_home*.

    Each root is attempted independently so a failure is represented honestly
    and a later retry can finish whichever root remains.  ``remove_confined_path``
    supplies the no-follow, owner-private confinement checks.
    """

    home = Path(os.path.abspath(os.path.expanduser(os.fspath(effective_home))))
    outcomes: list[FactoryResetRootOutcome] = []
    for relative_path in _ROOT_RELATIVE_PATHS:
        path = home / relative_path
        existed, observation_error = _entry_state(path)
        if observation_error is not None:
            outcomes.append(
                FactoryResetRootOutcome(
                    relative_path,
                    existed=True,
                    deleted=False,
                    error=observation_error,
                )
            )
            continue
        if not existed:
            outcomes.append(FactoryResetRootOutcome(relative_path, False, False))
            continue
        try:
            remove_confined_path(home, path)
        except Exception as error:  # noqa: BLE001
            outcomes.append(
                FactoryResetRootOutcome(
                    relative_path,
                    existed=True,
                    deleted=False,
                    error=type(error).__name__,
                )
            )
        else:
            remaining, observation_error = _entry_state(path)
            if observation_error is not None:
                outcomes.append(
                    FactoryResetRootOutcome(
                        relative_path,
                        existed=True,
                        deleted=False,
                        error=observation_error,
                    )
                )
            elif remaining:
                outcomes.append(
                    FactoryResetRootOutcome(
                        relative_path,
                        existed=True,
                        deleted=False,
                        error="root_reappeared",
                    )
                )
            else:
                outcomes.append(FactoryResetRootOutcome(relative_path, True, True))
    return FactoryResetDeletionResult(tuple(outcomes))  # type: ignore[arg-type]


def unchanged_memory_reset_result(
    effective_home: Path | None = None,
    *,
    reason: str,
) -> dict[str, object]:
    """Return the closed unchanged envelope after admission or retirement stops.

    The same lstat-based observation is used for every pre-delete failure so
    callers receive truthful outcomes for both exact mutable roots.
    """

    home = effective_home or paths.get_vibe_remote_dir()
    roots: list[dict[str, object]] = []
    for relative_path in _ROOT_RELATIVE_PATHS:
        existed, observation_error = _entry_state(home / relative_path)
        root: dict[str, object] = {
            "path": relative_path,
            "existed": existed,
            "deleted": False,
        }
        if observation_error is not None:
            root["error"] = observation_error
        roots.append(root)
    return {
        "ok": False,
        "error": "memory_factory_reset_failed",
        "result": "failed",
        "reason": reason,
        "data_deleted": False,
        "data_remaining": any(bool(root["existed"]) for root in roots),
        "roots": roots,
    }


__all__ = [
    "FactoryResetDeletionResult",
    "FactoryResetRootOutcome",
    "delete_memory_roots",
    "unchanged_memory_reset_result",
]
