"""CRUD over the ``media_objects`` proxy-token table.

A single write path (:func:`register`) mints (or reuses) an opaque token for a
local file so the workbench can serve it over ``/api/media/<token>`` without
ever putting a filesystem path in the URL. Both agent-reply media (rewritten in
``core/workbench_media``) and user uploads register here, so the proxy endpoint
and the UI file card have one shape to read.
"""

from __future__ import annotations

import logging
import mimetypes
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from storage.models import media_object_references, media_objects

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_token() -> str:
    # URL-safe, unguessable; the token IS the capability to fetch the file.
    return secrets.token_urlsafe(16)


def _record_session_reference(
    conn: Connection,
    *,
    token: str,
    session_id: Optional[str],
    created_at: Optional[str] = None,
) -> None:
    if not session_id:
        return
    conn.execute(
        sqlite_insert(media_object_references)
        .values(
            token=token,
            session_id=session_id,
            created_at=created_at or _utc_now_iso(),
        )
        .on_conflict_do_nothing(
            index_elements=[
                media_object_references.c.token,
                media_object_references.c.session_id,
            ]
        )
    )


def _probe_image_dimensions(
    kind: str, content_type: Optional[str], local_path: str
) -> tuple[Optional[int], Optional[int]]:
    """Read an image's pixel ``(width, height)`` from its file header, or
    ``(None, None)``.

    Header-only (``imagesize``, no full decode, no pixel buffer) so it's cheap and
    safe to run inline on the upload / register path. Only attempted for images;
    any failure (unsupported format, unreadable file, library missing) degrades to
    ``(None, None)`` — the UI then falls back to measuring the image once in the
    browser, so dimensions are an optimization, never a hard dependency.
    """
    is_image = kind == "image" or (content_type or "").lower().startswith("image/")
    if not is_image:
        return None, None
    try:
        import imagesize

        width, height = imagesize.get(local_path)
        if width and height and width > 0 and height > 0:
            return int(width), int(height)
    except Exception:
        logger.debug("media_service: could not read image dimensions for %s", local_path, exc_info=True)
    return None, None


def register(
    conn: Connection,
    *,
    scope_id: Optional[str],
    session_id: Optional[str],
    kind: str,
    source: str,
    local_path: str,
    file_name: Optional[str] = None,
    content_type: Optional[str] = None,
    message_id: Optional[str] = None,
) -> str:
    """Register *local_path* under a token and return it, reusing an existing
    token for the same file so its proxy URL is stable + cacheable.

    Dedup is scoped to the referencing session. A shared path referenced from
    different sessions receives separate tokens so each token retains the
    correct authorization target. ``mtime_ns`` + ``size_bytes`` is a stat-only
    change detector: a rewritten file (new size/mtime) mints a fresh token,
    busting the cache. ``content_type`` / ``file_ext`` / ``size_bytes`` are
    derived from the path when not supplied so the proxy response and UI card
    don't re-compute them.
    """
    path = Path(local_path)
    name = file_name or path.name
    ext = (path.suffix.lower().lstrip(".") or None)
    ctype = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    size: Optional[int] = None
    mtime_ns: Optional[int] = None
    try:
        if path.is_file():
            stat = path.stat()
            size = stat.st_size
            mtime_ns = stat.st_mtime_ns
    except OSError:
        size = None
        mtime_ns = None

    # Reuse an existing live token for the same fingerprint (stable, cacheable
    # URL). Only when both size + mtime are known — an unstattable file can't be
    # fingerprinted, so fall through to a fresh row.
    if size is not None and mtime_ns is not None:
        existing = conn.execute(
            select(media_objects.c.token).where(
                media_objects.c.local_path == str(local_path),
                media_objects.c.size_bytes == size,
                media_objects.c.mtime_ns == mtime_ns,
                media_objects.c.scope_id == scope_id,
                media_objects.c.session_id == session_id,
                media_objects.c.revoked_at.is_(None),
            )
        ).scalar()
        if existing:
            _record_session_reference(
                conn,
                token=str(existing),
                session_id=session_id,
            )
            return existing

    # Read image dimensions only for a freshly-minted row (a dedup hit above
    # already carries them) so the UI can reserve the image's box before it loads.
    width_px, height_px = _probe_image_dimensions(kind, ctype, str(local_path))

    token = _new_token()
    created_at = _utc_now_iso()
    conn.execute(
        media_objects.insert().values(
            token=token,
            scope_id=scope_id,
            session_id=session_id,
            message_id=message_id,
            kind=kind,
            source=source,
            local_path=str(local_path),
            file_name=name,
            content_type=ctype,
            file_ext=ext,
            size_bytes=size,
            mtime_ns=mtime_ns,
            width_px=width_px,
            height_px=height_px,
            created_at=created_at,
            expires_at=None,
            revoked_at=None,
        )
    )
    _record_session_reference(
        conn,
        token=token,
        session_id=session_id,
        created_at=created_at,
    )
    return token


def get_by_token(conn: Connection, token: str) -> Optional[dict[str, Any]]:
    """Return the media row for *token* as a plain dict, or ``None``."""
    if not token:
        return None
    row = conn.execute(select(media_objects).where(media_objects.c.token == token)).mappings().first()
    return dict(row) if row else None


def referenced_session_ids(conn: Connection, token: str) -> list[str]:
    """Return sessions that contain a trusted reference to *token*."""

    if not token:
        return []
    return [
        str(session_id)
        for session_id in conn.execute(
            select(media_object_references.c.session_id)
            .where(media_object_references.c.token == token)
            .order_by(media_object_references.c.session_id)
        ).scalars()
    ]


def is_referenced_by_session(conn: Connection, token: str, session_id: str) -> bool:
    if not token or not session_id:
        return False
    return (
        conn.execute(
            select(media_object_references.c.token)
            .where(
                media_object_references.c.token == token,
                media_object_references.c.session_id == session_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )
