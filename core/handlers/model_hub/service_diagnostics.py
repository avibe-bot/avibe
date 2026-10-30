"""Redacted diagnostics for failed hosted service and local fetch attempts.

This module deliberately inspects only bounded transport envelopes and emits
stable fields. It never includes the request arguments or response content.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from .adapter import SearchResponse


logger = logging.getLogger(__name__)
_JSONRPC_ERROR_CODE_FIELD = "code"
_CRAWL_ERROR_TAG = re.compile(r"CRAWL_[A-Z_]{1,40}")


def _safe_field(value: object) -> str:
    """Keep a structured log field on one line without carrying user text."""

    return str(value).replace("\r", " ").replace("\n", " ")[:128]


def _json_objects(body: bytes | None) -> tuple[Mapping[str, Any], ...]:
    if not body:
        return ()
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return ()
    values: list[Mapping[str, Any]] = []
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        value = None
    if isinstance(value, Mapping):
        values.append(value)
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            value = json.loads(data)
        except (json.JSONDecodeError, RecursionError):
            continue
        if isinstance(value, Mapping):
            values.append(value)
    return tuple(values)


def _jsonrpc_error_code(values: tuple[Mapping[str, Any], ...]) -> str | None:
    for value in values:
        error = value.get("error")
        if not isinstance(error, Mapping):
            continue
        code = error.get(_JSONRPC_ERROR_CODE_FIELD)
        if isinstance(code, (str, int)) and not isinstance(code, bool):
            return _safe_field(code)
    return None


def _mcp_is_error(values: tuple[Mapping[str, Any], ...]) -> bool:
    return any(
        isinstance(value.get("result"), Mapping) and value["result"].get("isError") is True
        for value in values
    )


def _mcp_crawl_error_tag(values: tuple[Mapping[str, Any], ...]) -> str | None:
    for value in values:
        result = value.get("result")
        if not isinstance(result, Mapping) or result.get("isError") is not True:
            continue
        content = result.get("content")
        if not isinstance(content, list):
            continue
        for item in content[:8]:
            if not isinstance(item, Mapping) or item.get("type") != "text":
                continue
            text = item.get("text")
            # Only the provider error wrapper, never a successful page's text.
            if isinstance(text, str) and len(text) <= 16384 and text.startswith("Error fetching URL(s):"):
                # The tag follows the final ': '; a tag-looking URL path/query
                # is not a cause and must never be copied into a diagnostic.
                if match := _CRAWL_ERROR_TAG.fullmatch(text.rsplit(": ", 1)[-1].strip()):
                    return match.group()
    return None


def transport_cause(response: SearchResponse) -> str:
    """Return transport facts only, never provider prose or request content."""

    if response.error:
        return _safe_field(response.error)
    values = _json_objects(response.body)
    if (code := _jsonrpc_error_code(values)) is not None:
        return f"jsonrpc_error={code}"
    if _mcp_is_error(values):
        return _mcp_crawl_error_tag(values) or "mcp_isError=true"
    if response.http_status is not None:
        return f"http_status={response.http_status}"
    return "unknown"


async def log_service_failure(
    *,
    tool: str,
    source_id: str,
    service_id: str,
    outcome: str,
    error_code: str,
    response: SearchResponse,
    elapsed_ms: int,
) -> None:
    """Emit one redacted warning without parsing the response on the event loop."""

    await asyncio.to_thread(
        _write_service_failure,
        tool=tool,
        source_id=source_id,
        service_id=service_id,
        outcome=outcome,
        error_code=error_code,
        response=response,
        elapsed_ms=elapsed_ms,
    )


def _write_service_failure(
    *,
    tool: str,
    source_id: str,
    service_id: str,
    outcome: str,
    error_code: str,
    response: SearchResponse,
    elapsed_ms: int,
) -> None:
    """Write exactly one redacted warning for one failed service attempt."""

    phase = next(
        (value for name, value in response.headers.items() if name.lower() == "x-avibe-mcp-phase"),
        None,
    )
    message = (
        "Model Hub service attempt failed "
        "tool=%s source_id=%s service_id=%s outcome=%s error_code=%s "
        "transport_cause=%s elapsed_ms=%d"
    )
    values: list[Any] = [
        _safe_field(tool),
        _safe_field(source_id),
        _safe_field(service_id),
        _safe_field(outcome),
        _safe_field(error_code),
        transport_cause(response),
        max(0, int(elapsed_ms)),
    ]
    if phase is not None:
        message += " phase=%s"
        values.append(_safe_field(phase))
    logger.warning(message, *values)


async def log_local_failure(
    *, error_code: str, http_status: int | None, hop_count: int,
    phase: str, transport_degraded: bool | None, elapsed_ms: int,
) -> None:
    """One warning per failed local attempt; no request or header inputs.

    Causes are closed categories, not exception messages. Unknown HTTP exchange
    failures share http_status (which is None before any response). extract_empty
    includes rejected/unusable extraction, not only a zero-length body.
    """
    if error_code == "fetch_dns_unverified":
        cause = "dns"
    elif error_code in {"fetch_url_blocked", "fetch_refused"}:
        cause = "policy"
    elif error_code in {"fetch_timeout", "service_busy"}:
        cause = "timeout"
    elif error_code == "fetch_challenge":
        cause = "challenge"
    elif phase == "loop":
        cause = "loop"
    elif phase == "extract":
        cause = "extract_empty"
    else:
        cause = "http_status"
    transport = "unknown" if transport_degraded is None else "degraded" if transport_degraded else "native"
    await asyncio.to_thread(
        logger.warning,
        "Model Hub local attempt failed tool=web_fetch source_id=src_localfetch "
        "error_code=%s http_status=%s hop_count=%d cause=%s transport=%s elapsed_ms=%d",
        _safe_field(error_code), http_status, hop_count, cause, transport, max(0, int(elapsed_ms)),
    )


__all__ = ["log_local_failure", "log_service_failure", "transport_cause"]
