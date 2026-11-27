"""Direct picker persistence. Discovery belongs to the controller, never to a reader.

A trusted adapter supplies the effective account/provider identity (and OpenCode's
working directory) on *each* read and captures the same key before asking a CLI.
No proven identity means no account snapshot. Hub menu state is owned separately
by ModelHubService; a native_cli Source can read inventory, not a user's menu.
All calls doing storage IO are synchronous: call off the event loop.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from core.chat_discovery import get_state_meta, set_state_meta, update_state_meta
from modules.agents.catalog import is_native_cli_backend


@dataclass(frozen=True)
class PickerModel:
    id: str  # Stable selection (e.g. opus or provider/id), not the resolved ID.
    label: str | None = None
    resolved_model: str | None = None
    reasoning_efforts: tuple[str, ...] | None = None
    default_reasoning_effort: str | None = None

    def __post_init__(self):
        _check_id(self.id)
        for text in (self.label, self.resolved_model, self.default_reasoning_effort):
            if text is not None and not isinstance(text, str):
                raise ValueError("invalid_model_metadata")
        if self.reasoning_efforts is not None:
            if not isinstance(self.reasoning_efforts, (list, tuple)) or not all(
                isinstance(e, str) for e in self.reasoning_efforts
            ):
                raise ValueError("invalid_reasoning_efforts")
            object.__setattr__(self, "reasoning_efforts", tuple(self.reasoning_efforts))


@dataclass(frozen=True)
class Snapshot:
    models: tuple[PickerModel, ...]
    # None means unknown (or a static fallback in read_menu), not a confirmed empty inventory.
    observed_at: str | None


def _check_id(value: object) -> None:
    if not isinstance(value, str) or not value.strip() or value.strip() != value:
        raise ValueError("invalid_model_id")


def _prefix(backend: str) -> str:
    if not is_native_cli_backend(backend):
        raise ValueError("unsupported_backend")
    return f"direct_model_menu.v1.{backend}."


def _inventory_key(backend: str, identity: str) -> str:
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("missing_inventory_identity")
    return _prefix(backend) + "inventory." + hashlib.sha256(identity.encode()).hexdigest()


def publish_inventory(
    backend: str,
    identity: str,
    models: Sequence[PickerModel] | None,
    *,
    db_path: Path | None = None,
) -> None:
    """Single-flight writer per scope. None (failed/partial read) never replaces a good list."""
    key = _inventory_key(backend, identity)
    if models is None:
        return
    if not all(isinstance(model, PickerModel) for model in models):
        raise ValueError("invalid_model_metadata")
    rows = [asdict(model) for model in models]
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("duplicate_model_id")
    set_state_meta(key, {"models": rows, "observed_at": datetime.now(timezone.utc).isoformat()}, db_path=db_path)


def read_inventory(backend: str, identity: str | None, *, db_path: Path | None = None) -> Snapshot:
    """Raw last-good answer for this exact scope. Never applies fallback or user removals."""
    _prefix(backend)
    if identity is not None:
        saved = get_state_meta(_inventory_key(backend, identity), db_path=db_path)
        if saved is not None:
            return Snapshot(tuple(PickerModel(**row) for row in saved["models"]), saved["observed_at"])
    return Snapshot((), None)


def read_menu(
    backend: str,
    identity: str | None,
    *,
    fallback: Sequence[PickerModel] = (),
    removed: bool = False,
    db_path: Path | None = None,
) -> Snapshot:
    """Normal picker, or only restorable rows for the Owner. Fallback must be public/static."""
    inventory = read_inventory(backend, identity, db_path=db_path)
    rows = inventory.models if inventory.observed_at is not None else fallback
    hidden = set(get_state_meta(_prefix(backend) + "removed", db_path=db_path) or ())
    return Snapshot(tuple(row for row in rows if (row.id in hidden) == removed), inventory.observed_at)


def set_removed(backend: str, model_id: str, removed: bool, *, db_path: Path | None = None) -> None:
    """Owner write. One Direct preference per backend/stable ID, independent of discovery."""
    key = _prefix(backend) + "removed"
    _check_id(model_id)
    if not isinstance(removed, bool):
        raise ValueError("invalid_removed")

    def change(previous):
        ids = set(previous or ())
        if removed:
            ids.add(model_id)
        else:
            ids.discard(model_id)
        return sorted(ids)

    update_state_meta(key, change, db_path=db_path)
