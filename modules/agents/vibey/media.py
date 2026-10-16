"""Images in Vibey's context: immutable snapshots over ``media_objects``.

An ``ImageBlock`` names a ``media_objects`` token whose bytes never change for a
committed block (C-1, C-2 ``MediaLoader``). Every image the agent puts into its
context - an input attachment or a file ``read`` attaches - is first copied to a
content-addressed file ``<state>/agent_core/media/<sha256>.<ext>`` and registered
there, so a later edit or deletion of the original file cannot change what a
transcript replays. Loading re-checks the digest.

A generated image is saved to the same root before the row that names it
commits, and that row registers it (``storage.agent_transcript``). A file no row
registers is left by an interruption in between; ``sweep`` removes it once it is
older than any Turn holds a save it has not committed (``vibey-hosted-tools.md``
§7.3).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.engine import Engine

from core.agent_core.messages import IMAGE_MIME_TYPES, HostedImage, ImageBlock
from core.agent_core.tools.read import detect_supported_image_mime_type
from storage import media_service
from storage.models import agent_sessions

_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
#: How old a file no row registers must be before ``sweep`` removes it: longer than any Turn holds a save it has not
#: committed. A file in use is kept younger than half of it (``_store``).
SWEEP_GRACE_S = 24 * 3600.0
_STORED_NAME = re.compile(r"[0-9a-f]{64}\.(?:png|jpg|gif|webp)")
_TEMP_NAME = re.compile(r"\.[0-9a-f]{64}\.[0-9a-f]{8}\.tmp")
#: Orders a save's reuse of a file against the sweep's last look at it and its removal, in this process, the only one
#: that writes the root.
_ROOT_LOCK = threading.Lock()


class MediaUnavailable(RuntimeError):
    """A context image's snapshot is missing, revoked, or no longer matches its digest."""


class MediaSnapshots:
    """``MediaLoader`` and ``MediaSink`` for the provider adapters, plus the snapshot writer."""

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

    # --- MediaSink -------------------------------------------------------------

    async def save_image(self, encoded: str) -> HostedImage:
        """A generated image (``vibey-hosted-tools.md`` §7.3), kept as an immutable file the agent's ``read`` opens.
        The surfaces register it for display when the row that names it commits."""
        return await asyncio.to_thread(self._save_image, encoded)

    def _save_image(self, encoded: str) -> HostedImage:
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("the image is not valid base64") from None
        mime_type = detect_supported_image_mime_type(data)
        if mime_type not in _EXTENSIONS:
            raise ValueError("the image is not a PNG, JPEG, GIF or WebP")
        return HostedImage(path=str(self._store(data, mime_type)), mime=mime_type, bytes=len(data))

    def _store(self, data: bytes, mime_type: str) -> Path:
        """The content-addressed file holding ``data``. An entry already at that name is reused only while it is a
        regular file that still holds those bytes: history reads these paths directly, and their owner can change
        them. Anything else is replaced by one atomic rename, which never follows the entry it replaces, so nothing is
        repaired in place and a link's target is never touched. A reused file older than half the sweep's grace is
        made new again, under the lock the sweep removes a file under, so ``sweep`` never takes a file a save is about
        to register."""
        with _ROOT_LOCK:
            return self._store_locked(data, mime_type)

    def _store_locked(self, data: bytes, mime_type: str) -> Path:
        digest = hashlib.sha256(data).hexdigest()
        path = self._root / f"{digest}.{_EXTENSIONS[mime_type]}"
        try:
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode) and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                if info.st_mtime < time.time() - SWEEP_GRACE_S / 2:
                    os.utime(path, follow_symlinks=False)
                return path
        except OSError:
            pass  # missing or unreadable: written below
        self._root.mkdir(parents=True, exist_ok=True)
        tmp = self._root / f".{digest}.{secrets.token_hex(4)}.tmp"
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return path

    # --- sweep -----------------------------------------------------------------

    def sweep(self, grace_s: float = SWEEP_GRACE_S) -> tuple[int, int]:
        """Remove each file of the root that no ``media_objects`` row registers and that is older than ``grace_s``,
        and each leftover temporary file that old; return how many files and bytes went (#2588). Run off the loop.

        Only the root's own names are considered, and only regular files: a link, a directory, or a file someone else
        put there is never touched. Each file is checked with one indexed lookup, under both its path and its resolved
        path, the two forms a row registers.
        """

        cutoff = time.time() - grace_s
        removed = freed = 0
        try:
            entries = list(os.scandir(self._root))
        except FileNotFoundError:
            return 0, 0
        with self._engine.connect() as conn:
            for entry in entries:
                temporary = _TEMP_NAME.fullmatch(entry.name) is not None
                if not temporary and not _STORED_NAME.fullmatch(entry.name):
                    continue
                path = Path(entry.path)
                if self._stale(path, cutoff) is None:
                    continue
                if not temporary and media_service.is_registered_path(conn, {str(path), str(path.resolve())}):
                    continue
                # Looked at again as it goes, under the lock a save reuses a file under: one that reused it since has
                # made it new (``_store``).
                with _ROOT_LOCK:
                    size = self._stale(path, cutoff)
                    if size is None:
                        continue
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        continue
                removed, freed = removed + 1, freed + size
        return removed, freed

    @staticmethod
    def _stale(path: Path, cutoff: float) -> Optional[int]:
        """The size of ``path`` when it is a regular file last written before ``cutoff``, else None."""

        try:
            info = os.lstat(path)
        except OSError:
            return None
        return info.st_size if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff else None

    def _snapshot(self, data: bytes, mime_type: str, session_id: str, name: Optional[str]) -> str:
        if mime_type not in IMAGE_MIME_TYPES:
            raise ValueError(f"unsupported image type: {mime_type}")
        path = self._store(data, mime_type)
        with self._engine.begin() as conn:
            scope_id = conn.execute(
                select(agent_sessions.c.scope_id).where(agent_sessions.c.id == session_id)
            ).scalar_one_or_none()
            return media_service.register(
                conn,
                scope_id=scope_id,
                session_id=session_id,
                kind="image",
                source=media_service.AGENT_CONTEXT_SOURCE,
                local_path=str(path),
                file_name=name or path.name,
                content_type=mime_type,
            )
