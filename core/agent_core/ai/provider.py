"""C-2 provider adapter interface (``agent-core-contracts/provider.md``).

One adapter per wire protocol. An adapter speaks HTTP to the endpoint it is
given, converts canonical messages with the cross-provider rules, and yields
``ProviderEvent``s ending in exactly one ``Done`` or ``ProviderError``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Mapping, Optional, Protocol, Union

from core.agent_core.cancel import CancelToken
from core.agent_core.messages import AssistantMessage, HostedImage, Message, ProtocolName
from core.agent_core.tools.base import ToolSpec

ErrorKind = Literal[
    "overflow",
    "rate_limit",
    "overloaded",
    "network",
    "stalled",
    "server",
    # The provider's stream ended after output without a terminal (Model Hub's ``modelHub.errors.stream_interrupted``).
    "interrupted",
    "auth",
    "invalid_request",
    "aborted",
    "unknown",
]
RETRYABLE_ERROR_KINDS: frozenset[str] = frozenset(
    {"rate_limit", "overloaded", "network", "stalled", "server", "interrupted"}
)
#: A response that dropped after streaming thinking alone is retried once for these (owner decision, 2026-10-11):
#: a transient failure, or one nothing classified. Never for a rate limit, an overflow, auth, or a rejected request.
THINKING_DROP_RETRY_KINDS: frozenset[str] = frozenset(
    {"overloaded", "network", "stalled", "server", "interrupted", "unknown"}
)


@dataclass(frozen=True)
class HostedToolRoute:
    """How a hop sends one enabled search tool, from its C-6 facts (``HopResolution.hosted_tools``,
    ``vibey-hosted-tools.md`` §3.4, §4.1).

    The agent declares the tool natively exactly when Model Hub assumes the hop runs it (``native: assumed``), with
    the wire ``declaration`` and the Responses ``include`` values; otherwise (``proxy``) as the same-named function,
    which the gateway's executor chain runs. ``replay`` is whether a hosted payload may go back verbatim to this hop.
    ``refused``: the hop's own Source would run the tool but refused it (a current refusal, C-6 ``refused``), so a call
    here is a fallback from that Source.
    """

    mode: Literal["native", "proxy"]
    replay: bool
    declaration: Optional[Mapping[str, Any]] = None
    include: tuple[str, ...] = ()
    refused: bool = False

    def __post_init__(self) -> None:
        if self.mode not in ("native", "proxy"):
            raise ValueError(f"unknown hosted tool mode: {self.mode!r}")
        if not isinstance(self.replay, bool) or not isinstance(self.refused, bool):
            raise ValueError("hosted replay and refused must be booleans")
        if self.refused and self.mode != "proxy":
            raise ValueError("a tool its Source refused runs as its function")
        if self.mode == "proxy":
            if self.declaration is not None or self.include:
                raise ValueError("a proxy hosted tool has no native declaration or include")
            return
        declaration = self.declaration
        if not isinstance(declaration, Mapping) or not isinstance(declaration.get("type"), str) or not declaration["type"]:
            raise ValueError("a native hosted tool needs a declaration with a type")
        if not isinstance(self.include, tuple) or not all(isinstance(item, str) and item for item in self.include):
            raise ValueError("hosted include must be a tuple of strings")


@dataclass(frozen=True)
class ImageModel:
    """One image model ``image_generate`` may call (C-6 ``HopResolution.hosted_tools.image_generate``, §7.2): its
    Source, its id, one short description line, and the optional arguments it takes."""

    source_id: str
    model_id: str
    description: str
    params: tuple[str, ...]
    size_forms: tuple[str, ...]


@dataclass(frozen=True)
class ImageModels:
    """The turn's image models, default first, as the ``image_generate`` enum lists them."""

    models: tuple[ImageModel, ...]

    def __post_init__(self) -> None:
        if not self.models or not all(isinstance(model, ImageModel) for model in self.models):
            raise ValueError("image models are a non-empty tuple of ImageModel")


@dataclass(frozen=True)
class ModelEndpoint:
    protocol: ProtocolName
    base_url: str
    model_id: str
    token: str = field(repr=False)
    request_headers: Mapping[str, str] = field(default_factory=dict)
    provider: str = ""
    #: Route-local cache lifetime override. The Model Hub validates provider
    #: semantics before it reaches the adapter; unknown/relay routes use it only
    #: for C-9 clearing, while official OpenAI Responses routes may also wire it.
    cache_ttl: Optional[Literal["5m", "30m", "1h"]] = None
    #: True only when Model Hub confirmed that the provider-specific TTL can be
    #: sent on the wire (currently official OpenAI Responses GPT-5.6+).
    cache_ttl_wire: bool = False
    #: The hop's Source (``HopResolution.source_id``): the pin of a request that carries native hosted content.
    source_id: Optional[str] = None
    #: That Source's credential-free identity (``HopResolution.source_identity``): the account a response's opaque
    #: payloads are bound to (``cross-provider.md``).
    source_identity: Optional[str] = None
    #: A portable request (§3.5, ``portable_endpoint``): never pinned, and no native hosted content. Its Source and
    #: identity stay, so a response its own Source serves is still attributed to them.
    portable: bool = False
    #: The opaque payloads, by (Source identity, kind), that go nowhere: their provider refused one in this Session
    #: (§3.5). A message that identity served is sent without them, as a different origin's would be.
    opaque_refused: frozenset[tuple[str | None, str]] = frozenset()
    #: Each enabled search tool's route on this hop, in Model Hub's order; empty when none is enabled.
    hosted_tools: Mapping[str, HostedToolRoute] = field(default_factory=dict)
    #: The models ``image_generate`` may call; None when the tool is off or no model is available.
    image_models: Optional[ImageModels] = None
    #: The upstream protocol whose response origin and tokenizer anchor C-9 for a converted hop; ``None`` when the
    #: wire protocol is also the served protocol.
    origin_api: Optional[ProtocolName] = None


