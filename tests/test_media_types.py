"""Every extension the Web UI plays inline must be guessed as media, and served inline, by the server.

The UI classifier (``ui/src/lib/filePreview.ts``) and the server's type inference drifted twice
(files the card offered to play were served as ``application/octet-stream`` attachments), so the
extension sets are read from the UI source itself rather than mirrored here.
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

import pytest

from core.file_browser_service import INLINE_SAFE_CONTENT_TYPES
from core.media_types import is_inline_safe_type
from vibe.ui_server import _INLINE_SAFE_MEDIA_TYPES

_FILE_PREVIEW = Path(__file__).resolve().parents[1] / "ui" / "src" / "lib" / "filePreview.ts"


def _ui_playable_exts() -> list[str]:
    src = _FILE_PREVIEW.read_text(encoding="utf-8")
    exts: list[str] = []
    for name in ("AUDIO_EXT", "VIDEO_EXT", "CONTAINER_EXT"):
        match = re.search(rf"const {name}\b[^=]*=\s*(?:new Set\(\[(.*?)\]\)|\{{(.*?)\}})", src, re.S)
        assert match, f"{name} not found in filePreview.ts"
        body = match.group(1) or match.group(2)
        exts += re.findall(r"'([a-z0-9]+)'", body) if match.group(1) else re.findall(r"(\w+)\s*:", body)
    return exts


UI_PLAYABLE_EXTS = _ui_playable_exts()


def test_ui_playable_extension_set_is_parsed():
    assert {"wav", "mp3", "ogg", "webm", "mp4", "weba", "ogv"} <= set(UI_PLAYABLE_EXTS)


@pytest.mark.parametrize("ext", UI_PLAYABLE_EXTS)
def test_ui_playable_extension_is_guessed_as_media_and_served_inline(ext):
    mime = mimetypes.guess_type(f"clip.{ext}")[0]

    assert mime and mime.split("/", 1)[0] in {"audio", "video"}, (ext, mime)
    assert is_inline_safe_type(mime, INLINE_SAFE_CONTENT_TYPES)
    assert is_inline_safe_type(mime, _INLINE_SAFE_MEDIA_TYPES)


@pytest.mark.parametrize("mime", ["image/svg+xml", "text/html", "application/xhtml+xml", "application/octet-stream", None])
def test_active_or_unknown_types_are_not_inline_by_major_type(mime):
    assert not is_inline_safe_type(mime, INLINE_SAFE_CONTENT_TYPES)
    assert not is_inline_safe_type(mime, _INLINE_SAFE_MEDIA_TYPES)
