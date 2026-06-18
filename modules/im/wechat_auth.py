"""WeChat QR code login flow for the vibe-remote setup wizard and CLI.

Ported from TypeScript: weixin-plugin-inspect/package/src/auth/login-qr.ts

Manages active login sessions in-memory (dict keyed by session_key).
Supports concurrent login sessions (for Web UI).
Auto-cleans expired sessions (5 min TTL).
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional
from urllib.parse import urlparse

from modules.im import wechat_api

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Short poll timeout for Web UI: the UI polls every 2s, so each individual
# status check should return quickly.  The iLink server itself long-polls
# for ~30s, but we cut that short on the client side so the Flask
# _run_async wrapper (10s timeout) never kills us mid-request.
QR_POLL_TIMEOUT_S = 3


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class WeChatLoginSession:
    """Tracks one QR code login attempt."""

    session_key: str
    qrcode: str = ""  # opaque QR token from server
    qrcode_url: str = ""  # URL to render as QR image
    started_at: float = field(default_factory=time.time)
    status: str = "wait"  # "wait" | "scaned" | "confirmed" | "expired"
    pending_verify_code: Optional[str] = None
    bot_token: Optional[str] = None
    bot_id: Optional[str] = None
    base_url: Optional[str] = None
    user_id: Optional[str] = None
    qr_refresh_count: int = 1  # tracks how many QR codes have been issued
    current_base_url: Optional[str] = None
    local_token_list: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Low-level API helpers
# ---------------------------------------------------------------------------
async def get_bot_qrcode(
    base_url: str,
    bot_type: str,
    *,
    timeout: float = 30,
    local_token_list: Optional[list[str]] = None,
) -> dict:
    """Fetch a new QR code from the iLink server.

    POST {base_url}/ilink/bot/get_bot_qrcode?bot_type={bot_type}

    Returns:
        dict with keys ``qrcode`` and ``qrcode_img_content``.
    """
    return await wechat_api.get_bot_qrcode(
        base_url,
        bot_type,
        local_token_list=local_token_list,
        timeout_ms=int(timeout * 1000),
    )


async def get_qrcode_status(
    base_url: str,
    qrcode: str,
    *,
    timeout: float = QR_POLL_TIMEOUT_S,
    verify_code: Optional[str] = None,
) -> dict:
    """Long-poll the QR code scan status.

    GET {base_url}/ilink/bot/get_qrcode_status?qrcode={qrcode}

    Returns:
        dict with at least ``status`` ("wait" | "scaned" | "confirmed" | "expired").
        On ``confirmed``, also includes ``bot_token``, ``ilink_bot_id``,
        ``baseurl``, and ``ilink_user_id``.
    """
    try:
        return await wechat_api.get_qrcode_status(
            base_url,
            qrcode,
            verify_code=verify_code,
            timeout_ms=int(timeout * 1000),
        )
    except asyncio.TimeoutError:
        logger.debug(
            "get_qrcode_status: client-side timeout after %ss, returning wait",
            timeout,
        )
        return {"status": "wait"}


# ---------------------------------------------------------------------------
# Auth manager
# ---------------------------------------------------------------------------
class WeChatAuthManager:
    """Manages QR code login sessions."""

    DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
    BOT_TYPE = "3"
    SESSION_TTL = 300  # 5 minutes
    MAX_QR_REFRESH = 3

    def __init__(self) -> None:
        self._sessions: Dict[str, WeChatLoginSession] = {}

    # -- helpers ----------------------------------------------------------

    def _is_session_fresh(self, session: WeChatLoginSession) -> bool:
        return (time.time() - session.started_at) < self.SESSION_TTL

    def cleanup_expired(self) -> None:
        """Remove sessions older than TTL."""
        expired_keys = [key for key, sess in self._sessions.items() if not self._is_session_fresh(sess)]
        for key in expired_keys:
            logger.debug("Cleaning up expired session: %s", key)
            del self._sessions[key]

    def get_session(self, session_key: str) -> Optional[WeChatLoginSession]:
        """Get current session state (for UI polling)."""
        self.cleanup_expired()
        return self._sessions.get(session_key)

    def _refresh_base_url(self, session: WeChatLoginSession, redirect_host: str) -> Optional[str]:
        host = redirect_host.strip()
        if not host:
            return None
        parsed = urlparse(host if "://" in host else f"https://{host}")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            logger.warning("Ignoring invalid WeChat QR redirect host: %s", redirect_host)
            return None
        refreshed = f"{parsed.scheme}://{parsed.netloc}"
        session.current_base_url = refreshed
        return refreshed

    async def _refresh_qr_session(self, session: WeChatLoginSession) -> dict:
        qr_response = await get_bot_qrcode(
            session.base_url or self.DEFAULT_BASE_URL,
            self.BOT_TYPE,
            local_token_list=session.local_token_list,
        )
        session.qrcode = qr_response.get("qrcode", "")
        session.qrcode_url = qr_response.get("qrcode_img_content", "")
        session.started_at = time.time()
        session.status = "wait"
        session.current_base_url = session.base_url
        session.pending_verify_code = None
        logger.info("New QR code obtained for session=%s", session.session_key)
        return {
            "status": "refreshed",
            "qrcode_url": session.qrcode_url,
            "message": (
                f"QR code expired, refreshed "
                f"({session.qr_refresh_count}/{self.MAX_QR_REFRESH}). "
                f"Please scan again."
            ),
        }

    # -- public API -------------------------------------------------------

    async def start_login(
        self,
        session_key: Optional[str] = None,
        base_url: Optional[str] = None,
        local_token_list: Optional[list[str]] = None,
    ) -> dict:
        """Start a new QR code login.

        Returns:
            dict with ``session_key``, ``qrcode_url`` (may be empty on error),
            and ``message``.
        """
        session_key = session_key or str(uuid.uuid4())
        base_url = base_url or self.DEFAULT_BASE_URL

        self.cleanup_expired()
        normalized_local_tokens = [
            token.strip() for token in (local_token_list or []) if isinstance(token, str) and token.strip()
        ]

        # Reuse an existing fresh session if available
        existing = self._sessions.get(session_key)
        if existing and self._is_session_fresh(existing) and existing.qrcode_url:
            existing.local_token_list = normalized_local_tokens
            return {
                "session_key": session_key,
                "qrcode_url": existing.qrcode_url,
                "message": "QR code is ready. Please scan with WeChat.",
            }

        if not base_url:
            return {
                "ok": False,
                "session_key": session_key,
                "qrcode_url": "",
                "error": "No base URL configured. Please set the WeChat base URL before logging in.",
            }

        try:
            logger.info("Starting WeChat login with bot_type=%s", self.BOT_TYPE)
            qr_response = await get_bot_qrcode(base_url, self.BOT_TYPE, local_token_list=normalized_local_tokens)
            qrcode = qr_response.get("qrcode", "")
            qrcode_url = qr_response.get("qrcode_img_content", "")

            logger.info(
                "QR code received, qrcode=%s...%s img_url_len=%d",
                qrcode[:8] if len(qrcode) > 8 else qrcode,
                qrcode[-4:] if len(qrcode) > 4 else "",
                len(qrcode_url),
            )

            session = WeChatLoginSession(
                session_key=session_key,
                qrcode=qrcode,
                qrcode_url=qrcode_url,
                started_at=time.time(),
                status="wait",
                base_url=base_url,
                current_base_url=base_url,
                local_token_list=normalized_local_tokens,
            )
            self._sessions[session_key] = session

            return {
                "session_key": session_key,
                "qrcode_url": qrcode_url,
                "message": "Scan the QR code with WeChat to connect.",
            }
        except Exception as exc:
            logger.error("Failed to start WeChat login: %s", exc)
            return {
                "ok": False,
                "session_key": session_key,
                "qrcode_url": "",
                "error": f"Failed to start login: {exc}",
            }

    async def poll_status(self, session_key: str, verify_code: Optional[str] = None) -> dict:
        """Poll login status for a single cycle.

        Returns:
            dict with ``status``, ``message``, and on success also
            ``bot_token``, ``bot_id``, ``base_url``, ``user_id``.
        """
        session = self._sessions.get(session_key)
        if session is None:
            return {
                "status": "expired",
                "message": "Login session expired. Please start a new login.",
            }

        if not self._is_session_fresh(session):
            del self._sessions[session_key]
            return {
                "status": "expired",
                "message": "QR code has expired. Please start a new login.",
            }

        base_url = session.current_base_url or session.base_url or self.DEFAULT_BASE_URL

        if verify_code is not None:
            session.pending_verify_code = verify_code.strip() or None

        try:
            status_resp = await get_qrcode_status(
                base_url,
                session.qrcode,
                verify_code=session.pending_verify_code,
            )
        except Exception as exc:
            # Don't delete session on transient errors (timeouts, network blips).
            # The UI will retry on the next poll cycle.
            logger.warning("Transient error polling QR status (session kept): %s", exc)
            return {
                "status": "wait",
                "message": "Waiting for QR code scan...",
            }

        status = status_resp.get("status", "wait")
        session.status = status
        logger.debug(
            "poll_status: session=%s status=%s has_bot_token=%s has_bot_id=%s",
            session_key,
            status,
            bool(status_resp.get("bot_token")),
            bool(status_resp.get("ilink_bot_id")),
        )

        if status == "wait":
            return {"status": "wait", "message": "Waiting for QR code scan..."}

        if status == "scaned":
            session.pending_verify_code = None
            return {"status": "scaned", "message": "QR code scanned. Please confirm in WeChat."}

        if status == "scaned_but_redirect":
            redirect_host = status_resp.get("redirect_host", "")
            refreshed_base_url = self._refresh_base_url(session, redirect_host)
            return {
                "status": "scaned",
                "message": "QR code scanned. Please confirm in WeChat.",
                **({"base_url": refreshed_base_url} if refreshed_base_url else {}),
            }

        if status == "binded_redirect":
            resp_base_url = session.current_base_url or session.base_url or self.DEFAULT_BASE_URL
            del self._sessions[session_key]
            return {
                "status": "already_connected",
                "message": "This WeChat bot is already connected. Existing credentials remain valid.",
                "base_url": resp_base_url,
            }

        if status == "need_verifycode":
            return {
                "status": "need_verifycode",
                "message": "Enter the verification code shown in WeChat to continue.",
            }

        if status == "verify_code_blocked":
            session.pending_verify_code = None
            session.qr_refresh_count += 1
            if session.qr_refresh_count > self.MAX_QR_REFRESH:
                del self._sessions[session_key]
                return {
                    "status": "expired",
                    "message": "WeChat verification failed too many times. Please restart the login flow.",
                }
            try:
                return await self._refresh_qr_session(session)
            except Exception as exc:
                logger.error("Failed to refresh QR code after blocked verification: %s", exc)
                del self._sessions[session_key]
                return {
                    "status": "error",
                    "message": f"Failed to refresh QR code: {exc}",
                }

        if status == "expired":
            session.qr_refresh_count += 1
            if session.qr_refresh_count > self.MAX_QR_REFRESH:
                logger.warning(
                    "QR expired %d times, giving up session=%s",
                    self.MAX_QR_REFRESH,
                    session_key,
                )
                del self._sessions[session_key]
                return {
                    "status": "expired",
                    "message": "Login timed out: QR code expired multiple times. Please restart the login flow.",
                }

            logger.info(
                "QR expired, refreshing (%d/%d) session=%s",
                session.qr_refresh_count,
                self.MAX_QR_REFRESH,
                session_key,
            )
            try:
                return await self._refresh_qr_session(session)
            except Exception as exc:
                logger.error("Failed to refresh QR code: %s", exc)
                del self._sessions[session_key]
                return {
                    "status": "error",
                    "message": f"Failed to refresh QR code: {exc}",
                }

        if status == "confirmed":
            bot_id = status_resp.get("ilink_bot_id")
            if not bot_id:
                del self._sessions[session_key]
                logger.error("Login confirmed but ilink_bot_id missing from response")
                return {
                    "status": "error",
                    "message": "Login failed: server did not return ilink_bot_id.",
                }

            bot_token = status_resp.get("bot_token")
            session.pending_verify_code = None
            resp_base_url = status_resp.get("baseurl") or session.current_base_url or session.base_url
            user_id = status_resp.get("ilink_user_id")

            # Store on session before removing
            session.bot_token = bot_token
            session.bot_id = bot_id
            session.base_url = resp_base_url
            session.user_id = user_id
            session.status = "confirmed"

            del self._sessions[session_key]

            logger.info(
                "Login confirmed! bot_id=%s user_id=%s...",
                bot_id,
                (user_id[:8] + "...") if user_id and len(user_id) > 8 else user_id,
            )
            return {
                "status": "confirmed",
                "bot_token": bot_token,
                "bot_id": bot_id,
                "base_url": resp_base_url,
                "user_id": user_id,
                "message": "Successfully connected to WeChat!",
            }

        # Unknown status fallback
        return {"status": status, "message": f"Unknown status: {status}"}

    async def wait_for_login(
        self,
        session_key: str,
        timeout_s: int = 480,
    ) -> dict:
        """Block until login completes or times out. Intended for CLI use.

        Returns:
            dict with ``status``, ``message``, and credentials on success.
        """
        deadline = time.time() + max(timeout_s, 1)

        while time.time() < deadline:
            result = await self.poll_status(session_key)
            status = result.get("status")
            message = result.get("message", "").lower()

            if status in ("confirmed", "error"):
                return result

            if status == "expired" and (
                "restart" in message or "start a new login" in message
            ):
                # Max refreshes exhausted or the session no longer exists.
                return result

            # For "wait", "scaned", "refreshed" — keep polling
            await asyncio.sleep(1)

        # Timed out
        if session_key in self._sessions:
            del self._sessions[session_key]
        logger.warning(
            "wait_for_login: timed out after %ds session=%s",
            timeout_s,
            session_key,
        )
        return {
            "status": "expired",
            "message": "Login timed out. Please try again.",
        }
