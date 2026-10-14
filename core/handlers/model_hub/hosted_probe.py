"""The hosted-tool probes (``vibey-hosted-tools.md`` §3.2, capability model §3.3).

A probe is the production request. Vibey's own builder makes it, from a probe
resolution that pins the probed (Source, model) and carries the overlay's native
declaration whatever the current evidence. Two things differ from a real turn:
the transcript is one fixed user message, and on protocols that allow it the
hosted tool is forced. Vibey's adapter reads the response, so a result is
recognized only as the ``HostedToolBlock`` a real turn would keep. Nothing here
parses a provider's wire format.

The ``call`` probe asks the model to use the tool. When it verifies, the
``replay`` probe sends that turn back with the tool's native payload in place,
as Vibey replays history to the Source that produced it, and asks for a result
without using the tool again.

Every request the adapter sends runs on exactly one Source through Model Hub's
own invocation, with no route and no failover. This module only builds the
request, serves each pinned call to the adapter, and names the outcome; the
service owns the calls themselves.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator, Awaitable, Callable, Final, Literal, Optional, Protocol

import httpx

from config.v2_config import ModelHubSourceConfig
from core.agent_core.ai.provider import (
    RETRYABLE_ERROR_KINDS,
    Done,
    HostedToolRoute,
    ModelEndpoint,
    ModelRequest,
    ProviderError,
)
from core.agent_core.ai.registry import create_adapter
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import (
    IMAGE_MIME_TYPES,
    AssistantMessage,
    HostedImage,
    HostedToolBlock,
    TextBlock,
    UserMessage,
)
from core.agent_core.tools.hosted import hosted_tool_spec as canonical_tool_spec
from core.agent_core.tools.read import detect_supported_image_mime_type
from vibe.backend_model_catalog import HostedToolSpec

from .adapter import InvokeHandle, RawCallOutcome, RawOutcomeKind
from .classification import classify_outcome
from .evidence_rules import LearnedEvidence
from .stream_wire import ProtocolUsageReport

ProbeOutcome = Literal["verified", "unsupported", "inconclusive", "transient"]

# Every catalog tool: Vibey's adapters parse each one's results.
PROBED_TOOLS: tuple[str, ...] = ("web_search", "x_search", "image_generation")

# The web search asks for fresh articles: the replay probe asks about the other results, and a model given only the
# first could infer the rest of a fixed topic's results (its site's other pages), but not a page published after it
# was trained.
_MESSAGES = {
    "web_search": "Search the web for a news article published today about technology and reply with its URL.",
    "x_search": "Search X for posts about 'Avibe local-first agent OS' and reply with the first post's URL.",
    "image_generation": "Generate a small, simple image: a blue circle on a white background.",
}
# The replay probe's question: one a model can answer only from the results the replayed payload holds, since the
# answer it already gave shows just the first.
_REPLAY_MESSAGE = (
    "Without using any tool again, list the URLs of the other results you found, beyond the one you already gave."
)
_SYSTEM = "You check that a tool works: use it, then answer in one line."
# Room for a reasoning model to think, call the tool and answer in one line; a hop whose Vibey turns ask for less
# gets that instead.
PROBE_MAX_OUTPUT_TOKENS = 4096
# What one call probe is estimated to use, for its cost hint before it runs: the
# fixed message and the tool's results in context, a one-line answer, and one
# search (an X search reads up to ten posts). The 2026-10-13 spike measured
# such probes at about $0.01-0.11.
ESTIMATED_USAGE = ProtocolUsageReport.of(input_tokens=8000, cached_input_tokens=0, output_tokens=500)
ESTIMATED_COUNTS: dict[str, dict[str, int]] = {
    "web_search": {"web_search_calls": 1, "web_search_requests": 1},
    "x_search": {"x_search_calls": 1, "x_posts_fetched": 10, "x_users_fetched": 0},
    "image_generation": {},
}
# What the replay probe is estimated to use: the call probe's turn with its results, a question, and a one-line
# answer, with no tool fee (the answer uses no tool).
ESTIMATED_REPLAY_USAGE = ProtocolUsageReport.of(input_tokens=12000, cached_input_tokens=0, output_tokens=300)
# The protocols whose adapters send a hosted call's native payload back in place (``transform.native_replay``).
NATIVE_REPLAY_PROTOCOLS: frozenset[str] = frozenset({"anthropic", "openai_responses"})
# The adapter's requests never leave this process: the transport serves each
# with a Model Hub call pinned to the probed Source.
_IN_PROCESS_URL = "http://model-hub-probe.invalid"
# The classified failures a later try may not meet: a dropped connection or a
# timeout, an overload or 5xx, and a rate limit.
_TRANSIENT_REASONS = frozenset({"network", "server_error", "rate_limited"})


def probe_request(
    source: ModelHubSourceConfig, model_id: str, tool: str, spec: HostedToolSpec, *, max_tokens: int
) -> ModelRequest:
    """The call probe for ``tool`` on this Source and model, as Vibey would send it natively.

    It matches a real turn's declaration, include and provider shaping, and Vibey's own adapter and transport send
    it; ``max_tokens`` is the smallest output any Vibey turn on this hop would ask for, at most
    ``PROBE_MAX_OUTPUT_TOKENS``.
    """

    route = HostedToolRoute(
        mode="native",
        executor=None,
        replay=False,
        declaration=spec.declaration,
        include=tuple(spec.include),
    )
    return ModelRequest(
        # As the Vibey launch builds a hop's endpoint: the Source's vendor is its provider, which shapes the
        # request (Anthropic's one-hour cache lifetime), and the model is the one the Source serves.
        endpoint=ModelEndpoint(
            protocol=source.protocol,  # type: ignore[arg-type]
            base_url=_IN_PROCESS_URL,
            model_id=model_id,
            token="",
            provider=source.vendor,
            source_id=source.id,
            hosted_tools={tool: route},
        ),
        system=_SYSTEM,
        messages=(UserMessage(content=(TextBlock(text=_MESSAGES[tool]),)),),
        tools=(canonical_tool_spec(tool),),
        max_tokens=max_tokens,
        # Current Claude models refuse a forced choice, so Anthropic is asked in the message alone.
        tool_choice=None if source.protocol == "anthropic" else tool,
    )


def replay_request(call: ModelRequest, tool: str, answered: AssistantMessage) -> ModelRequest:
    """The replay probe after a verified ``call`` probe: its turn sent back, with the native declaration still
    present and the hop allowed to take the tool's native payload, and a question only those results answer."""

    route = call.endpoint.hosted_tools[tool]
    return replace(
        call,
        endpoint=replace(
            call.endpoint, hosted_tools={tool: HostedToolRoute("native", None, True, route.declaration, route.include)}
        ),
        messages=(*call.messages, answered, UserMessage(content=(TextBlock(text=_REPLAY_MESSAGE),))),
        # A forced choice would ask for the very call the replay checks the model does not need.
        tool_choice=None,
    )


