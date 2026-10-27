"""Model routing for Vibey over Model Hub hop resolution (C-2, C-6).

Model Hub answers "how should the ``vibey`` backend call model M now" with a
``HopResolution`` (``ModelHubRuntimeRouter.resolve_hop``). ``selection_from_hop``
turns it into the loop's ``ModelEndpoint`` and ``ModelCapabilities`` and keeps
unknown capabilities ``None``; the loop applies the conservative defaults.
Transport is the provider adapter registered for the endpoint's protocol.

A ``google`` hop is called natively, at the gateway's ``/v1beta`` Gemini
surface (``vibey-gemini-native.md``). The served origin comes from
``x-avibe-served-hop`` on every protocol.

Each enabled hosted tool's route rides on the endpoint as Model Hub resolved it
(``hosted_tools``): an adapter declares a tool natively exactly where its mode
is ``native``, and pins that request to the hop's Source
(``vibey-hosted-tools.md`` §3.5, §4.1). The agent never decides nativity.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Mapping, Optional

from core.agent_core.agent.models import ModelSelection
from core.agent_core.ai.provider import (
    HostedToolRoute,
    ImageModel,
    ImageModels,
    ModelCapabilities,
    ModelEndpoint,
    ProviderAdapter,
)
from core.agent_core.messages import HOSTED_TOOLS, PROTOCOLS, ProtocolName

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


#: The facts of one search tool's ``hosted_tools`` entry, exactly (hop-resolution.schema.json): the declaration and
#: include values are present exactly when the tool is assumed to run natively.
_FACTS = ("assumed", "unsupported")
_SEARCH_KEYS = {"assumed": {"native", "replay", "declaration", "include"}, "unsupported": {"native", "replay"}}
#: A key an ``unsupported`` entry may carry: the hop's own Source refused the tool (C-6 ``refused``).
_REFUSED_KEY = "refused"
_IMAGE_KEYS = {"source_id", "model_id", "description", "params", "size_forms"}
_IMAGE_PARAMS = {"size", "images", "n"}
_SIZE_FORMS = {"pixels", "ratio"}
_SOURCE_ID = re.compile(r"src_[a-z0-9]{8,}")


def _hosted_routes(raw: Any) -> tuple[dict[str, HostedToolRoute], Optional[ImageModels]]:
    """``HopResolution.hosted_tools``: each enabled search tool's facts as its route, and ``image_generate``'s models."""
    if not isinstance(raw, Mapping):
        raise HopResolutionError("hop has no hosted_tools")
    routes: dict[str, HostedToolRoute] = {}
    images: Optional[ImageModels] = None
    for tool, entry in raw.items():
        try:
            if tool == "image_generate":
                images = _image_models(entry)
            else:
                routes[tool] = _hosted_route(tool, entry)
        except (TypeError, ValueError, KeyError) as error:
            # Every malformed entry fails here, whatever the parse raised on it.
            raise HopResolutionError(f"hop hosted tool {tool!r} is invalid: {error}") from None
    return routes, images


def _hosted_route(tool: Any, entry: Any) -> HostedToolRoute:
    """One search tool's facts, exactly as the hop schema allows them: declared natively exactly when ``native`` is
    ``assumed`` (§3.4, §4.1)."""
    if tool == "web_fetch":
        if entry != {"native": "unsupported", "replay": "unsupported"}:
            raise ValueError("web_fetch is function-only, with no native replay")
        return HostedToolRoute(mode="proxy", replay=False)
    if tool not in HOSTED_TOOLS or not isinstance(entry, Mapping):
        raise ValueError("not a search tool entry")
    native, replay = entry.get("native"), entry.get("replay")
    if native not in _FACTS or replay not in _FACTS:
        raise ValueError("native and replay are each assumed or unsupported")
    refused = _REFUSED_KEY in entry
    if refused and (native != "unsupported" or entry[_REFUSED_KEY] is not True):
        raise ValueError("refused is true, and only on an unsupported entry")
    if set(entry) - {_REFUSED_KEY} != _SEARCH_KEYS[native]:
        raise ValueError(f"a {native} entry has exactly {', '.join(sorted(_SEARCH_KEYS[native]))}")
    include = entry.get("include", [])
    if not isinstance(include, list):
        raise ValueError("include must be a list")
    return HostedToolRoute(
        mode="native" if native == "assumed" else "proxy",
        replay=replay == "assumed",
        declaration=entry.get("declaration"),
        include=tuple(include),
        refused=refused,
    )


def _image_models(entry: Any) -> ImageModels:
    """``image_generate``'s models, default first, exactly as the hop schema allows them (§7.2)."""
    if not isinstance(entry, Mapping) or set(entry) != {"models", "default"}:
        raise ValueError("an image_generate entry has exactly models and default")
    raw_models, default = entry["models"], entry["default"]
    if not isinstance(raw_models, list) or not raw_models:
        raise ValueError("models is a non-empty list")
    models = []
    for item in raw_models:
        if not isinstance(item, Mapping) or set(item) != _IMAGE_KEYS:
            raise ValueError(f"an image model has exactly {', '.join(sorted(_IMAGE_KEYS))}")
        if not isinstance(item["source_id"], str) or not _SOURCE_ID.fullmatch(item["source_id"]):
            raise ValueError("an image model names its Source")
        for name in ("model_id", "description"):
            if not isinstance(item[name], str) or not item[name]:
                raise ValueError(f"an image model has a {name}")
        params, forms = item["params"], item["size_forms"]
        if not isinstance(params, list) or not set(params) <= _IMAGE_PARAMS or len(set(params)) != len(params):
            raise ValueError("params lists distinct size, images and n")
        if not isinstance(forms, list) or not set(forms) <= _SIZE_FORMS or len(set(forms)) != len(forms):
            raise ValueError("size_forms lists distinct pixels and ratio")
        models.append(
            ImageModel(item["source_id"], item["model_id"], item["description"], tuple(params), tuple(forms))
        )
    if not isinstance(default, Mapping) or set(default) != {"source_id", "model_id"}:
        raise ValueError("default is a Source id and a model id")
    if (models[0].source_id, models[0].model_id) != (default["source_id"], default["model_id"]):
        raise ValueError("the default is the first model")
    return ImageModels(tuple(models))


