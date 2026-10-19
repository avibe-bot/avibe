"""Fit every image a request sends within the per-image size limit providers enforce.

A Session's images are sent again with every request, and Anthropic refuses any image with a side over 2000 px once
a request holds more than 20 images. An image accepted when it entered the context therefore made every later request
of its Session fail as soon as the 21st image arrived, and the Session could not recover.

Each image with a side over ``MAX_IMAGE_SIDE`` is sent downscaled to fit, in every request alike, so a history image's
bytes do not change from one request to the next and the provider's prompt cache keeps them. Its snapshot keeps the
original. A note after the image gives the model the original size and the coordinate scale, worded as Pi's ``read``
words it (``packages/coding-agent/src/utils/image-resize.ts``, MIT, Copyright (c) 2025 Mario Zechner). An image too
large to decode safely is left out, with a note in its place. Bytes Pillow cannot open are sent as they are. An
animation is fitted as its first frame, which is what the providers read of it, and the note says so.

A downscale is kept in memory, keyed by the original bytes, so a Session does not pay for it on every request.
Downscales run one at a time, so their memory peak does not multiply across concurrent Sessions.
"""

from __future__ import annotations

import base64
import hashlib
import io
import threading
from collections import OrderedDict
from typing import NamedTuple, Optional

from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError

#: The longest side an image is sent with: Anthropic's per-image limit for a request with more than 20 images, and
#: the resize default of Pi's ``read``. Providers scale larger images down before the model reads them in any case.
MAX_IMAGE_SIDE = 2000
#: The most pixels a downscale decodes: a peak of about 230 MB for an image with alpha (measured). A JPEG decodes at a
#: reduced scale first, so this counts its reduced pixels. A larger image is left out rather than risk the process's
#: memory; providers refuse a side over 8000 px anyway.
MAX_DECODE_PIXELS = 24_000_000
#: The memory the kept results may hold: their base64 text and notes, plus ``_ENTRY_OVERHEAD`` each.
DOWNSCALE_CACHE_BYTES = 64 * 1024 * 1024
_ENTRY_OVERHEAD = 256

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
        with _DOWNSCALE_LOCK:
            fitted = _DOWNSCALES.get(key)
            if fitted is not None:
                return fitted
            try:
                fitted = _downscale(image)
            except (OSError, ValueError, Image.DecompressionBombError):
                fitted = None  # an image that does not decode cannot be sent at its size either: it is left out
            if fitted is None:
                fitted = LoadedImage(None, mime_type, _omitted_note((width, height)))
            _DOWNSCALES.put(key, fitted)
    return fitted


def _downscale(image: Image.Image) -> Optional[LoadedImage]:
    """``image`` upright and within ``MAX_IMAGE_SIDE``, or ``None`` when it is over ``MAX_DECODE_PIXELS``. A JPEG (a
    camera's MPO is one too) or WebP stays one, anything else becomes a PNG.

    The image is reduced before anything else holds a full-size copy: a JPEG decodes at a reduced scale (``draft``),
    and orientation is applied to the reduced image in place."""

    source_format, frames = image.format, getattr(image, "n_frames", 1)
    width, height = image.size
    image.draft(None, (MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    if image.size[0] * image.size[1] > MAX_DECODE_PIXELS:
        return None
    if image.getexif().get(ExifTags.Base.Orientation) in (5, 6, 7, 8):
        width, height = height, width
    if image.mode not in ("L", "LA", "RGB", "RGBA"):
        image = image.convert("RGBA" if image.has_transparency_data else "RGB")
    image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)
    ImageOps.exif_transpose(image, in_place=True)
    out = io.BytesIO()
    if source_format in ("JPEG", "MPO"):
        image.save(out, "JPEG", quality=_LOSSY_QUALITY)
        mime_type = "image/jpeg"
    elif source_format == "WEBP":
        image.save(out, "WEBP", quality=_LOSSY_QUALITY)
        mime_type = "image/webp"
    else:
        image.save(out, "PNG")
        mime_type = "image/png"
    shown_width, shown_height = image.size
    note = (
        f"[Image: original {width}x{height}, displayed at {shown_width}x{shown_height}. "
        f"Multiply coordinates by {width / shown_width:.2f} to map to original image.]"
    )
    if frames > 1:
        note = f"{note[:-1]} Only the first of its {frames} frames is shown.]"
    return LoadedImage(_base64(out.getvalue()), mime_type, note)


def _omitted_note(size: Optional[tuple[int, int]]) -> str:
    shown = f"{size[0]}x{size[1]} " if size else ""
    return f"[Image omitted: {shown}is too large to downscale to {MAX_IMAGE_SIDE}x{MAX_IMAGE_SIDE} for this request.]"


def _base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class _Downscales:
    """The kept results, least recently used first, within ``budget`` bytes (``_entry_size``)."""

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
        size = _entry_size(value)
        if size > self._budget:
            return
        with self._lock:
            if key in self._items:
                return
            self._items[key] = value
            self._size += size
            while self._size > self._budget:
                _, evicted = self._items.popitem(last=False)
                self._size -= _entry_size(evicted)


def _entry_size(value: LoadedImage) -> int:
    return len(value.data or "") + len(value.note or "") + _ENTRY_OVERHEAD


_DOWNSCALES = _Downscales(DOWNSCALE_CACHE_BYTES)
_DOWNSCALE_LOCK = threading.Lock()
