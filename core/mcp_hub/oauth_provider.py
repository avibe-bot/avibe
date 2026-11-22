"""The MCP SDK OAuth provider subclass for HTTP MCP Hub servers.

Its own module, imported only by ``oauth.make_provider``: importing it loads the
MCP SDK, which a controller with no HTTP MCP server never needs. Module scope
keeps one class object under concurrent first use (the import lock).
"""
from __future__ import annotations

import asyncio
import logging

from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

from core.mcp_hub.oauth import _write

logger = logging.getLogger("core.mcp_hub.oauth")


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