def selection_from_hop(hop: Mapping[str, Any]) -> ModelSelection:
    """Build the loop's selection from one ``HopResolution`` (hop-resolution.schema.json)."""
    if hop.get("backend") != "vibey":
        raise HopResolutionError(f"hop resolved for backend {hop.get('backend')!r}, not vibey")
    protocol = hop.get("protocol")
    if protocol not in PROTOCOLS:
        raise HopResolutionError(f"hop protocol {protocol!r} is not supported")
    for name in ("base_url", "token", "runtime_model", "provider", "source_id", "source_identity"):
        if not isinstance(hop.get(name), str) or not hop[name]:
            raise HopResolutionError(f"hop has no {name}")
    headers = hop.get("request_headers")
    if not isinstance(headers, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
    ):
        raise HopResolutionError("hop request_headers must map strings to strings")
    hosted_tools, image_models = _hosted_routes(hop.get("hosted_tools"))
    raw = hop.get("capabilities")
    if not isinstance(raw, Mapping):
        raise HopResolutionError("hop has no capabilities")
    efforts = raw.get("reasoning_efforts") or []
    if not isinstance(efforts, (list, tuple)) or not all(isinstance(item, str) for item in efforts):
        raise HopResolutionError("hop capability reasoning_efforts must be a list of strings")
    steps = raw.get("context_price_steps") or []
    if not isinstance(steps, (list, tuple)) or not all(
        isinstance(size, int) and not isinstance(size, bool) and size > 0 for size in steps
    ):
        raise HopResolutionError("hop capability context_price_steps must be a list of positive integers")
    capabilities = ModelCapabilities(
        context_window=_optional(raw.get("context_window"), int, "context_window"),
        input_limit=_optional(raw.get("input_limit"), int, "input_limit"),
        max_output_tokens=_optional(raw.get("max_output_tokens"), int, "max_output_tokens"),
        supports_tools=_optional(raw.get("supports_tools"), bool, "supports_tools"),
        supports_images=_optional(raw.get("supports_images"), bool, "supports_images"),
        supports_reasoning=_optional(raw.get("supports_reasoning"), bool, "supports_reasoning"),
        reasoning_efforts=tuple(efforts),
        context_price_steps=tuple(sorted(steps)),
    )
    base_url = hop["base_url"]
    raw_cache_ttl = hop.get("cache_ttl")
    if raw_cache_ttl is not None and (
        not isinstance(raw_cache_ttl, str)
        or raw_cache_ttl not in {"5m", "30m", "1h", "12h"}
    ):
        raise HopResolutionError("hop cache_ttl is invalid")
    cache_ttl = raw_cache_ttl
    raw_cache_ttl_wire = hop.get("cache_ttl_wire", False)
    if not isinstance(raw_cache_ttl_wire, bool):
        raise HopResolutionError("hop cache_ttl_wire is invalid")
    cache_ttl_wire = raw_cache_ttl_wire
    if cache_ttl_wire and cache_ttl not in {"30m", "12h"}:
        raise HopResolutionError("hop cache_ttl_wire requires 30m or 12h")
    endpoint = ModelEndpoint(
        protocol=protocol,
        base_url=base_url,
        model_id=hop["runtime_model"],
        token=hop["token"],
        request_headers=dict(headers),
        provider=hop["provider"],
        cache_ttl=cache_ttl,
        cache_ttl_wire=cache_ttl_wire,
        source_id=hop["source_id"],
        source_identity=hop["source_identity"],
        hosted_tools=hosted_tools,
        image_models=image_models,
    )
    return ModelSelection(endpoint=endpoint, capabilities=capabilities)


class HubModelRouter:
    """``ModelRouter`` for one run: a fresh hop for every model attempt (C-6 item 3).

    ``first`` is the selection the adapter already resolved during preflight; the
    loop's first attempt reuses it, so a run resolves once and again only after a
    retryable error or a refusal (``vibey-hosted-tools.md`` §3.5).
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
        #: The route preflight resolved; the Turn's tool list is fixed from it.
        self.preflight = first
        #: The hop the Turn runs on now, its latest resolution. The gateway answers the Turn's requests only on that
        #: hop's credential (C-6 item 3), so the Turn's hosted calls go out on it too.
        self.current = first
        self._adapters: dict[str, ProviderAdapter] = {}

    async def resolve(self) -> ModelSelection:
        if self._first is not None:
            selection, self._first = self._first, None
        else:
            selection = await self._resolve()
        self.current = selection
        return selection

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

        return create_adapter(protocol, client=client, media_loader=media_loader, gateway=True)

    return create
