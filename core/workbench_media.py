"""Rewrite agent-reply ``file://`` links into same-origin media-proxy URLs.

The IM path (``core/message_dispatcher`` + ``core/reply_enhancer``) strips
``file://`` markdown links out of the reply text and uploads the referenced
files to the IM platform. The avibe workbench Chat needs the opposite: keep the
link **in place** in the Markdown but point it at a same-origin proxy URL, so
the browser can render an agent-produced image inline (and a file as a download
card) without ever touching ``file://`` or an attacker-chosen remote host.

We reuse the reply-enhancer's file-link parser (one home for "what a file link
looks like") and, for each link, register the local file under an opaque token
(:func:`storage.media_service.register`) then swap the URL for
``/api/media/<token>``. The ``!``/``[]`` Markdown shape is preserved, so the
frontend renders images vs files purely from element type.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import os
import re
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from sqlalchemy.engine import Connection

from config import paths
from core.reply_enhancer import _file_uri_to_local_path, _parse_file_uri, _replace_file_links
from storage import media_service

logger = logging.getLogger(__name__)

MAX_SHOW_SCREENSHOT_LONG_EDGE = 2048
MAX_SHOW_SCREENSHOT_BYTES = 25 * 1024 * 1024
MAX_WORKBENCH_ATTACHMENT_BYTES = 100 * 1024 * 1024
_WORKBENCH_UPLOAD_CHUNK_BYTES = 1024 * 1024
_WORKBENCH_UPLOAD_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,80}")
_WORKBENCH_BASE64_WHITESPACE_DELETE = str.maketrans("", "", " \t\r\n\v\f")
_WORKBENCH_UPLOAD_LOCKS_GUARD = threading.Lock()
_WORKBENCH_UPLOAD_LOCKS: dict[str, "_WorkbenchUploadLock"] = {}
_SHOW_SCREENSHOT_DATA_URL_RE = re.compile(
    r"\Adata:(image/(?P<format>png|webp));base64,(?P<data>[A-Za-z0-9+/=]+)\Z",
    re.IGNORECASE,
)


class InvalidShowScreenshot(ValueError):
    """Raised when an annotation screenshot cannot be safely materialized."""


@dataclass(frozen=True)
class MaterializedShowScreenshot:
    attachment_id: str
    path: str
    content_type: str
    width: int
    height: int


class WorkbenchAttachmentUploadError(ValueError):
    """A user-correctable workbench attachment upload failure."""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class MaterializedWorkbenchAttachment:
    token: str
    name: str
    mime: str
    size: int
    kind: str
    path: str
    width: int | None
    height: int | None
    created: bool


@dataclass
class _WorkbenchUploadLock:
    lock: threading.Lock
    users: int = 0


def normalize_workbench_upload_id(value: object) -> str | None:
    """Validate the optional stable browser key used for upload retries."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or _WORKBENCH_UPLOAD_ID_RE.fullmatch(value) is None:
        raise WorkbenchAttachmentUploadError("invalid_upload", "Upload ID is invalid", 400)
    return value


