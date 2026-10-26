"""Redacted diagnostics for failed hosted service attempts.

This module deliberately inspects only bounded transport envelopes and emits
stable fields. It never includes the request arguments or response content.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

from .adapter import SearchResponse


logger = logging.getLogger(__name__)


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
        code = error.get("code")
        if isinstance(code, (str, int)) and not isinstance(code, bool):
            return _safe_field(code)
    return None


def _mcp_is_error(values: tuple[Mapping[str, Any], ...]) -> bool:
    return any(
        isinstance(value.get("result"), Mapping) and value["result"].get("isError") is True
        for value in values
    )


def transport_cause(response: SearchResponse) -> str:
    """Return transport facts only, never provider prose or request content."""

    if response.error:
        return _safe_field(response.error)
    values = _json_objects(response.body)
    if (code := _jsonrpc_error_code(values)) is not None:
        return f"jsonrpc_error={code}"
    if _mcp_is_error(values):
        return "mcp_isError=true"
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


__all__ = ["log_service_failure", "transport_cause"]
