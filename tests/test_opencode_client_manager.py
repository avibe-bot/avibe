from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.agents.opencode import client_manager as client_manager_module
from modules.agents.opencode.client_manager import OpenCodeClientManager


def test_reset_config_keeps_existing_server_manager_until_refresh_completes(monkeypatch) -> None:
    old_config = SimpleNamespace(binary="/old/opencode", port=4096, request_timeout_seconds=60)
    new_config = SimpleNamespace(binary="/new/opencode", port=4100, request_timeout_seconds=15)
    existing_server = SimpleNamespace()
    created = []

    async def _get_instance(**kwargs):
        created.append(kwargs)
        return existing_server

    monkeypatch.setattr(client_manager_module.OpenCodeServerManager, "get_instance", _get_instance)

    async def _run():
        manager = OpenCodeClientManager(old_config)
        first = await manager.get_server()
        previous = await manager.reset_config(new_config)
        second = await manager.get_server()
        return first, previous, second

    first, previous, second = asyncio.run(_run())
    assert first is existing_server
    assert previous is existing_server
    assert second is existing_server
    assert created == [
        {
            "binary": "/old/opencode",
            "port": 4096,
            "request_timeout_seconds": 60,
            "resource_governor": None,
        }
    ]
