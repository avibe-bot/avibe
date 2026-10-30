"""Web API for MCP Hub (``/api/mcp*``).

Reads come from published artifacts. Mutations are Controller-owned operations
over control IPC. Env/header values never leave the owner files.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.mcp_hub import repository
from core.mcp_hub.definition import parse_provider_json, provider_entry
from core.mcp_hub.errors import McpHubError

_STATUS = {"usage": 400, "not_found": 404, "exists": 409, "unavailable": 503, "unknown_outcome": 502}


def _error(exc: McpHubError) -> tuple[dict[str, Any], int]:
    status = _STATUS.get(exc.type, 400)
    return {"ok": False, "error": exc.payload()}, status


async def _owner(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    from vibe import internal_client

    try:
        response = await internal_client.mcp_request(operation, payload)
    except internal_client.InternalServerUnavailable:
        raise McpHubError("unavailable", "The Avibe service is not running.") from None
    except internal_client.InternalServerTimeout:
        raise McpHubError("unknown_outcome", "The Avibe service connection broke; refresh to see the result.") from None
    if not response.get("ok"):
        if operation == "add" and (response.get("failed") or response.get("unchanged") is not None):
            return response
        raise McpHubError.from_payload(response.get("error"))
    return response


def _servers_payload(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"name": item["name"], "server": provider_entry(item)}
        for item in definitions
    ]


async def run(handler, *args) -> tuple[dict[str, Any], int]:
    try:
        return await handler(*args), 200
    except McpHubError as cop:
        return _error(cop)


async def list_servers() -> dict[str, Any]:
    return {"ok": True, "servers": await asyncio.to_thread(repository.list_packages)}


async def add(body: dict[str, Any]) -> dict[str, Any]:
    definitions = parse_provider_json(str(body.get("text") or ""))
    payload = {"replace": body.get("replace") is True, "servers": _servers_payload(definitions)}
    response = await _owner("add", payload)
    parse_failed = getattr(definitions, "failures", None) or []
    if parse_failed:
        failed = list(response.get("failed") or []) + parse_failed
        response = {**response, "failed": failed, "ok": bool(response.get("added") or response.get("unchanged"))}
        if failed and not response.get("added"):
            summary = "; ".join(f"{item['name']}: {item.get('error') or 'failed'}" for item in failed)
            response["error"] = {"type": "add_failed" if not response.get("unchanged") else "add_partial", "message": summary, "failed": failed}
        elif failed:
            summary = "; ".join(f"{item['name']}: {item.get('error') or 'failed'}" for item in failed)
            response["error"] = {"type": "add_partial", "message": summary, "failed": failed}
    return response


async def detail(name: str) -> dict[str, Any]:
    def _read() -> dict[str, Any]:
        summary = repository.summarize(name)
        published = repository.read_published(name)
        payload: dict[str, Any] = {"ok": True, "server": summary}
        if published is not None:
            payload["tools"] = [
                {"name": tool.get("name"), "description": tool.get("description") or ""}
                for tool in published["tools"]
            ]
        return payload

    return await asyncio.to_thread(_read)


async def discover() -> dict[str, Any]:
    return await _owner("discover", {})


async def refresh(name: str) -> dict[str, Any]:
    return await _owner("refresh", {"name": name, "waitStdio": True})


async def remove(name: str) -> dict[str, Any]:
    return await _owner("remove", {"name": name})
