"""WeChat iLink bot HTTP API client.

Handles all HTTP communication with the WeChat iLink bot backend.
All messaging endpoints are POST to ``{base_url}/ilink/bot/{endpoint}``;
auth/QR endpoints are GET.

Ported from the TypeScript reference implementation.
"""

import asyncio
import base64
import logging
import struct
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Message types (proto: MessageType)
MESSAGE_TYPE_NONE = 0
MESSAGE_TYPE_USER = 1
MESSAGE_TYPE_BOT = 2

# Message item types (proto: MessageItemType)
ITEM_NONE = 0
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

# Message states (proto: MessageState)
STATE_NEW = 0
STATE_GENERATING = 1
STATE_FINISH = 2

# Typing status
TYPING_START = 1
TYPING_CANCEL = 2

# Upload media types (proto: UploadMediaType)
UPLOAD_MEDIA_IMAGE = 1
UPLOAD_MEDIA_VIDEO = 2
UPLOAD_MEDIA_FILE = 3
UPLOAD_MEDIA_VOICE = 4

# Timeouts (milliseconds)
DEFAULT_LONG_POLL_TIMEOUT_MS = 35_000
DEFAULT_LONG_POLL_TIMEOUT_GRACE_MS = 5_000
DEFAULT_API_TIMEOUT_MS = 15_000
DEFAULT_CONFIG_TIMEOUT_MS = 10_000

# Channel version reported in base_info
CHANNEL_VERSION = "vibe-remote"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _random_wechat_uin() -> str:
    """Generate ``X-WECHAT-UIN`` header: random uint32 -> decimal string -> base64."""
    import os

    raw = os.urandom(4)
    uint32 = struct.unpack("!I", raw)[0]
    return base64.b64encode(str(uint32).encode("utf-8")).decode("ascii")


def _ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else f"{url}/"


def _build_base_info() -> Dict[str, str]:
    """Build the ``base_info`` payload included in every API request."""
    return {"channel_version": CHANNEL_VERSION}


def _build_headers(token: Optional[str] = None, body_bytes: Optional[bytes] = None) -> Dict[str, str]:
    """Build common headers for iLink bot API requests."""
    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
    }
    if body_bytes is not None:
        headers["Content-Length"] = str(len(body_bytes))
    if token and token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    return headers


def _redact_token(token: Optional[str]) -> str:
    """Redact token for safe logging."""
    if not token:
        return "(none)"
    return f"{token[:8]}...{token[-4:]}" if len(token) > 16 else "***"


# ---------------------------------------------------------------------------
# Core fetch wrapper
# ---------------------------------------------------------------------------


async def _api_fetch(
    base_url: str,
    endpoint: str,
    body: dict,
    token: Optional[str] = None,
    timeout_ms: int = DEFAULT_API_TIMEOUT_MS,
) -> dict:
    """Common POST wrapper: POST JSON to a Weixin API endpoint.

    Returns the parsed JSON response dict.
    Raises ``aiohttp.ClientError`` or ``RuntimeError`` on HTTP error.
    """
    import json

    url = urljoin(_ensure_trailing_slash(base_url), endpoint)
    body_with_base = {**body, "base_info": _build_base_info()}
    body_str = json.dumps(body_with_base)
    body_bytes = body_str.encode("utf-8")
    headers = _build_headers(token=token, body_bytes=body_bytes)

    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000.0)
    logger.debug("POST %s body=%s", url, body_str[:200])

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, data=body_bytes, headers=headers) as resp:
            raw_text = await resp.text()
            logger.debug(
                "%s status=%d raw=%s",
                endpoint,
                resp.status,
                raw_text[:300] if raw_text else "(empty)",
            )
            if not resp.ok:
                raise RuntimeError(f"{endpoint} {resp.status}: {raw_text}")
            return json.loads(raw_text)


# ---------------------------------------------------------------------------
# Public API methods
# ---------------------------------------------------------------------------


