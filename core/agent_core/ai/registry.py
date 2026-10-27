"""Native protocol adapter registry."""

from __future__ import annotations

from typing import Any

from core.agent_core.ai import anthropic, google, openai_chat, openai_responses
from core.agent_core.ai.anthropic import AnthropicAdapter
from core.agent_core.ai.google import GoogleAdapter
from core.agent_core.ai.openai_chat import OpenAIChatAdapter
from core.agent_core.ai.openai_responses import OpenAIResponsesAdapter
from core.agent_core.ai.provider import ProviderAdapter
from core.agent_core.messages import ProtocolName

ADAPTERS: dict[ProtocolName, type[ProviderAdapter]] = {
    "anthropic": AnthropicAdapter,
    "google": GoogleAdapter,
    "openai_chat": OpenAIChatAdapter,
    "openai_responses": OpenAIResponsesAdapter,
}

def sent_effort(protocol: str, model_id: str, effort: str) -> str | None:
    """The effort token a request on ``protocol`` to ``model_id`` carries for ``effort``, exactly as the adapter sends
    it: at ``output_config.effort`` (Anthropic), ``reasoning.effort`` (Responses), ``reasoning_effort`` (Chat), or the
    thinking level a Gemini request's ``thinkingConfig`` carries, a budget named by the effort it stands for
    (``google``). None when the request carries no effort token. Model Hub's effort menu reads the same answer, so the menu and the wire cannot drift."""

    if protocol == "anthropic":
        return anthropic.sent_effort(model_id, effort)
    if protocol == "openai_responses":
        return openai_responses.sent_effort(effort)
    if protocol == "openai_chat":
        return openai_chat.sent_effort(effort)
    if protocol == "google":
        return google.sent_effort(model_id, effort)
    raise ValueError(f"unsupported provider protocol: {protocol}")


def adapter_class(protocol: ProtocolName) -> type[ProviderAdapter]:
    try:
        return ADAPTERS[protocol]
    except KeyError as exc:
        raise ValueError(f"unsupported provider protocol: {protocol}") from exc


def create_adapter(protocol: ProtocolName, **kwargs: Any) -> ProviderAdapter:
    """Construct the adapter registered for ``protocol``."""

    return adapter_class(protocol)(**kwargs)  # type: ignore[call-arg]


get_adapter = create_adapter
