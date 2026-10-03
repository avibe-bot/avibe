"""Provider error classification.

Overflow expressions are ported from Pi's ``packages/ai/src/utils/overflow.ts``
(MIT, revision 7fbbd5f). The adapter layer adds HTTP and transport semantics
from C-2.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
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
_SECRET_KEY_PATTERN = (
    r"(?:authorization|proxy[-_ ]?authorization|"
    r"api[-_ ]?key|x[-_ ]?api[-_ ]?key|x[-_ ]?goog[-_ ]?api[-_ ]?key|"
    r"ocp[-_ ]?apim[-_ ]?subscription[-_ ]?key|"
    r"password|cookie|set[-_ ]?cookie|credential[s]?|private[-_ ]?key|"
    r"client[-_ ]?secret|"
    r"(?:oauth|access|refresh|id|auth|session|csrf|bearer)[-_ ]?token"
    r"(?:[-_ ]?(?:name|value))?|"
    r"token|secret)"
)
_AUTHORIZATION_HEADER_RE = re.compile(
    rf"(?im)(?P<key>[\"']?(?:proxy[-_ ]?authorization|authorization)[\"']?)"
    r"(?P<separator>\s*[:=]\s*)(?P<value>[^\r\n]*)"
)
_COOKIE_HEADER_RE = re.compile(
    r"(?im)(?P<key>[\"']?(?:set[-_ ]?cookie|cookie)[\"']?)"
    r"(?P<separator>\s*[:=]\s*)(?P<value>[^\r\n]*)"
)
_CREDENTIAL_HEADER_RE = re.compile(
    rf"(?im)(?P<key>[\"']?(?!(?:proxy[-_ ]?authorization|authorization|"
    rf"set[-_ ]?cookie|cookie)\b){_SECRET_KEY_PATTERN}[\"']?)"
    r"(?P<separator>\s*:\s*)(?P<value>[^\r\n]*)"
)
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    rf"(?i)(?P<key>[\"']?{_SECRET_KEY_PATTERN}[\"']?)"
    r"(?P<separator>\s*=\s*)(?P<value>[^\s,;}\"']+)"
)
_QUERY_SECRET_RE = re.compile(
    rf"(?i)([?&](?:key|api[-_ ]?key|token|secret|password|"
    rf"[A-Za-z0-9_.-]*(?:token|secret)[A-Za-z0-9_.-]*)=)[^&\s]+"
)
_TOKEN_SHAPE_RE = re.compile(
    r"(?ix)(?<![A-Za-z0-9_-])(?:"
    r"sk-[A-Za-z0-9][A-Za-z0-9_-]*|"
    r"AIza[A-Za-z0-9_-]+|"
    r"gsk_[A-Za-z0-9_-]+|"
    r"gh[pousr]_[A-Za-z0-9_-]+|"
    r"eyJ[A-Za-z0-9_-]{3,}\.[A-Za-z0-9_-]{3,}\.[A-Za-z0-9_-]{3,}"
    r")(?![A-Za-z0-9_-])"
)
_SENSITIVE_KEYS = {
    "authorization",
    "proxy_authorization",
    "api_key",
    "apikey",
    "x-api-key",
    "x_api_key",
    "x-goog-api-key",
    "x_goog_api_key",
    "ocp_apim_subscription_key",
    "token",
    "secret",
    "password",
    "cookie",
    "set_cookie",
    "credential",
    "credentials",
    "private_key",
    "client_secret",
    "access_token",
    "refresh_token",
    "oauth_token",
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
    protocol: str | None = None,
    redact: bool = True,
) -> ProviderError:
    """Classify one HTTP, provider-body, or network failure."""

    message = _error_message(body, exc, redact=redact)
    retry_after = parse_retry_after(_header_value(headers, "retry-after"))
    extracted_code = _extract_code(body, protocol=protocol)
    effective_code = code
    if not effective_code or _is_generic_code(effective_code):
        effective_code = extracted_code or effective_code
    kind = _classify_kind(
        status=status,
        message=f"{body}\n{message}",
        exc=exc,
        code=effective_code,
    )
    retryable = kind in {"rate_limit", "overloaded", "network", "server"} and not streamed
    error = ProviderError(
        kind=kind,
        message=message,
        retryable=retryable,
        retry_after_s=retry_after,
        status=status,
        partial=partial,
    )
    return redact_provider_error(error) if redact else error


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
    if status is not None and 300 <= status < 400:
        return "invalid_request"
    if status is not None and status >= 500:
        if status in {529} or "overloaded" in message.lower():
            return "overloaded"
        return "server"
    if status in {408, 409, 425}:
        return "server"
    normalized_code = (code or "").lower().replace("-", "_")
    if normalized_code in {
        "authentication_error",
        "authentication",
        "auth_error",
        "invalid_api_key",
        "invalid_api_key_error",
        "api_key_invalid",
        "api_key_invalid_error",
        "permission",
        "permission_error",
        "permission_denied",
        "unauthenticated",
        "unauthorized",
        "forbidden",
    }:
        return "auth"
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
    if is_overflow_message(message, status=status):
        return "overflow"
    if exc is not None:
        if isinstance(
            exc,
            (
                TimeoutError,
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.TransportError,
                OSError,
            ),
        ):
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


def _error_message(body: str, exc: BaseException | None, *, redact: bool) -> str:
    if body:
        try:
            value = json.loads(body)
        except (TypeError, ValueError):
            message = body.strip() or "provider request failed"
            return redact_provider_text(message) if redact else message
        extracted = _extract_message(value)
        if extracted:
            return redact_provider_text(extracted) if redact else extracted
        value = _redact_json(value) if redact else value
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if exc is not None:
        message = str(exc) or type(exc).__name__
        return redact_provider_text(message) if redact else message
    return "provider request failed"


def redact_provider_text(value: str) -> str:
    """Redact credentials from any provider diagnostic text."""

    def redact_authorization(match: re.Match[str]) -> str:
        raw = match.group("value").strip().strip("\"'")
        scheme = re.match(r"[A-Za-z][A-Za-z0-9_-]*", raw)
        replacement = f"{scheme.group(0)} [redacted]" if scheme else "[redacted]"
        return f"{match.group('key')}{match.group('separator')}{replacement}"

    def redact_cookie(match: re.Match[str]) -> str:
        parts = []
        for part in match.group("value").split(";"):
            stripped = part.strip()
            if not stripped:
                continue
            if "=" in stripped:
                name, _ = stripped.split("=", 1)
                parts.append(f"{name.strip()}=[redacted]")
            else:
                parts.append("[redacted]")
        return f"{match.group('key')}{match.group('separator')}{'; '.join(parts)}"

    value = _AUTHORIZATION_HEADER_RE.sub(redact_authorization, value)
    value = _COOKIE_HEADER_RE.sub(redact_cookie, value)
    value = _CREDENTIAL_HEADER_RE.sub(
        lambda match: (
            f"{match.group('key')}{match.group('separator')}[redacted]"
        ),
        value,
    )
    value = _CREDENTIAL_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('key')}{match.group('separator')}[redacted]",
        value,
    )
    value = _QUERY_SECRET_RE.sub(r"\1[redacted]", value)
    return _TOKEN_SHAPE_RE.sub("[redacted]", value)


def redact_provider_error(error: ProviderError) -> ProviderError:
    """Apply the single provider-diagnostic redaction boundary."""

    partial = error.partial
    if partial is not None and partial.error_message is not None:
        partial = replace(
            partial,
            error_message=redact_provider_text(partial.error_message),
        )
    return replace(
        error,
        message=redact_provider_text(error.message),
        partial=partial,
    )


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


def _extract_code(body: str, *, protocol: str | None = None) -> str | None:
    """Extract the provider's standard code from any common error envelope."""

    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        return None
    return _find_code(value, protocol=protocol)


