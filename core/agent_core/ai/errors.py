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
from urllib.parse import urlsplit

import httpx

from core.agent_core.messages import AssistantMessage
from core.agent_core.ai.provider import RETRYABLE_ERROR_KINDS, ProviderError

class ProviderStalled(TimeoutError):
    """The connected provider sent nothing for the whole silence bound."""

    def __init__(self, timeout_s: float) -> None:
        super().__init__(f"provider sent no data for {timeout_s:g}s")
        self.timeout_s = timeout_s


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
_BEARER_RE = re.compile(
    r"(?ix)\bBearer\s+"
    r"(?=[A-Za-z0-9._~+/=-]{8,}(?:\b|$))"
    r"(?=[A-Za-z0-9._~+/=-]*\d)"
    r"[A-Za-z0-9._~+/=-]+"
)
# Syntax only: the predicate below owns the vocabulary for JSON, free text,
# and configured headers alike. Scan prefixes only: a non-sensitive key never
# rescans or copies the remainder of the line.
_CREDENTIAL_KEY_RE = re.compile(
    r"""(?ix)
    (?<![a-z0-9_.-])
    (?P<key>["']?[a-z][a-z0-9_.-]*(?:[ \t]+[a-z][a-z0-9_.-]*){0,3}["']?)
    (?P<separator>[ \t]*[:=][ \t]*)
    """
)
_QUOTED_CREDENTIAL_VALUE_RE = re.compile(r""""(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'""")
_LINE_END_RE = re.compile(r"[\r\n]")
_AUTH_SCHEME_RE = re.compile(r"(?i)(Bearer|Basic|Digest|Token|AWS4-HMAC-SHA256)[ \t]+\S")
_DIAGNOSTIC_URL_RE = re.compile(r"""https?://[^\s"'<>\\]+""", re.IGNORECASE)
_TOKEN_SHAPE_RE = re.compile(
    r"(?ix)(?<![A-Za-z0-9_-])(?:"
    r"sk-[A-Za-z0-9][A-Za-z0-9._~+/=-]*|"
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
    "key",
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
    "auth",
    "auth_key",
    "access_key",
    "client_key",
    "refresh_key",
    "oauth_key",
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
) -> ProviderError:
    """Classify one HTTP, provider-body, or network failure."""

    message = _error_message(body, exc)
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
    retryable = kind in RETRYABLE_ERROR_KINDS and not streamed
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
        # A stall is not a connection failure: the provider was reached and
        # then stopped sending.
        if isinstance(exc, ProviderStalled):
            return "stalled"
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


def _error_message(body: str, exc: BaseException | None) -> str:
    if body:
        try:
            value = json.loads(body)
        except (TypeError, ValueError):
            return body.strip() or "provider request failed"
        extracted = _extract_message(value)
        if extracted:
            return extracted
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if exc is not None:
        return str(exc) or type(exc).__name__
    return "provider request failed"


