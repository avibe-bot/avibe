"""Configured/keyless web-fetch service mappings.

The adapter owns credential custody and bounded HTTP/MCP transport. This module
only maps one already-completed response into fetch facts and billing evidence;
it never decides chain order or writes health.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from core.agent_core.messages import HostedCost

from .adapter import SearchResponse
from .events import bounded_diagnostic_detail

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


def _decode(body: bytes | None) -> Mapping[str, Any] | None:
    if body is None:
        return None
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    return value if isinstance(value, Mapping) else None


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


def _mcp_payload(body: Mapping[str, Any] | None) -> tuple[Mapping[str, Any] | None, bool]:
    if not isinstance(body, Mapping):
        return body, False
    result = body.get("result")
    if not isinstance(result, Mapping):
        return body, False
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping):
        return structured, result.get("isError") is True
    content = result.get("content")
    if isinstance(content, list):
        for item in content[:8]:
            if not isinstance(item, Mapping) or item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str):
                try:
                    value = json.loads(text)
                except (json.JSONDecodeError, RecursionError):
                    continue
                if isinstance(value, Mapping):
                    return value, result.get("isError") is True
    return body, result.get("isError") is True

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
    if service.get("vendor") == "tavily":
        items = _path(body, result.get("items"))
        if isinstance(items, list) and items:
            item = items[0] if isinstance(items[0], Mapping) else {}
            content = item.get(result.get("content", "raw_content"))
            title = item.get(result.get("title", "title"))
            final_url = item.get(result.get("url", "url"))
        failures = _path(body, result.get("failures"))
        if isinstance(failures, list) and failures:
            return None, None, None, False
    if service.get("vendor") == "exa":
        items = _path(body, result.get("items", "results"))
        statuses = _path(body, result.get("statuses"))
        if isinstance(statuses, list) and statuses:
            first = statuses[0]
            if isinstance(first, Mapping) and str(first.get("status", "")).lower() not in {"success", "completed"}:
                return None, None, None, False
        if isinstance(items, list) and items:
            item = items[0] if isinstance(items[0], Mapping) else {}
            content = item.get(result.get("content", "text"))
            title = item.get(result.get("title", "title"))
            final_url = item.get(result.get("url", "url"))
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
        title if isinstance(title, str) else None,
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
    body = _decode(response.body)
    status = response.http_status
    outcomes = service.get("outcomes") if isinstance(service.get("outcomes"), Mapping) else {}
    vendor = str(service.get("vendor") or "")
    mcp_error = False
    if vendor == "exa-mcp":
        body, mcp_error = _mcp_payload(body)
    free = vendor in {"exa-mcp", "jina-reader"}
    retry_after = next((value for key, value in response.headers.items() if key.lower() == "retry-after"), None)
    code: str | None = None
    target_failure = False
    if status in set(outcomes.get("credential") or ()):
        code = "service_key_invalid"
    elif status in set(outcomes.get("balance") or ()):
        code = "service_balance"
    elif status in set(outcomes.get("limit") or ()):
        code = "service_limit"
    elif status == 429:
        code = "service_rate_limited"
    elif status in set(outcomes.get("refused") or ()):
        code = "service_refused"
    elif status in set(outcomes.get("target_unreadable") or ()):
        code, target_failure = "fetch_target_unreadable", True
    elif vendor == "exa-mcp" and mcp_error:
        tag = _tag(body)
        if tag and tag.startswith("CRAWL_"):
            code, target_failure = "fetch_target_unreadable", True
        elif tag and tag.startswith("PROHIBITED"):
            code = "service_refused"
        else:
            code = "service_unavailable"
    elif _tag(body) and _tag(body) in {str(item).upper() for item in outcomes.get("refusal_tags", ())}:
        code = "service_refused"
    elif _tag(body) and _tag(body) in {str(item).upper() for item in outcomes.get("target_tags", ())}:
        code, target_failure = "fetch_target_unreadable", True
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
            billable = bool(sent and not free)
            listed = float((service.get("price") or {}).get("usd_per_1k", 0)) / 1000
            reported = _path(body, "costDollars.total") if vendor == "exa" else None
            valid_reported = (
                isinstance(reported, (int, float)) and not isinstance(reported, bool)
                and math.isfinite(float(reported)) and 0 <= float(reported) <= max(listed * 100, 1.0)
            )
            cost = ZERO if sent and free else (
                HostedCost(basis="reported", usd=float(reported)) if billable and valid_reported else
                HostedCost(basis="list", usd=listed) if billable else None
            )
            return FetchServiceResult(record, cost, billable, False, None, False, retry_after=retry_after,
                                      reported_usd=float(reported) if billable and valid_reported else None)
    if code is None:
        code = "service_unavailable"
    result = FetchServiceResult(
        _failed(code, say, detail=_provider_error(body) if code == "service_refused" else None),
        ZERO if sent and free else None,
        False,
        bool(sent and not free and status is None and not target_failure),
        {"service_key_invalid": "models.source.needs_action.credential_revoked",
         "service_balance": "models.source.needs_action.balance_exhausted",
         "service_limit": "models.source.needs_action.spend_limit_reached"}.get(code),
        not target_failure and code in {"service_unavailable", "service_unreadable", "service_rate_limited"},
        target_failure,
        retry_after,
        reported_usd=None,
    )
    return result
