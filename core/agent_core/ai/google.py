"""Google Gemini ``streamGenerateContent`` streaming adapter.

Payload and stream rules are ported from Pi's ``packages/ai/src/api/google-shared.ts`` and
``google-generative-ai.ts`` (MIT, ``7fbbd5f``) and tau's ``tau_ai/google.py`` (MIT) onto the shared stream
assembler; ``agent-core-contracts/conversion-tables.md`` ("Google Gemini") is the table.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import quote

import httpx

from core.agent_core.ai.hosted import (
    GOOGLE_SEARCH_TOOL_TYPE,
    google_grounding_sources,
    google_hosted_block,
    google_search_count,
)
from core.agent_core.ai.image_limits import LoadedImage
from core.agent_core.ai._common import (
    PinUnavailable,
    ServedHopResolver,
    StreamAssembler,
    WireField,
    WireRequest,
    auth_headers,
    content_parts,
    drive_sse_stream,
    endpoint_origin,
    hosted_pin,
    join_endpoint_url,
    json_object,
    native_hosted_tools,
    pin_refusal,
    prepare_messages,
    request_credential_values,
    usage_counter_error,
    validate_wire_shape,
)
from core.agent_core.ai.provider import Done, MediaLoader, ModelRequest, ProviderAdapter, ProviderError
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import (
    AssistantMessage,
    HostedToolBlock,
    ImageBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)

#: The generateContent tool each overlay declaration type names (``vibey-gemini-native.md`` §4): the overlay spells
#: Google Search as Google's typed tools do, and this API takes it as a keyed tool object.
_NATIVE_TOOLS = {"google_search": {"googleSearch": {}}}
#: Google's documented stand-in for the signature of a function call Gemini did not produce ("Thought signatures",
#: FAQ 1): another origin's call in the current turn, which Gemini 3 and later validate.
DUMMY_THOUGHT_SIGNATURE = "skip_thought_signature_validator"
# A thought signature is base64 bytes (TYPE_BYTES); anything else in a transcript is not Gemini's to receive.
_SIGNATURE = re.compile(r"[A-Za-z0-9+/]+={0,2}")
# Gemini 3 and later, and the aliases Google points at them, take a discrete thinking level; Gemini 2.5 a budget.
_LEVEL_MODEL = re.compile(r"^(?:models/)?(?:gemini-(?:[3-9]|\d{2,})(?:\.\d+)?-|gemini-(?:flash|flash-lite|pro)-latest$)")
_BUDGET_MODEL = re.compile(r"^(?:models/)?gemini-2\.5-")
_MAJOR_VERSION = re.compile(r"^(?:models/)?gemini-(\d+)")
_LEVELS = {"minimal": "minimal", "low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
# Google's own effort-to-budget table for Gemini 2.5 ("OpenAI compatibility", Thinking).
_BUDGETS = {"none": 0, "minimal": 1024, "low": 1024, "medium": 8192, "high": 24576, "xhigh": 24576, "max": 24576}
# The effort each budget stands for, so one wire budget is one evidence key.
_BUDGET_EFFORTS = {0: "none", 1024: "low", 8192: "medium", 24576: "high"}
_SAFETY_FINISH = frozenset({
    "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII",
    "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION",
})

_GOOGLE_USAGE_FIELDS = (
    "promptTokenCount", "cachedContentTokenCount", "candidatesTokenCount", "thoughtsTokenCount",
    "toolUsePromptTokenCount", "totalTokenCount",
)
_GOOGLE_PART_FIELDS = (
    WireField("text", str),
    WireField("thought", bool),
    WireField("thoughtSignature", str),
    WireField(
        "functionCall",
        Mapping,
        children=(WireField("name", str), WireField("id", str), WireField("args", Mapping)),
    ),
    WireField("toolCall", Mapping, children=(WireField("toolType", str), WireField("id", str), WireField("args", Mapping))),
    WireField(
        "toolResponse", Mapping, children=(WireField("toolType", str), WireField("id", str), WireField("response", Mapping)),
    ),
)
_GOOGLE_WIRE_SHAPES = {
    "chunk": (
        WireField("usageMetadata", Mapping),
        WireField("error", Mapping),
        WireField("promptFeedback", Mapping, children=(WireField("blockReason", str),)),
        WireField(
            "candidates",
            list,
            item_children=(
                WireField("finishReason", str),
                WireField("finishMessage", str),
                WireField(
                    "groundingMetadata",
                    Mapping,
                    children=(WireField("webSearchQueries", list), WireField("groundingChunks", list)),
                ),
                WireField(
                    "content",
                    Mapping,
                    children=(WireField("role", str), WireField("parts", list, item_children=_GOOGLE_PART_FIELDS)),
                ),
            ),
        ),
    ),
}


class GoogleAdapter(ProviderAdapter):
    """Adapter for Gemini ``models/<model>:streamGenerateContent?alt=sse`` endpoints."""

    protocol = "google"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        media_loader: MediaLoader | None = None,
        served_hop_resolver: ServedHopResolver | None = None,
        gateway: bool = False,
    ) -> None:
        # The Model Hub gateway is loopback: an environment or system proxy must never carry it.
        self._client = client or httpx.AsyncClient(timeout=None, trust_env=not gateway)
        self._owns_client = client is None
        self._media_loader = media_loader
        self._served_hop_resolver = served_hop_resolver
        self._gateway = gateway

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[Any]:
        return self._stream(request, cancel)

    async def _stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[Any]:
        target = endpoint_origin(request.endpoint)
        assembler = StreamAssembler(
            origin=target,
            protocol=self.protocol,
            verified_origin=not self._gateway,
            endpoint_url=request.endpoint.base_url,
            sensitive_values=request_credential_values(request.endpoint),
        )
        if cancel.cancelled:
            terminal = assembler.terminal(assembler.aborted(cancel.reason))
            if terminal is not None:
                yield terminal
            return
        prepared = await prepare_messages(
            request.messages,
            target=target,
            supports_images=request.supports_images,
            protocol=self.protocol,
            media_loader=self._media_loader,
            cancel=cancel,
            endpoint=request.endpoint,
        )
        if isinstance(prepared, ProviderError):
            terminal = assembler.terminal(prepared)
            if terminal is not None:
                yield terminal
            return
        transformed_messages, loaded_images = prepared
        if cancel.cancelled:
            terminal = assembler.terminal(assembler.aborted(cancel.reason))
            if terminal is not None:
                yield terminal
            return
        stream = _GoogleStream(assembler, frozenset(native_hosted_tools(request)))
        try:
            wire = wire_request(request, transformed_messages, gateway=self._gateway, loaded_images=loaded_images)

            async def translate(events: AsyncIterator[Any]) -> AsyncIterator[Any]:
                async for event in events:
                    if cancel.cancelled:
                        terminal = assembler.terminal(assembler.aborted(cancel.reason))
                        if terminal is not None:
                            yield terminal
                        return
                    if not event.data:
                        continue
                    for item in stream.chunk(event.data):
                        yield item
                    if stream.ended:
                        return
                if cancel.cancelled:
                    terminal = assembler.terminal(assembler.aborted(cancel.reason))
                    if terminal is not None:
                        yield terminal
                    return
                for item in stream.finish():
                    yield item

            driver = drive_sse_stream(
                self._client,
                method="POST",
                url=wire.url,
                json_body=wire.body,
                headers=wire.headers,
                cancel=cancel,
                assembler=assembler,
                endpoint=request.endpoint,
                resolver=self._served_hop_resolver,
                gateway=self._gateway,
                translate=translate,
            )
            try:
                async for event in driver:
                    if isinstance(event, PinUnavailable):
                        terminal = assembler.terminal(pin_refusal(assembler, request.endpoint, event))
                        if terminal is not None:
                            yield terminal
                        return
                    yield event
            finally:
                await driver.aclose()
        except Exception as exc:
            terminal = assembler.terminal(assembler.exception(exc))
            if terminal is not None:
                yield terminal


class _GoogleStream:
    """One response's chunks to canonical provider events.

    Every chunk is a ``GenerateContentResponse`` whose parts are deltas. Consecutive parts of one kind (thought text,
    answer text) extend one block; a ``functionCall`` part is a whole call. A signature stays with the part it came
    on: a call's on its ``ToolCallBlock``, a thought's on its ``ThinkingBlock``. ``TextBlock`` holds none, so an
    answer's goes on an empty ``ThinkingBlock`` right after its text (or alone, for the empty signed part Gemini may
    stream), and the request builder puts it back on that text part.
    """

    def __init__(self, assembler: StreamAssembler, hosted: frozenset[str] = frozenset()) -> None:
        self.assembler = assembler
        # The tools this request declared natively: a server-side call of any other stays out of the transcript.
        self.hosted = hosted
        # Each Google Search by its call id: its slot and its toolCall part, until its toolResponse arrives.
        self._searches: dict[str, tuple[tuple[str, str], dict[str, Any]]] = {}
        self._grounding: Mapping[str, Any] | None = None
        self.ended = False
        self._finish: str | None = None
        self._finish_message: str | None = None
        self._blocked: str | None = None
        self._kind: str | None = None
        self._block = 0
        self._tools = 0
        self._text_signature: str | None = None

    def _fail(self, message: str, *, kind: str | None = "invalid_request") -> list[Any]:
        self.ended = True
        terminal = self.assembler.terminal(self.assembler.error(message, kind=kind))
        return [terminal] if terminal is not None else []

    def _key(self, kind: str) -> tuple[str, int]:
        if self._kind != kind:
            self._end_block()
            self._kind = kind
            self._block += 1
        return (f"google-{kind}", self._block)

    def _end_block(self) -> None:
        """Close the open block; an answer's signature lands right after its text, before whatever follows."""

        self._kind = None
        if self._text_signature is not None:
            self._block += 1
            self.assembler.ensure_thinking_slot(("google-signature", self._block), signature=self._text_signature)
            self._text_signature = None

    def chunk(self, data: str) -> list[Any]:
        chunk = json_object(data)
        if chunk is None:
            return self._fail("Provider returned invalid Gemini JSON")
        shape_error = validate_wire_shape(chunk, "chunk", _GOOGLE_WIRE_SHAPES)
        if shape_error is not None:
            return self._fail(shape_error)
        error = chunk.get("error")
        if isinstance(error, Mapping):
            return self._fail(json.dumps(dict(chunk), ensure_ascii=False), kind=None)
        usage = chunk.get("usageMetadata")
        usage_error = usage_counter_error(usage, label="Gemini usageMetadata", fields=_GOOGLE_USAGE_FIELDS)
        if usage_error is not None:
            return self._fail(usage_error)
        if isinstance(usage, Mapping):
            self.assembler.set_usage(_google_usage(usage))
        feedback = chunk.get("promptFeedback")
        if isinstance(feedback, Mapping) and feedback.get("blockReason"):
            self._blocked = feedback["blockReason"]
        events: list[Any] = []
        candidates = chunk.get("candidates") or []
        if not candidates:
            return events
        candidate = candidates[0]
        content = candidate.get("content")
        for part in (content.get("parts") or []) if isinstance(content, Mapping) else []:
            events.extend(self._part(part))
        if isinstance(candidate.get("groundingMetadata"), Mapping):
            self._grounding = candidate["groundingMetadata"]
        if candidate.get("finishReason"):
            self._finish = candidate["finishReason"]
            self._finish_message = candidate.get("finishMessage")
        return events

    def _part(self, part: Mapping[str, Any]) -> list[Any]:
        events: list[Any] = []
        signature = part.get("thoughtSignature") or None
        if isinstance(part.get("toolCall"), Mapping) or isinstance(part.get("toolResponse"), Mapping):
            self._server_part(part)
            return events
        call = part.get("functionCall")
        if isinstance(call, Mapping):
            name = call.get("name")
            if not name:
                return self._fail("Gemini function call name must not be empty")
            self._end_block()
            self._tools += 1
            key = ("google-tool", self._tools)
            start = self.assembler.tool_start(key, name=name, native_id=call.get("id") or None, signature=signature)
            if start is not None:
                events.append(start)
            delta = self.assembler.tool_arguments(key, json.dumps(dict(call.get("args") or {}), ensure_ascii=False))
            if delta is not None:
                events.append(delta)
            return events
        text = part.get("text")
        if part.get("thought") is True:
            key = self._key("thinking")
            if text:
                emitted = self.assembler.thinking_delta(key, text)
                if emitted is not None:
                    events.append(emitted)
            if signature:
                self.assembler.set_thinking_signature(key, signature)
            return events
        if text:
            emitted = self.assembler.text_delta(self._key("text"), text)
            if emitted is not None:
                events.append(emitted)
        if signature:
            # A signature can come on any delta of the answer, or on an empty part after it: it belongs to the text.
            if self._kind != "text":
                self._end_block()
            self._text_signature = signature
            if self._kind != "text":
                self._end_block()
        # Parts of a kind this adapter does not declare (inline data, code execution) carry nothing to the loop.
        return events

    def _server_part(self, part: Mapping[str, Any]) -> None:
        """A Google Search Gemini ran inside the response: one hosted call, its two parts kept verbatim (§4)."""

        source_id = self.assembler.source_id
        call = part.get("toolCall")
        response = part.get("toolResponse")
        item = call if isinstance(call, Mapping) else response
        if (
            "web_search" not in self.hosted or source_id is None or not isinstance(item, Mapping)
            or item.get("toolType") != GOOGLE_SEARCH_TOOL_TYPE
        ):
            return
        self._end_block()
        if isinstance(call, Mapping):
            call_id = call.get("id") or f"google_search_{len(self._searches) + 1}"
            key = ("hosted", call_id)
            self._searches[call_id] = (key, dict(part))
            self.assembler.hosted_block(key, google_hosted_block(part, None, source_id=source_id, call_id=call_id))
            return
        # A response names its call; one that names none answers the latest call still waiting.
        call_id = response.get("id") or next(
            (pending for pending, (key, _) in reversed(self._searches.items())
             if (block := self.assembler.hosted_block_at(key)) is not None and block.record.status == "pending"),
            None,
        )
        if call_id not in self._searches:
            return
        key, call_part = self._searches[call_id]
        self.assembler.hosted_block(
            key, google_hosted_block(call_part, part, source_id=source_id, call_id=call_id),
        )

    def _ground(self) -> None:
        """Gemini reports a response's searches and sources once: they count its calls and fill its last record."""

        if not self._searches or self._grounding is None:
            return
        searches = google_search_count(self._grounding)
        if searches:
            self.assembler.set_hosted_facts(counts={"web_search_requests": searches})
        sources = google_grounding_sources(self._grounding)
        completed = [
            (key, block) for key, _ in self._searches.values()
            if (block := self.assembler.hosted_block_at(key)) is not None and block.record.status == "completed"
        ]
        if sources and completed:
            key, block = completed[-1]
            self.assembler.hosted_block(key, replace(block, record=replace(block.record, sources=sources)))

    def finish(self) -> list[Any]:
        if self._finish is None and self._blocked is None:
            terminal = self.assembler.terminal(self.assembler.incomplete())
            return [terminal] if terminal is not None else []
        self._end_block()
        self._ground()
        events: list[Any] = list(self.assembler.block_end_events())
        if self._finish is None:
            final = self.assembler.finalize("safety", error_message=f"Gemini blocked the prompt: {self._blocked}")
        else:
            stop, message = _normalize_stop(self._finish, self._finish_message)
            final = self.assembler.finalize(stop, error_message=message)
        terminal = self.assembler.terminal(final if isinstance(final, ProviderError) else Done(final))
        if terminal is not None:
            events.append(terminal)
        return events


