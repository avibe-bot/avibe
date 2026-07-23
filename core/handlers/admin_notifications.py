"""Shared delivery for plain-text administrator notifications."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from config.v2_settings import _infer_user_platform, _split_scoped_key


logger = logging.getLogger(__name__)


class AdminNotificationClient(Protocol):
    async def send_dm(self, user_id: str, text: str, **kwargs: Any) -> object: ...


class AdminNotificationController(Protocol):
    im_clients: Mapping[str, AdminNotificationClient]
    im_client: AdminNotificationClient


def delivery_succeeded(result: object) -> bool:
    if result is None:
        return False
    if isinstance(result, bool):
        return result
    if isinstance(result, dict) and "ok" in result:
        return bool(result["ok"])
    return True


async def send_admin_text(
    controller: AdminNotificationController,
    admin_ids: Iterable[object],
    text: str,
    *,
    log_label: str,
) -> set[str]:
    """Send one inert text notification to configured admins across transports."""

    delivered_platforms: set[str] = set()
    for user_id in admin_ids:
        scoped_platform, raw_user_id = _split_scoped_key(str(user_id))
        platform = scoped_platform or _infer_user_platform(raw_user_id)
        client = controller.im_clients.get(platform, controller.im_client)
        try:
            result = await client.send_dm(raw_user_id, text)
            if delivery_succeeded(result):
                delivered_platforms.add(platform)
                logger.info("Sent %s to admin %s", log_label, user_id)
        except Exception as exc:
            logger.error("Failed to send %s to admin %s: %s", log_label, user_id, exc)
    return delivered_platforms
