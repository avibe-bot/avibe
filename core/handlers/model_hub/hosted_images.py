"""``image_generate`` direct execution (``vibey-hosted-tools.md`` §7.2, §7.4).

The call names one model from its turn's frozen enum. This module owns what a
call may carry, the images-endpoint request it becomes, and how the endpoint's
answer becomes the canonical record and the image bytes; the service owns the
call itself, and Vibey saves the images through its media sink.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any, Final, Mapping

# The one size grammar: Vibey checks a call with it before sending, so nothing it sends is refused here.
from core.agent_core.tools.hosted import size_form
from core.agent_core.tools.read import MAX_INLINE_IMAGE_BYTES, detect_supported_image_mime_type
from vibe.backend_model_catalog import ImageModelSpec

from .hosted_executor import ExecutorCallRefused, Say, failed_record

IMAGE_TIMEOUT_SECONDS: Final = 300.0
MAX_REFERENCE_IMAGES: Final = 4
MAX_IMAGES_PER_CALL: Final = 4
# The call's body: its reference images, each within read's inline limit as base64, plus the rest.
IMAGE_CALL_MAX_BYTES: Final = MAX_REFERENCE_IMAGES * ((MAX_INLINE_IMAGE_BYTES + 2) // 3 * 4) + 1024 * 1024
# One delivered image, decoded, and every image of one answer.
IMAGE_MAX_BYTES: Final = 24 * 1024 * 1024
IMAGES_TOTAL_MAX_BYTES: Final = 64 * 1024 * 1024
# The endpoint's answer as base64 JSON: the decoded budget, inflated, plus a margin for the rest.
IMAGE_RESPONSE_MAX_BYTES: Final = IMAGES_TOTAL_MAX_BYTES * 4 // 3 + 1024 * 1024
# Where each size form goes on the images endpoints.
_SIZE_FIELDS: Final = {"pixels": "size", "ratio": "aspect_ratio"}


@dataclass(frozen=True)
class ImageCall:
    model: str
    prompt: str
    size: str | None = None
    n: int | None = None
    # Each reference image's MIME type and base64 data, as read resolved them.
    images: tuple[tuple[str, str], ...] = ()

    @property
    def given(self) -> tuple[str, ...]:
        """The optional parameters this call names, in catalog order."""

        return tuple(
            name for name, value in (("images", self.images), ("n", self.n), ("size", self.size)) if value
        )


def parse_image_call(payload: object) -> ImageCall:
    """Validate one ``image_generate`` body; parse it off the loop, its images are megabytes."""

    if not isinstance(payload, dict) or set(payload) != {"arguments"}:
        raise ExecutorCallRefused("invalid_request_error", 400)
    arguments = payload["arguments"]
    if not isinstance(arguments, dict) or set(arguments) - {"model", "prompt", "size", "images", "n"}:
        raise ExecutorCallRefused("invalid_request_error", 400)
    model, prompt = arguments.get("model"), arguments.get("prompt")
    size, n, images = arguments.get("size"), arguments.get("n"), arguments.get("images")
    if (
        not isinstance(model, str) or not model
        or not isinstance(prompt, str) or not prompt.strip()
        or (size is not None and size_form(size) is None)
        or (n is not None and (isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_IMAGES_PER_CALL))
        or (images is not None and (not isinstance(images, list) or not 1 <= len(images) <= MAX_REFERENCE_IMAGES))
    ):
        raise ExecutorCallRefused("invalid_request_error", 400)
    references = []
    for encoded in images or ():
        # Vibey read each one as ``read`` reads it; the gateway checks again as the execution boundary.
        try:
            data = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else b""
        except (binascii.Error, ValueError):
            data = b""
        mime = detect_supported_image_mime_type(data) if 0 < len(data) <= MAX_INLINE_IMAGE_BYTES else None
        if mime is None:
            raise ExecutorCallRefused("invalid_request_error", 400)
        references.append((mime, encoded))
    return ImageCall(model, prompt, size, n, tuple(references))


def unsupported_argument(call: ImageCall, spec: ImageModelSpec) -> str | None:
    """The first argument the model does not take, or a size in a form it does not accept."""

    for name in call.given:
        if name not in spec.params:
            return name
    if call.size is not None and size_form(call.size) not in spec.size_forms:
        return "size"
    return None


def image_request(call: ImageCall, spec: ImageModelSpec) -> dict[str, Any]:
    """The images-endpoint body: the model's fixed fields, then what the call names.

    The engine routes a body with reference images to ``/v1/images/edits``.
    """

    body: dict[str, Any] = {**spec.request, "prompt": call.prompt}
    if call.n is not None:
        body["n"] = call.n
    if call.size is not None:
        body[_SIZE_FIELDS[size_form(call.size)]] = call.size
    if call.images:
        body["images"] = [{"image_url": f"data:{mime};base64,{data}"} for mime, data in call.images]
    return body


@dataclass(frozen=True)
class ImageResult:
    """The record for the call, the images it delivered (base64, in order), and the endpoint's own facts."""

    record: dict[str, Any]
    images: tuple[str, ...] = ()
    generation: Mapping[str, str] | None = None
    usage: Mapping[str, Any] | None = None


def image_result(body: bytes | bytearray, say: Say) -> ImageResult:
    """The canonical record of one images-endpoint answer; parse it off the loop.

    Completion is an allow-list: only an answer whose every item is a usable
    base64 image completes. An endpoint that answers with URLs only fails, as
    the gateway never fetches an arbitrary URL.
    """

    try:
        answer = json.loads(body)
    except (ValueError, RecursionError):
        return ImageResult(failed_record(say("modelHub.hostedTools.unreadable")))
    data = answer.get("data") if isinstance(answer, dict) else None
    if not isinstance(data, list) or not data or not all(isinstance(item, dict) for item in data):
        return ImageResult(failed_record(say("modelHub.hostedTools.imageMissing")))
    if len(data) > MAX_IMAGES_PER_CALL:
        # A call asks for at most four; more is not what was asked for, and none of it is kept.
        return ImageResult(failed_record(say("modelHub.hostedTools.tooLarge")))
    images: list[str] = []
    total = 0
    for item in data:
        encoded = item.get("b64_json")
        if not isinstance(encoded, str) or not encoded:
            return ImageResult(failed_record(say("modelHub.hostedTools.imageMissing")))
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return ImageResult(failed_record(say("modelHub.hostedTools.imageUnreadable")))
        total += len(decoded)
        if not decoded or len(decoded) > IMAGE_MAX_BYTES or total > IMAGES_TOTAL_MAX_BYTES:
            return ImageResult(failed_record(say("modelHub.hostedTools.tooLarge")))
        if detect_supported_image_mime_type(decoded[:4100]) is None:
            return ImageResult(failed_record(say("modelHub.hostedTools.imageUnreadable")))
        images.append(encoded)
    generation = {
        name: value
        for name, value in (
            ("size", answer.get("size")),
            ("quality", answer.get("quality")),
            ("revised_prompt", data[0].get("revised_prompt")),
        )
        if isinstance(value, str) and value
    }
    usage = answer.get("usage")
    return ImageResult(
        {"status": "completed", "sources": []},
        images=tuple(images),
        generation=generation or None,
        usage=usage if isinstance(usage, Mapping) else None,
    )
