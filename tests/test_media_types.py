"""Every extension the Web UI plays natively must be guessed as media, and served inline, by the server.

The UI classifier and the server's type inference drifted twice (files the card offered to play were
served as ``application/octet-stream`` attachments); both now read ``vibe/data/media_types.json``, and
this holds the server side of that catalog to the inline contract on any host.
"""

from __future__ import annotations

import mimetypes

import pytest

from core.file_browser_service import INLINE_SAFE_CONTENT_TYPES
from core.media_types import MEDIA_TYPES_BY_EXT, is_inline_safe_type
from vibe.ui_server import _INLINE_SAFE_MEDIA_TYPES


@pytest.mark.parametrize("ext", sorted(MEDIA_TYPES_BY_EXT))
def test_ui_playable_extension_is_guessed_as_media_and_served_inline(ext):
    mime = mimetypes.guess_type(f"clip.{ext}")[0]

    assert mime and mime.split("/", 1)[0] in {"audio", "video"}, (ext, mime)
    assert is_inline_safe_type(mime, INLINE_SAFE_CONTENT_TYPES)
    assert is_inline_safe_type(mime, _INLINE_SAFE_MEDIA_TYPES)


@pytest.mark.parametrize("mime", ["image/svg+xml", "text/html", "application/xhtml+xml", "application/octet-stream", None])
def test_active_or_unknown_types_are_not_inline_by_major_type(mime):
    assert not is_inline_safe_type(mime, INLINE_SAFE_CONTENT_TYPES)
    assert not is_inline_safe_type(mime, _INLINE_SAFE_MEDIA_TYPES)
