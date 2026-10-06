"""User-facing copy for the built-in agent's failures, named by the catalog.

``AgentError.kind`` is the engine's stable discriminator (loop-control.md section 6);
``AgentError.message`` is diagnostic detail and is never shown as display copy.
Every kind maps to one ``vibe/i18n`` key under ``avibeAgent.error``.
"""

from __future__ import annotations

from typing import Optional

from modules.agents.catalog import display_name_for_backend
from vibe.i18n import t as i18n_t

# Engine and provider kinds (C-2 error classification, C-3 run outcomes) with their own copy.
_KIND_KEYS: dict[str, str] = {
    "refusal": "refusal",
    "safety": "safety",
    "empty_response": "emptyResponse",
    "dependency_cancelled": "interrupted",
    "aborted": "interrupted",
    "overflow": "contextExhausted",
    "context_exhausted": "contextExhausted",
    # The newest unit alone cannot fit (C-9 section 8 d): starting over would not help, so the copy says what to do.
    "tool_output_too_large": "toolOutputTooLarge",
    "input_too_large": "inputTooLarge",
    "step_too_large": "stepTooLarge",
    "rate_limit": "rateLimit",
    "overloaded": "overloaded",
    "network": "network",
    "server": "server",
    "auth": "auth",
    "invalid_request": "invalidRequest",
    "error": "modelError",
    # The loop refuses a route that declares no tool support before any request.
    "UnsupportedModelRoute": "toolsUnsupported",
    # A provider answer the canonical transcript cannot hold.
    "ProviderProtocolViolation": "providerProtocol",
}
# Run reasons that carry their own copy when no error event names a kind.
_REASON_KEYS: dict[str, str] = {
    "context_exhausted": "contextExhausted",
    "aborted": "interrupted",
}


def error_key(kind: Optional[str], *, reason: Optional[str] = None) -> str:
    """The i18n key for an error kind, falling back to the run reason, then to generic copy."""
    name = _KIND_KEYS.get(kind or "") or _REASON_KEYS.get(reason or "") or "generic"
    return f"avibeAgent.error.{name}"


def error_text(kind: Optional[str], lang: str, *, reason: Optional[str] = None) -> str:
    return i18n_t(error_key(kind, reason=reason), lang, backend=display_name_for_backend("avibe"))
