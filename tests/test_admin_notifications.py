"""Contracts for shared administrator text delivery."""

from __future__ import annotations

from core.handlers.admin_notifications import send_admin_text
from core.controller import Controller


class _Client:
    def __init__(self, result: object = True, *, fails: bool = False) -> None:
        self.result = result
        self.fails = fails
        self.calls: list[tuple[str, str]] = []

    async def send_dm(self, user_id: str, text: str, **_kwargs: object) -> object:
        self.calls.append((user_id, text))
        if self.fails:
            raise RuntimeError("delivery failed")
        return self.result


class _Controller:
    def __init__(self, clients: dict[str, _Client], fallback: _Client) -> None:
        self.im_clients = clients
        self.im_client = fallback


async def test_admin_text_routes_scoped_ids_and_isolates_delivery_failures() -> None:
    slack = _Client({"ok": True})
    discord = _Client(fails=True)
    fallback = _Client("message-id")
    controller = _Controller({"slack": slack, "discord": discord}, fallback)

    delivered = await send_admin_text(
        controller,
        ["slack::U1", "discord::D1", "telegram::123"],
        "Memory processing paused",
        log_label="Memory alert",
    )

    assert delivered == {"slack", "telegram"}
    assert slack.calls == [("U1", "Memory processing paused")]
    assert discord.calls == [("D1", "Memory processing paused")]
    assert fallback.calls == [("123", "Memory processing paused")]


class _Store:
    def get_admins(self) -> dict[str, object]:
        return {"slack::U1": object()}


class _SettingsManager:
    def get_store(self) -> _Store:
        return _Store()


class _ControllerCallbackStub:
    def __init__(self, client: _Client) -> None:
        self.settings_manager = _SettingsManager()
        self.im_clients = {"slack": client}
        self.im_client = client
        self.translation_calls: list[tuple[str, dict[str, object]]] = []

    def _t(self, key: str, **kwargs: object) -> str:
        self.translation_calls.append((key, kwargs))
        return key


async def test_memory_processing_callback_selects_copy_and_acks_delivery() -> None:
    client = _Client({"ok": True})
    controller = _ControllerCallbackStub(client)

    delivered = await Controller._send_memory_processing_event(
        controller,
        "fault",
        "credential",
        "2026-01-01T00:00:00.000Z",
        4,
    )

    assert delivered is True
    assert controller.translation_calls == [
        (
            "memory.alert.credential",
            {"occurred_at": "2026-01-01T00:00:00.000Z", "queued": 4},
        )
    ]
    assert client.calls == [("U1", "memory.alert.credential")]
