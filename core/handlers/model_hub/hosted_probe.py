"""The hosted-tool call probe (``vibey-hosted-tools.md`` §3.2, capability model §3.3).

A probe is the production request. Vibey's own builder makes it, from a probe
resolution that pins the probed (Source, model) and carries the overlay's native
declaration whatever the current evidence. Two things differ from a real turn:
the transcript is one fixed user message, and on protocols that allow it the
hosted tool is forced. Vibey's adapter reads the response, so a result is
recognized only as the ``HostedToolBlock`` a real turn would keep. Nothing here
parses a provider's wire format.

Every request the adapter sends runs on exactly one Source through Model Hub's
own invocation, with no route and no failover. This module only builds the
request, serves each pinned call to the adapter, and names the outcome; the
service owns the calls themselves.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Optional, Protocol

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
from core.agent_core.messages import HostedToolBlock, TextBlock, UserMessage
from core.agent_core.tools.hosted import hosted_tool_spec as canonical_tool_spec
from vibe.backend_model_catalog import HostedToolSpec

from .adapter import InvokeHandle, RawCallOutcome, RawOutcomeKind
from .classification import classify_outcome
from .evidence_rules import LearnedEvidence
from .stream_wire import ProtocolUsageReport

ProbeOutcome = Literal["verified", "unsupported", "inconclusive", "transient"]

# The tools whose results Vibey's adapters parse today. ``image_generation``
# joins when its results are parsed: until then a probe could never see one,
# and every image probe would read as the model not using the tool.
PROBED_TOOLS: tuple[str, ...] = ("web_search", "x_search")

_MESSAGES = {
    "web_search": "Search the web for 'Avibe local-first agent OS' and reply with the first result's URL.",
    "x_search": "Search X for posts about 'Avibe local-first agent OS' and reply with the first post's URL.",
}
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
}
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
    """Read the call's stream to its end into ``chunks``, so the call settles as it ended upstream, and always end
    the queue: on a failure the adapter meets the same error where it would have read it."""

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
            await _pump(handle, chunks)
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


async def run_probe(request: ModelRequest, call: PinnedCall) -> tuple[Done | ProviderError | None, list[ProbeCall]]:
    """Run Vibey's adapter for ``request`` over pinned calls; its terminal event and the calls it made."""

    transport = PinnedCallTransport(call)
    terminal: Done | ProviderError | None = None
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            adapter = create_adapter(request.endpoint.protocol, client=client, gateway=False)
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
            isinstance(block, HostedToolBlock) and block.tool == tool and block.record.status == "completed"
            for block in terminal.message.content
        )
        return "verified" if used else "inconclusive"
    if terminal is not None:
        # By what failed, not by ``retryable``: an adapter never retries a response that already streamed output,
        # but a dropped connection or an overload is just as transient for a probe the user can run again.
        return "transient" if terminal.kind in RETRYABLE_ERROR_KINDS else "inconclusive"
    return "transient" if classify_outcome(outcome).reason in _TRANSIENT_REASONS else "inconclusive"
