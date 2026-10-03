"""Images in the Avibe Agent's context: immutable snapshots over ``media_objects``.

An ``ImageBlock`` names a ``media_objects`` token whose bytes never change for a
committed block (C-1, C-2 ``MediaLoader``). Every image the agent puts into its
context - an input attachment or a file ``read`` attaches - is first copied to a
content-addressed file ``<state>/agent_core/media/<sha256>.<ext>`` and registered
there, so a later edit or deletion of the original file cannot change what a
transcript replays. Loading re-checks the digest.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.engine import Engine

from core.agent_core.messages import IMAGE_MIME_TYPES, ImageBlock
from storage import media_service
from storage.models import agent_sessions

MEDIA_SOURCE = "agent_context"
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


class MediaUnavailable(RuntimeError):
    """A context image's snapshot is missing, revoked, or no longer matches its digest."""


class MediaSnapshots:
    """``MediaLoader`` for the provider adapters plus the snapshot writer."""

    def __init__(self, engine: Engine, root: Path) -> None:
        self._engine = engine
        self._root = Path(root)

    # --- MediaLoader ---------------------------------------------------------

    async def load(self, media_token: str) -> tuple[bytes, str]:
        return await asyncio.to_thread(self._load, media_token)

    def _load(self, media_token: str) -> tuple[bytes, str]:
        with self._engine.connect() as conn:
            row = media_service.get_by_token(conn, media_token)
        if row is None or row.get("revoked_at"):
            raise MediaUnavailable(f"media {media_token} is not available")
        path = Path(row["local_path"])
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise MediaUnavailable(f"media {media_token} cannot be read") from exc
        if path.parent == self._root and path.stem != hashlib.sha256(data).hexdigest():
            raise MediaUnavailable(f"media {media_token} no longer matches its snapshot")
        return data, str(row.get("content_type") or "")

    # --- snapshots -----------------------------------------------------------

    async def snapshot(self, data: bytes, mime_type: str, *, session_id: str, name: Optional[str] = None) -> str:
        """Store ``data`` immutably and return its token for ``session_id``."""
        return await asyncio.to_thread(self._snapshot, data, mime_type, session_id, name)

    async def snapshot_file(
        self, path: str, mime_type: str, *, session_id: str, name: Optional[str] = None
    ) -> ImageBlock:
        data = await asyncio.to_thread(Path(path).read_bytes)
        token = await self.snapshot(data, mime_type, session_id=session_id, name=name or Path(path).name)
        return ImageBlock(mime_type=mime_type, media_token=token, name=name or Path(path).name)

    def image_sink(self, session_id: str):
        """The ``read`` tool's sink: ``(bytes, mime_type, name) -> media token``."""

        async def sink(data: bytes, mime_type: str, name: str) -> str:
            return await self.snapshot(data, mime_type, session_id=session_id, name=name)

        return sink

    def _snapshot(self, data: bytes, mime_type: str, session_id: str, name: Optional[str]) -> str:
        if mime_type not in IMAGE_MIME_TYPES:
            raise ValueError(f"unsupported image type: {mime_type}")
        digest = hashlib.sha256(data).hexdigest()
        path = self._root / f"{digest}.{_EXTENSIONS[mime_type]}"
        if not path.is_file():
            self._root.mkdir(parents=True, exist_ok=True)
            tmp = self._root / f".{digest}.{secrets.token_hex(4)}.tmp"
            tmp.write_bytes(data)
            os.chmod(tmp, 0o444)
            os.replace(tmp, path)
        with self._engine.begin() as conn:
            scope_id = conn.execute(
                select(agent_sessions.c.scope_id).where(agent_sessions.c.id == session_id)
            ).scalar_one_or_none()
            return media_service.register(
                conn,
                scope_id=scope_id,
                session_id=session_id,
                kind="image",
                source=MEDIA_SOURCE,
                local_path=str(path),
                file_name=name or path.name,
                content_type=mime_type,
            )