def build_google_payload(
    request: ModelRequest,
    messages: tuple[Any, ...],
    *,
    loaded_images: Mapping[str, LoadedImage] | None = None,
) -> dict[str, Any]:
    model = request.endpoint.model_id
    payload: dict[str, Any] = {"contents": _contents(messages, model, request.supports_images, loaded_images)}
    if request.system:
        payload["systemInstruction"] = {"parts": [{"text": request.system}]}
    # A hosted tool this hop runs natively is sent as Gemini's own tool in its spec's place (§4.1), and Gemini runs a
    # server-side tool next to functions only when the request says it will circulate their calls.
    native = native_hosted_tools(request)
    functions = [
        {"name": tool.name, "description": tool.description, "parametersJsonSchema": dict(tool.input_schema)}
        for tool in request.tools
        if tool.name not in native
    ]
    tools: list[dict[str, Any]] = [{"functionDeclarations": functions}] if functions else []
    for route in native.values():
        kind = (route.declaration or {}).get("type")
        if kind not in _NATIVE_TOOLS:
            raise ValueError(f"unsupported Gemini hosted declaration: {kind!r}")
        tools.append(copy.deepcopy(_NATIVE_TOOLS[kind]))
    if tools:
        payload["tools"] = tools
    if native:
        payload["toolConfig"] = {"includeServerSideToolInvocations": True}
    generation: dict[str, Any] = {"maxOutputTokens": request.max_tokens}
    thinking = _thinking_config(model, _effort(request.reasoning_effort or ""))
    if thinking is not None:
        generation["thinkingConfig"] = thinking
    payload["generationConfig"] = generation
    return payload


