"""Durable OAuth primitives for HTTP MCP Hub servers."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from config import paths
from config.atomic_io import write_atomic
from core.mcp_hub.errors import McpHubError
from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata, OAuthClientInformationFull, OAuthMetadata, OAuthToken, ProtectedResourceMetadata

logger = logging.getLogger(__name__)


def oauth_path(name: str) -> Path:
    return paths.get_vibe_remote_dir() / "mcp" / name / "oauth.json"


def delete_oauth(name: str) -> None:
    try:
        oauth_path(name).unlink()
    except FileNotFoundError:
        pass


def read_state(name: str, server_url: str) -> dict:
    try:
        value = json.loads(oauth_path(name).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return value if isinstance(value, dict) and value.get("serverUrl") == server_url else {}


def _write(name: str, value: dict) -> None:
    write_atomic(oauth_path(name), json.dumps(value, ensure_ascii=False, indent=2) + "\n", mode=0o600)


class AvibeTokenStorage:
    def __init__(self, name: str, server_url: str, redirect_uri: str | None = None) -> None:
        self.name, self.server_url, self.redirect_uri = name, server_url, redirect_uri

    def raw(self) -> dict:
        return read_state(self.name, self.server_url)

    async def get_tokens(self) -> OAuthToken | None:
        value = (await asyncio.to_thread(self.raw)).get("tokens")
        return OAuthToken.model_validate(value) if isinstance(value, dict) else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        value = await asyncio.to_thread(self.raw)
        value.update(schemaVersion=1, serverUrl=self.server_url, tokens=tokens.model_dump(mode="json"))
        if tokens.expires_in is not None:
            value["expiresAt"] = int(time.time()) + int(tokens.expires_in)
        else:
            value.pop("expiresAt", None)
        await asyncio.to_thread(_write, self.name, value)
    async def get_client_info(self) -> OAuthClientInformationFull | None:
        value = (await asyncio.to_thread(self.raw)).get("client")
        if isinstance(value, dict) and self.redirect_uri is not None:
            uris = [str(item) for item in value.get("redirect_uris") or []]
            if self.redirect_uri not in uris:
                return None
        return OAuthClientInformationFull.model_validate(value) if isinstance(value, dict) else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        value = await asyncio.to_thread(self.raw)
        value.update(schemaVersion=1, serverUrl=self.server_url, client=client_info.model_dump(mode="json"))
        await asyncio.to_thread(_write, self.name, value)

    def drop_client(self) -> None:
        value = self.raw()
        value.pop("client", None)
        _write(self.name, value)

    async def clear_tokens(self) -> None:
        value = await asyncio.to_thread(self.raw)
        value.pop("tokens", None)
        value.pop("expiresAt", None)
        await asyncio.to_thread(_write, self.name, value)

class AvibeOAuthProvider(OAuthClientProvider):
    """SDK provider with persisted expiry, AS metadata and PRM."""

    async def _initialize(self) -> None:
        await super()._initialize()
        raw = await asyncio.to_thread(self.context.storage.raw) if hasattr(self.context.storage, "raw") else {}
        if raw.get("expiresAt") is not None:
            self.context.token_expiry_time = float(raw["expiresAt"])
        for key, attr, model in (("authServerMetadata", "oauth_metadata", OAuthMetadata), ("resourceMetadata", "protected_resource_metadata", ProtectedResourceMetadata)):
            if isinstance(raw.get(key), dict):
                try:
                    setattr(self.context, attr, model.model_validate(raw[key]))
                except Exception:
                    logger.warning("Ignoring invalid stored MCP OAuth metadata")
        configured = getattr(self, "_configured_client_info", None)
        if configured is not None:
            self.context.client_info = configured

    async def _handle_token_response(self, response) -> None:
        await super()._handle_token_response(response)
        raw = await asyncio.to_thread(self.context.storage.raw)
        if self.context.oauth_metadata is not None:
            raw["authServerMetadata"] = self.context.oauth_metadata.model_dump(mode="json")
        if self.context.protected_resource_metadata is not None:
            raw["resourceMetadata"] = self.context.protected_resource_metadata.model_dump(mode="json")
        await asyncio.to_thread(_write, self.context.storage.name, raw)

    async def _handle_refresh_response(self, response) -> bool:
        status = getattr(response, "status_code", 0)
        ok = await super()._handle_refresh_response(response)
        if not ok and status in {400, 401}:
            await self.context.storage.clear_tokens()
        return ok


def redirect_mode(origin: str | None, *, cloud_origin: str | None = None, cli: bool = False, port: int | None = None) -> tuple[str, str]:
    candidate = cloud_origin if cli and cloud_origin else origin
    parsed = urlparse(candidate or "")
    host = (parsed.hostname or "").lower()
    direct = parsed.scheme == "https" or (parsed.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"})
    if direct:
        return ("direct", f"{parsed.scheme}://{parsed.netloc}/api/mcp/oauth/callback")
    if port is None:
        raise McpHubError("usage", "A loopback setup port is required for MCP OAuth paste-mode redirects.")
    return ("paste", f"http://127.0.0.1:{int(port)}/api/mcp/oauth/callback")


def parse_callback_url(value: str) -> dict[str, str]:
    parsed = urlparse(value)
    if parsed.path != "/api/mcp/oauth/callback":
        raise McpHubError("invalid_callback", "The callback URL is not an Avibe MCP OAuth callback.")
    result = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
    if not result.get("code") or not result.get("state"):
        raise McpHubError("invalid_callback", "The callback URL is missing OAuth state or code.")
    return result


def make_provider(name: str, definition: dict, redirect_uri: str, *, redirect_handler, callback_handler, filter_redirect: bool = True) -> AvibeOAuthProvider:
    transport = definition["transport"]
    oauth = definition.get("oauth") or {}
    provider = AvibeOAuthProvider(
        transport["url"],
        OAuthClientMetadata(redirect_uris=[redirect_uri], client_name="Avibe MCP Hub", application_type="native", token_endpoint_auth_method="none", scope=oauth.get("scopes")),
        AvibeTokenStorage(name, transport["url"], redirect_uri if filter_redirect else None),
        redirect_handler=redirect_handler, callback_handler=callback_handler,
    )
    if isinstance(oauth.get("clientId"), str) and oauth["clientId"]:
        provider.context.client_info = OAuthClientInformationFull(client_id=oauth["clientId"], redirect_uris=[redirect_uri])
        provider._configured_client_info = provider.context.client_info
    return provider
