"""Keyless web-fetch service mappings.

The adapter owns bounded HTTP/MCP transport. This module maps completed free
service responses into fetch facts; configured paid mappings ship separately.
It never decides chain order or writes health.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from core.agent_core.messages import HostedCost

from .adapter import SearchResponse
from .events import bounded_diagnostic_detail
from .search_services import _keyless_rate_limited

ZERO = HostedCost(basis="list", usd=0)


def _path(value: Any, path: str | None) -> Any:
    if not path:
        return None
    for part in path.split("."):
        value = value.get(part) if isinstance(value, Mapping) else None
    return value


def _nested(value: Any, *, depth: int = 0):
    if depth > 5:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key), item
            yield from _nested(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value[:16]:
            yield from _nested(item, depth=depth + 1)


def _decode(body: bytes | None) -> tuple[Mapping[str, Any] | None, bool]:
    if body is None:
        return None, False
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None, False
    return (value, True) if isinstance(value, Mapping) else (None, False)


def _provider_error(body: Mapping[str, Any] | None) -> str | None:
    if not isinstance(body, Mapping):
        return None
    for key, value in _nested(body):
        if key.lower() in {"message", "detail", "error", "text"} and isinstance(value, str):
            return bounded_diagnostic_detail(value)
    return None


def _tag(body: Mapping[str, Any] | None) -> str | None:
    if not isinstance(body, Mapping):
        return None
    for key, value in _nested(body):
        if key.lower() in {"tag", "code", "type"} and isinstance(value, str):
            upper = value.upper()
            if upper.startswith(("CRAWL_", "PROHIBITED_", "CONTENT_FILTER")):
                return upper
    return None


_EXA_MARKDOWN_HEADER = re.compile(
    r"\A(?:#\s+|Title:\s*)(?P<title>[^\r\n]*)\r?\n"
    r"URL:\s*(?P<url>https?://[^\r\n]*)\r?\n"
    r"(?:(?:Published|Author):[^\r\n]*\r?\n)*"
    r"(?:\r?\n)?(?P<content>.*)\Z",
    re.DOTALL,
)
_EXA_NO_CONTENT = re.compile(r"\ANo content found(?: for the provided URL\(s\))?\.\Z", re.IGNORECASE)
_EXA_CRAWL_TAG = re.compile(r"\b(CRAWL_[A-Z0-9_]+)\b")


def _exa_markdown_payload(text: str) -> dict[str, Any]:
    """Keep one Exa Markdown block intact, peeling only its leading wrapper."""
    match = _EXA_MARKDOWN_HEADER.match(text)
    if match is None:
        stripped = text.strip()
        payload: dict[str, Any] = {"content": text}
        tag = _EXA_CRAWL_TAG.search(stripped) if stripped.startswith("Error fetching URL(s):") else None
        if _EXA_NO_CONTENT.fullmatch(stripped) or _EXA_CRAWL_TAG.fullmatch(stripped) or tag:
            payload["_mcp_target_failure"] = True
            if tag:
                payload["tag"] = tag.group(1)
        return payload
    payload = {
        "content": match.group("content"),
        "title": match.group("title"),
        "url": match.group("url"),
    }
    if not match.group("content").strip():
        payload["_mcp_target_failure"] = True
    return payload


def _mcp_payload(body: Mapping[str, Any] | None) -> tuple[Mapping[str, Any] | None, bool]:
    """Separate RPC/tool failures from successfully returned document text."""
    if not isinstance(body, Mapping):
        return body, False
    if body.get("error") is not None:
        return body, True
    result = body.get("result")
    if not isinstance(result, Mapping):
        return body, True
    is_error = result.get("isError") is True

    def normalize(payload: Mapping[str, Any]):
        normalized = dict(payload)
        metadata = result.get("_meta")
        if isinstance(metadata, Mapping):
            normalized["_meta"] = dict(metadata)
        # Missing/wrong-shaped payloads are protocol faults, not proof of an
        # empty target. A genuine empty content string remains target-scoped.
        failed = is_error or not isinstance(normalized.get("content"), str)
        if failed:
            normalized["_mcp_is_error"] = True
        return normalized, failed

    structured = result.get("structuredContent")
    if isinstance(structured, Mapping):
        return normalize(structured)
    content = result.get("content")
    if isinstance(content, list):
        for item in content[:8]:
            if not isinstance(item, Mapping) or item.get("type") != "text":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            if is_error:
                try:
                    value = json.loads(text)
                except (json.JSONDecodeError, RecursionError):
                    value = None
                if isinstance(value, Mapping):
                    return normalize(value)
            # Successful text blocks are documents regardless of their JSON
            # keys; structured provider results belong in structuredContent.
            value = _exa_markdown_payload(text)
            if is_error:
                value["text"] = text
            return normalize(value)
    return body, True

def _url_value(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 8192:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value


def _content(service: Mapping[str, Any], body: Mapping[str, Any] | None) -> tuple[str | None, str | None, str | None, bool]:
    result = service.get("result") if isinstance(service.get("result"), Mapping) else {}
    content = _path(body, result.get("content"))
    title = _path(body, result.get("title"))
    final_url = _path(body, result.get("url"))
    if service.get("vendor") == "exa-mcp":
        content = _path(body, result.get("content", "content"))
        title = _path(body, result.get("title", "title"))
        final_url = _path(body, result.get("url", "url"))
    if service.get("vendor") == "jina-reader" and isinstance(body, Mapping):
        data = body.get("data")
        if isinstance(data, Mapping):
            content = data.get("content")
            title = data.get("title")
            final_url = data.get("url")
    provider_truncated = any(
        value is True
        for key, value in _nested(body)
        if key.lower() in {"truncated", "istruncated", "is_truncated"}
    )
    return (
        content if isinstance(content, str) and content.strip() else None,
        title[:300] if isinstance(title, str) else None,
        _url_value(final_url),
        provider_truncated,
    )


@dataclass(frozen=True)
class FetchServiceResult:
    record: dict[str, Any]
    cost: HostedCost | None
    billable: bool
    uncertain: bool
    blocker: str | None
    cooling: bool
    target_failure: bool = False
    retry_after: str | None = None
    reported_usd: float | None = None


def _failed(code: str, say: Callable[..., str], *, detail: str | None = None) -> dict[str, Any]:
    error = say(f"hostedTools.errorCode.{code}")
    if detail:
        error = f"{error} ({detail})"
    return {"status": "failed", "error": error[:1000], "error_code": code, "content": error[:1000]}


def map_fetch_response(
    service: Mapping[str, Any],
    response: SearchResponse,
    *,
    requested_url: str,
    sent: bool,
    say: Callable[..., str],
) -> FetchServiceResult:
    """Map one service response, keeping target failures separate from service health."""
    body, decoded = _decode(response.body)
    status = response.http_status
    outcomes = service.get("outcomes") if isinstance(service.get("outcomes"), Mapping) else {}
    vendor = str(service.get("vendor") or "")
    if vendor not in {"exa-mcp", "jina-reader"}:
        raise ValueError("configured fetch mappings are not available in this build")
    mcp_error = False
    mcp_target_failure = False
    if vendor == "exa-mcp":
        body, mcp_error = _mcp_payload(body)
        mcp_target_failure = bool(isinstance(body, Mapping) and body.get("_mcp_target_failure") is True)
    retry_after = next((value for key, value in response.headers.items() if key.lower() == "retry-after"), None)
    tag = _tag(body)
    refusal_tags = {str(item).upper() for item in outcomes.get("refusal_tags", ())}
    code: str | None = None
    target_failure = False
    if status == 429:
        code = "service_rate_limited"
    elif status in set(outcomes.get("refused") or ()):
        code = "service_refused"
    elif status in set(outcomes.get("target_unreadable") or ()):
        code, target_failure = "fetch_target_unreadable", True
    elif vendor == "exa-mcp" and _keyless_rate_limited(status, body):
        code = "service_rate_limited"
    elif mcp_error and tag and tag in refusal_tags:
        code = "service_refused"
    elif vendor == "exa-mcp" and mcp_target_failure:
        code, target_failure = "fetch_target_unreadable", True
        if tag and not tag.startswith("CRAWL_"):
            code, target_failure = "service_unavailable", False
    elif vendor == "exa-mcp" and mcp_error:
        if tag and tag.startswith("CRAWL_"):
            code, target_failure = "fetch_target_unreadable", True
        elif tag and tag.startswith("PROHIBITED"):
            code = "service_refused"
        else:
            code = "service_unavailable"
    elif status == 200 and not decoded:
        # A provider envelope that cannot be decoded is a service fault, not
        # evidence that this particular target was unreadable.
        code = "service_unreadable" if response.error == "body_too_large" else "service_unavailable"
    elif response.error in {"timeout", "connection", "redirect", "body_too_large", "invalid_endpoint"}:
        code = "service_unavailable" if response.error != "body_too_large" else "service_unreadable"
    elif status != 200:
        code = "service_unavailable" if status is None or status >= 500 else "service_refused"
    else:
        content, title, final_url, provider_truncated = _content(service, body)
        if content is None:
            code, target_failure = "fetch_target_unreadable", True
        else:
            record = {
                "status": "completed",
                "requested_url": requested_url,
                "content": content,
                "title": title,
                "final_url": final_url,
                "source_content_type": None,
                "source_size_bytes": None,
                "provider_truncated": provider_truncated,
            }
            return FetchServiceResult(record, ZERO if sent else None, False, False, None, False,
                                      retry_after=retry_after)
    if code is None:
        code = "service_unavailable"
    result = FetchServiceResult(
        _failed(code, say, detail=_provider_error(body) if code == "service_refused" else None),
        ZERO if sent else None,
        False,
        False,
        None,
        not target_failure and code in {"service_unavailable", "service_unreadable", "service_rate_limited"},
        target_failure,
        retry_after,
        reported_usd=None,
    )
    return result