def wire_request(
    request: ModelRequest,
    messages: tuple[Any, ...],
    *,
    gateway: bool,
    loaded_images: Mapping[str, LoadedImage] | None = None,
) -> WireRequest:
    """The ``streamGenerateContent`` request for already prepared ``messages``, as the adapter sends it."""

    payload = build_google_payload(request, messages, loaded_images=loaded_images)
    headers = auth_headers(request.endpoint, provider="google", gateway=gateway)
    headers.setdefault("content-type", "application/json")
    headers.update(hosted_pin(request, messages))
    model = quote(request.endpoint.model_id, safe="")
    url = join_endpoint_url(request.endpoint.base_url, f"/models/{model}:streamGenerateContent?alt=sse")
    return WireRequest(url, headers, payload)


def _contents(
    messages: tuple[Any, ...],
    model: str,
    supports_images: bool,
    loaded_images: Mapping[str, LoadedImage] | None,
) -> list[dict[str, Any]]:
    # Gemini validates the signatures of the current turn: everything after the last user message.
    turn_start = max((index for index, message in enumerate(messages) if isinstance(message, UserMessage)), default=-1)
    multimodal_results = _major_version(model) is None or (_major_version(model) or 0) >= 3
    contents: list[dict[str, Any]] = []
    # Gemini before 3 reads a result's images only from a user content of their own, which must follow the whole
    # step's results: a content between two of them splits a parallel batch, and Gemini refuses that.
    deferred_images: list[dict[str, Any]] = []

    def flush_images() -> None:
        if deferred_images:
            contents.append({"role": "user", "parts": [{"text": "Tool result image:"}, *deferred_images]})
            deferred_images.clear()

    for position, message in enumerate(messages):
        if not isinstance(message, ToolResultMessage):
            flush_images()
        if isinstance(message, UserMessage):
            contents.append({"role": "user", "parts": _user_parts(message.content, supports_images, loaded_images)})
        elif isinstance(message, AssistantMessage):
            parts = _model_parts(message, current_turn=position > turn_start)
            if parts:
                contents.append({"role": "model", "parts": parts})
        elif isinstance(message, ToolResultMessage):
            response, images = _function_response(message, supports_images, loaded_images)
            if images and multimodal_results:
                response["functionResponse"]["parts"] = images
            last = contents[-1] if contents else None
            # One user content holds a step's results in call order; Gemini refuses them interleaved with calls.
            if last is not None and last["role"] == "user" and all("functionResponse" in part for part in last["parts"]):
                last["parts"].append(response)
            else:
                contents.append({"role": "user", "parts": [response]})
            if images and not multimodal_results:
                deferred_images.extend(images)
        else:
            raise TypeError(f"unsupported message {type(message).__name__}")
    flush_images()
    return contents