def hidden_result_urls(message: AssistantMessage) -> frozenset[str]:
    """The result URLs a response's hosted calls returned that its visible answer does not show.

    Only these prove a replay: a model that names one read the replayed native
    payload, since nothing else it was sent holds it. A URL the answer wrote or
    cited is visible text, which any request carries.
    """

    returned = {source.url for block in message.content if isinstance(block, HostedToolBlock) for source in block.record.sources}
    # Read generously, a trailing slash aside: a result the answer shows but the probe takes for hidden is what could
    # verify a replay falsely.
    shown = {key.rstrip("/") for key in written_urls(message)}
    return frozenset(url for url in returned if url and not {key.rstrip("/") for key in _readings(url)} & shown)


def written_urls(message: AssistantMessage) -> frozenset[str]:
    """Every URL a response's answer shows, as comparison keys: each reading of a URL its text writes
    (``_readings``) and each URL it cites, as given."""

    text = " ".join(block.text for block in message.content if isinstance(block, TextBlock) and block.text)
    keys = {key for url in _URL.findall(text) for key in _readings(url)}
    cited = [
        citation.url
        for block in message.content
        if isinstance(block, TextBlock) and block.citations
        for citation in block.citations
    ]
    if message.hosted_summary is not None:
        cited.extend(source.url for source in message.hosted_summary.cited_sources)
    keys.update(_same_url(url) for url in cited if url)
    return frozenset(keys)


