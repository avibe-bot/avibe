"""Native image generation (``vibey-hosted-tools.md`` §7.3, §7.5). Scenario MH-HOSTED-VIBEY-007.

A generated image is saved to a file as its done item is parsed, and the call's record names that file: no base64
enters the message, the transcript or the model's context. Partial previews are never kept, and the terminal response's
copy of the image is never decoded. A stream that may carry an image gets the whole event cap as its line cap.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from core.agent_core.ai import openai_responses
from core.agent_core.ai.hosted import in_place_text
from core.agent_core.ai.provider import Done, HostedToolRoute, ModelEndpoint, ModelRequest, ProviderError
from core.agent_core.ai.registry import create_adapter
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import HostedAction, HostedImage, HostedToolBlock, TextBlock, UserMessage, message_to_dict
from core.agent_core.tools.hosted import hosted_tool_spec
from core.agent_core.tools.read import MAX_INLINE_IMAGE_BYTES

SOURCE = "src_served00001"
DECLARATION = {"type": "image_generation", "partial_images": 2, "output_format": "jpeg", "output_compression": 90}
JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + bytes(range(256)) * 4


class Sink:
    """The adapter layer's media store, as a directory: each saved image is one file."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.saved: list[Path] = []

    async def save_image(self, encoded: str) -> HostedImage:
        data = base64.b64decode(encoded, validate=True)
        path = self.root / f"image-{len(self.saved)}.jpg"
        path.write_bytes(data)
        self.saved.append(path)
        return HostedImage(path=str(path), mime="image/jpeg", bytes=len(data))


def _sse(*events: tuple[str | None, Any]) -> str:
    """Named SSE events; a string payload goes out as it is, so it need not be JSON."""

    frames = []
    for name, payload in events:
        data = payload if isinstance(payload, str) else json.dumps(payload)
        frames.append((f"event: {name}\n" if name else "") + f"data: {data}\n\n")
    return "".join(frames)


def _image_item(result: str | None, *, status: str = "completed") -> dict[str, Any]:
    item = {
        "id": "ig_1",
        "type": "image_generation_call",
        "status": status,
        "revised_prompt": "A lighthouse at dusk, oil painting",
        "size": "1536x1024",
        "quality": "high",
        "output_format": "jpeg",
    }
    if result is not None:
        item["result"] = result
    return item


def _stream(
    encoded: str,
    *,
    done: bool = True,
    terminal_copy: str = "!!not base64, never decoded!!",
    previews: int = 1,
    unnamed_previews: bool = False,
    status: str = "completed",
) -> str:
    events: list[tuple[str | None, Any]] = [
        ("response.created", {"type": "response.created", "response": {"status": "in_progress"}}),
        (
            "response.output_item.added",
            {"type": "response.output_item.added", "output_index": 0, "item": _image_item(None, status="in_progress")},
        ),
    ]
    # A preview: not JSON at all here, so the stream fails if anything parses it. A source that names no SSE event
    # sends it as JSON, known only by its type.
    preview: tuple[str | None, Any] = (
        (None, {"type": "response.image_generation_call.partial_image", "partial_image_b64": "AAAA"})
        if unnamed_previews
        else ("response.image_generation_call.partial_image", "{partial preview bytes")
    )
    events += [preview] * previews
    if done:
        events.append(
            (
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": 0, "item": _image_item(encoded, status=status)},
            )
        )
    message = {"id": "msg_1", "type": "message", "content": [{"type": "output_text", "text": "Here it is.", "annotations": []}]}
    events += [
        ("response.output_item.added", {"type": "response.output_item.added", "output_index": 1, "item": {**message, "content": []}}),
        ("response.output_text.delta", {"type": "response.output_text.delta", "output_index": 1, "delta": "Here it is."}),
        ("response.output_item.done", {"type": "response.output_item.done", "output_index": 1, "item": message}),
        (
            "response.completed",
            {
                "type": "response.completed",
                "response": {"status": "completed", "output": [_image_item(terminal_copy), message], "usage": {}},
            },
        ),
    ]
    return _sse(*events)


async def _generate(body: str, sink: Sink, *, declared: bool = True) -> list[Any]:
    routes = {"image_generation": HostedToolRoute("native", None, False, declaration=DECLARATION)} if declared else {}
    request = ModelRequest(
        endpoint=ModelEndpoint(
            "openai_responses",
            "http://hub.invalid/vibey/v1",
            "gpt-6-sol",
            "hub-token",
            provider="openai",
            source_id=SOURCE,
            hosted_tools=routes,
        ),
        system="system",
        messages=(UserMessage((TextBlock("paint a lighthouse"),)),),
        tools=(hosted_tool_spec("image_generation"),),
        max_tokens=256,
        cache="none",
    )
    served = {"provider": "openai", "api": "openai_responses", "model": "gpt-6-sol", "source": SOURCE}
    headers = {"content-type": "text/event-stream", "x-avibe-served-hop": json.dumps(served, separators=(",", ":"))}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, headers=headers, text=body))
    ) as client:
        adapter = create_adapter("openai_responses", client=client, gateway=True, media_sink=sink)
        return [event async for event in adapter.stream(request, CancelToken())]


