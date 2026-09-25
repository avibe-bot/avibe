"""Server-side audio/video content types shared by every file-serving route.

``vibe/data/media_types.json`` is the one catalog of extensions the Web UI plays natively; the UI
classifier (``ui/src/lib/filePreview.ts``) imports the same file, so the two cannot drift. Python's
built-in ``mimetypes`` table omits several of them (``.flac``, ``.m4a``, ``.ogg``, ``.weba``, ``.m4v``,
``.ogv`` …) unless the host ships a ``mime.types`` file, so a clean Linux install would serve them as
``application/octet-stream`` attachments. Registering the catalog at import keeps the guessed type
identical across hosts.
"""

from __future__ import annotations

import json
import mimetypes
from importlib import resources

MEDIA_TYPES_BY_EXT: dict[str, str] = json.loads(
    resources.files("vibe").joinpath("data", "media_types.json").read_text(encoding="utf-8")
)

for _ext, _mime in MEDIA_TYPES_BY_EXT.items():
    mimetypes.add_type(_mime, f".{_ext}")

INLINE_SAFE_MEDIA_MAJOR_TYPES = {"audio", "video"}


def is_inline_safe_type(mime: str | None, allowlist: set[str]) -> bool:
    """Whether a response of ``mime`` may be served ``inline`` (always together with ``nosniff``).

    Audio and video are inline by major type: sent with ``nosniff`` the browser can only treat
    them as media, never as active content, so every format the Web UI player accepts streams
    inline without a hand-kept alias list. Everything else (notably ``image/svg+xml`` and
    ``text/html``) must be listed explicitly in ``allowlist``."""
    base = (mime or "").split(";", 1)[0].strip().lower()
    return base.split("/", 1)[0] in INLINE_SAFE_MEDIA_MAJOR_TYPES or base in allowlist