def carries_native_payload(body: Any, message: AssistantMessage) -> bool:
    """Whether a request body sends every hosted call of ``message`` back verbatim, its provider items in place."""

    def canonical(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    # Every object the body holds, once, then a lookup per item: one walk, whatever the payload's size.
    held: set[str] = set()
    pending = [body]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            held.add(canonical(node))
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    items = [item for block in message.content if isinstance(block, HostedToolBlock) for item in block.native.payload]
    return bool(items) and all(canonical(item) in held for item in items)


class ProbeImageCheck:
    """The image probe's media sink: it checks a generated image is one Vibey can keep, and keeps nothing.

    A probe asks whether the tool works, so its image is decoded and recognized
    exactly as a turn's would be, and then discarded.
    """

    async def save_image(self, encoded: str) -> HostedImage:
        return await asyncio.to_thread(self._check, encoded)

    @staticmethod
    def _check(encoded: str) -> HostedImage:
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("the image is not valid base64") from None
        mime_type = detect_supported_image_mime_type(data)
        # The kinds a turn's sink keeps (``IMAGE_MIME_TYPES``, which ``MediaSnapshots`` stores); any other is refused there.
        if mime_type not in IMAGE_MIME_TYPES:
            raise ValueError("the image is not a PNG, JPEG, GIF or WebP")
        return HostedImage(path="probe:discarded", mime=mime_type, bytes=len(data))


# How long the UI process waits for any Model Hub RPC (``vibe/model_hub_client.py`` reads it from here). A probe
# answers inside it, with room for the RPC's own overhead.
MODEL_HUB_RPC_TIMEOUT_SECONDS: Final = 300.0
PROBE_BUDGET_SECONDS: Final = MODEL_HUB_RPC_TIMEOUT_SECONDS - 20.0
# The replay probe runs only with at least this long left; otherwise it reports a transient failure, sending nothing.
REPLAY_MIN_SECONDS: Final = 60.0

# A probe sends at most this many requests: its own, and the one resend an adapter makes when an endpoint refuses
# a field before answering (Anthropic's one-hour cache lifetime). Only an answered request runs the model.
MAX_PROBE_REQUESTS = 2

class CallResult(Protocol):
    """How one pinned call settled: the Source it ran on, its outcome, and whether it retired that Source's own
    pending verification marker (``ModelHubService._user_test_call``)."""

    source: ModelHubSourceConfig
    outcome: RawCallOutcome
    retired_own_marker: bool


# Model Hub's one pinned call for a request body: it opens the call on the probed Source, hands it to the reader,
# and settles and meters it once the reader returns.
PinnedCall = Callable[[dict[str, Any], Callable[[InvokeHandle], Awaitable[None]]], Awaitable[CallResult]]


@dataclass(frozen=True)
class ProbeCall:
    """One request the adapter sent, and how Model Hub's pinned call for it ended."""

    body: dict[str, Any]
    source: ModelHubSourceConfig
    outcome: RawCallOutcome
    # Proof that this call's own success retired the Source's pending verification.
    retired_own_marker: bool = False


class _CallStream(httpx.AsyncByteStream):
    """The adapter's side of one pinned call: what the call's own task read from its stream, in order.

    Only that task ever iterates the call's stream, so the call can settle (and
    close it) whatever the adapter is doing, a deadline included.
    """

    def __init__(self, chunks: asyncio.Queue, released: asyncio.Event) -> None:
        self._chunks = chunks
        self._released = released

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while (item := await self._chunks.get()) is not None:
            if isinstance(item, BaseException):
                raise item
            yield item

    async def aclose(self) -> None:
        self._released.set()


async def _pump(handle: InvokeHandle, chunks: asyncio.Queue) -> None:
    """Read the call's stream into ``chunks`` and always end the queue: on a failure the adapter meets the same error
    where it would have read it."""

    try:
        if handle.stream is not None:
            async for chunk in handle.stream:
                chunks.put_nowait(chunk)
    except Exception as failed:
        chunks.put_nowait(failed)
    finally:
        chunks.put_nowait(None)


def _failure_body(outcome: RawCallOutcome) -> dict[str, Any]:
    """What an adapter reads for a call that failed before any output: the upstream's status and its own words."""

    message = outcome.upstream_detail or outcome.redacted_message or "the upstream call failed"
    return {"type": "error", "error": {"type": outcome.error_type or outcome.error_code or "api_error", "message": message}}


class PinnedCallTransport(httpx.AsyncBaseTransport):
    """Serves every request the adapter sends with its own pinned call of the probed Source.

    The adapter runs exactly as it does for a real turn, resend included, and
    each of its requests is one Model Hub call on that Source alone, with no
    route and no failover. ``calls`` records them in order.
    """

    def __init__(self, call: PinnedCall) -> None:
        self._call = call
        # In request order: each request reserves its place before its call settles.
        self._slots: list[Optional[ProbeCall]] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._releases: list[asyncio.Event] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if len(self._tasks) >= MAX_PROBE_REQUESTS:
            raise RuntimeError("a probe sends at most two requests")
        body = json.loads(await request.aread())
        slot = len(self._slots)
        self._slots.append(None)
        opened: asyncio.Future[InvokeHandle] = asyncio.get_running_loop().create_future()
        released = asyncio.Event()
        self._releases.append(released)
        chunks: asyncio.Queue = asyncio.Queue()

        async def read(handle: InvokeHandle) -> None:
            opened.set_result(handle)
            # The call's own reader runs until the stream ends or the adapter closes its side, as production closes a
            # response it has finished with; it is always over before the call settles and closes the stream.
            pump = asyncio.create_task(_pump(handle, chunks))
            closed = asyncio.create_task(released.wait())
            try:
                await asyncio.wait({pump, closed}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                pump.cancel()
                closed.cancel()
                await asyncio.gather(pump, closed, return_exceptions=True)
            await released.wait()

        async def run() -> None:
            settled = await self._call(body, read)
            self._slots[slot] = ProbeCall(body, settled.source, settled.outcome, settled.retired_own_marker)

        task = asyncio.create_task(run())
        self._tasks.append(task)
        await asyncio.wait({opened, task}, return_when=asyncio.FIRST_COMPLETED)
        if not opened.done() and task.exception() is not None:
            # Refused before any call opened (the Source changed, the engine is down): ``settle`` raises it.
            raise RuntimeError("the probe's call did not open")
        if opened.done() and opened.result().stream is not None:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_CallStream(chunks, released))
        # The call failed before any output, or timed out before it opened: answer as the upstream did, so the
        # adapter decides what to do as it would for a real turn.
        released.set()
        await task
        outcome = self._slots[slot].outcome  # type: ignore[union-attr]
        status = outcome.http_status or (504 if outcome.kind is RawOutcomeKind.TIMEOUT else 502)
        return httpx.Response(status, json=_failure_body(outcome))

    @property
    def calls(self) -> list[ProbeCall]:
        """Every request the adapter sent whose call settled, in the order the adapter sent them."""

        return [call for call in self._slots if call is not None]

    async def settle(self) -> None:
        """Let every call finish settling; re-raise the first one Model Hub refused to open."""

        for released in self._releases:
            released.set()
        results = await asyncio.gather(*self._tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result


async def run_probe(
    request: ModelRequest, call: PinnedCall, *, media_sink: Optional[ProbeImageCheck] = None
) -> tuple[Done | ProviderError | None, list[ProbeCall]]:
    """Run Vibey's adapter for ``request`` over pinned calls; its terminal event and the calls it made."""

    transport = PinnedCallTransport(call)
    terminal: Done | ProviderError | None = None
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            # Only the image probe has an image to keep, and only the Responses adapter produces one.
            adapter = create_adapter(
                request.endpoint.protocol,
                client=client,
                gateway=False,
                **({"media_sink": media_sink} if media_sink is not None else {}),
            )
            async for event in adapter.stream(request, CancelToken()):
                if isinstance(event, (Done, ProviderError)):
                    terminal = event
    finally:
        await transport.settle()
    return terminal, transport.calls


def call_outcome(
    tool: str,
    terminal: Done | ProviderError | None,
    outcome: RawCallOutcome,
    learned: Optional[LearnedEvidence],
) -> ProbeOutcome:
    """What one call probe shows about ``tool`` on its Source and model.

    * ``unsupported``: only a 400 that names the tool's declared type, through the same narrow rule real requests
      learn from;
    * ``verified``: a completed response that carries the tool's own completed result;
    * ``inconclusive``: a completed response without one (the model may simply not have used the tool, which looks
      like a silent drop), or a request that failed for another reason;
    * ``transient``: an overload, a 5xx, a rate limit, a dropped connection or a timeout.

    Only the first two are evidence. A call that failed before any output has no
    ``terminal``; Model Hub's own reading of its ``outcome`` says which failure it was.
    """

    if learned is not None and (learned.feature.kind, learned.feature.name, learned.feature.aspect) == (
        "hosted_tool", tool, "call"
    ):
        return "unsupported"
    if isinstance(terminal, Done):
        used = any(
            isinstance(block, HostedToolBlock)
            and block.tool == tool
            and block.record.status == "completed"
            # A generated image counts only once Vibey could keep it.
            and (block.record.images is None or bool(block.record.images))
            for block in terminal.message.content
        )
        return "verified" if used else "inconclusive"
    return _failure(terminal, outcome)


def replay_outcome(
    tool: str,
    terminal: Done | ProviderError | None,
    outcome: RawCallOutcome,
    learned: Optional[LearnedEvidence],
    *,
    asked: frozenset[str],
    carried: bool,
) -> ProbeOutcome:
    """What one replay probe shows about sending ``tool``'s native payload back to its Source (§12 item 23).

    * ``unsupported``: a 400 naming the replayed content, through the narrow rule real requests learn from, or an
      answer that calls the tool again instead of using the results it holds;
    * ``verified``: an answer that names a URL only those results hold (``asked``, ``hidden_result_urls``) without
      calling the tool;
    * ``inconclusive``: an answer that names none, or a refusal, and any request that did not carry the native
      payload verbatim (``carried``), since it tested nothing;
    * ``transient``: as for the call probe.
    """

    if not carried:
        # A request that did not send the payload verbatim tested nothing, whatever the provider said about it.
        return "inconclusive"
    if learned is not None and (learned.feature.kind, learned.feature.name, learned.feature.aspect) == (
        "hosted_tool", tool, "replay"
    ):
        return "unsupported"
    if isinstance(terminal, Done):
        message = terminal.message
        if any(isinstance(block, HostedToolBlock) and block.tool == tool for block in message.content):
            return "unsupported"
        if message.stop_reason in ("refusal", "safety"):
            # A refused or blocked answer proves nothing, whatever it quotes.
            return "inconclusive"
        named = written_urls(message) & {_same_url(url) for url in asked}
        return "verified" if named else "inconclusive"
    return _failure(terminal, outcome)


# A URL as an answer writes it: up to the whitespace, quote or bracket that ends it. Parentheses stay, since a URL
# may hold them; one that closes the prose around it is trailing text.
_URL = re.compile(r"https?://[^\s<>\[\]{}\"'`]+", re.IGNORECASE)
# What prose and Markdown can leave right after a URL, and a URL can also end in.
_TRAILING = ".,;:!?)*_"


def _readings(written: str) -> frozenset[str]:
    """The comparison keys a URL written in prose can stand for: as written, and without each run of the trailing
    text a sentence may leave on it. A provider's URL is exact; only where prose ends a URL is ambiguous."""

    readings = {_same_url(written)}
    while written and written[-1] in _TRAILING:
        written = written[:-1]
        readings.add(_same_url(written))
    return frozenset(readings)


def _same_url(url: str) -> str:
    """A URL as a comparison key: exactly as given but for what RFC 3986 makes equivalent, the case of its scheme and
    host and an empty path, which is ``/``. Anything else, such as a trailing slash or the case of its user
    information, names another URL."""

    scheme, separator, rest = url.partition("://")
    if not separator:
        return url
    authority = _AUTHORITY.match(rest)[0]
    path = rest[len(authority):]
    userinfo, at, host = authority.rpartition("@")
    return f"{scheme.lower()}://{userinfo}{at}{host.lower()}{path if path.startswith('/') else '/' + path}"


_AUTHORITY = re.compile(r"[^/?#]*")


def _failure(terminal: ProviderError | None, outcome: RawCallOutcome) -> ProbeOutcome:
    """A probe request that failed: ``transient`` when a later try may not meet the same failure."""

    if terminal is not None:
        # By what failed, not by ``retryable``: an adapter never retries a response that already streamed output,
        # but a dropped connection or an overload is just as transient for a probe the user can run again.
        return "transient" if terminal.kind in RETRYABLE_ERROR_KINDS else "inconclusive"
    return "transient" if classify_outcome(outcome).reason in _TRANSIENT_REASONS else "inconclusive"
