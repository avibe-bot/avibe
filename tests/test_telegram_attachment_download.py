from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from config.v2_config import TelegramConfig
from modules.im import telegram_api
from modules.im.telegram import TelegramBot


class _Content:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk


class _Response:
    def __init__(self, chunks: list[bytes], content_length: int | None = None) -> None:
        self.content = _Content(chunks)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def raise_for_status(self) -> None:
        return None


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def get(self, _url: str):
        return self.response


@pytest.mark.asyncio
async def test_telegram_api_streams_to_disk_and_removes_midstream_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "telegram.bin"
    response = _Response([b"123", b"456"])
    monkeypatch.setattr(
        telegram_api.aiohttp,
        "ClientSession",
        lambda **_kwargs: _Session(response),
    )

    with pytest.raises(telegram_api.TelegramFileTooLargeError, match="max_bytes"):
        await telegram_api.download_file_to_path(
            "token",
            "documents/file.bin",
            target,
            max_bytes=5,
        )

    assert not target.exists()


@pytest.mark.asyncio
async def test_telegram_api_rejects_response_header_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "telegram.bin"
    response = _Response([b"never-written"], content_length=6)
    monkeypatch.setattr(
        telegram_api.aiohttp,
        "ClientSession",
        lambda **_kwargs: _Session(response),
    )

    with pytest.raises(telegram_api.TelegramFileTooLargeError, match="max_bytes"):
        await telegram_api.download_file_to_path(
            "token",
            "documents/file.bin",
            target,
            max_bytes=5,
        )

    assert not target.exists()


@pytest.mark.asyncio
async def test_telegram_adapter_checks_declared_size_before_bot_api(monkeypatch, tmp_path: Path) -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    get_file = AsyncMock()
    monkeypatch.setattr(telegram_api, "get_file", get_file)

    result = await bot.download_file_to_path(
        {"url": "file-id", "size": 6},
        str(tmp_path / "file.bin"),
        max_bytes=5,
    )

    assert result.success is False
    assert result.error == "File exceeds max_bytes"
    assert result.failure_reason == "file_too_large"
    get_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_adapter_streams_resolved_file_with_bound(monkeypatch, tmp_path: Path) -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    monkeypatch.setattr(
        telegram_api,
        "get_file",
        AsyncMock(return_value={"result": {"file_path": "docs/file.pdf", "file_size": 5}}),
    )
    stream = AsyncMock(return_value=5)
    monkeypatch.setattr(telegram_api, "download_file_to_path", stream)
    target = tmp_path / "file.pdf"

    result = await bot.download_file_to_path(
        {"url": "file-id", "size": 5},
        str(target),
        max_bytes=5,
        timeout_seconds=17,
    )

    assert result.success is True
    stream.assert_awaited_once_with(
        "123456:test-token",
        "docs/file.pdf",
        target,
        max_bytes=5,
        timeout_seconds=17,
        proxy_url=bot._proxy_url,
    )


@pytest.mark.asyncio
async def test_telegram_adapter_does_not_classify_unrelated_value_error_as_overflow(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    monkeypatch.setattr(
        telegram_api,
        "get_file",
        AsyncMock(return_value={"result": {"file_path": "docs/file.pdf"}}),
    )
    monkeypatch.setattr(
        telegram_api,
        "download_file_to_path",
        AsyncMock(side_effect=ValueError("malformed proxy or file URL")),
    )
    target = tmp_path / "file.pdf"

    result = await bot.download_file_to_path(
        {"url": "file-id"},
        str(target),
        max_bytes=None,
    )

    assert result.success is False
    assert result.error == "Telegram file download failed"
    assert result.failure_reason is None
    assert not target.exists()


@pytest.mark.asyncio
async def test_telegram_adapter_preserves_streaming_overflow_reason(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    monkeypatch.setattr(
        telegram_api,
        "get_file",
        AsyncMock(return_value={"result": {"file_path": "docs/file.pdf"}}),
    )
    monkeypatch.setattr(
        telegram_api,
        "download_file_to_path",
        AsyncMock(
            side_effect=telegram_api.TelegramFileTooLargeError(
                "Downloaded file exceeds max_bytes"
            )
        ),
    )
    target = tmp_path / "file.pdf"

    result = await bot.download_file_to_path(
        {"url": "file-id"},
        str(target),
        max_bytes=5,
    )

    assert result.success is False
    assert result.error == "File exceeds max_bytes"
    assert result.failure_reason == "file_too_large"
    assert not target.exists()
