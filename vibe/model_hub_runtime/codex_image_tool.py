"""The hosted image tool for Codex CLI requests, added by Avibe instead of the engine.

The engine runs with ``disable-image-generation: passthrough`` (``config.py``), so
it forwards exactly the tools a client declares and adds none. Through Model Hub,
the hosted tool the engine used to inject is Codex's only image path
(``docs/plans/vibey-hosted-tools.md`` §7.1), so the gateway adds it back for Codex
CLI requests alone, with the engine's own tool and skip conditions. Every other
caller reaches the upstream with exactly the tools it declared.

Ported from CLIProxyAPI (MIT), ``internal/runtime/executor/codex_executor_request.go``
(``ensureImageGenerationTool``, ``isImageGenerationFunctionTool``,
``isCodexFreePlanAuth``), ``internal/util/codex.go`` (``IsCodexResponsesLiteRequest``)
and ``internal/thinking/suffix.go`` (``ParseSuffix``), at d318bcc3 (v8.0.23).
"""

from __future__ import annotations

import copy
from typing import Any, Final, Mapping

IMAGE_GENERATION_TOOL: Final = {"type": "image_generation", "output_format": "png"}
_RESPONSES_LITE_METADATA: Final = "ws_request_header_x_openai_internal_codex_responses_lite"


def with_codex_image_tool(
    request: Mapping[str, Any], *, model_id: str, plan_type: str | None
) -> Mapping[str, Any]:
    """Return a Codex Responses request with the engine's image tool, as the engine added it.

    Call it only for a Codex CLI request to a Source the engine serves through its
    Codex executor. ``model_id`` is the hop's model and ``plan_type`` the plan the
    engine derives for the credential (None for an API key). The request is
    returned unchanged when the engine would have skipped it: Responses-Lite
    metadata, a ``spark`` model, a free plan, or an image tool already present.
    Otherwise the tool is appended to ``tools``, or ``tools`` becomes that one
    tool when it is absent or not a list.
    """

    if _responses_lite(request) or _base_model(model_id).endswith("spark"):
        return request
    if plan_type is not None and plan_type.strip().casefold() == "free":
        return request
    tools = request.get("tools")
    if isinstance(tools, list):
        if any(_declares_image_tool(tool) for tool in tools):
            return request
        injected_tools = [*tools, dict(IMAGE_GENERATION_TOOL)]
    else:
        injected_tools = [dict(IMAGE_GENERATION_TOOL)]
    # A shallow copy keeps the request's metadata (protocol, caller headers).
    injected = copy.copy(request)
    injected["tools"] = injected_tools
    return injected


def _responses_lite(request: Mapping[str, Any]) -> bool:
    # The header form is not forwarded by the gateway, so only the body mirror counts.
    metadata = request.get("client_metadata")
    value = metadata.get(_RESPONSES_LITE_METADATA) if isinstance(metadata, Mapping) else None
    return value is True or (isinstance(value, str) and value.strip().casefold() == "true")


def _base_model(model_id: str) -> str:
    """The model without a ``(value)`` thinking suffix, as the engine compares it."""

    opening = model_id.rfind("(")
    if opening == -1 or not model_id.endswith(")"):
        return model_id
    return model_id[:opening]


def _declares_image_tool(tool: Any) -> bool:
    if not isinstance(tool, Mapping):
        return False
    kind = tool.get("type")
    if kind == "image_generation":
        return True
    if kind == "function":
        return tool.get("name") == "image_gen.imagegen"
    if kind == "namespace" and tool.get("name") == "image_gen":
        nested = tool.get("tools")
        return isinstance(nested, list) and any(
            isinstance(item, Mapping) and item.get("type") == "function" and item.get("name") == "imagegen"
            for item in nested
        )
    return False
