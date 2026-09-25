"""Server-side audio/video content types shared by every file-serving route.

Python's built-in ``mimetypes`` table omits several formats the Web UI player accepts
(``.flac``, ``.m4a``, ``.ogg``, ``.weba``, ``.m4v``, ``.ogv`` …) unless the host happens to
ship a ``mime.types`` file, so a clean Linux install would serve them as
``application/octet-stream`` attachments. Registering them here at import keeps the guessed
type identical across hosts. Keep this map a superset of the UI's playable extensions
(``ui/src/lib/filePreview.ts`` ``mediaKind``); ``tests/test_media_types.py`` enforces it.
"""

from __future__ import annotations

import mimetypes

MEDIA_TYPES_BY_EXT = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".weba": "audio/webm",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
}

for _ext, _mime in MEDIA_TYPES_BY_EXT.items():
    mimetypes.add_type(_mime, _ext)

INLINE_SAFE_MEDIA_MAJOR_TYPES = {"audio", "video"}


def is_inline_safe_type(mime: str | None, allowlist: set[str]) -> bool:
    """Whether a response of ``mime`` may be served ``inline`` (always together with ``nosniff``).

    Audio and video are inline by major type: sent with ``nosniff`` the browser can only treat
    them as media, never as active content, so every format the Web UI player accepts streams
    inline without a hand-kept alias list. Everything else (notably ``image/svg+xml`` and
    ``text/html``) must be listed explicitly in ``allowlist``."""
    base = (mime or "").split(";", 1)[0].strip().lower()
    return base.split("/", 1)[0] in INLINE_SAFE_MEDIA_MAJOR_TYPES or base in allowlist