async def test_a_native_image_is_saved_once_from_its_done_item_and_history_holds_only_its_path(tmp_path):
    """MH-HOSTED-VIBEY-007: the record names the file, the action keeps the provider's revised prompt and resolved
    size and quality, and no copy of the image, done, partial or terminal, enters the message."""

    sink = Sink(tmp_path)
    encoded = base64.b64encode(JPEG).decode()

    events = await _generate(_stream(encoded), sink)

    assert isinstance(events[-1], Done), events[-1]
    message = events[-1].message
    (call,) = [block for block in message.content if isinstance(block, HostedToolBlock)]
    assert [path.read_bytes() for path in sink.saved] == [JPEG]
    image = HostedImage(path=str(sink.saved[0]), mime="image/jpeg", width=1536, height=1024, bytes=len(JPEG))
    assert call.tool == "image_generation" and call.record.status == "completed" and call.record.images == (image,)
    assert call.action == HostedAction(
        "generate", prompt="A lighthouse at dusk, oil painting", size="1536x1024", quality="high"
    )
    assert call.native.payload[0]["result"] == str(sink.saved[0])
    stored = json.dumps(message_to_dict(message))
    assert encoded not in stored and "never decoded" not in stored
    assert in_place_text(call).startswith('[image_generation: "A lighthouse at dusk, oil painting"]\nImage saved: ')


@pytest.mark.parametrize(
    ("done", "status", "encoded", "error"),
    [
        (False, "completed", base64.b64encode(JPEG).decode(), "the image was not streamed before the response ended"),
        (True, "completed", "@@@ not base64 @@@", "the image could not be saved: "),
        # A call the provider did not complete lists no image, so its bytes are never saved: no file without a record.
        (True, "failed", base64.b64encode(JPEG).decode(), "the provider ended the call failed"),
    ],
    ids=["only-in-the-terminal-copy", "unreadable", "failed-with-bytes"],
)
async def test_an_image_that_was_not_saved_is_a_failed_call_never_a_completed_one(
    tmp_path, done, status, encoded, error
):
    sink = Sink(tmp_path)

    events = await _generate(_stream(encoded, done=done, terminal_copy=encoded, status=status), sink)

    (call,) = [block for block in events[-1].message.content if isinstance(block, HostedToolBlock)]
    assert call.record.status == "failed" and call.record.error.startswith(error)
    assert call.record.images == () and "result" not in call.native.payload[0]
    assert not sink.saved


async def test_a_six_megabyte_image_streams_under_the_image_line_cap_and_its_record_says_read_cannot_show_it(tmp_path):
    """§7.3, §7.5: an image of 6 MiB is one base64 line over the default 8 MiB line cap, which a stream that may carry
    an image raises to the event cap. Such an image is over ``read``'s inline limit, so its record says so; a stream
    that declared no image keeps the default cap."""

    large = JPEG + bytes(6 * 1024 * 1024 - len(JPEG))
    encoded = base64.b64encode(large).decode()

    events = await _generate(_stream(encoded), Sink(tmp_path))
    undeclared = await _generate(_stream(encoded), Sink(tmp_path), declared=False)

    (call,) = [block for block in events[-1].message.content if isinstance(block, HostedToolBlock)]
    assert call.record.images[0].bytes == len(large) > MAX_INLINE_IMAGE_BYTES
    assert "inline image limit, so read cannot show it" in in_place_text(call)
    assert isinstance(undeclared[-1], ProviderError) and "line" in undeclared[-1].message


@pytest.mark.parametrize(
    ("previews", "unnamed", "budget", "reason"),
    [
        # The declaration asks for 2 previews and the final: 3 events per image call, and a margin of 2.
        (5, False, None, "more images than the response asked for"),
        (5, True, None, "more images than the response asked for"),
        (1, False, 1024, "more image data than one response may carry"),
    ],
    ids=["events", "events-known-by-type", "bytes"],
)
async def test_a_response_that_streams_images_past_its_budget_ends_as_an_error(
    tmp_path, monkeypatch, previews, unnamed, budget, reason
):
    """Plan item 25: previews are never kept, but each counts, named or known by its type, so a provider cannot
    stream images without bound."""

    if budget is not None:
        monkeypatch.setattr(openai_responses, "IMAGE_BYTES_PER_RESPONSE", budget)
    sink = Sink(tmp_path)

    events = await _generate(
        _stream(base64.b64encode(JPEG).decode(), previews=previews, unnamed_previews=unnamed), sink
    )

    assert isinstance(events[-1], ProviderError) and reason in events[-1].message
    assert not sink.saved


def test_the_images_one_response_may_stream_fit_the_terminal_event_that_repeats_them():
    """The terminal response repeats every final image in one SSE event: the response's image budget, as base64 with a
    margin for the rest of that event, is never more than one event may carry, so a response within its budget is
    never rejected by the event cap after its images were saved."""

    from core.agent_core.ai.sse import DEFAULT_MAX_PENDING_EVENT_SIZE

    as_base64 = -(-openai_responses.IMAGE_BYTES_PER_RESPONSE // 3) * 4
    assert as_base64 + 1024 * 1024 <= DEFAULT_MAX_PENDING_EVENT_SIZE
