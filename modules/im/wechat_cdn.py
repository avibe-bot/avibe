"""WeChat iLink CDN encryption and media upload/download.

Handles AES-128-ECB encryption for CDN file transfers, including:
- Encrypting files before upload to the WeChat CDN
- Decrypting files downloaded from the CDN
- High-level upload workflows for images, files, and videos

Ported from the TypeScript reference implementation.
"""

import base64
import html
import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from modules.im.wechat_api import (
    UPLOAD_MEDIA_FILE,
    UPLOAD_MEDIA_IMAGE,
    UPLOAD_MEDIA_VIDEO,
    get_upload_url,
)

logger = logging.getLogger(__name__)

# AES-128 block size in bits (for PKCS7 padding)
_AES_BLOCK_BITS = 128
# AES-128 block size in bytes
_AES_BLOCK_BYTES = 16
# Maximum retry attempts for CDN upload
_UPLOAD_MAX_RETRIES = 3
_CDN_ERROR_BODY_LIMIT = 500


class _CdnUploadStatusError(RuntimeError):
    """CDN upload response error with explicit retry semantics."""

    def __init__(self, status: int, message: str, *, retryable: bool):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


# ---------------------------------------------------------------------------
# AES-128-ECB primitives
# ---------------------------------------------------------------------------