def _user_parts(
    content: tuple[Any, ...],
    supports_images: bool,
    loaded_images: Mapping[str, LoadedImage] | None,
) -> list[dict[str, Any]]:
    parts = [
        {"text": part["text"]} if part["type"] == "text"
        else {"inlineData": {"mimeType": part["mime_type"], "data": part["data"]}}
        for part in content_parts(content, include_images=supports_images, loaded_images=loaded_images)
    ]
    return parts or [{"text": "(no content)"}]


def _model_parts(message: AssistantMessage, *, current_turn: bool) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    first_call = True
    for block in message.content:
        if isinstance(block, TextBlock):
            if block.text:
                parts.append({"text": block.text})
        elif isinstance(block, ThinkingBlock):
            signature = _signature(block.signature)
            if block.text:
                part: dict[str, Any] = {"text": block.text, "thought": True}
            elif signature is None:
                continue
            elif parts and set(parts[-1]) == {"text"}:
                # An answer's signature, kept beside its text: back on the part it came on.
                parts[-1]["thoughtSignature"] = signature
                continue
            else:
                part = {"text": ""}
            if signature is not None:
                part["thoughtSignature"] = signature
            parts.append(part)
        elif isinstance(block, ToolCallBlock):
            call: dict[str, Any] = {"functionCall": {"name": block.name, "args": dict(block.arguments), "id": block.id}}
            signature = _signature(block.signature)
            if signature is None and first_call and current_turn:
                # Gemini validates the first call of every step in the current turn; one it did not sign is another
                # origin's, which Google's documented stand-in lets through.
                signature = DUMMY_THOUGHT_SIGNATURE
            if signature is not None:
                call["thoughtSignature"] = signature
            first_call = False
            parts.append(call)
        elif isinstance(block, HostedToolBlock):
            # Its toolCall and toolResponse parts, verbatim with their signatures (transform allowed it for this
            # Source); Gemini circulates them as its own.
            parts.extend(copy.deepcopy(list(block.native.payload)))
    return parts


