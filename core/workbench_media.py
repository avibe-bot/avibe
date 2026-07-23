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
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy.engine import Connection

from config import paths
from core.reply_enhancer import _FILE_LINK_RE, _file_uri_to_local_path
from storage import media_service

logger = logging.getLogger(__name__)

MAX_SHOW_SCREENSHOT_LONG_EDGE = 2048
MAX_SHOW_SCREENSHOT_BYTES = 25 * 1024 * 1024
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
    if not text or "file://" not in text:
        return text

    def _replace(match) -> str:
        bang, label, url = match.group(1), match.group(2), match.group(3)
        parsed = urlparse(url)
        if parsed.scheme != "file":
            return match.group(0)
        path = _file_uri_to_local_path(parsed)
        if not os.path.isabs(path):
            logger.warning("workbench_media: skipping non-absolute file link: %s", url)
            return match.group(0)
        try:
            safe_path = str(Path(path).resolve())
        except Exception:
            logger.warning("workbench_media: could not resolve file link: %s", url)
            return match.group(0)
        try:
            token = register_agent_reply_media(
                conn,
                scope_id=scope_id,
                session_id=session_id,
                kind="image" if bang == "!" else "file",
                local_path=safe_path,
                file_name=label or os.path.basename(safe_path),
            )
        except Exception:
            logger.exception("workbench_media: failed to register media for %s", safe_path)
            return match.group(0)
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
        return f"{bang}[{label}]({url})"

    return _FILE_LINK_RE.sub(_replace, text)


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
