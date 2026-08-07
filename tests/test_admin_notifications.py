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
    def __init__(
        self,
        clients: dict[str, _Client],
        fallback: _Client,
        *,
        primary_platform: str = "slack",
    ) -> None:
        self.im_clients = clients
        self.im_client = fallback
        self.primary_platform = primary_platform


async def test_admin_text_routes_only_to_active_platform_clients() -> None:
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

    assert delivered == {"slack"}
    assert slack.calls == [("U1", "Memory processing paused")]
    assert discord.calls == [("D1", "Memory processing paused")]
    assert fallback.calls == []


async def test_admin_text_routes_legacy_unknown_users_to_the_primary_platform() -> None:
    telegram = _Client({"ok": True})
    fallback = _Client("message-id")
    controller = _Controller(
        {"telegram": telegram},
        fallback,
        primary_platform="telegram",
    )

    delivered = await send_admin_text(
        controller,
        ["unknown::123456", "wx_admin"],
        "Update complete",
        log_label="post-update notification",
    )

    assert delivered == {"telegram"}
    assert telegram.calls == [
        ("123456", "Update complete"),
        ("wx_admin", "Update complete"),
    ]
    assert fallback.calls == []


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
