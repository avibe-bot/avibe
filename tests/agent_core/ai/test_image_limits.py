"""Every image a request sends fits the providers' per-image size limit (``core/agent_core/ai/image_limits.py``).

A Session's images go out again with every request, and Anthropic refuses an image with a side over 2000 px once a
request holds more than 20 images: one tall screenshot read early refused every request of its Session after the 21st
image arrived, for good.
"""

from __future__ import annotations

import base64
import io
from typing import Any, Iterator

import pytest
from PIL import Image

from core.agent_core.ai.provider import MediaLoader, ModelEndpoint, ModelRequest
from core.agent_core.messages import ImageBlock, TextBlock, UserMessage
from tests.agent_core.ai._wire import build_wire_request

_PROVIDERS = {"anthropic": "anthropic", "openai_chat": "openai", "openai_responses": "openai"}


def _png(size: tuple[int, int], mode: str = "RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, 128).save(out, "PNG")
    return out.getvalue()


class _Loader(MediaLoader):
    def __init__(self, images: dict[str, bytes]) -> None:
        self._images = images

    async def load(self, media_token: str) -> tuple[bytes, str]:
        return self._images[media_token], "image/png"


def _request(protocol: str, images: dict[str, bytes]) -> ModelRequest:
    blocks = [TextBlock("Compare these screens")]
    blocks += [ImageBlock("image/png", token, f"{token}.png") for token in images]
    return ModelRequest(
        endpoint=ModelEndpoint(protocol, "https://model.test/v1", "model-x", "token", provider=_PROVIDERS[protocol]),
        system="system",
        messages=(UserMessage(tuple(blocks)),),
        tools=(),
        max_tokens=128,
        reasoning_effort=None,
        supports_images=True,
    )


def _parts(value: Any) -> Iterator[tuple[str, Any]]:
    """The text and image parts of a wire payload, in order, for the three protocols' shapes."""

    if isinstance(value, list):
        for item in value:
            yield from _parts(item)
    elif isinstance(value, dict):
        kind = value.get("type")
        if kind in ("text", "input_text") and isinstance(value.get("text"), str):
            yield "text", value["text"]
        elif kind == "image":
            yield "image", base64.b64decode(value["source"]["data"])
        elif kind in ("image_url", "input_image"):
            url = value["image_url"]["url"] if kind == "image_url" else value["image_url"]
            yield "image", base64.b64decode(url.split(";base64,", 1)[1])
        else:
            for item in value.values():
                yield from _parts(item)


async def _sent(protocol: str, images: dict[str, bytes]) -> list[tuple[str, Any]]:
    wire = await build_wire_request(_request(protocol, images), gateway=False, media_loader=_Loader(images))
    return list(_parts(wire.body["messages" if protocol != "openai_responses" else "input"]))


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", sorted(_PROVIDERS))
async def test_a_many_image_request_sends_every_image_within_2000_px(protocol: str) -> None:
    # The failed Session's shape: 21 ordinary screenshots and one 716x3838 phone capture read earlier.
    images = {"tall": _png((716, 3838)), **{f"screen{index}": _png((1280, 1040 + index)) for index in range(21)}}

    parts = await _sent(protocol, images)

    sizes = {index: Image.open(io.BytesIO(data)).size for index, (kind, data) in enumerate(parts) if kind == "image"}
    assert len(sizes) == 22
    assert all(max(size) <= 2000 for size in sizes.values())
    tall = next(index for index, size in sizes.items() if size == (373, 2000))
    assert parts[tall + 1] == (
        "text",
        "[Image: original 716x3838, displayed at 373x2000. Multiply coordinates by 1.92 to map to original image.]",
    )
    # An image within the limit goes out as its snapshot holds it, with no note, so its bytes stay cache-stable.
    within = [data for kind, data in parts if kind == "image" and data != parts[tall][1]]
    assert within == [images[f"screen{index}"] for index in range(21)]
    assert sum(1 for kind, text in parts if kind == "text" and text.startswith("[Image")) == 1


def _gif(size: tuple[int, int], frames: int) -> bytes:
    out = io.BytesIO()
    images = [Image.new("P", size, index) for index in range(frames)]
    images[0].save(out, "GIF", save_all=True, append_images=images[1:])
    return out.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("image", "sent"),
    [
        pytest.param(
            lambda: _png((9000, 6000), mode="L"),
            ["[Image omitted: 9000x6000 is too large to downscale to 2000x2000 for this request.]"],
            id="too-large-to-decode",
        ),
        pytest.param(
            lambda: _gif((2500, 300), frames=3),
            [
                "<image 2000x240>",
                "[Image: original 2500x300, displayed at 2000x240. Multiply coordinates by 1.25 to map to original "
                "image. Only the first of its 3 frames is shown.]",
            ],
            id="animation",
        ),
    ],
)
async def test_the_note_says_what_an_oversized_image_lost(image: Any, sent: list[str]) -> None:
    parts = await _sent("anthropic", {"large": image()})

    shown = [
        value if kind == "text" else "<image {}x{}>".format(*Image.open(io.BytesIO(value)).size) for kind, value in parts
    ]
    assert shown == ["Compare these screens", *sent]
