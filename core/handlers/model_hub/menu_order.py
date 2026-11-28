"""Explicit relative menu order, independent of membership and Source evidence."""
from __future__ import annotations

import json
from pathlib import Path

from config.v2_config import ModelHubBackendModelConfig
from .events import contains_credential_material
from .state_file import write_state_document


class MenuOrder:
    """An older binary can ignore this preference without losing its routing config."""

    def __init__(self, path: Path):
        self.path = path
        self.damaged = False
        self.ids: tuple[str, ...] = ()
        try:
            ids = json.loads(path.read_bytes())
            if (not isinstance(ids, list) or not all(isinstance(mid, str) and mid and not contains_credential_material(mid)
                                                    for mid in ids) or len(set(ids)) != len(ids)):
                raise ValueError("invalid menu order")
            self.ids = tuple(ids)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, RecursionError):
            self.damaged = True

    def apply(self, rows: list[ModelHubBackendModelConfig]) -> list[ModelHubBackendModelConfig]:
        rank = {mid: index for index, mid in enumerate(self.ids)}
        return sorted(rows, key=lambda row: rank.get(row.id, len(rank))) if rank else rows

    def save(self, desired: list[str], renames: dict[str, str] | None = None) -> None:
        if self.damaged:
            raise OSError("unreadable menu order")
        # Keep positions for temporarily absent offers without displaying them.
        shown = set(desired)
        reordered = iter(desired)
        # The renamed row takes its own slot, not that of a previously withdrawn
        # row that happened to use the destination ID. Leave unrelated slots alone.
        moves = renames or {}
        destinations = {new for old, new in moves.items() if old in self.ids}
        prior = [moves.get(mid, mid) for mid in self.ids if mid not in destinations]
        result = [next(reordered) if mid in shown else mid for mid in prior]
        result.extend(reordered)
        if tuple(result) != self.ids:
            write_state_document(self.path, result)
            self.ids = tuple(result)
