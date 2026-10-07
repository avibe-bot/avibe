"""Model routing for Vibey over Model Hub hop resolution (C-2, C-6).

Model Hub answers "how should the ``vibey`` backend call model M now" with a
``HopResolution`` (``ModelHubRuntimeRouter.resolve_hop``). ``selection_from_hop``
turns it into the loop's ``ModelEndpoint`` and ``ModelCapabilities`` and keeps
unknown capabilities ``None``; the loop applies the conservative defaults.
Transport is the provider adapter registered for the endpoint's protocol.

A ``google`` hop is called over Chat Completions at the same gateway: Model Hub
serves every source on its OpenAI-compatible ``/v1`` surface and converts, so
the agent needs no Gemini transport. The served origin still comes from
``x-avibe-served-hop``.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Mapping, Optional

from core.agent_core.agent.models import ModelSelection
from core.agent_core.ai.provider import ModelCapabilities, ModelEndpoint, ProviderAdapter
from core.agent_core.messages import PROTOCOLS, ProtocolName

logger = logging.getLogger(__name__)

SelectionResolver = Callable[[], Awaitable[ModelSelection]]
ProviderFactory = Callable[[ProtocolName], ProviderAdapter]


class HopResolutionError(ValueError):
    """Model Hub returned a hop the agent cannot call."""


def _optional(value: Any, kind: type, what: str) -> Any:
    if value is None:
        return None
    if not isinstance(value, kind) or (isinstance(value, bool) and kind is not bool):
        raise HopResolutionError(f"hop capability {what} must be {kind.__name__} or null")
    return value


def selection_from_hop(hop: Mapping[str, Any], *, gateway_base_url: Optional[str] = None) -> ModelSelection:
    """Build the loop's selection from one ``HopResolution`` (hop-resolution.schema.json).

    ``gateway_base_url`` is the Model Hub gateway prefix the hop's ``base_url`` was
    built from; a ``google`` hop needs it, because its ``base_url`` is the
    ``/v1beta`` Gemini surface and the agent speaks Chat to ``/v1``.
    """
    if hop.get("backend") != "vibey":
        raise HopResolutionError(f"hop resolved for backend {hop.get('backend')!r}, not vibey")
    protocol = hop.get("protocol")
    if protocol not in PROTOCOLS:
        raise HopResolutionError(f"hop protocol {protocol!r} is not supported")
    for name in ("base_url", "token", "runtime_model", "provider"):
        if not isinstance(hop.get(name), str) or not hop[name]:
            raise HopResolutionError(f"hop has no {name}")
    headers = hop.get("request_headers")
    if not isinstance(headers, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
    ):
        raise HopResolutionError("hop request_headers must map strings to strings")
    raw = hop.get("capabilities")
    if not isinstance(raw, Mapping):
        raise HopResolutionError("hop has no capabilities")
    efforts = raw.get("reasoning_efforts") or []
    if not isinstance(efforts, (list, tuple)) or not all(isinstance(item, str) for item in efforts):
        raise HopResolutionError("hop capability reasoning_efforts must be a list of strings")
    capabilities = ModelCapabilities(
        context_window=_optional(raw.get("context_window"), int, "context_window"),
        input_limit=_optional(raw.get("input_limit"), int, "input_limit"),
        max_output_tokens=_optional(raw.get("max_output_tokens"), int, "max_output_tokens"),
        supports_tools=_optional(raw.get("supports_tools"), bool, "supports_tools"),
        supports_images=_optional(raw.get("supports_images"), bool, "supports_images"),
        supports_reasoning=_optional(raw.get("supports_reasoning"), bool, "supports_reasoning"),
        reasoning_efforts=tuple(efforts),
    )
    base_url = hop["base_url"]
    if protocol == "google":
        if not gateway_base_url:
            raise HopResolutionError("a google hop needs the gateway prefix to call Chat Completions")
        protocol, base_url = "openai_chat", f"{gateway_base_url.rstrip('/')}/v1"
    endpoint = ModelEndpoint(
        protocol=protocol,
        base_url=base_url,
        model_id=hop["runtime_model"],
        token=hop["token"],
        request_headers=dict(headers),
        provider=hop["provider"],
    )
    return ModelSelection(endpoint=endpoint, capabilities=capabilities)


class HubModelRouter:
    """``ModelRouter`` for one run: a fresh hop for every model attempt (C-6 item 3).

    ``first`` is the selection the adapter already resolved during preflight; the
    loop's first attempt reuses it, so a run resolves once and again only after a
    retryable error.
    """

    def __init__(
        self,
        resolve: SelectionResolver,
        providers: ProviderFactory,
        *,
        first: Optional[ModelSelection] = None,
    ) -> None:
        self._resolve = resolve
        self._providers = providers
        self._first = first
        self._adapters: dict[str, ProviderAdapter] = {}

    async def resolve(self) -> ModelSelection:
        if self._first is not None:
            selection, self._first = self._first, None
            return selection
        return await self._resolve()

    def provider_for(self, protocol: ProtocolName) -> ProviderAdapter:
        adapter = self._adapters.get(protocol)
        if adapter is None:
            adapter = self._adapters[protocol] = self._providers(protocol)
        return adapter

    async def aclose(self) -> None:
        """Close every adapter, best effort: run cleanup never changes the Turn's outcome."""
        adapters, self._adapters = list(self._adapters.items()), {}
        for protocol, adapter in adapters:
            close = getattr(adapter, "aclose", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:
                logger.warning("Vibey could not close its %s provider adapter", protocol, exc_info=True)


def registry_providers(*, media_loader: Any, client: Any = None) -> ProviderFactory:
    """Provider adapters from the ``ai`` registry, speaking to the Model Hub gateway.

    In gateway mode each adapter reads the served origin from ``x-avibe-served-hop``
    with the engine's own resolver, the one owner of that C-6 rule.
    """

    def create(protocol: ProtocolName) -> ProviderAdapter:
        from core.agent_core.ai.registry import create_adapter

        return create_adapter(
            protocol,
            client=client,
            media_loader=media_loader,
            gateway=True,
        )

    return create