def _is_generic_code(code: str) -> bool:
    return code.lower().replace("-", "_") in {
        "error",
        "provider_error",
        "invalid_argument",
        "invalid_request",
        "bad_request",
    }


def _find_code(value: Any, *, protocol: str | None = None) -> str | None:
    if isinstance(value, Mapping):
        # Prefer a specific code on the current envelope. Generic wrapper
        # statuses such as INVALID_ARGUMENT still defer to nested provider
        # details from provider-specific nested error metadata.
        key_order = {
            "anthropic": ("type", "status"),
            "openai_chat": ("code", "type"),
            "openai_responses": ("code", "type"),
        }.get(protocol, ("code", "status", "type"))
        for key in key_order:
            candidate = value.get(key)
            if (
                isinstance(candidate, str)
                and not _is_generic_code(candidate)
                and not candidate.lstrip("-").isdigit()
            ):
                return candidate
        for key in ("error", "details", "cause"):
            if key in value:
                nested = _find_code(value[key], protocol=protocol)
                if nested:
                    return nested
        for key in key_order:
            candidate = value.get(key)
            if isinstance(candidate, str) and not candidate.lstrip("-").isdigit():
                return candidate
        candidate = value.get("reason")
        if isinstance(candidate, str):
            return candidate
    elif isinstance(value, list):
        for item in value:
            nested = _find_code(item, protocol=protocol)
            if nested:
                return nested
    return None


def _redact_json(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _is_sensitive_key(key):
        return "[redacted]"
    if isinstance(value, Mapping):
        return {str(item_key): _redact_json(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    if normalized in _SENSITIVE_KEYS:
        return True
    if normalized.endswith("_tokens") or normalized in {
        "max_tokens",
        "budget_tokens",
        "token_limit",
        "context_tokens",
    }:
        return False
    return "token" in normalized or "secret" in normalized


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
