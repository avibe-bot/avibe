"""Read-only Model Hub snapshots, retaining the config dataclasses' value semantics.

Only the store freezes an exclusively owned parse, once per file revision. Normal
V2Config objects remain mutable. Editors use their existing payload clone, or
``dataclasses.replace`` on the particular node they change; unchanged inventories
can then be shared. This is an ownership boundary, not a security sandbox.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, is_dataclass
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from config.v2_config import ModelHubConfig


def _readonly(*_args, **_kwargs) -> NoReturn:
    raise FrozenInstanceError("Model Hub snapshots are read-only; copy the node before editing")


class _ReadOnlyList(list):
    # Keep list equality and JSON serialization: tuple conversion would change
    # pending-default comparisons and leak a different shape to API consumers.
    __setitem__ = __delitem__ = __iadd__ = __imul__ = _readonly
    append = clear = extend = insert = pop = remove = reverse = sort = _readonly


class _ReadOnlyDict(dict):
    __setitem__ = __delitem__ = __ior__ = _readonly
    clear = pop = popitem = setdefault = update = _readonly


class ModelHubSnapshotNode:
    """Mutable during construction/editing, read-only when owned by the store."""

    def __setattr__(self, name: str, value: object) -> None:
        if self.__dict__.get("_snapshot_readonly", False):
            _readonly()
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if self.__dict__.get("_snapshot_readonly", False):
            _readonly()
        object.__delattr__(self, name)


def _freeze(value: Any) -> Any:
    if isinstance(value, ModelHubSnapshotNode) and value.__dict__.get("_snapshot_readonly", False):
        return value
    if is_dataclass(value):
        # Include non-field instance state, notably vibey_supply_pending.
        for name, item in vars(value).items():
            object.__setattr__(value, name, _freeze(item))
        if isinstance(value, ModelHubSnapshotNode):
            object.__setattr__(value, "_snapshot_readonly", True)
        elif not value.__dataclass_params__.frozen:
            raise TypeError(f"Snapshot node is not freezable: {type(value).__name__}")
        return value
    if isinstance(value, list):
        return _ReadOnlyList(_freeze(item) for item in value)
    if isinstance(value, dict):
        return _ReadOnlyDict((key, _freeze(item)) for key, item in value.items())
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def freeze_model_hub(config: ModelHubConfig) -> ModelHubConfig:
    """Transfer an exclusively owned config graph to a deeply read-only snapshot."""

    return _freeze(config)
