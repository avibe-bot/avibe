"""Describe the code identity served by this Avibe process."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


BUILD_METADATA_PATH_ENV = "VIBE_BUILD_METADATA_PATH"
_GIT_REVISION_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


@dataclass(frozen=True)
class BuildIdentity:
    kind: str
    revision: str | None = None
    dirty: bool | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"kind": self.kind}
        if self.revision is not None:
            payload["revision"] = self.revision
        if self.dirty is not None:
            payload["dirty"] = self.dirty
        return payload


def get_build_identity() -> BuildIdentity:
    """Return package identity or source identity from deployment metadata.

    A configured metadata path is itself the source-deployment marker. Invalid
    or temporarily unavailable metadata must not make a source checkout fall
    back to package update behavior.
    """

    metadata_path = os.environ.get(BUILD_METADATA_PATH_ENV, "").strip()
    if not metadata_path:
        return BuildIdentity(kind="package")

    try:
        payload = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return BuildIdentity(kind="source")
    if not isinstance(payload, dict):
        return BuildIdentity(kind="source")

    raw_revision = payload.get("commit")
    revision = str(raw_revision).strip() if raw_revision is not None else ""
    if not _GIT_REVISION_RE.fullmatch(revision):
        revision = ""
    raw_dirty = payload.get("dirty")
    dirty = raw_dirty if isinstance(raw_dirty, bool) else None
    return BuildIdentity(kind="source", revision=revision or None, dirty=dirty)