def redact_provider_text(
    value: str,
    *,
    literal_secrets: tuple[str, ...] = (),
    endpoint_url: str | None = None,
) -> str:
    """Redact credentials from any provider diagnostic text."""

    # Parse first so URL punctuation and escaped quotes cannot corrupt JSON.
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, (Mapping, list)):
        return json.dumps(
            _redact_json(parsed, literal_secrets=literal_secrets, endpoint_url=endpoint_url),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return _redact_free_text(value, literal_secrets=literal_secrets, endpoint_url=endpoint_url)


def _redact_free_text(
    value: str,
    *,
    literal_secrets: tuple[str, ...] = (),
    endpoint_url: str | None = None,
) -> str:
    value = sanitize_endpoint_text(value, endpoint_url)
    # Decide key sensitivity before replacing literals so a configured
    # placeholder cannot destroy a credential key in a diagnostic.
    value = _redact_credential_pairs(value)
    for secret in _literal_secret_variants(literal_secrets):
        value = value.replace(secret, "[redacted]")
    value = _BEARER_RE.sub("Bearer [redacted]", value)
    return _TOKEN_SHAPE_RE.sub("[redacted]", value)


def redact_provider_error(
    error: ProviderError,
    *,
    literal_secrets: tuple[str, ...] = (),
    endpoint_url: str | None = None,
) -> ProviderError:
    """Apply the single provider-diagnostic redaction boundary."""

    partial = error.partial
    if partial is not None and partial.error_message is not None:
        partial = replace(
            partial,
            error_message=redact_provider_text(
                partial.error_message,
                literal_secrets=literal_secrets,
                endpoint_url=endpoint_url,
            ),
        )
    return replace(
        error,
        message=redact_provider_text(
            error.message,
            literal_secrets=literal_secrets,
            endpoint_url=endpoint_url,
        ),
        partial=partial,
    )


def _redact_credential_pairs(value: str) -> str:
    pieces: list[str] = []
    position = copied = 0
    while match := _CREDENTIAL_KEY_RE.search(value, position):
        position = match.end()
        key = match.group("key").strip("\"'")
        if not _is_sensitive_key(key):
            continue
        quoted = _QUOTED_CREDENTIAL_VALUE_RE.match(value, position)
        quote = value[position] if quoted else ""
        if quoted is not None:
            end = quoted.end()
        else:
            newline = _LINE_END_RE.search(value, position)
            end = newline.start() if newline else len(value)
        raw_start = position + bool(quote)
        scheme = _AUTH_SCHEME_RE.match(value, raw_start, end)
        replacement = "[redacted]"
        if key.lower().endswith("authorization") and scheme:
            replacement = f"{scheme.group(1)} [redacted]"
        pieces.extend((value[copied:position], quote, replacement, quote))
        position = copied = end
    pieces.append(value[copied:])
    return "".join(pieces)


def _literal_secret_variants(literal_secrets: tuple[str, ...]) -> tuple[str, ...]:
    variants: set[str] = set()
    for secret in literal_secrets:
        if not secret:
            continue
        escaped = secret.encode("unicode_escape").decode("ascii")
        for candidate in (secret, secret.strip(), escaped, escaped.strip()):
            if len(candidate) >= 8:
                variants.add(candidate)
    return tuple(sorted(variants, key=len, reverse=True))


def _credential_free_url(raw: str) -> str:
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return "[redacted-url]"
    if not parsed.scheme:
        return "[redacted-url]"
    host = parsed.hostname
    if host is None:
        authority = parsed.netloc.rsplit("@", 1)[-1]
        return f"{parsed.scheme}://{authority}{parsed.path or '/'}"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        host = f"{host}:{port}"
    identity = f"{parsed.scheme}://{host}{parsed.path or '/'}"
    return identity


def sanitize_endpoint_text(text: str, endpoint_url: str | None = None) -> str:
    """Strip URL credentials without consuming surrounding diagnostic syntax."""

    if endpoint_url:
        text = text.replace(endpoint_url, _credential_free_url(endpoint_url))

    def sanitize(match: re.Match[str]) -> str:
        raw = match.group(0)
        url = raw.rstrip(".,;:!?)]}")
        return _credential_free_url(url) + raw[len(url):]

    return _DIAGNOSTIC_URL_RE.sub(sanitize, text)


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


def _redact_json(
    value: Any,
    *,
    key: str | None = None,
    literal_secrets: tuple[str, ...] = (),
    endpoint_url: str | None = None,
) -> Any:
    if key is not None and _is_sensitive_key(key):
        return "[redacted]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_json(
                item_value,
                key=str(item_key),
                literal_secrets=literal_secrets,
                endpoint_url=endpoint_url,
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [
            _redact_json(item, literal_secrets=literal_secrets, endpoint_url=endpoint_url)
            for item in value
        ]
    if isinstance(value, str):
        return redact_provider_text(value, literal_secrets=literal_secrets, endpoint_url=endpoint_url)
    return value


def _is_sensitive_key(key: str) -> bool:
    camel_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    normalized = re.sub(r"[^a-z0-9]+", "_", camel_case.lower()).strip("_")
    if normalized in _SENSITIVE_KEYS:
        return True
    if normalized in {"tokenizer", "tokens", "token_count", "token_limit"} or normalized.endswith(
        ("_tokens", "_token_count", "_token_limit")
    ) or normalized in {
        "max_tokens",
        "budget_tokens",
        "context_tokens",
    }:
        return False
    parts = set(normalized.split("_"))
    if any(word in normalized for word in ("secret", "credential", "password")):
        return True
    if parts & {
        "authorization",
        "auth",
        "apikey",
        "password",
        "cookie",
        "credential",
        "credentials",
        "private",
        "secret",
        "token",
    }:
        return True
    return len(parts) >= 2 and (
        parts.intersection({"access", "client", "refresh", "oauth", "private", "auth", "api"})
        and parts.intersection({"key", "secret", "token"})
    )


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