def aes_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt *plaintext* with AES-128-ECB and PKCS7 padding.

    Args:
        plaintext: Data to encrypt.
        key: 16-byte AES key.

    Returns:
        Ciphertext bytes (always a multiple of 16 bytes).
    """
    padder = PKCS7(_AES_BLOCK_BITS).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def aes_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """Decrypt *ciphertext* with AES-128-ECB and remove PKCS7 padding.

    Args:
        ciphertext: Encrypted data (must be a multiple of 16 bytes).
        key: 16-byte AES key.

    Returns:
        Decrypted plaintext bytes.
    """
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = PKCS7(_AES_BLOCK_BITS).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def aes_ecb_padded_size(plaintext_size: int) -> int:
    """Compute AES-128-ECB ciphertext size after PKCS7 padding.

    PKCS7 always adds at least 1 byte of padding, so the result is
    ``ceil((plaintext_size + 1) / 16) * 16``.
    """
    return ((plaintext_size + 1 + _AES_BLOCK_BYTES - 1) // _AES_BLOCK_BYTES) * _AES_BLOCK_BYTES


# ---------------------------------------------------------------------------
# AES key parsing
# ---------------------------------------------------------------------------


def parse_aes_key(aes_key_b64: str) -> bytes:
    """Parse a CDNMedia ``aes_key`` field into a raw 16-byte AES key.

    Two encodings are seen in the wild:

    - ``base64(raw 16 bytes)`` -- images (aes_key from media field)
    - ``base64(hex string of 16 bytes)`` -- file / voice / video

    In the second case, base64-decoding yields 32 ASCII hex chars which must
    then be parsed as hex to recover the actual 16-byte key.

    Args:
        aes_key_b64: Base64-encoded AES key string from the API.

    Returns:
        Raw 16-byte AES key.

    Raises:
        ValueError: If the decoded key is neither 16 raw bytes nor a 32-char
            hex string.
    """
    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == _AES_BLOCK_BYTES:
        return decoded
    if len(decoded) == 32:
        try:
            ascii_str = decoded.decode("ascii")
            # Verify it's a valid hex string
            key = bytes.fromhex(ascii_str)
            if len(key) == _AES_BLOCK_BYTES:
                return key
        except (UnicodeDecodeError, ValueError):
            pass
    raise ValueError(
        f"aes_key must decode to 16 raw bytes or 32-char hex string, got {len(decoded)} bytes (base64={aes_key_b64!r})"
    )


# ---------------------------------------------------------------------------
# CDN URL construction
# ---------------------------------------------------------------------------


def _build_cdn_upload_url(cdn_base_url: str, upload_param: str, filekey: str) -> str:
    """Build a CDN upload URL from upload_param and filekey."""
    return f"{cdn_base_url}/upload?encrypted_query_param={quote(upload_param)}&filekey={quote(filekey)}"


def _build_cdn_download_url(cdn_base_url: str, encrypted_query_param: str) -> str:
    """Build a CDN download URL from encrypted_query_param."""
    return f"{cdn_base_url}/download?encrypted_query_param={quote(encrypted_query_param)}"


def _extract_upload_full_url(upload_resp: Dict[str, Any]) -> Optional[str]:
    """Return a direct CDN upload URL from known iLink response variants."""
    for key in ("upload_full_url", "uploadFullUrl", "upload_url", "uploadUrl"):
        value = upload_resp.get(key)
        if isinstance(value, str) and value.strip():
            return html.unescape(value.strip())
    return None


def _upload_url_host(upload_url: str) -> str:
    """Return the upload URL host for diagnostics."""
    try:
        return urlparse(upload_url).netloc or "(unknown)"
    except Exception:
        return "(invalid)"


def _upload_url_for_log(upload_url: str) -> str:
    """Return a diagnostic upload URL without credential-bearing query values."""
    try:
        parsed = urlparse(upload_url)
        redacted_query = urlencode([(key, "<redacted>") for key, _value in parse_qsl(parsed.query, keep_blank_values=True)])
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, redacted_query, ""))
    except Exception:
        return "(invalid)"


def _truncate_cdn_error_body(body: str) -> str:
    body = (body or "").strip()
    if len(body) <= _CDN_ERROR_BODY_LIMIT:
        return body
    return f"{body[:_CDN_ERROR_BODY_LIMIT]}...(truncated)"


def _cdn_error_message(resp: aiohttp.ClientResponse, body: str) -> str:
    header_msg = (resp.headers.get("x-error-message") or "").strip()
    body_msg = _truncate_cdn_error_body(body)
    if header_msg and body_msg:
        return f"{header_msg}; body={body_msg}"
    if header_msg:
        return header_msg
    if body_msg:
        return body_msg
    return f"status {resp.status}"


async def _read_cdn_error_body(resp: aiohttp.ClientResponse) -> str:
    try:
        return await resp.text()
    except Exception as exc:
        return f"(failed to read response body: {type(exc).__name__})"


def _resolve_cdn_upload_url(cdn_base_url: str, upload_resp: Dict[str, Any], filekey: str, label: str) -> str:
    """Resolve a CDN upload URL from legacy or full-url getUploadUrl responses."""
    upload_full_url = _extract_upload_full_url(upload_resp)
    if upload_full_url:
        logger.debug("%s: using upload_full_url host=%s", label, _upload_url_host(upload_full_url))
        return upload_full_url

    upload_param = upload_resp.get("upload_param")
    if upload_param:
        fallback_url = _build_cdn_upload_url(cdn_base_url, str(upload_param), filekey)
        logger.info(
            "%s: getUploadUrl returned no upload_full_url; falling back to configured CDN host=%s",
            label,
            _upload_url_host(fallback_url),
        )
        return fallback_url

    logger.error(
        "%s: getUploadUrl returned neither upload_param nor upload_full_url, resp=%s",
        label,
        upload_resp,
    )
    raise RuntimeError(f"{label}: getUploadUrl returned no upload target")


# ---------------------------------------------------------------------------
# CDN upload / download
# ---------------------------------------------------------------------------


async def upload_buffer_to_cdn(
    upload_url: str,
    data: bytes,
    aes_key: bytes,
) -> str:
    """Upload a buffer to the WeChat CDN with AES-128-ECB encryption.

    Encrypts *data* then POSTs the ciphertext to the CDN. Retries up to 3
    times on server errors (5xx); client errors (4xx) abort immediately.

    Args:
        upload_url: Fully resolved upload URL from ``get_upload_url``.
        data: Raw plaintext bytes to upload.
        aes_key: 16-byte AES key for encryption.

    Returns:
        The ``x-encrypted-param`` response header value (download param).

    Raises:
        RuntimeError: If all upload attempts fail or the response is missing
            the expected header.
    """
    ciphertext = aes_ecb_encrypt(data, aes_key)
    upload_host = _upload_url_host(upload_url)
    upload_log_url = _upload_url_for_log(upload_url)
    logger.debug(
        "CDN upload: POST host=%s url=%s raw_size=%d ciphertext_size=%d",
        upload_host,
        upload_log_url,
        len(data),
        len(ciphertext),
    )

    download_param: Optional[str] = None
    last_error: Optional[Exception] = None

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(1, _UPLOAD_MAX_RETRIES + 1):
            try:
                async with session.post(
                    upload_url,
                    data=ciphertext,
                    headers={"Content-Type": "application/octet-stream"},
                ) as resp:
                    if 400 <= resp.status < 500:
                        body = await _read_cdn_error_body(resp)
                        err_msg = _cdn_error_message(resp, body)
                        logger.error(
                            "CDN client error attempt=%d status=%d host=%s url=%s raw_size=%d ciphertext_size=%d err=%s",
                            attempt,
                            resp.status,
                            upload_host,
                            upload_log_url,
                            len(data),
                            len(ciphertext),
                            err_msg,
                        )
                        raise _CdnUploadStatusError(
                            resp.status,
                            f"CDN upload client error {resp.status}: {err_msg}",
                            retryable=False,
                        )
                    if resp.status != 200:
                        body = await _read_cdn_error_body(resp)
                        err_msg = _cdn_error_message(resp, body)
                        logger.warning(
                            "CDN server error attempt=%d status=%d host=%s url=%s raw_size=%d ciphertext_size=%d err=%s",
                            attempt,
                            resp.status,
                            upload_host,
                            upload_log_url,
                            len(data),
                            len(ciphertext),
                            err_msg,
                        )
                        raise _CdnUploadStatusError(
                            resp.status,
                            f"CDN upload server error {resp.status}: {err_msg}",
                            retryable=True,
                        )

                    download_param = resp.headers.get("x-encrypted-param")
                    if not download_param:
                        logger.error(
                            "CDN response missing x-encrypted-param header attempt=%d",
                            attempt,
                        )
                        raise RuntimeError("CDN upload response missing x-encrypted-param header")
                    logger.debug("CDN upload success attempt=%d", attempt)
                    return download_param

            except _CdnUploadStatusError as e:
                last_error = e
                if not e.retryable:
                    raise
                if attempt < _UPLOAD_MAX_RETRIES:
                    logger.warning(
                        "CDN upload attempt %d failed, retrying host=%s raw_size=%d ciphertext_size=%d err=%s",
                        attempt,
                        upload_host,
                        len(data),
                        len(ciphertext),
                        e,
                    )
                else:
                    logger.error(
                        "CDN upload all %d attempts failed host=%s url=%s raw_size=%d ciphertext_size=%d err=%s",
                        _UPLOAD_MAX_RETRIES,
                        upload_host,
                        upload_log_url,
                        len(data),
                        len(ciphertext),
                        e,
                    )
            except RuntimeError as e:
                last_error = e
                if attempt < _UPLOAD_MAX_RETRIES:
                    logger.warning(
                        "CDN upload attempt %d failed, retrying host=%s raw_size=%d ciphertext_size=%d err=%s",
                        attempt,
                        upload_host,
                        len(data),
                        len(ciphertext),
                        e,
                    )
                else:
                    logger.error(
                        "CDN upload all %d attempts failed host=%s url=%s raw_size=%d ciphertext_size=%d err=%s",
                        _UPLOAD_MAX_RETRIES,
                        upload_host,
                        upload_log_url,
                        len(data),
                        len(ciphertext),
                        e,
                    )
            except aiohttp.ClientError as e:
                last_error = e
                if attempt < _UPLOAD_MAX_RETRIES:
                    logger.warning(
                        "CDN upload attempt %d network error, retrying host=%s raw_size=%d ciphertext_size=%d err=%s",
                        attempt,
                        upload_host,
                        len(data),
                        len(ciphertext),
                        e,
                    )
                else:
                    logger.error(
                        "CDN upload all %d attempts failed host=%s url=%s raw_size=%d ciphertext_size=%d err=%s",
                        _UPLOAD_MAX_RETRIES,
                        upload_host,
                        upload_log_url,
                        len(data),
                        len(ciphertext),
                        e,
                    )

    raise last_error or RuntimeError(f"CDN upload failed after {_UPLOAD_MAX_RETRIES} attempts")


async def download_and_decrypt(
    cdn_base_url: str,
    encrypted_query_param: str,
    aes_key_b64: str,
) -> bytes:
    """Download and AES-128-ECB decrypt a CDN media file.

    Handles dual key encoding via :func:`parse_aes_key`.

    Args:
        cdn_base_url: Base URL of the CDN.
        encrypted_query_param: Encrypted query param identifying the file.
        aes_key_b64: Base64-encoded AES key (see :func:`parse_aes_key`).

    Returns:
        Decrypted plaintext bytes.

    Raises:
        RuntimeError: On HTTP error.
        ValueError: If the AES key cannot be parsed.
    """
    key = parse_aes_key(aes_key_b64)
    url = _build_cdn_download_url(cdn_base_url, encrypted_query_param)
    logger.debug("CDN download: GET %s", url[:80])

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            logger.debug("CDN download: status=%d", resp.status)
            if not resp.ok:
                body = await resp.text()
                raise RuntimeError(f"CDN download {resp.status} {resp.reason}: {body[:200]}")
            encrypted = await resp.read()

    logger.debug("CDN download: %d bytes, decrypting", len(encrypted))
    decrypted = aes_ecb_decrypt(encrypted, key)
    logger.debug("CDN download: decrypted %d bytes", len(decrypted))
    return decrypted


async def download_plain(
    cdn_base_url: str,
    encrypted_query_param: str,
) -> bytes:
    """Download raw CDN bytes without decryption."""

    url = _build_cdn_download_url(cdn_base_url, encrypted_query_param)
    logger.debug("CDN plain download: GET %s", url[:80])

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            logger.debug("CDN plain download: status=%d", resp.status)
            if not resp.ok:
                body = await resp.text()
                raise RuntimeError(f"CDN download {resp.status} {resp.reason}: {body[:200]}")
            return await resp.read()


# ---------------------------------------------------------------------------
# High-level upload workflows
# ---------------------------------------------------------------------------


async def _upload_media_to_cdn(
    base_url: str,
    token: str,
    cdn_base_url: str,
    to_user_id: str,
    file_path: str,
    media_type: int,
    label: str,
) -> Dict[str, Any]:
    """Common upload pipeline: read file -> hash -> gen aeskey -> getUploadUrl -> upload -> return info.

    Args:
        base_url: iLink API base URL.
        token: Bot auth token.
        cdn_base_url: CDN base URL.
        to_user_id: Recipient user ID.
        file_path: Local path to the file.
        media_type: Upload media type constant.
        label: Logging label.

    Returns:
        Dict with ``encrypt_query_param``, ``aes_key`` (base64), ``file_size``,
        ``file_size_ciphertext``, ``filekey``, ``raw_md5``.
    """
    path = Path(file_path)
    plaintext = path.read_bytes()
    rawsize = len(plaintext)
    rawfilemd5 = hashlib.md5(plaintext).hexdigest()
    filesize = aes_ecb_padded_size(rawsize)
    filekey = os.urandom(16).hex()
    aes_key = os.urandom(16)

    logger.debug(
        "%s: file=%s rawsize=%d filesize=%d md5=%s filekey=%s",
        label,
        file_path,
        rawsize,
        filesize,
        rawfilemd5,
        filekey,
    )

    upload_resp = await get_upload_url(
        base_url,
        token,
        params={
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": rawfilemd5,
            "filesize": filesize,
            "no_need_thumb": True,
            "aeskey": aes_key.hex(),
        },
    )

    upload_url = _resolve_cdn_upload_url(cdn_base_url, upload_resp, filekey, label)

    try:
        download_param = await upload_buffer_to_cdn(
            upload_url=upload_url,
            data=plaintext,
            aes_key=aes_key,
        )
    except Exception:
        logger.warning(
            "%s: CDN upload failed file=%s raw_size=%d ciphertext_size=%d upload_host=%s",
            label,
            file_path,
            rawsize,
            filesize,
            _upload_url_host(upload_url),
            exc_info=True,
        )
        raise

    # Encode the AES key as hex then base64 for the CDNMedia.aes_key field
    aes_key_b64 = base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii")

    return {
        "encrypt_query_param": download_param,
        "aes_key": aes_key_b64,
        "file_size": rawsize,
        "file_size_ciphertext": filesize,
        "filekey": filekey,
        "raw_md5": rawfilemd5,
    }


async def upload_image_to_cdn(
    base_url: str,
    token: str,
    cdn_base_url: str,
    to_user_id: str,
    file_path: str,
) -> Dict[str, Any]:
    """Upload a local image file to the WeChat CDN with AES-128-ECB encryption.

    Args:
        base_url: iLink API base URL.
        token: Bot auth token.
        cdn_base_url: CDN base URL.
        to_user_id: Recipient user ID.
        file_path: Local path to the image file.

    Returns:
        Dict with ``encrypt_query_param``, ``aes_key``, ``file_size``,
        ``file_size_ciphertext``, ``filekey``, ``raw_md5``.
    """
    return await _upload_media_to_cdn(
        base_url=base_url,
        token=token,
        cdn_base_url=cdn_base_url,
        to_user_id=to_user_id,
        file_path=file_path,
        media_type=UPLOAD_MEDIA_IMAGE,
        label="upload_image_to_cdn",
    )


async def upload_file_to_cdn(
    base_url: str,
    token: str,
    cdn_base_url: str,
    to_user_id: str,
    file_path: str,
    media_type: int = UPLOAD_MEDIA_FILE,
) -> Dict[str, Any]:
    """Upload a local file (or video) to the WeChat CDN.

    Args:
        base_url: iLink API base URL.
        token: Bot auth token.
        cdn_base_url: CDN base URL.
        to_user_id: Recipient user ID.
        file_path: Local path to the file.
        media_type: ``UPLOAD_MEDIA_FILE`` (3, default) or ``UPLOAD_MEDIA_VIDEO`` (2).

    Returns:
        Dict with ``encrypt_query_param``, ``aes_key``, ``file_size``,
        ``file_size_ciphertext``, ``filekey``, ``raw_md5``.
    """
    label = "upload_video_to_cdn" if media_type == UPLOAD_MEDIA_VIDEO else "upload_file_to_cdn"
    return await _upload_media_to_cdn(
        base_url=base_url,
        token=token,
        cdn_base_url=cdn_base_url,
        to_user_id=to_user_id,
        file_path=file_path,
        media_type=media_type,
        label=label,
    )
