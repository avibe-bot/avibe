"""Confined deletion primitive for destructive Memory data operations.

The Controller owns lifecycle and aggregate replacement. This module accepts no
caller-supplied path: it removes the one mutable data root and narrowly named
retired recovery residue while preserving the stable identity store.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from config import paths
from avibe_memory.confined_filesystem import (
    ConfinedRemovalProgress,
    remove_confined_path,
)


@dataclass(frozen=True, slots=True)
class MemoryDataRootOutcome:
    """Deletion result for one exact effective-home-relative surface."""

    relative_path: str
    existed: bool
    deleted: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryDataResetResult:
    """Truthful result for every fixed Memory deletion surface."""

    roots: tuple[MemoryDataRootOutcome, ...]

    @property
    def data_deleted(self) -> bool:
        return any(root.deleted for root in self.roots)

    @property
    def data_remaining(self) -> bool:
        return any(
            root.error is not None or (root.existed and not root.deleted)
            for root in self.roots
        )

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


_DATA_ROOT_RELATIVE_PATH = "memory"
_RETIRED_RECOVERY_RELATIVE_PATHS = (
    "state/memory/clear-intent.json",
    "state/memory/clear-journal.sqlite",
    "state/memory/clear-snapshots",
    "state/memory/backup-restore-journal.sqlite",
    "state/memory/backups",
)
_RESET_RELATIVE_PATHS = (
    _DATA_ROOT_RELATIVE_PATH,
    *_RETIRED_RECOVERY_RELATIVE_PATHS,
)


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


def reset_memory_data_roots(effective_home: Path) -> MemoryDataResetResult:
    """Remove the exact mutable root and retired recovery residue.

    ``state/memory/memory.sqlite`` is deliberately outside this list because it
    owns stable scope identity and the project catalog. Each fixed surface is
    attempted independently so a failure is represented honestly and a later
    explicit operation can start again. ``remove_confined_path`` supplies the
    no-follow, owner-private confinement checks.
    """

    home = Path(os.path.abspath(os.path.expanduser(os.fspath(effective_home))))
    outcomes: list[MemoryDataRootOutcome] = []
    for relative_path in _RESET_RELATIVE_PATHS:
        path = home / relative_path
        existed, observation_error = _entry_state(path)
        if observation_error is not None:
            outcomes.append(
                MemoryDataRootOutcome(
                    relative_path,
                    existed=True,
                    deleted=False,
                    error=observation_error,
                )
            )
            continue
        progress = ConfinedRemovalProgress()
        try:
            remove_confined_path(home, path, progress=progress)
        except Exception as error:  # noqa: BLE001
            outcomes.append(
                MemoryDataRootOutcome(
                    relative_path,
                    existed=existed,
                    deleted=progress.changed,
                    error=type(error).__name__,
                )
            )
        else:
            remaining, observation_error = _entry_state(path)
            if observation_error is not None:
                outcomes.append(
                    MemoryDataRootOutcome(
                        relative_path,
                        existed=True,
                        deleted=progress.changed,
                        error=observation_error,
                    )
                )
            elif remaining:
                outcomes.append(
                    MemoryDataRootOutcome(
                        relative_path,
                        existed=True,
                        deleted=progress.changed,
                        error="root_reappeared",
                    )
                )
            else:
                outcomes.append(
                    MemoryDataRootOutcome(
                        relative_path,
                        existed=existed,
                        deleted=existed,
                    )
                )
    return MemoryDataResetResult(tuple(outcomes))


def unchanged_memory_data_result(
    effective_home: Path | None = None,
    *,
    operation: str,
    reason: str,
) -> dict[str, object]:
    """Return the closed unchanged envelope after admission or retirement stops.

    The same lstat-based observation is used for every pre-delete failure so
    callers receive truthful outcomes for every exact deletion surface.
    """

    home = effective_home or paths.get_vibe_remote_dir()
    roots: list[dict[str, object]] = []
    for relative_path in _RESET_RELATIVE_PATHS:
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
        "operation": operation,
        "error": f"memory_{operation}_failed",
        "result": "failed",
        "reason": reason,
        "data_deleted": False,
        "data_remaining": any(bool(root["existed"]) for root in roots),
        "roots": roots,
    }


__all__ = [
    "MemoryDataResetResult",
    "MemoryDataRootOutcome",
    "reset_memory_data_roots",
    "unchanged_memory_data_result",
]
