"""Web API for MCP Hub (``/api/mcp*``).

Reads come from published artifacts. Mutations are Controller-owned operations
over control IPC. Env/header values never leave the owner files.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from core.mcp_hub.definition import parse_provider_json, provider_entry
from core.mcp_hub.errors import McpHubError

project_context: ContextVar[str | None] = ContextVar("mcp_project", default=None)

_STATUS = {"usage": 400, "not_found": 404, "exists": 409, "unavailable": 503, "unknown_outcome": 502, "auth_required": 401, "invalid_callback": 400, "oauth": 400, "internal": 500}


def _error(exc: McpHubError) -> tuple[dict[str, Any], int]:
    status = _STATUS.get(exc.type, 400)
    return {"ok": False, "error": exc.payload()}, status


def _with_project(payload: dict[str, Any]) -> dict[str, Any]:
    project = project_context.get()
    return {**payload, "project": project} if project else payload


async def _owner(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    from vibe import internal_client

    try:
        response = await internal_client.mcp_request(operation, _with_project(payload))
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
    except Exception as exc:  # noqa: BLE001 - Web MCP must not leak a string envelope
        return _error(McpHubError("internal", str(exc) or "Unexpected MCP Hub error."))


async def list_servers(origin: str | None = None, port: int | None = None) -> dict[str, Any]:
    from core.mcp_hub.oauth import redirect_mode

    body = await _owner("list", {})
    try:
        body["oauthMode"] = redirect_mode(origin, port=port)[0]
    except McpHubError:
        body["oauthMode"] = "paste"
    return body


async def add(body: dict[str, Any]) -> dict[str, Any]:
    from core.mcp_hub.scope import project_add_definition
    from core.mcp_hub.definition import definition_from_provider

    validator = project_add_definition if project_context.get() else definition_from_provider
    definitions = parse_provider_json(str(body.get("text") or ""), validator=validator)
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
    return await _owner("detail", {"name": name})


async def discover() -> dict[str, Any]:
    return await _owner("discover", {})


async def refresh(name: str) -> dict[str, Any]:
    return await _owner("refresh", {"name": name, "waitStdio": True})


async def remove(name: str) -> dict[str, Any]:
    return await _owner("remove", {"name": name})


async def oauth_start(name: str, body: dict[str, Any], origin: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name, "origin": origin}
    if body.get("port") is not None:
        payload["port"] = body["port"]
    if body.get("cli") is True:
        payload["cli"] = True
    if body.get("cloudOrigin"):
        payload["cloudOrigin"] = body["cloudOrigin"]
    return await _owner("oauth_start", payload)


async def oauth_callback(body: dict[str, Any]) -> dict[str, Any]:
    return await _owner("oauth_callback", body)


async def oauth_complete(name: str, callback_url: str) -> dict[str, Any]:
    return await _owner("oauth_complete", {"name": name, "callbackUrl": callback_url})


async def oauth_logout(name: str) -> dict[str, Any]:
    return await _owner("oauth_logout", {"name": name})


async def approve(name: str) -> dict[str, Any]:
    return await _owner("approve", {"name": name})


async def configure(name: str, body: dict) -> dict[str, Any]:
    return await _owner("configure", {"name": name, "secrets": body})
