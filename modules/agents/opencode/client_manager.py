"""Client facade for talking to OpenCode server."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from .server import OpenCodeServerManager


class OpenCodeClientManager:
    """Lazily initializes and returns a shared OpenCodeServerManager instance."""

    def __init__(self, opencode_config):
        self._config = opencode_config
        self._server_manager: Optional[OpenCodeServerManager] = None
        self._lock = asyncio.Lock()
        self._resource_governor = None
        self._model_hub_overlay_preparer: (
            Callable[[], Awaitable[Any | None]] | None
        ) = None

    def set_resource_governor(self, governor) -> None:
        self._resource_governor = governor
        if self._server_manager is not None:
            self._server_manager.resource_governor = governor

    def set_model_hub_overlay_preparer(
        self,
        preparer: Callable[[], Awaitable[Any | None]],
    ) -> None:
        self._model_hub_overlay_preparer = preparer
        if self._server_manager is not None:
            self._server_manager.set_model_hub_overlay_preparer(preparer)

    async def get_server(self) -> OpenCodeServerManager:
        async with self._lock:
            if self._server_manager is None:
                self._server_manager = await OpenCodeServerManager.get_instance(
                    binary=self._config.binary,
                    port=self._config.port,
                    request_timeout_seconds=self._config.request_timeout_seconds,
                    resource_governor=self._resource_governor,
                )
                if self._model_hub_overlay_preparer is not None:
                    self._server_manager.set_model_hub_overlay_preparer(
                        self._model_hub_overlay_preparer
                    )
            return self._server_manager

    async def reset_config(self, opencode_config) -> Optional[OpenCodeServerManager]:
        async with self._lock:
            previous = self._server_manager
            self._config = opencode_config
            return previous
