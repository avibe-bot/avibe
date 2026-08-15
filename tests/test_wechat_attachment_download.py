from __future__ import annotations

from pathlib import Path

import pytest

from config.v2_config import WeChatConfig
from modules.im import wechat_cdn
from modules.im.wechat import WeChatBot, wechat_cdn as wechat_cdn_client


class _Content:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk


class _Response:
    ok = True
    status = 200
    reason = "OK"

    def __init__(self, chunks: list[bytes], content_length: int | None = None) -> None:
        self.content = _Content(chunks)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
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
async def test_wechat_decrypts_incrementally_to_bounded_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = b"0123456789abcdef"
    plaintext = b"bounded plaintext"
    encrypted = wechat_cdn.aes_ecb_encrypt(plaintext, key)
    response = _Response([encrypted[:7], encrypted[7:]])
    monkeypatch.setattr(
        wechat_cdn.aiohttp,
        "ClientSession",
        lambda **_kwargs: _Session(response),
    )
    target = tmp_path / "wechat.bin"

    size = await wechat_cdn.download_and_decrypt_to_path(
        "https://cdn.example.test",
        "opaque-query",
        "MDEyMzQ1Njc4OWFiY2RlZg==",
        target,
        max_bytes=len(plaintext),
        timeout_seconds=11,
    )

    assert size == len(plaintext)
    assert target.read_bytes() == plaintext


@pytest.mark.asyncio
async def test_wechat_decrypt_removes_partial_plaintext_on_final_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = b"0123456789abcdef"
    encrypted = wechat_cdn.aes_ecb_encrypt(b"123456789", key)
    response = _Response([encrypted])
    monkeypatch.setattr(
        wechat_cdn.aiohttp,
        "ClientSession",
        lambda **_kwargs: _Session(response),
    )
    target = tmp_path / "wechat.bin"

    with pytest.raises(ValueError, match="max_bytes"):
        await wechat_cdn.download_and_decrypt_to_path(
            "https://cdn.example.test",
            "opaque-query",
            "MDEyMzQ1Njc4OWFiY2RlZg==",
            target,
            max_bytes=8,
        )

    assert not target.exists()


@pytest.mark.asyncio
async def test_wechat_decrypt_rejects_ciphertext_header_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response([b"never-written"], content_length=33)
    monkeypatch.setattr(
        wechat_cdn.aiohttp,
        "ClientSession",
        lambda **_kwargs: _Session(response),
    )
    target = tmp_path / "wechat.bin"

    with pytest.raises(ValueError, match="max_bytes"):
        await wechat_cdn.download_and_decrypt_to_path(
            "https://cdn.example.test",
            "opaque-query",
            "MDEyMzQ1Njc4OWFiY2RlZg==",
            target,
            max_bytes=16,
        )

    assert not target.exists()


@pytest.mark.asyncio
async def test_wechat_adapter_rejects_declared_plaintext_before_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = WeChatBot(WeChatConfig(bot_token="test-token"))
    called = False

    async def acquire(*_args, **_kwargs):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(wechat_cdn_client, "download_and_decrypt", acquire)

    result = await bot.download_file_to_path(
        {
            "url": "opaque-query",
            "size": 9,
            "cdn_info": {"aes_key": "key"},
        },
        str(tmp_path / "wechat.bin"),
        max_bytes=8,
    )

    assert result.success is False
    assert result.error == "File exceeds max_bytes"
    assert called is False
