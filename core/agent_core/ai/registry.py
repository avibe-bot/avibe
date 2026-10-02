"""Native protocol adapter registry."""

from __future__ import annotations

from typing import Any

from core.agent_core.ai.anthropic import AnthropicAdapter
from core.agent_core.ai.google import GoogleAdapter
from core.agent_core.ai.openai_chat import OpenAIChatAdapter
from core.agent_core.ai.openai_responses import OpenAIResponsesAdapter
from core.agent_core.ai.provider import ProviderAdapter
from core.agent_core.messages import ProtocolName

ADAPTERS: dict[ProtocolName, type[ProviderAdapter]] = {
    "anthropic": AnthropicAdapter,
    "openai_chat": OpenAIChatAdapter,
    "openai_responses": OpenAIResponsesAdapter,
    "google": GoogleAdapter,
}


def adapter_class(protocol: ProtocolName) -> type[ProviderAdapter]:
    try:
        return ADAPTERS[protocol]
    except KeyError as exc:
        raise ValueError(f"unsupported provider protocol: {protocol}") from exc


def create_adapter(protocol: ProtocolName, **kwargs: Any) -> ProviderAdapter:
    """Construct the adapter registered for ``protocol``."""

    return adapter_class(protocol)(**kwargs)  # type: ignore[call-arg]


get_adapter = create_adapter
