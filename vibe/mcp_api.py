"""Web API for MCP Hub (``/api/mcp*``), shared by ``vibe/ui_server.py`` routes.

Reads come from the published artifacts in this process (they work with the
owner or the server down). Every mutation is the same Controller-owned operation
the CLI uses, over control IPC. Env/header values never leave the owner files:
responses carry masked definitions with ``has_<field>`` metadata.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.mcp_hub import repository
from core.mcp_hub.definition import masked_definition, parse_provider_json, provider_entry
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
        raise McpHubError.from_payload(response.get("error"))
    return response


def _servers_payload(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"name": item["name"], "server": provider_entry(item), "description": item.get("description")}
        for item in definitions
    ]


async def run(handler, *args) -> tuple[dict[str, Any], int]:
    try:
        return await handler(*args), 200
    except McpHubError as exc:
        return _error(exc)


async def list_servers() -> dict[str, Any]:
    return {"ok": True, "servers": await asyncio.to_thread(repository.list_packages)}


async def parse(body: dict[str, Any]) -> dict[str, Any]:
    definitions = parse_provider_json(str(body.get("text") or ""))

    def _preview() -> list[dict[str, Any]]:
        existing = set(repository.list_names())
        return [{**masked_definition(item), "exists": item["name"] in existing} for item in definitions]

    return {"ok": True, "servers": await asyncio.to_thread(_preview)}


async def save(body: dict[str, Any]) -> dict[str, Any]:
    definitions = parse_provider_json(str(body.get("text") or ""))
    # Same rule as PATCH: a blank env/header value keeps the stored one.
    payload = {
        "replace": body.get("replace") is True,
        "preserveSecrets": True,
        "servers": _servers_payload(definitions),
    }
    return await _owner("save", payload)


async def detail(name: str) -> dict[str, Any]:
    def _read() -> dict[str, Any]:
        summary = repository.summarize(name)
        payload: dict[str, Any] = {"ok": True, "server": summary}
        published = repository.read_published(name)
        native_error = summary.get("error")
        try:
            payload["definition"] = masked_definition(repository.read_definition(name))
        except McpHubError:
            if published is not None:
                payload["definition"] = masked_definition(published["definition"])
            elif native_error:
                payload["partial"] = True
        return payload

    return await asyncio.to_thread(_read)


async def update(name: str, body: dict[str, Any]) -> dict[str, Any]:
    """Replace the definition; blank env/header values keep the stored ones."""

    from core.mcp_hub.definition import definition_from_provider

    definition = definition_from_provider(name, body.get("server"), description=body.get("description"))
    payload = {"replace": True, "preserveSecrets": True, "servers": _servers_payload([definition])}
    return await _owner("save", payload)


async def tools(name: str) -> dict[str, Any]:
    def _read() -> dict[str, Any]:
        published = repository.read_published(name)
        if published is None:
            repository.read_definition(name)
            raise McpHubError("not_generated", f"{name} has not been generated yet.")
        return {
            "ok": True,
            "revision": published["revision"],
            "tools": [
                {**tool, "flags": command["flags"], "validated": command["validated"]}
                for tool, command in zip(published["tools"], published["commands"])
            ],
        }

    return await asyncio.to_thread(_read)


async def operate(operation: str, name: str) -> dict[str, Any]:
    if operation not in {"generate", "enable", "disable", "remove"}:
        raise McpHubError("not_found", "Unknown MCP operation.")
    return await _owner(operation, {"name": name})