async def get_updates(
    base_url: str,
    token: str,
    get_updates_buf: str = "",
    timeout_ms: int = DEFAULT_LONG_POLL_TIMEOUT_MS,
) -> dict:
    """Long-poll ``getUpdates``.

    Server holds the request until new messages arrive or the timeout elapses.
    On client-side timeout (no server response within *timeout_ms*), returns an
    empty response with ``ret=0`` so the caller can simply retry.

    Returns:
        Parsed JSON dict with keys ``ret``, ``msgs``, ``get_updates_buf``, etc.
    """
    import json

    url = urljoin(_ensure_trailing_slash(base_url), "ilink/bot/getupdates")
    body_dict = {
        "get_updates_buf": get_updates_buf,
        "base_info": _build_base_info(),
    }
    body_str = json.dumps(body_dict)
    body_bytes = body_str.encode("utf-8")
    headers = _build_headers(token=token, body_bytes=body_bytes)

    timeout = aiohttp.ClientTimeout(total=(timeout_ms + DEFAULT_LONG_POLL_TIMEOUT_GRACE_MS) / 1000.0)
    logger.debug(
        "get_updates: POST %s token=%s timeout=%dms buf_len=%d",
        url,
        _redact_token(token),
        timeout_ms,
        len(get_updates_buf),
    )

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=body_bytes, headers=headers) as resp:
                raw_text = await resp.text()
                logger.debug(
                    "getUpdates: status=%d body_len=%d raw=%s",
                    resp.status,
                    len(raw_text) if raw_text else 0,
                    raw_text[:300] if raw_text else "(empty)",
                )
                if not resp.ok:
                    raise RuntimeError(f"getUpdates {resp.status}: {raw_text}")
                return json.loads(raw_text)
    except (aiohttp.ServerTimeoutError, asyncio.TimeoutError, TimeoutError):
        logger.debug(
            "getUpdates: client-side timeout after %dms, returning empty response",
            timeout_ms,
        )
        return {"ret": 0, "msgs": [], "get_updates_buf": get_updates_buf}


def _generate_client_id() -> str:
    """Generate a unique client ID for message deduplication."""
    return f"vibe-remote-{uuid.uuid4().hex[:16]}"


async def send_message(
    base_url: str,
    token: str,
    to_user_id: str,
    context_token: str,
    item_list: List[Dict[str, Any]],
) -> dict:
    """Send a single message downstream.

    Wraps the items in a ``WeixinMessage`` with ``message_type=BOT``,
    ``message_state=FINISH``.

    Args:
        to_user_id: Recipient user ID.
        context_token: Conversation context token.
        item_list: List of message item dicts.  Each item must use the
                   iLink protobuf format, e.g.
                   ``{"type": 1, "text_item": {"text": "hello"}}``.

    Returns:
        Parsed JSON response dict.
    """
    msg = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": _generate_client_id(),
            "context_token": context_token,
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": STATE_FINISH,
            "item_list": item_list,
        }
    }
    return await _api_fetch(
        base_url,
        "ilink/bot/sendmessage",
        msg,
        token=token,
        timeout_ms=DEFAULT_API_TIMEOUT_MS,
    )


async def get_upload_url(
    base_url: str,
    token: str,
    params: Dict[str, Any],
) -> dict:
    """Get a pre-signed CDN upload URL for a file.

    *params* should contain fields such as ``filekey``, ``media_type``,
    ``to_user_id``, ``rawsize``, ``rawfilemd5``, ``filesize``, ``aeskey``,
    and optionally thumbnail fields.

    Returns:
        Parsed JSON dict with either ``upload_full_url`` or ``upload_param``
        (and possibly thumbnail upload fields).
    """
    return await _api_fetch(
        base_url,
        "ilink/bot/getuploadurl",
        params,
        token=token,
        timeout_ms=DEFAULT_API_TIMEOUT_MS,
    )


