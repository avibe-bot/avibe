"""User-facing copy for Avibe Agent failures and context management.

``AgentError.kind`` is the engine's stable discriminator (loop-control.md section 6);
``AgentError.message`` is diagnostic detail and is never shown as display copy.
Every kind maps to one ``vibe/i18n`` key under ``avibeAgent.error``. The answers to
``/compact``, the auto-compaction pause, and the parts of a context that cannot fit
(C-9) are under ``avibeAgent.compact`` and ``avibeAgent.contextPart``.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

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
    return i18n_t(error_key(kind, reason=reason), lang)


def compact_text(key: str, lang: str, **values: Any) -> str:
    """Copy for ``/compact`` and the auto-compaction pause (C-9 section 10), under ``avibeAgent.compact``."""
    return i18n_t(f"avibeAgent.compact.{key}", lang, **values)


def context_exhausted_text(limit: int, parts: Sequence[Any], lang: str) -> str:
    """What fills a context that cannot fit (C-9 section 8 d), part by part, in the user's language."""
    separator = i18n_t("avibeAgent.contextPart.separator", lang)
    listed = separator.join(
        f"{i18n_t(f'avibeAgent.contextPart.{part.name}', lang)} ~{part.tokens:,}" for part in parts
    )
    return i18n_t("avibeAgent.error.contextExhaustedParts", lang, limit=f"{limit:,}", parts=listed)
