"""Contracts for shared administrator text delivery."""

from __future__ import annotations

import logging

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
        "Update available",
        log_label="update notification",
    )

    assert delivered == {"slack"}
    assert slack.calls == [("U1", "Update available")]
    assert discord.calls == [("D1", "Update available")]
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
    def __init__(self) -> None:
        self.get_admins_calls = 0

    def get_admins(self) -> dict[str, object]:
        self.get_admins_calls += 1
        return {
            "slack::U1": object(),
            "wechat::W1": object(),
        }


class _SettingsManager:
    def __init__(self) -> None:
        self.store = _Store()

    def get_store(self) -> _Store:
        return self.store


class _ControllerCallbackStub:
    def __init__(self) -> None:
        self.settings_manager = _SettingsManager()
        self.im_clients = {
            "slack": _Client({"ok": True}),
            "wechat": _Client({"ok": True}),
        }
        self.im_client = self.im_clients["slack"]
        self.primary_platform = "slack"


async def test_memory_indep_012_processing_events_stay_in_service_logs(caplog) -> None:
    """MEMORY-INDEP-012: processing health stays observable without IM delivery."""

    controller = _ControllerCallbackStub()

    with caplog.at_level(logging.INFO, logger="core.controller"):
        results = [
            await Controller._log_memory_processing_event(
                controller,
                "fault",
                "credential",
                "2026-01-01T00:00:00.000Z",
                4,
            ),
            await Controller._log_memory_processing_event(
                controller,
                "fault",
                "engine",
                "2026-01-01T00:01:00.000Z",
                2,
            ),
            await Controller._log_memory_processing_event(
                controller,
                "recovered",
                None,
                "2026-01-01T00:02:00.000Z",
                0,
            ),
        ]

    assert results == [True, True, True]
    assert controller.settings_manager.store.get_admins_calls == 0
    assert all(client.calls == [] for client in controller.im_clients.values())
    records = [record for record in caplog.records if record.name == "core.controller"]
    assert [(record.levelno, record.getMessage()) for record in records] == [
        (
            logging.WARNING,
            "Memory processing event=fault kind=credential "
            "occurred_at=2026-01-01T00:00:00.000Z queued=4",
        ),
        (
            logging.WARNING,
            "Memory processing event=fault kind=engine "
            "occurred_at=2026-01-01T00:01:00.000Z queued=2",
        ),
        (
            logging.INFO,
            "Memory processing event=recovered kind=none "
            "occurred_at=2026-01-01T00:02:00.000Z queued=0",
        ),
    ]
