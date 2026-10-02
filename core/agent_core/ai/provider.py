"""C-2 provider adapter interface (``agent-core-contracts/provider.md``).

One adapter per wire protocol. An adapter speaks HTTP to the endpoint it is
given, converts canonical messages with the cross-provider rules, and yields
``ProviderEvent``s ending in exactly one ``Done`` or ``ProviderError``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Mapping, Optional, Protocol, Union

from core.agent_core.cancel import CancelToken
from core.agent_core.messages import AssistantMessage, Message, ProtocolName
from core.agent_core.tools.base import ToolSpec

ErrorKind = Literal[
    "overflow", "rate_limit", "overloaded", "network", "server", "auth", "invalid_request", "aborted", "unknown"
]
RETRYABLE_ERROR_KINDS: frozenset[str] = frozenset({"rate_limit", "overloaded", "network", "server"})


@dataclass(frozen=True)
class ModelEndpoint:
    protocol: ProtocolName
    base_url: str
    model_id: str
    token: str = field(repr=False)
    request_headers: Mapping[str, str] = field(default_factory=dict)
    provider: str = ""


@dataclass(frozen=True)
class ModelRequest:
    endpoint: ModelEndpoint
    system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]
    max_tokens: int
    reasoning_effort: Optional[str] = None
    cache: Literal["default", "none"] = "default"
    supports_images: bool = True


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
class ProviderError:
    kind: ErrorKind
    message: str
    retryable: bool
    retry_after_s: Optional[float] = None
    status: Optional[int] = None
    partial: Optional[AssistantMessage] = None


ProviderEvent = Union[TextDelta, ThinkingDelta, ToolCallStart, ToolCallDelta, BlockEnd, Done, ProviderError]


class ProviderAdapter(Protocol):
    protocol: ProtocolName

    def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ProviderEvent]: ...
