"""Request metadata carried through the frozen EngineAdapter surface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Mapping

if TYPE_CHECKING:
    from .provenance import HopOrigin

# Caller headers that cross from an agent CLI's request to the local engine.
# Protocol capability headers select request features. ``user-agent`` keeps
# the calling client's identity: the engine forwards it upstream, and some
# Anthropic-compatible gateways rewrite the system prompt of any request that
# does not identify as Claude Code. Without it the upstream sees Avibe's HTTP
# library instead of the agent. Credentials and all other headers stay local.
FORWARDED_CALLER_HEADERS: Final = frozenset(
    {
        "anthropic-beta",
        "anthropic-version",
        "openai-beta",
        "user-agent",
    }
)


class ModelHubRequest(dict[str, Any]):
    """Raw request body plus the protocol spoken by the local caller."""

    def __init__(
        self,
        payload: Mapping[str, Any],
        *,
        protocol: str,
        headers: Mapping[str, str] | None = None,
        primary_origin: HopOrigin | None = None,
    ) -> None:
        super().__init__(payload)
        self.protocol = protocol
        self.headers = dict(headers or {})
        # Launch snapshot, not wire JSON. Every retry compares against this
        # origin, even when resolution restarts after cooldown/config changes.
        self.primary_origin = primary_origin


_OPAQUE_FIELDS = frozenset({"signature", "thoughtSignature", "thought_signature", "encrypted_content"})


def _plain_metadata(value: Any) -> Any:
    """Remove opaque fields only within protocol-owned extension metadata."""

    if isinstance(value, dict):
        return {key: _plain_metadata(item) for key, item in value.items() if key not in _OPAQUE_FIELDS}
    if isinstance(value, list):
        return [_plain_metadata(item) for item in value]
    return value


def _plain_history(value: Any) -> Any:
    if isinstance(value, list):
        return [plain for item in value if (plain := _plain_history(item)) is not None]
    if not isinstance(value, dict):
        return value
    kind = value.get("type")
    if kind == "redacted_thinking":
        return None
    if kind == "thinking":
        text = value.get("thinking", value.get("text"))
        return {"type": "text", "text": text} if isinstance(text, str) and text else None
    if kind == "reasoning":
        # Responses summaries (and visible reasoning content) are readable
        # history, not portable reasoning items. Never replay their item ids.
        texts = [
            {"type": "output_text", "text": part["text"]}
            for field in ("summary", "content")
            for part in (value.get(field) if isinstance(value.get(field), list) else [])
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]
        ]
        return {"role": "assistant", "content": texts} if texts else None
    plain = {key: item for key, item in value.items() if key not in _OPAQUE_FIELDS}
    if plain.get("thought") is True:
        # Gemini's readable thought text is ordinary history across origins.
        plain.pop("thought")
    for field in ("content", "parts", "tool_calls"):
        if field in plain:
            plain[field] = _plain_history(plain[field])
    if "extra_content" in plain:
        plain["extra_content"] = _plain_metadata(plain["extra_content"])
    if plain.get("role") == "assistant" and plain.get("content") == [] and not plain.get("tool_calls"):
        # A redacted-only assistant turn has no portable content. Leaving an
        # empty message behind would make an otherwise valid history invalid.
        return None
    # Tool input/arguments/results, schemas, and arbitrary user data are not
    # protocol blocks: a user field named "signature" there must survive.
    return plain


def without_opaque_history(request: Mapping[str, Any]) -> Mapping[str, Any]:
    """Copy a request's history for an unverified/different Avibe hop origin."""

    payload = dict(request)
    for field in ("messages", "input", "contents"):
        if field in payload:
            payload[field] = _plain_history(payload[field])
    if isinstance(request, ModelHubRequest):
        return ModelHubRequest(
            payload, protocol=request.protocol, headers=request.headers,
            primary_origin=request.primary_origin,
        )
    return payload
