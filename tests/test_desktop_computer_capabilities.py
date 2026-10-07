from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from core import internal_server
from vibe import internal_client
from vibe.ui_server import app


def test_controller_ipc_owns_desktop_capability_and_identity() -> None:
    """The UI process must not infer support from its own package version."""

    controller = SimpleNamespace(controller_id="controller-test")
    response = TestClient(internal_server.create_app(controller)).get(
        "/internal/desktop/capabilities"
    )
    assert response.status_code == 200
    assert response.json() == {
        "computer_use_schema": 1,
        "controller_id": "controller-test",
    }


def test_old_controller_unknown_endpoint_maps_to_schema_zero(monkeypatch) -> None:
    """A released pre-feature Controller is definitively unsupported."""

    endpoint = SimpleNamespace(
        base_url="http://controller",
        headers={},
        descriptor=None,
    )

    async def resolve(_socket_path):
        return endpoint

    monkeypatch.setattr(internal_client, "_resolve_endpoint_async", resolve)
    monkeypatch.setattr(
        internal_client,
        "_async_transport",
        lambda _endpoint: httpx.MockTransport(
            lambda _request: httpx.Response(404, json={"detail": "Not Found"})
        ),
    )

    assert asyncio.run(internal_client.desktop_capabilities()) == {
        "computer_use_schema": 0,
        "controller_id": "legacy",
    }


def test_ui_desktop_capabilities_are_forwarded_from_controller(monkeypatch) -> None:
    """The native shell receives the stable public contract over /api."""

    async def capabilities():
        return {
            "computer_use_schema": 1,
            "controller_id": "controller-test",
        }

    monkeypatch.setattr(internal_client, "desktop_capabilities", capabilities)
    response = app.test_client().get(
        "/api/desktop/capabilities",
        base_url="http://127.0.0.1:5123",
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "computer_use_schema": 1,
        "controller_id": "controller-test",
    }
    assert response.headers["Cache-Control"] == "no-store"
