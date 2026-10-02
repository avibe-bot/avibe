"""Hook-state JSON identity, without Python-to-JSON coercion."""

from __future__ import annotations

import json
import math
from typing import Any


class HookStateError(ValueError):
    """A hook supplied a value outside the exact JSON state contract."""


def state_representation(state: Any) -> str:
    """Validate before comparison; bool, int and float remain distinct."""
    active: set[int] = set()

    def validate(value: Any) -> None:
        kind = type(value)
        if kind in (str, int, bool, type(None)):
            return
        if kind is float:
            if not math.isfinite(value):
                raise HookStateError("Hook state numbers must be finite.")
            return
        if kind not in (dict, list):
            raise HookStateError(f"Hook state requires exact JSON types, not {kind.__name__}.")
        if id(value) in active:
            raise HookStateError("Hook state must not contain cycles.")
        active.add(id(value))
        try:
            if kind is dict:
                for key, item in value.items():
                    if type(key) is not str:
                        raise HookStateError("Hook state object keys must be strings.")
                    validate(item)
            else:
                for item in value:
                    validate(item)
        finally:
            active.remove(id(value))

    if type(state) is not dict:
        raise HookStateError("Hook state must be a JSON object.")
    validate(state)
    return json.dumps(state, allow_nan=False, sort_keys=True, separators=(",", ":"))
