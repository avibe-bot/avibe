"""Minimal Telegram Bot API helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import aiohttp
from aiohttp_socks import ProxyConnector


def _api_url(bot_token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{bot_token}/{method}"


def _file_url(bot_token: str, file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{bot_token}/{file_path.lstrip('/')}"


async def call_api(
    bot_token: str,
    method: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    timeout_seconds: int = 60,
    form: Optional[aiohttp.FormData] = None,
    proxy_url: Optional[str] = None,
) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    connector = None
    if proxy_url:
        connector = ProxyConnector.from_url(proxy_url)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        if form is not None:
            async with session.post(_api_url(bot_token, method), data=form) as resp:
                data = await resp.json()
        else:
            async with session.post(_api_url(bot_token, method), json=payload or {}) as resp:
                data = await resp.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or f"Telegram API call failed: {method}")
    return data


async def get_me(bot_token: str, proxy_url: Optional[str] = None) -> dict[str, Any]:
    return await call_api(bot_token, "getMe", proxy_url=proxy_url)


async def create_forum_topic(bot_token: str, chat_id: str, name: str, proxy_url: Optional[str] = None) -> dict[str, Any]:
    return await call_api(bot_token, "createForumTopic", {"chat_id": chat_id, "name": name}, proxy_url=proxy_url)


async def get_updates(bot_token: str, offset: Optional[int], timeout_seconds: int = 30, proxy_url: Optional[str] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "timeout": timeout_seconds,
        "allowed_updates": ["message", "callback_query", "my_chat_member", "chat_member"],
    }
    if offset is not None:
        payload["offset"] = offset
    return await call_api(bot_token, "getUpdates", payload, timeout_seconds=timeout_seconds + 10, proxy_url=proxy_url)


async def download_file(bot_token: str, file_path: str, *, timeout_seconds: int = 60, proxy_url: Optional[str] = None) -> bytes:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    connector = None
    if proxy_url:
        connector = ProxyConnector.from_url(proxy_url)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async with session.get(_file_url(bot_token, file_path)) as resp:
            resp.raise_for_status()
            return await resp.read()


async def get_file(bot_token: str, file_id: str, proxy_url: Optional[str] = None) -> dict[str, Any]:
    return await call_api(bot_token, "getFile", {"file_id": file_id}, proxy_url=proxy_url)


async def delete_message(bot_token: str, chat_id: str, message_id: str, proxy_url: Optional[str] = None) -> dict[str, Any]:
    return await call_api(bot_token, "deleteMessage", {"chat_id": chat_id, "message_id": int(message_id)}, proxy_url=proxy_url)


async def set_message_reaction(
    bot_token: str,
    chat_id: str,
    message_id: str,
    emoji: str,
    *,
    is_big: bool = False,
    proxy_url: Optional[str] = None,
) -> dict[str, Any]:
    payload = {
        "chat_id": chat_id,
        "message_id": int(message_id),
        "reaction": [{"type": "emoji", "emoji": emoji}],
        "is_big": is_big,
    }
    return await call_api(bot_token, "setMessageReaction", payload, proxy_url=proxy_url)


async def clear_message_reaction(bot_token: str, chat_id: str, message_id: str, proxy_url: Optional[str] = None) -> dict[str, Any]:
    payload = {
        "chat_id": chat_id,
        "message_id": int(message_id),
        "reaction": [],
    }
    return await call_api(bot_token, "setMessageReaction", payload, proxy_url=proxy_url)


async def set_my_commands(
    bot_token: str,
    commands: list[dict[str, str]],
    *,
    scope: Optional[dict[str, Any]] = None,
    language_code: Optional[str] = None,
    proxy_url: Optional[str] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"commands": commands}
    if scope is not None:
        payload["scope"] = scope
    if language_code:
        payload["language_code"] = language_code
    return await call_api(bot_token, "setMyCommands", payload, proxy_url=proxy_url)


async def set_chat_menu_button(
    bot_token: str,
    *,
    menu_button: dict[str, Any],
    proxy_url: Optional[str] = None,
) -> dict[str, Any]:
    return await call_api(
        bot_token,
        "setChatMenuButton",
        {"menu_button": menu_button},
        proxy_url=proxy_url,
    )


async def send_multipart_file(
    bot_token: str,
    method: str,
    payload: dict[str, Any],
    file_path: str,
    field_name: str,
    proxy_url: Optional[str] = None,
) -> dict[str, Any]:
    form = aiohttp.FormData()
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            form.add_field(key, json.dumps(value))
        else:
            form.add_field(key, str(value))
    path = Path(file_path)
    form.add_field(field_name, path.read_bytes(), filename=path.name)
    return await call_api(bot_token, method, form=form, proxy_url=proxy_url)