@dataclass(frozen=True)
class ModelCapabilities:
    """From C-6 hop resolution. ``None`` means Model Hub does not know; callers treat it conservatively."""

    context_window: Optional[int] = None
    input_limit: Optional[int] = None
    max_output_tokens: Optional[int] = None
    supports_tools: Optional[bool] = None
    supports_images: Optional[bool] = None
    supports_reasoning: Optional[bool] = None
    reasoning_efforts: tuple[str, ...] = ()
    #: The context sizes past which the model's list price steps up, ascending; empty when flat or unknown. C-9 keeps
    #: the compaction threshold below the smallest one it can (``context.md`` section 1).
    context_price_steps: tuple[int, ...] = ()


class MediaLoader(Protocol):
    """Supplied by the adapter layer; resolves an ``ImageBlock.media_token`` to its bytes at request time."""

    async def load(self, media_token: str) -> tuple[bytes, str]:
        """Return the bytes and their mime type."""
        ...


class MediaSink(Protocol):
    """Supplied by the adapter layer; keeps a generated image as a local file (``vibey-hosted-tools.md`` §7.3).

    The image never enters a message or the model's context: only its path, in the call's record.
    """

    async def save_image(self, encoded: str) -> HostedImage:
        """Decode base64 ``encoded``, store it, and return its record: the file's absolute path, its type as its
        bytes show it, and its size. Raises ``ValueError`` when ``encoded`` is not a supported image in base64, and
        ``OSError`` when it cannot be stored."""
        ...


@dataclass(frozen=True)
class ModelRequest:
    endpoint: ModelEndpoint
    system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]
    max_tokens: int
    reasoning_effort: Optional[str] = None
    cache: Literal["default", "none"] = "default"
    supports_images: bool = False
    """Resolved by the loop from ``ModelCapabilities``; an unknown capability counts as False."""


@dataclass(frozen=True)
class TextDelta:
    index: int
    delta: str


@dataclass(frozen=True)
class ThinkingDelta:
    index: int
    delta: str


@dataclass(frozen=True)
class ToolCallStart:
    index: int
    id: str
    name: str


@dataclass(frozen=True)
class ToolCallDelta:
    index: int
    arguments_delta: str


@dataclass(frozen=True)
class BlockEnd:
    index: int


@dataclass(frozen=True)
class Done:
    message: AssistantMessage


@dataclass(frozen=True)
class MalformedToolCall:
    """A provider tool call whose final JSON arguments were not a JSON object.

    This carries only canonical call identity. The malformed provider bytes never
    cross the adapter boundary or enter the transcript.
    """

    id: str
    name: str


@dataclass(frozen=True)
class ProviderError:
    kind: ErrorKind
    message: str
    retryable: bool
    retry_after_s: Optional[float] = None
    status: Optional[int] = None
    partial: Optional[AssistantMessage] = None
    #: The response streamed thinking and nothing else before this error (``THINKING_DROP_RETRY_KINDS``): it is
    #: ``retryable``, and the loop retries it at most once per request (``provider.md``).
    after_thinking: bool = False
    #: A refusal before any output, which the loop answers by rendering the request again (``vibey-hosted-tools.md``
    #: §3.5), never by the generic retry.
    refusal: Optional["Refusal"] = None
    #: Permission to continue from the committed transcript, not replay this request. False by default:
    #: only a classified transient failure grants it, even when streaming makes ``retryable`` false.
    continuable: bool = False
    #: Malformed final arguments are still strict protocol errors, but this
    #: narrow metadata grants Vibey one transcript-only recovery. The partial
    #: contains safe empty arguments for these calls; the loop must not execute them.
    malformed_tool_calls: tuple[MalformedToolCall, ...] = ()


@dataclass(frozen=True)
class Refusal:
    """Why a request was refused before any output, so the next attempt renders it differently (§3.5).

    ``pin_unavailable``: the gateway handed a pinned request back; ``tool`` and ``aspect`` name the narrow hosted rule
    that fired, when one did. ``opaque_refused``: the provider refused an opaque payload a response of this Source
    identity minted, replayed under an account that cannot read it (a key change, or a relay serving from another
    account); ``payloads`` says which kind (``OPAQUE_PAYLOADS``): Anthropic names it, xAI's "Could not decrypt the
    provided encrypted_content" does not, so it is both. ``source_id`` and ``source_identity`` are the refused
    request's hop.
    """

    kind: Literal["pin_unavailable", "opaque_refused"]
    source_id: Optional[str] = None
    source_identity: Optional[str] = None
    tool: Optional[str] = None
    aspect: Optional[str] = None
    payloads: tuple["OpaquePayload", ...] = ()


#: The kinds of opaque payload a response carries for its own account: ``hosted``, a hosted call's native payload
#: (Anthropic's encrypted search results); ``signature``, signed or redacted thinking and other per-block signatures.
OpaquePayload = Literal["hosted", "signature"]
OPAQUE_PAYLOADS: tuple[OpaquePayload, ...] = ("hosted", "signature")


ProviderEvent = Union[TextDelta, ThinkingDelta, ToolCallStart, ToolCallDelta, BlockEnd, Done, ProviderError]


class ProviderAdapter(Protocol):
    protocol: ProtocolName

    def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ProviderEvent]: ...
