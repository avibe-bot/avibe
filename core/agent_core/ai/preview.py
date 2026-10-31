"""Read-only provider preparation, using the adapters' production wire builders.

This prepares the first HTTP request only. It never opens a client, sends a
request, retries, or follows an Anthropic pause. Authentication remains in the
in-memory WireRequest; callers must not serialize it to a diagnostic artifact.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import httpx

from core.agent_core.ai._common import WireRequest, endpoint_origin, prepare_messages
from core.agent_core.ai.provider import MediaLoader, ModelRequest, ProviderError
from core.agent_core.cancel import CancelToken


@dataclass(frozen=True)
class WirePreview:
    wire: WireRequest = field(repr=False)
    body: bytes = field(repr=False)
    body_sha256: str


async def preview_wire_request(
    request: ModelRequest, *, gateway: bool = False, media_loader: MediaLoader | None = None,
) -> WirePreview | ProviderError:
    """Compile without sending; use HTTPX's JSON encoder, as ``build_request`` does.

    The input must already have passed the Agent's context-transition check.
    Media is loaded through the same immutable snapshot loader as a real send.
    """
    from core.agent_core.ai import anthropic, google, openai_chat, openai_responses

    builders = {
        "anthropic": anthropic.wire_request,
        "openai_responses": openai_responses.wire_request,
        "openai_chat": openai_chat.wire_request,
        "google": google.wire_request,
    }
    protocol = request.endpoint.protocol
    builder = builders.get(protocol)
    if builder is None:
        raise ValueError(f"Unsupported preview protocol: {protocol}")
    prepared = await prepare_messages(
        request.messages, target=endpoint_origin(request.endpoint),
        supports_images=request.supports_images, protocol=protocol,
        media_loader=media_loader, cancel=CancelToken(), endpoint=request.endpoint,
    )
    if isinstance(prepared, ProviderError):
        return prepared
    messages, images = prepared
    wire = builder(request, messages, gateway=gateway, loaded_images=images)
    # AsyncClient.build_request delegates JSON serialization to Request, too.
    # No client construction (or environment/proxy/credential lookup) is needed.
    body = httpx.Request("POST", wire.url, headers=wire.headers, json=wire.body).content
    return WirePreview(wire, body, hashlib.sha256(body).hexdigest())
