"""Fit every image a request sends within the per-image size limit providers enforce.

A Session's images are sent again with every request, and Anthropic refuses any image with a side over 2000 px once
a request holds more than 20 images. An image accepted when it entered the context therefore made every later request
of its Session fail as soon as the 21st image arrived, and the Session could not recover.

Each image with a side over ``MAX_IMAGE_SIDE`` is sent downscaled to fit, in every request alike, so a history image's
bytes do not change from one request to the next and the provider's prompt cache keeps them. Its snapshot keeps the
original. A note after the image gives the model the original size and the coordinate scale, worded as Pi's ``read``
words it (``packages/coding-agent/src/utils/image-resize.ts``, MIT, Copyright (c) 2025 Mario Zechner). An image too
large to decode safely is left out, with a note in its place. Bytes Pillow cannot open are sent as they are.

A downscale is kept in memory, keyed by the original bytes, so a Session does not pay for it on every request.
"""

from __future__ import annotations

import base64
import hashlib
import io
import threading
from collections import OrderedDict
from typing import NamedTuple, Optional

from PIL import Image, ImageOps, UnidentifiedImageError

#: The longest side an image is sent with: Anthropic's per-image limit for a request with more than 20 images, and
#: the resize default of Pi's ``read``. Providers scale larger images down before the model reads them in any case.
MAX_IMAGE_SIDE = 2000
#: The most pixels a downscale decodes (about 200 MB as RGBA). A larger image is left out rather than risk the
#: process's memory; providers refuse a side over 8000 px anyway.
MAX_DECODE_PIXELS = 50_000_000
#: The memory the kept downscales may hold, as base64 text.
DOWNSCALE_CACHE_BYTES = 64 * 1024 * 1024

_LOSSY_QUALITY = 90


class LoadedImage(NamedTuple):
    """An image as a request sends it: base64 ``data`` (``None`` when it is left out), its type, and a ``note`` the
    request sends right after it."""

    data: Optional[str]
    mime_type: str
    note: Optional[str] = None


def fit_image(raw: bytes, mime_type: str) -> LoadedImage:
    """``raw`` as a request sends it. Blocking: call it off the event loop."""

    try:
        image = Image.open(io.BytesIO(raw))
    except Image.DecompressionBombError:
        return LoadedImage(None, mime_type, _omitted_note(None))
    except (UnidentifiedImageError, OSError, ValueError):
        return LoadedImage(_base64(raw), mime_type)
    with image:
        width, height = image.size
        if width <= MAX_IMAGE_SIDE and height <= MAX_IMAGE_SIDE:
            return LoadedImage(_base64(raw), mime_type)
        key = hashlib.sha256(raw).digest()
        fitted = _DOWNSCALES.get(key)
        if fitted is not None:
            return fitted
        fitted = LoadedImage(None, mime_type, _omitted_note((width, height)))
        if width * height <= MAX_DECODE_PIXELS:
            try:
                fitted = _downscale(image)
            except (OSError, ValueError, Image.DecompressionBombError):
                pass  # an image that does not decode cannot be sent at its size either: it stays left out
    _DOWNSCALES.put(key, fitted)
    return fitted


def _downscale(image: Image.Image) -> LoadedImage:
    """``image`` upright and within ``MAX_IMAGE_SIDE``: a JPEG (a camera's MPO is one too) or WebP stays one, anything
    else becomes a PNG."""

    source_format = image.format
    upright = ImageOps.exif_transpose(image)
    original_width, original_height = upright.size
    if upright.mode not in ("L", "LA", "RGB", "RGBA"):
        upright = upright.convert("RGBA" if upright.has_transparency_data else "RGB")
    upright.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    if source_format in ("JPEG", "MPO"):
        upright.save(out, "JPEG", quality=_LOSSY_QUALITY)
        mime_type = "image/jpeg"
    elif source_format == "WEBP":
        upright.save(out, "WEBP", quality=_LOSSY_QUALITY)
        mime_type = "image/webp"
    else:
        upright.save(out, "PNG")
        mime_type = "image/png"
    width, height = upright.size
    note = (
        f"[Image: original {original_width}x{original_height}, displayed at {width}x{height}. "
        f"Multiply coordinates by {original_width / width:.2f} to map to original image.]"
    )
    return LoadedImage(_base64(out.getvalue()), mime_type, note)


def _omitted_note(size: Optional[tuple[int, int]]) -> str:
    shown = f"{size[0]}x{size[1]} " if size else ""
    return f"[Image omitted: {shown}is too large to downscale to {MAX_IMAGE_SIDE}x{MAX_IMAGE_SIDE} for this request.]"


def _base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class _Downscales:
    """The kept downscales, least recently used first, within ``budget`` bytes of base64 text."""

    def __init__(self, budget: int) -> None:
        self._budget = budget
        self._items: OrderedDict[bytes, LoadedImage] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: bytes) -> Optional[LoadedImage]:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: bytes, value: LoadedImage) -> None:
        size = len(value.data or "")
        if size > self._budget:
            return
        with self._lock:
            if key in self._items:
                return
            self._items[key] = value
            self._size += size
            while self._size > self._budget:
                _, evicted = self._items.popitem(last=False)
                self._size -= len(evicted.data or "")


_DOWNSCALES = _Downscales(DOWNSCALE_CACHE_BYTES)
