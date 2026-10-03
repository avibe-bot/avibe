"""Model selection and retry policy owned by the loop, not a provider."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Optional, Protocol

from core.agent_core.ai.provider import (
    ModelCapabilities,
    ModelEndpoint,
    ProviderAdapter,
    ProviderError,
)
from core.agent_core.messages import ProtocolName

DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_MAX_OUTPUT_TOKENS = 8_192


@dataclass(frozen=True)
class ModelSelection:
    endpoint: ModelEndpoint
    capabilities: ModelCapabilities

    @property
    def context_window(self) -> int:
        """Effective C-9 budget; preserve nullable source capabilities."""
        value = self.capabilities.context_window
        return DEFAULT_CONTEXT_WINDOW if value is None else value

    @property
    def max_output_tokens(self) -> int:
        value = self.capabilities.max_output_tokens
        return DEFAULT_MAX_OUTPUT_TOKENS if value is None else value


class ModelRouter(Protocol):
    async def resolve(self) -> ModelSelection:
        """Resolve the next call, including a fresh resolution after a retry."""
        ...

    def provider_for(self, protocol: ProtocolName) -> ProviderAdapter:
        """Select transport after before_model has had a chance to change it."""
        ...


@dataclass(frozen=True)
class RetryPolicy:
    """C-2: at most three extra attempts within 120 s; Retry-After is a floor."""

    max_retries: int = 2
    initial_delay_s: float = 0.5
    max_delay_s: float = 8.0
    max_elapsed_s: float = 120.0

    def __post_init__(self) -> None:
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 3:
            raise ValueError("max_retries must be an integer between 0 and 3")
        if any(
            not isfinite(value) or value < 0 for value in (self.initial_delay_s, self.max_delay_s, self.max_elapsed_s)
        ):
            raise ValueError("retry delays and budget must be finite and nonnegative")
        if self.max_elapsed_s > 120:
            raise ValueError("max_elapsed_s cannot exceed 120 seconds")

    def delay(self, error: ProviderError, *, retries: int, streamed: bool, elapsed_s: float = 0) -> Optional[float]:
        if (
            retries >= self.max_retries
            or not error.retryable
        ):
            return None
        # ``ProviderError.retryable`` is the provider boundary decision. The
        # loop only applies the bounded count/time budget; it must not infer a
        # second retry boundary from a partial message or its own event count.
        del streamed
        backoff = min(self.max_delay_s, self.initial_delay_s * 2**retries)
        delay = max(backoff, error.retry_after_s or 0.0)
        return delay if elapsed_s + delay < self.max_elapsed_s else None
