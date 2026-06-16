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

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy.engine import Connection

from core.reply_enhancer import _FILE_LINK_RE, _file_uri_to_local_path
from storage import media_service

logger = logging.getLogger(__name__)


def register_agent_reply_media(
    conn: Connection,
    *,
    scope_id: str,
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


def rewrite_agent_media(conn: Connection, *, scope_id: str, session_id: str, text: str) -> str:
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
