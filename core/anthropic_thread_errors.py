"""Closed diagnostic fields from an Anthropic thread rejection; never used for routing."""

import json

_CODES = frozenset(
    {
        "thread_not_found",
        "thread_fingerprint_mismatch",
        "thread_already_continued",
        "thread_continue_in_flight",
        "thread_unsupported_request",
    }
)
_TYPES = frozenset(
    {
        "invalid_request_error",
        "api_error",
        "not_found_error",
        "authentication_error",
        "permission_error",
        "rate_limit_error",
        "overloaded_error",
        "request_too_large",
    }
)


def thread_error_fields(status: int | None, body: str | bytes, *, hub: bool = False) -> dict[str, str]:
    if status not in (400, 404):
        return {}
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        return {}
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return {}
    details = error.get("details")
    details = details if isinstance(details, dict) else {}
    code = details.get("error_code")
    kind = details.get("upstream_error_type", error.get("type")) if hub else error.get("type")
    fields = {}
    if isinstance(code, str):
        fields["error_code"] = code if code in _CODES else "other"
    if isinstance(kind, str):
        fields["error_type"] = kind if kind in _TYPES else "other"
    return fields