def decode_legacy_workbench_attachment(data: str) -> io.BytesIO:
    """Decode the pre-multipart JSON contract without relaxing validation."""
    encoded_data = data
    if encoded_data.startswith("data:") and "," in encoded_data:
        encoded_data = encoded_data.split(",", 1)[1]
    encoded_data = encoded_data.translate(_WORKBENCH_BASE64_WHITESPACE_DELETE)
    max_encoded_bytes = ((MAX_WORKBENCH_ATTACHMENT_BYTES + 2) // 3) * 4
    if len(encoded_data) > max_encoded_bytes:
        raise WorkbenchAttachmentUploadError(
            "too_large",
            "Attachment exceeds the size limit",
            413,
        )
    try:
        return io.BytesIO(base64.b64decode(encoded_data, validate=True))
    except (binascii.Error, ValueError) as exc:
        raise WorkbenchAttachmentUploadError(
            "invalid_upload",
            "Attachment data is invalid",
            400,
        ) from exc


@contextmanager
def workbench_attachment_upload_lock(session_id: str, upload_id: str | None):
    """Serialize retries for one upload key without serializing other files."""
    if upload_id is None:
        yield
        return
    key = f"{session_id}\0{upload_id}"
    with _WORKBENCH_UPLOAD_LOCKS_GUARD:
        entry = _WORKBENCH_UPLOAD_LOCKS.get(key)
        if entry is None:
            entry = _WorkbenchUploadLock(lock=threading.Lock())
            _WORKBENCH_UPLOAD_LOCKS[key] = entry
        entry.users += 1
    entry.lock.acquire()
    try:
        yield
    finally:
        entry.lock.release()
        with _WORKBENCH_UPLOAD_LOCKS_GUARD:
            entry.users -= 1
            if entry.users == 0:
                _WORKBENCH_UPLOAD_LOCKS.pop(key, None)


def materialize_workbench_attachment(
    conn: Connection,
    *,
    scope_id: str | None,
    session_id: str,
    file_name: object,
    content_type: object,
    source: BinaryIO,
    upload_id: object = None,
) -> MaterializedWorkbenchAttachment:
    """Stream one browser upload to disk and register its media capability.

    The multipart parser already enforces the same cap while receiving the body;
    this second boundary also covers the legacy JSON compatibility path and any
    future caller that hands us a file-like object directly.
    """
    raw_name = file_name.strip() if isinstance(file_name, str) else ""
    name = raw_name.replace("\\", "/").rsplit("/", 1)[-1].strip() or "upload"
    raw_mime = content_type.strip() if isinstance(content_type, str) else ""
    mime = raw_mime[:255] or "application/octet-stream"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "upload"
    safe_name = safe_name[-160:]
    stable_upload_id = normalize_workbench_upload_id(upload_id)

    upload_dir = paths.get_attachments_dir() / "avibe" / session_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    path_id = stable_upload_id or uuid.uuid4().hex[:16]
    if stable_upload_id:
        for candidate in sorted(upload_dir.glob(f"{stable_upload_id}_*")):
            if not candidate.is_file():
                continue
            candidate_path = str(candidate.resolve())
            existing = media_service.get_live_user_upload_by_path(
                conn,
                session_id=session_id,
                local_path=candidate_path,
            )
            if existing:
                return MaterializedWorkbenchAttachment(
                    token=existing["token"],
                    name=existing.get("file_name") or name,
                    mime=existing.get("content_type") or mime,
                    size=existing.get("size_bytes") or candidate.stat().st_size,
                    kind=existing.get("kind") or "file",
                    path=candidate_path,
                    width=existing.get("width_px"),
                    height=existing.get("height_px"),
                    created=False,
                )
            candidate.unlink(missing_ok=True)
        for stale_temp in upload_dir.glob(f".{stable_upload_id}.*.tmp"):
            stale_temp.unlink(missing_ok=True)

    local_path = upload_dir / f"{path_id}_{safe_name}"
    canonical_path = str(local_path.resolve())
    temp_path = upload_dir / f".{path_id}.{uuid.uuid4().hex[:8]}.tmp"
    size = 0
    try:
        with temp_path.open("xb") as target:
            while True:
                chunk = source.read(_WORKBENCH_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise TypeError("attachment stream must return bytes")
                size += len(chunk)
                if size > MAX_WORKBENCH_ATTACHMENT_BYTES:
                    raise WorkbenchAttachmentUploadError(
                        "too_large",
                        "File exceeds the attachment size limit",
                        413,
                    )
                target.write(chunk)
        if size == 0:
            raise WorkbenchAttachmentUploadError("empty_file", "File is empty", 400)
        os.replace(temp_path, local_path)
        canonical_path = str(local_path.resolve(strict=True))
        kind = "image" if mime.startswith("image/") else "file"
        token = media_service.register(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            kind=kind,
            source="user_upload",
            local_path=canonical_path,
            file_name=name,
            content_type=mime,
        )
        row = media_service.get_by_token(conn, token)
    except Exception:
        temp_path.unlink(missing_ok=True)
        local_path.unlink(missing_ok=True)
        raise

    return MaterializedWorkbenchAttachment(
        token=token,
        name=name,
        mime=mime,
        size=size,
        kind=kind,
        path=canonical_path,
        width=row.get("width_px") if row else None,
        height=row.get("height_px") if row else None,
        created=True,
    )


def register_agent_reply_media(
    conn: Connection,
    *,
    scope_id: str | None,
    session_id: str | None,
    kind: str,
    local_path: str,
    file_name: str,
) -> str:
    """Register a local agent-reply file under the shared media proxy."""
    return media_service.register(
        conn,
        scope_id=scope_id,
        session_id=session_id,
        kind=kind,
        source="agent_reply",
        local_path=local_path,
        file_name=file_name,
    )


def materialize_show_screenshot(
    conn: Connection,
    *,
    scope_id: str,
    session_id: str,
    data_url: object,
) -> MaterializedShowScreenshot:
    """Persist an annotation screenshot and register it with the media proxy.

    Show Page clients still submit a data URL. This boundary validates the
    encoded image, writes it into the existing session attachment tree, and
    returns the opaque media token plus the canonical path for the local agent.
    """
    if not isinstance(data_url, str):
        raise InvalidShowScreenshot("screenshot.dataUrl must be a PNG or WebP data URL.")
    match = _SHOW_SCREENSHOT_DATA_URL_RE.fullmatch(data_url)
    if match is None:
        raise InvalidShowScreenshot("screenshot.dataUrl must be a PNG or WebP data URL.")

    encoded = match.group("data")
    if len(encoded) > ((MAX_SHOW_SCREENSHOT_BYTES + 2) // 3) * 4:
        raise InvalidShowScreenshot("screenshot.dataUrl is too large.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidShowScreenshot("screenshot.dataUrl contains invalid base64.") from exc
    if not raw or len(raw) > MAX_SHOW_SCREENSHOT_BYTES:
        raise InvalidShowScreenshot("screenshot.dataUrl is empty or too large.")

    image_format = match.group("format").lower()
    content_type = f"image/{image_format}"
    if image_format == "png":
        valid_signature = raw.startswith(b"\x89PNG\r\n\x1a\n")
    else:
        valid_signature = len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    if not valid_signature:
        raise InvalidShowScreenshot(f"screenshot.dataUrl is not a valid {image_format.upper()} image.")

    try:
        import imagesize

        width, height = imagesize.get(io.BytesIO(raw))
    except Exception as exc:
        raise InvalidShowScreenshot("screenshot.dataUrl image dimensions could not be read.") from exc
    if width <= 0 or height <= 0:
        raise InvalidShowScreenshot("screenshot.dataUrl image dimensions could not be read.")
    if max(width, height) > MAX_SHOW_SCREENSHOT_LONG_EDGE:
        raise InvalidShowScreenshot(
            f"screenshot.dataUrl long edge exceeds {MAX_SHOW_SCREENSHOT_LONG_EDGE}px."
        )

    upload_dir = paths.get_attachments_dir() / "avibe" / session_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    local_path = upload_dir / f"screenshot_{uuid.uuid4().hex[:16]}.{image_format}"
    try:
        local_path.write_bytes(raw)
        canonical_path = str(local_path.resolve(strict=True))
        token = media_service.register(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            kind="image",
            source="show_annotation",
            local_path=canonical_path,
            file_name=local_path.name,
            content_type=content_type,
        )
    except Exception:
        local_path.unlink(missing_ok=True)
        raise
    return MaterializedShowScreenshot(
        attachment_id=token,
        path=canonical_path,
        content_type=content_type,
        width=int(width),
        height=int(height),
    )


def rewrite_agent_media(conn: Connection, *, scope_id: str | None, session_id: str, text: str) -> str:
    """Return *text* with ``file://`` links rewritten to media-proxy URLs.

    Registers each referenced file in ``media_objects`` (same transaction as the
    caller's message insert) and swaps the ``file://`` URL for a same-origin
    ``/api/media/<token>``. Any absolute path the agent references is allowed:
    this is the user's own machine and the agent (Claude Code / Codex) already
    has full filesystem read access, so the proxy grants no capability it didn't
    already have — it just lets the user view what the agent points at. The path
    is resolved to its canonical (symlink-free) form before registering, and the
    serve endpoint re-resolves at fetch time and refuses if it changed, so a
    token can't be repointed at another file after minting. Non-``file://`` URLs
    and non-absolute paths are left untouched. Best-effort: a registration
    failure leaves that one link as written rather than dropping the reply.
    """
    if not text:
        return text

    def _replace(match) -> str:
        bang, url = match.group(1), match.group(3)
        source_url = text[match.start(3) : match.end(3)]
        parsed = _parse_file_uri(url)
        if parsed.scheme.casefold() != "file":
            return source_url
        path = _file_uri_to_local_path(parsed)
        if not os.path.isabs(path):
            logger.warning("workbench_media: skipping non-absolute file link: %s", url)
            return source_url
        try:
            safe_path = str(Path(path).resolve())
        except Exception:
            logger.warning("workbench_media: could not resolve file link: %s", url)
            return source_url
        try:
            token = register_agent_reply_media(
                conn,
                scope_id=scope_id,
                session_id=session_id,
                kind="image" if bang == "!" else "file",
                local_path=safe_path,
                file_name=os.path.basename(safe_path),
            )
        except Exception:
            logger.exception("workbench_media: failed to register media for %s", safe_path)
            return source_url
        url = f"/api/media/{token}"
        # For an image, carry its pixel dimensions on the URL (``?w=&h=``) so the
        # browser reserves the box before it loads — the transcript never shifts on
        # scroll. The proxy ignores the query and serves by token. Best-effort.
        if bang == "!":
            try:
                row = media_service.get_by_token(conn, token)
                w, h = (row or {}).get("width_px"), (row or {}).get("height_px")
                if w and h:
                    url = f"{url}?w={w}&h={h}"
            except Exception:
                logger.debug("workbench_media: no dimensions for %s", safe_path, exc_info=True)
        return url

    return _replace_file_links(text, _replace)


def resolve_attachment_specs(conn: Connection, *, session_id: str, attachments) -> list[dict]:
    """Resolve UI-sent attachment refs (media tokens) to agent-turn file specs.

    The browser only ever holds opaque tokens (never local paths); this maps each
    token back to its on-disk file via ``media_objects``, scoped to the session,
    and returns JSON-friendly ``{name, mimetype, path, size}`` dicts. Shared by
    the send path (→ dispatch payload) and the queue-flush path (→ rebuilt turn)
    so both carry the same uploaded files into the agent turn.
    """
    specs: list[dict] = []
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            continue
        token = attachment.get("token")
        if not token:
            continue
        row = media_service.get_by_token(conn, token)
        if not row or row.get("session_id") != session_id or row.get("revoked_at"):
            continue
        specs.append(
            {
                "name": row.get("file_name"),
                "mimetype": row.get("content_type"),
                "path": row.get("local_path"),
                "size": row.get("size_bytes"),
            }
        )
    return specs


def file_attachments_from_specs(specs) -> list | None:
    """Build ``FileAttachment`` objects from JSON file specs (already-local web
    uploads — ``{name, mimetype, path, size}``). Returns ``None`` when empty so
    ``MessageContext.files`` stays falsy for text-only turns. Shared by the
    dispatch payload (internal_server) and the queue-flush re-run (session_turns).
    """
    from modules.im.base import FileAttachment

    files = []
    for spec in specs or []:
        if not isinstance(spec, dict):
            continue
        path = spec.get("path")
        if not path:
            continue
        files.append(
            FileAttachment(
                name=spec.get("name") or "attachment",
                mimetype=spec.get("mimetype") or "application/octet-stream",
                local_path=path,
                size=spec.get("size"),
            )
        )
    return files or None