def _function_response(
    message: ToolResultMessage,
    supports_images: bool,
    loaded_images: Mapping[str, LoadedImage] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    text = "\n".join(block.text for block in message.content if isinstance(block, TextBlock) and block.text is not None)
    images = [
        {"inlineData": {"mimeType": part["mime_type"], "data": part["data"]}}
        for part in content_parts(
            tuple(block for block in message.content if isinstance(block, ImageBlock)),
            include_images=supports_images,
            loaded_images=loaded_images,
        )
        if part["type"] == "image"
    ]
    value = text or ("(see attached image)" if images else "(no tool output)")
    response = {
        "functionResponse": {
            "name": message.tool_name,
            "id": message.tool_call_id,
            "response": {"error": value} if message.is_error else {"output": value},
        },
    }
    return response, images


def _signature(value: str | None) -> str | None:
    """A signature Gemini can take back: well-formed base64, which a Chat reasoning marker in the transcript is not."""

    if value and len(value) % 4 == 0 and _SIGNATURE.fullmatch(value):
        return value
    return None


def _major_version(model: str) -> int | None:
    match = _MAJOR_VERSION.match(model.lower())
    return int(match.group(1)) if match else None


def _thinking_config(model: str, effort: str | None) -> dict[str, Any] | None:
    """Thoughts are always asked for on a thinking model; the effort sets its level or budget."""

    name = model.lower()
    if _LEVEL_MODEL.match(name):
        config: dict[str, Any] = {"includeThoughts": True}
        level = _LEVELS.get(effort or "") or ("minimal" if effort == "none" else None)
        if level == "minimal" and "flash" not in name:
            # Only the Flash family thinks at MINIMAL; Pro-class models refuse it, so Google's own OpenAI-compatible
            # layer sends LOW for them, and so does this.
            level = "low"
        if level is not None:
            config["thinkingLevel"] = level
        return config
    if _BUDGET_MODEL.match(name):
        config = {"includeThoughts": True}
        if effort in _BUDGETS:
            config["thinkingBudget"] = _BUDGETS[effort]
            if _BUDGETS[effort] == 0:
                config["includeThoughts"] = False
        return config
    return None


def _normalize_stop(reason: str, message: str | None) -> tuple[str, str | None]:
    if reason == "STOP":
        return "stop", None
    if reason == "MAX_TOKENS":
        return "length", None
    detail = f"Gemini finished with {reason}" + (f": {message}" if message else "")
    return ("safety" if reason in _SAFETY_FINISH else "error"), detail


def _google_usage(value: Mapping[str, Any]) -> Any:
    """Project ``usageMetadata``: the prompt count includes cached tokens, and thoughts are counted apart from the
    candidates, so output is both (as every other adapter's output includes reasoning)."""

    from core.agent_core.messages import Usage

    cached = _count(value.get("cachedContentTokenCount"))
    thoughts = value.get("thoughtsTokenCount")
    return Usage(
        input_tokens=max(0, _count(value.get("promptTokenCount")) - cached),
        output_tokens=_count(value.get("candidatesTokenCount")) + _count(thoughts),
        cache_read_tokens=cached,
        reasoning_tokens=_count(thoughts) if thoughts is not None else None,
    )


def _effort(effort: str) -> str | None:
    value = effort.lower()
    return None if not value or value in {"off", "disabled"} else value


def sent_effort(model_id: str, effort: str) -> str | None:
    """The effort token a request to ``model_id`` carries for ``effort``: its thinking level, or the effort its thinking
    budget stands for. Model Hub keys effort evidence by it, and the gateway reads it back from the request
    (``wire_effort``), so a refusal governs every effort that sends the same thing."""

    return _wire_effort((_thinking_config(model_id, _effort(effort)) or {}))


def wire_effort(request: Mapping[str, Any]) -> str | None:
    """The effort token a Gemini request body carries (``sent_effort``), or None without a thinking control."""

    generation = request.get("generationConfig")
    thinking = generation.get("thinkingConfig") if isinstance(generation, Mapping) else None
    return _wire_effort(thinking) if isinstance(thinking, Mapping) else None


def _wire_effort(thinking: Mapping[str, Any]) -> str | None:
    level = thinking.get("thinkingLevel")
    if isinstance(level, str) and level:
        return level.lower()
    budget = thinking.get("thinkingBudget")
    if type(budget) is int:
        return _BUDGET_EFFORTS.get(budget, str(budget))
    return None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


GoogleProvider = GoogleAdapter
