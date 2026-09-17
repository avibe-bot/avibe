"""Shared Model Hub integration errors."""

import re

from .events import redact_credential_material


UPSTREAM_ERROR_MESSAGE_LIMIT = 1024
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(?:authorization|cookie|api[_ -]?key|[\w-]*token|password|secret|credential)"
    r"[\"']?\s*[:=]\s*(?:(?:bearer|basic)\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def sanitize_upstream_error_message(value: object) -> str | None:
    """Retain a bounded error message, never an entire response or request."""

    if not isinstance(value, str):
        return None
    # Redact before truncation so a clipped credential is never persisted.
    message = redact_credential_material(value)
    message = _SENSITIVE_ASSIGNMENT.sub("[redacted]", message)
    message = _URL.sub("[redacted-url]", message)
    message = " ".join(message.split())
    return message[:UPSTREAM_ERROR_MESSAGE_LIMIT] or None


class ModelDiscoveryError(RuntimeError):
    """A source credential or upstream catalog probe could not be validated."""
