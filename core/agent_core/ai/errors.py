"""Provider error classification.

Overflow expressions are ported from Pi's ``packages/ai/src/utils/overflow.ts``
(MIT, revision 7fbbd5f). The adapter layer adds HTTP and transport semantics
from C-2.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

import httpx

from core.agent_core.messages import AssistantMessage
from core.agent_core.ai.provider import ProviderError

_OVERFLOW_PATTERNS = (
    r"prompt (?:is )?too long",
    r"prompt exceeds max length",
    r"request_too_large",
    r"input is too long for requested model",
    r"exceeds the context window",
    r"exceeds (?:the )?(?:model'?s )?maximum context length(?: of [\d,]+ tokens?|\s*\([\d,]+\))",
    r"input token count.*exceeds the maximum",
    r"maximum prompt length is \d+",
    r"reduce the length of the messages",
    r"maximum context length is \d+ tokens",
    r"exceeds (?:the )?maximum allowed input length of [\d,]+ tokens?",
    r"input \(\d+ tokens\) is longer than the model'?s context length \d+ tokens",
    r"exceeds the limit of \d+",
    r"exceeds the available context size",
    r"greater than the context length",
    r"context window exceeds limit",
    r"exceeded model token limit",
    r"too large for model with \d+ maximum context length",
    r"prompt has [\d,]+ tokens?, but the configured context size is [\d,]+ tokens?",
    r"model_context_window_exceeded",
    r"prompt too long; exceeded (?:max )?context length",
    r"range of input length should be",
    r"context[_ ]length[_ ]exceeded",
    r"too many tokens",
    r"token limit exceeded",
    r"4(?:00|13)\s*(?:status code)?\s*\(no body\)",
)
_OVERFLOW_RE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in _OVERFLOW_PATTERNS)
_NON_OVERFLOW_RE = (
    re.compile(r"^(?:throttling error|service unavailable):", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_QUOTED_NAMED_SECRET_RE = re.compile(
    r"(?i)(?P<key>\"?(?:authorization|api[_-]?key|x-api-key|token|secret)\"?)"
    r"(?P<separator>\s*[:=]\s*)(?P<quote>[\"'])[^\"']*(?P=quote)"
)
_BARE_NAMED_SECRET_RE = re.compile(
    r"(?i)(?P<key>\"?(?:authorization|api[_-]?key|x-api-key|token|secret)\"?)"
    r"(?P<separator>\s*[:=]\s*)(?P<value>[^\s,;}\"']+)"
)
_QUERY_SECRET_RE = re.compile(r"(?i)([?&](?:key|token|api[_-]?key)=)[^&\s]+")
_SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "x-api-key",
    "x_api_key",
    "token",
    "secret",
    "password",
    "access_token",
    "refresh_token",
}


def classify_error(
    *,
    status: int | None = None,
    body: str = "",
    headers: Mapping[str, str] | None = None,
    streamed: bool = False,
    partial: AssistantMessage | None = None,
    exc: BaseException | None = None,
    code: str | None = None,
) -> ProviderError:
    """Classify one HTTP, provider-body, or network failure."""

    message = _error_message(body, exc)
    retry_after = parse_retry_after(_header_value(headers, "retry-after"))
    kind = _classify_kind(status=status, message=f"{body}\n{message}", exc=exc, code=code)
    retryable = kind in {"rate_limit", "overloaded", "network", "server"} and not streamed
    return ProviderError(
        kind=kind,
        message=message,
        retryable=retryable,
        retry_after_s=retry_after,
        status=status,
        partial=partial,
    )


def classify_http_error(
    status: int,
    body: str = "",
    *,
    headers: Mapping[str, str] | None = None,
    streamed: bool = False,
    partial: AssistantMessage | None = None,
) -> ProviderError:
    return classify_error(
        status=status,
        body=body,
        headers=headers,
        streamed=streamed,
        partial=partial,
    )


def classify_network_error(
    exc: BaseException,
    *,
    streamed: bool = False,
    partial: AssistantMessage | None = None,
) -> ProviderError:
    return classify_error(exc=exc, streamed=streamed, partial=partial)


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def is_overflow_message(message: str, *, status: int | None = None) -> bool:
    if status == 413:
        return True
    if any(pattern.search(message) for pattern in _NON_OVERFLOW_RE):
        return False
    return any(pattern.search(message) for pattern in _OVERFLOW_RE)


def _classify_kind(
    *,
    status: int | None,
    message: str,
    exc: BaseException | None,
    code: str | None = None,
) -> str:
    if status == 413:
        return "overflow"
    if status in {401, 403}:
        return "auth"
    if status == 429:
        return "rate_limit"
    if status is not None and status >= 500:
        if status in {529} or "overloaded" in message.lower():
            return "overloaded"
        return "server"
    if status in {408, 409, 425}:
        return "server"
    normalized_code = (code or "").lower()
    if normalized_code in {
        "rate_limit",
        "rate_limited",
        "rate_limit_error",
        "too_many_requests",
        "resource_exhausted",
    }:
        return "rate_limit"
    if normalized_code in {"overloaded", "overload", "overloaded_error"}:
        return "overloaded"
    if normalized_code in {
        "server_error",
        "internal_server_error",
        "bad_gateway",
        "service_unavailable",
        "internal",
        "api_error",
        "unavailable",
        "deadline_exceeded",
        "aborted",
    }:
        return "server"
    if normalized_code in {"context_length_exceeded", "request_too_large"}:
        return "overflow"
    if normalized_code in {"permission_denied", "unauthenticated"}:
        return "auth"
    if is_overflow_message(message, status=status):
        return "overflow"
    if exc is not None:
        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError, OSError)):
            return "network"
        return "unknown"
    lowered = message.lower()
    if "rate limit" in lowered or "too many requests" in lowered or "rate_limit" in lowered:
        return "rate_limit"
    if "overloaded" in lowered or "overload" in lowered:
        return "overloaded"
    if any(token in lowered for token in ("service unavailable", "internal server", "bad gateway", "server error")):
        return "server"
    if "unauthorized" in lowered or "forbidden" in lowered:
        return "auth"
    if status is not None and 400 <= status < 500:
        return "invalid_request"
    return "unknown"


def _error_message(body: str, exc: BaseException | None) -> str:
    if body:
        try:
            value = json.loads(body)
        except (TypeError, ValueError):
            return _redact(body.strip() or "provider request failed")
        extracted = _extract_message(value)
        if extracted:
            return _redact(extracted)
        return _redact(json.dumps(_redact_json(value), ensure_ascii=False, separators=(",", ":")))
    if exc is not None:
        return _redact(str(exc) or type(exc).__name__)
    return "provider request failed"


def _redact(value: str) -> str:
    value = _BEARER_RE.sub("Bearer [redacted]", value)
    value = _QUOTED_NAMED_SECRET_RE.sub(
        lambda match: (
            f"{match.group('key')}{match.group('separator')}"
            f"{match.group('quote')}[redacted]{match.group('quote')}"
        ),
        value,
    )
    value = _BARE_NAMED_SECRET_RE.sub(
        lambda match: f"{match.group('key')}{match.group('separator')}[redacted]",
        value,
    )
    return _QUERY_SECRET_RE.sub(r"\1[redacted]", value)


def _extract_message(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("message", "error", "detail", "description", "status"):
            if key not in value:
                continue
            nested = _extract_message(value[key])
            if nested:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = _extract_message(item)
            if nested:
                return nested
    return None


def _redact_json(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.lower().replace("-", "_") in _SENSITIVE_KEYS:
        return "[redacted]"
    if isinstance(value, Mapping):
        return {str(item_key): _redact_json(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    return value


def _header_value(headers: Mapping[str, str] | None, name: str) -> str | None:
    if headers is None:
        return None
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


class ProviderErrorClassifier:
    """Small callable wrapper useful to tests and adapter integrations."""

    def __call__(self, **kwargs: Any) -> ProviderError:
        return classify_error(**kwargs)


classify_provider_error = classify_error