async def get_config(
    base_url: str,
    token: str,
    ilink_user_id: str,
    context_token: Optional[str] = None,
) -> dict:
    """Fetch bot config (includes ``typing_ticket``) for a given user.

    Returns:
        Parsed JSON dict with ``ret``, ``typing_ticket``, etc.
    """
    body: Dict[str, Any] = {"ilink_user_id": ilink_user_id}
    if context_token is not None:
        body["context_token"] = context_token
    return await _api_fetch(
        base_url,
        "ilink/bot/getconfig",
        body,
        token=token,
        timeout_ms=DEFAULT_CONFIG_TIMEOUT_MS,
    )


async def send_typing(
    base_url: str,
    token: str,
    ilink_user_id: str,
    typing_ticket: str,
    status: int = TYPING_START,
) -> dict:
    """Send a typing indicator to a user.

    Args:
        status: ``TYPING_START`` (1) or ``TYPING_CANCEL`` (2).

    Returns:
        Parsed JSON response dict.
    """
    body = {
        "ilink_user_id": ilink_user_id,
        "typing_ticket": typing_ticket,
        "status": status,
    }
    return await _api_fetch(
        base_url,
        "ilink/bot/sendtyping",
        body,
        token=token,
        timeout_ms=DEFAULT_CONFIG_TIMEOUT_MS,
    )


# ---------------------------------------------------------------------------
# Auth / QR code endpoints (GET requests, no token)
# ---------------------------------------------------------------------------


async def get_bot_qrcode(
    base_url: str,
    bot_type: str = "3",
) -> dict:
    """Fetch a new QR code for bot login.

    GET ``ilink/bot/get_bot_qrcode?bot_type={bot_type}``

    Returns:
        Dict with ``qrcode`` (string token) and ``qrcode_img_content`` (URL/data).
    """
    url = urljoin(
        _ensure_trailing_slash(base_url),
        f"ilink/bot/get_bot_qrcode?bot_type={quote(bot_type)}",
    )
    logger.info("Fetching QR code from: %s", url)

    timeout = aiohttp.ClientTimeout(total=DEFAULT_API_TIMEOUT_MS / 1000.0)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if not resp.ok:
                body = await resp.text()
                logger.error(
                    "QR code fetch failed: %d %s body=%s",
                    resp.status,
                    resp.reason,
                    body[:200],
                )
                raise RuntimeError(f"Failed to fetch QR code: {resp.status} {resp.reason}")
            # iLink API may return application/octet-stream instead of application/json
            return await resp.json(content_type=None)


async def get_qrcode_status(
    base_url: str,
    qrcode: str,
) -> dict:
    """Long-poll QR code scan status.

    GET ``ilink/bot/get_qrcode_status?qrcode={qrcode}``
    Includes ``iLink-App-ClientVersion: 1`` header.

    On client-side timeout (35 s), returns ``{"status": "wait"}``.

    Returns:
        Dict with ``status`` (``"wait"`` / ``"scaned"`` / ``"confirmed"`` / ``"expired"``),
        and on confirmation: ``bot_token``, ``ilink_bot_id``, ``baseurl``, ``ilink_user_id``.
    """
    import json

    url = urljoin(
        _ensure_trailing_slash(base_url),
        f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode)}",
    )
    headers = {"iLink-App-ClientVersion": "1"}

    timeout = aiohttp.ClientTimeout(total=DEFAULT_LONG_POLL_TIMEOUT_MS / 1000.0)
    logger.debug("Long-poll QR status from: %s", url)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                raw_text = await resp.text()
                logger.debug(
                    "pollQRStatus: HTTP %d body=%s",
                    resp.status,
                    raw_text[:200],
                )
                if not resp.ok:
                    raise RuntimeError(f"Failed to poll QR status: {resp.status} {resp.reason}")
                return json.loads(raw_text)
    except (aiohttp.ServerTimeoutError, TimeoutError):
        logger.debug(
            "pollQRStatus: client-side timeout after %dms, returning wait",
            DEFAULT_LONG_POLL_TIMEOUT_MS,
        )
        return {"status": "wait"}
