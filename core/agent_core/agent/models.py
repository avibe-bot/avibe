"""Model selection and retry policy owned by the loop, not a provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from core.agent_core.ai.provider import (
    RETRYABLE_ERROR_KINDS,
    ModelEndpoint,
    ProviderAdapter,
    ProviderError,
)
from core.agent_core.messages import ProtocolName


@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int
    max_output_tokens: int
    input_limit: Optional[int] = None
    supports_tools: bool = True
    supports_images: bool = True
    supports_reasoning: bool = False
    reasoning_efforts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelSelection:
    endpoint: ModelEndpoint
    capabilities: ModelCapabilities


class ModelRouter(Protocol):
    async def resolve(self) -> ModelSelection:
        """Resolve the next call, including a fresh resolution after a retry."""
        ...

    def provider_for(self, protocol: ProtocolName) -> ProviderAdapter:
        """Select transport after before_model has had a chance to change it."""
        ...


@dataclass(frozen=True)
class RetryPolicy:
    """At most ``max_retries`` extra attempts; Retry-After is a lower bound."""

    max_retries: int = 2
    initial_delay_s: float = 0.5
    max_delay_s: float = 8.0

    def __post_init__(self) -> None:
        if self.max_retries < 0 or self.initial_delay_s < 0 or self.max_delay_s < 0:
            raise ValueError("retry counts and delays must be nonnegative")

    def delay(self, error: ProviderError, *, retries: int, streamed: bool) -> Optional[float]:
        if (
            retries >= self.max_retries
            or streamed
            or error.partial is not None
            or not error.retryable
            or error.kind not in RETRYABLE_ERROR_KINDS
        ):
            return None
        backoff = min(self.max_delay_s, self.initial_delay_s * 2**retries)
        return max(backoff, error.retry_after_s or 0.0)
