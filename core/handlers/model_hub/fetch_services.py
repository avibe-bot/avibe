"""Web-fetch service mappings.

The adapter owns credential custody and bounded HTTP/MCP transport. This module
maps one completed response into fetch facts, billing evidence and target/service
scope; it never decides chain order or writes Source health.
"""
from __future__ import annotations

import json
import math
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


def _url_matches(value: Any, requested_url: str) -> bool:
    if not isinstance(value, str):
        return False
    if value == requested_url:
        return True
    try:
        left, right = urlsplit(value), urlsplit(requested_url)
        left_port, right_port = left.port, right.port
    except ValueError:
        return False
    return (
        left.username is None and left.password is None
        and right.username is None and right.password is None
        and left.scheme.lower() == right.scheme.lower()
        and (left.hostname or "").lower() == (right.hostname or "").lower()
        and left_port == right_port
        and left.path.rstrip("/") == right.path.rstrip("/")
        and left.query == right.query
        and not left.fragment
        and not right.fragment
    )


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


def _configured_content(
    service: Mapping[str, Any], body: Mapping[str, Any] | None, requested_url: str,
) -> tuple[str, str | None, str | None, bool]:
    """Return success/target/malformed plus bounded content facts for keyed APIs."""
    vendor = str(service.get("vendor") or "")
    result = service.get("result") if isinstance(service.get("result"), Mapping) else {}
    if not isinstance(body, Mapping):
        return "malformed", None, None, False
    if vendor == "tavily":
        rows = _path(body, result.get("items"))
        failures = _path(body, result.get("failures"))
        if not isinstance(rows, list) or not isinstance(failures, list):
            return "malformed", None, None, False
        if any(not isinstance(row, Mapping) for row in (*rows, *failures)):
            return "malformed", None, None, False
        row = next((item for item in rows if _url_matches(item.get("url"), requested_url)), None)
        if row is None:
            return "target", None, None, False
        content = row.get(result.get("content", "raw_content"))
        if not isinstance(content, str) or not content.strip():
            return "target", None, None, False
        title = row.get(result.get("title", "title"))
        return "success", content, title[:300] if isinstance(title, str) else None, False
    if vendor == "exa":
        rows = _path(body, result.get("items", "results"))
        statuses = _path(body, result.get("statuses"))
        if not isinstance(rows, list) or not isinstance(statuses, list):
            return "malformed", None, None, False
        if any(not isinstance(row, Mapping) for row in rows) or any(not isinstance(item, Mapping) for item in statuses):
            return "malformed", None, None, False
        row = next(
            (item for item in rows if _url_matches(item.get("id"), requested_url) or _url_matches(item.get("url"), requested_url)),
            None,
        )
        if row is None:
            return "target", None, None, False
        status_row = next(
            (item for item in statuses if _url_matches(item.get("id"), requested_url) or _url_matches(item.get("url"), requested_url)),
            statuses[0] if len(statuses) == 1 and not any(item.get("id") or item.get("url") for item in statuses) else None,
        )
        if status_row is None or not isinstance(status_row.get("status"), str):
            return "malformed", None, None, False
        if status_row["status"].lower() not in {"success", "completed"}:
            return "target", None, None, False
        content = row.get(result.get("content", "text"))
        if not isinstance(content, str) or not content.strip():
            return "target", None, None, False
        title = row.get(result.get("title", "title"))
        provider_truncated = any(
            value is True
            for key, value in _nested(body)
            if key.lower() in {"truncated", "istruncated", "is_truncated"}
        )
        return "success", content, title[:300] if isinstance(title, str) else None, provider_truncated
    if vendor == "deepseek":
        content_key = result.get("content", "content")
        if content_key not in body or not isinstance(body.get(content_key), str):
            return "malformed", None, None, False
        content = body[content_key]
        if not content.strip():
            return "target", None, None, False
        title = body.get(result.get("title", "title"))
        return "success", content, title[:300] if isinstance(title, str) else None, False
    return "malformed", None, None, False


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
    authenticated: bool = False


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
    """Map one completed response without confusing target facts with service health."""
    body, decoded = _decode(response.body)
    status = response.http_status
    outcomes = service.get("outcomes") if isinstance(service.get("outcomes"), Mapping) else {}
    vendor = str(service.get("vendor") or "")
    if vendor not in {"exa-mcp", "jina-reader", "tavily", "exa", "deepseek"}:
        raise ValueError("unsupported web-fetch service mapping")
    free = bool(service.get("source_id"))
    billing = service.get("billing") if isinstance(service.get("billing"), Mapping) else {}
    unbilled_errors = {str(item) for item in billing.get("unbilled_errors", ())}
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
    uncertain = False
    authenticated = False
    content = title = final_url = None
    provider_truncated = False

    if status in set(outcomes.get("credential") or ()):
        code = "service_key_invalid"
    elif status in set(outcomes.get("balance") or ()):
        code = "service_balance"
        authenticated = True
    elif status in set(outcomes.get("limit") or ()):
        code = "service_limit"
        authenticated = True
    elif status == 429:
        code = "service_rate_limited"
    elif status in set(outcomes.get("refused") or ()):
        code = "service_refused"
    elif status in set(outcomes.get("target_unreadable") or ()):
        code, target_failure = "fetch_target_unreadable", True
    elif vendor == "exa-mcp" and _keyless_rate_limited(status, body):
        code = "service_rate_limited"
    elif tag and tag in refusal_tags and (vendor != "exa-mcp" or mcp_error):
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
    elif response.error in {"timeout", "connection", "redirect", "body_too_large", "invalid_endpoint", "cancelled"} and status != 200:
        code = "service_unreadable" if response.error == "body_too_large" else "service_unavailable"
        uncertain = sent and not free and response.error not in unbilled_errors and status is None
    elif status == 200 and not decoded:
        # A 200 whose body cannot be recovered is not evidence of an empty target.
        code = "service_unreadable" if response.error == "body_too_large" else "service_unavailable"
        uncertain = sent and not free
    elif status != 200:
        code = "service_unavailable" if status is None or status >= 500 else "service_refused"
    elif vendor in {"tavily", "exa", "deepseek"}:
        outcome, content, title, provider_truncated = _configured_content(service, body, requested_url)
        if outcome == "malformed":
            code, uncertain = "service_unreadable", sent and not free
        elif outcome == "target":
            code, target_failure = "fetch_target_unreadable", True
        else:
            if vendor == "exa" and isinstance(body, Mapping):
                rows = _path(body, (service.get("result") or {}).get("items", "results"))
                row = next(
                    (item for item in rows if isinstance(item, Mapping)
                     and (_url_matches(item.get("id"), requested_url) or _url_matches(item.get("url"), requested_url))),
                    None,
                ) if isinstance(rows, list) else None
                final_url = _url_value(row.get((service.get("result") or {}).get("url", "url"))) if row else None
            authenticated = True
    else:
        content, title, final_url, provider_truncated = _content(service, body)
        if content is None:
            code, target_failure = "fetch_target_unreadable", True
        else:
            authenticated = True

    if status == 200 and vendor in {"tavily", "exa", "deepseek"}:
        # A response from the configured endpoint is positive credential
        # evidence even when its body is malformed or target-scoped.
        authenticated = True
    if code is None:
        listed = float((service.get("price") or {}).get("usd_per_1k", 0)) / 1000
        if free:
            cost = ZERO if sent else None
            return FetchServiceResult(
                {"status": "completed", "requested_url": requested_url, "content": content,
                 "title": title, "final_url": final_url, "source_content_type": None,
                 "source_size_bytes": None, "provider_truncated": provider_truncated},
                cost, False, False, None, False, retry_after=retry_after, authenticated=True,
            )
        reported_usd = None
        result_spec = service.get("result") if isinstance(service.get("result"), Mapping) else {}
        reported_spec = result_spec.get("reported_cost") if isinstance(result_spec.get("reported_cost"), Mapping) else None
        reported = _path(body, reported_spec.get("path")) if reported_spec else None
        ceiling = reported_spec.get("max_usd") if reported_spec else None
        if not isinstance(ceiling, (int, float)) or isinstance(ceiling, bool) or ceiling < 0:
            ceiling = listed * 100
        if isinstance(reported, (int, float)) and not isinstance(reported, bool):
            try:
                if math.isfinite(float(reported)) and 0 <= float(reported) <= float(ceiling):
                    reported_usd = float(reported)
            except (OverflowError, ValueError):
                pass
        cost = HostedCost(
            basis="reported", usd=reported_usd, estimate=bool(reported_spec.get("estimate"))
        ) if reported_usd is not None else HostedCost(basis="list", usd=listed)
        return FetchServiceResult(
            {"status": "completed", "requested_url": requested_url, "content": content,
             "title": title, "final_url": final_url, "source_content_type": None,
             "source_size_bytes": None, "provider_truncated": provider_truncated},
            cost if sent else None, bool(sent), False, None, False,
            retry_after=retry_after, reported_usd=reported_usd, authenticated=True,
        )

    blocker = {
        "service_key_invalid": "models.source.needs_action.credential_revoked",
        "service_balance": "models.source.needs_action.balance_exhausted",
        "service_limit": "models.source.needs_action.spend_limit_reached",
    }.get(code)
    return FetchServiceResult(
        _failed(code, say, detail=_provider_error(body) if code == "service_refused" else None),
        ZERO if sent and not uncertain else None,
        False,
        uncertain,
        blocker,
        not target_failure and code in {"service_unavailable", "service_unreadable", "service_rate_limited"},
        target_failure,
        retry_after,
        authenticated=authenticated,
    )
