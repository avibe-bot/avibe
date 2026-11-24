"""Controller-free Antigravity native session history provider over ACP ``session/list``."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from modules.agents.acp import ACP_AUTH_REQUIRED, AcpClient, AcpRpcError, AcpTransportError
from modules.agents.antigravity.agent import (
    build_antigravity_command,
    build_antigravity_env,
    resolve_antigravity_cli_path,
)
from modules.agents.antigravity.auth import authenticate_antigravity_client

from .base import (
    NativeSessionProvider,
    build_tail_preview,
    derive_first_prompt_title,
    dt_from_ts,
    normalize_title_text,
)
from .types import BackendSessionTitle, NativeResumeSession

logger = logging.getLogger(__name__)

_CLIENT_INFO = {"name": "avibe-native-sessions", "version": "1.0.0"}
_MAX_LIST_PAGES = 20


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return dt_from_ts(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return dt_from_ts(text)
    return None


def _load_persisted_antigravity_config() -> Any:
    try:
        from config.v2_config import V2Config

        v2 = V2Config.load()
        return getattr(v2.agents, "antigravity", None)
    except (FileNotFoundError, OSError, ValueError) as exc:
        logger.debug("Persisted Antigravity config unavailable for native session listing: %s", exc)
        return None


class AntigravityNativeSessionProvider(NativeSessionProvider):
    """Passive ``session/list`` provider for Antigravity ACP sessions (prefix ``ag``)."""

    agent_name = "antigravity"
    _PLACEHOLDER_TITLES = {"new session", "untitled session", "未命名会话"}

    def __init__(
        self,
        *,
        cli_path: str | None = None,
        extra_args: Sequence[str] = (),
        config: Any = None,
        request_timeout: float = 15.0,
    ) -> None:
        self._cli_path = cli_path
        self._extra_args = tuple(extra_args)
        self._config = config
        self._request_timeout = request_timeout

    def _effective_config(self) -> Any:
        if self._config is not None:
            return self._config
        return _load_persisted_antigravity_config()

    def _build_launch(
        self,
        working_path: str,
        *,
        config: Any = None,
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        effective_config = config if config is not None else self._effective_config()
        raw_cli_path = (
            self._cli_path
            or getattr(effective_config, "cli_path", None)
            or getattr(effective_config, "binary", None)
            or "agy"
        )
        resolved_cli = resolve_antigravity_cli_path(raw_cli_path)
        extra_args = self._extra_args or tuple(getattr(effective_config, "extra_args", None) or ())
        # Passive session listing MUST NOT pass --dangerously-skip-permissions.
        command = build_antigravity_command(
            resolved_cli,
            execution=False,
            extra_args=extra_args,
        )
        env = build_antigravity_env(
            effective_config,
            cli_path=resolved_cli,
            working_dir=working_path or None,
        )
        return command, env

    async def _list_raw_sessions(self, working_path: str) -> list[dict[str, Any]]:
        config = await asyncio.to_thread(self._effective_config)
        command, env = await asyncio.to_thread(self._build_launch, working_path, config=config)
        client = AcpClient(
            command,
            cwd=working_path or None,
            env=env,
            request_timeout=self._request_timeout,
        )
        sessions: list[dict[str, Any]] = []
        try:
            await client.start()
            await client.initialize(client_info=_CLIENT_INFO, client_capabilities={})
            await authenticate_antigravity_client(client, getattr(config, "auth_mode", None))
            if not client.supports_list:
                return []

            cursor: str | None = None
            for _ in range(_MAX_LIST_PAGES):
                resp = await client.session_list(cwd=working_path or None, cursor=cursor)
                page = resp.get("sessions")
                if isinstance(page, list):
                    for item in page:
                        if isinstance(item, Mapping):
                            sessions.append(dict(item))
                next_cursor = resp.get("nextCursor")
                if not isinstance(next_cursor, str) or not next_cursor:
                    break
                cursor = next_cursor
            return sessions
        except AcpRpcError as exc:
            if exc.code == ACP_AUTH_REQUIRED:
                logger.debug("Antigravity session/list requires authentication")
            else:
                logger.warning("Antigravity session/list RPC failed (%s): %s", exc.code, exc.message)
            return []
        except AcpTransportError as exc:
            logger.debug("Antigravity session/list transport unavailable: %s", exc)
            return []
        finally:
            await client.close()

    @staticmethod
    def _ensure_no_running_loop(method_name: str) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise RuntimeError(
            f"AntigravityNativeSessionProvider.{method_name}() cannot be called from a running "
            f"event loop; await {method_name}_async() or run via asyncio.to_thread() instead."
        )

    @staticmethod
    def _format_sessions(
        raw_sessions: Sequence[Mapping[str, Any]],
        working_path: str,
    ) -> list[NativeResumeSession]:
        items: list[NativeResumeSession] = []
        for entry in raw_sessions:
            native_session_id = str(entry.get("sessionId") or "").strip()
            if not native_session_id:
                continue
            entry_cwd = str(entry.get("cwd") or working_path or "").strip()
            title = str(entry.get("title") or "").strip()
            created_at = _parse_timestamp(entry.get("createdAt"))
            updated_at = _parse_timestamp(entry.get("updatedAt"))
            sort_dt = updated_at or created_at
            items.append(
                NativeResumeSession(
                    agent="antigravity",  # type: ignore[arg-type]
                    agent_prefix="ag",  # type: ignore[arg-type]
                    native_session_id=native_session_id,
                    working_path=entry_cwd,
                    created_at=created_at,
                    updated_at=updated_at,
                    sort_ts=sort_dt.timestamp() if sort_dt else 0.0,
                    locator={"title": title},
                )
            )
        return items

    @classmethod
    def _resolve_title(
        cls,
        items: Sequence[NativeResumeSession],
        *,
        native_session_id: str,
        first_user_message: str | None = None,
    ) -> BackendSessionTitle | None:
        for item in items:
            if item.native_session_id != native_session_id:
                continue
            title = normalize_title_text(str(item.locator.get("title") or ""))
            if title and not cls.is_placeholder_title(title):
                return BackendSessionTitle(title=title, source="backend", confidence="high")
            break
        return derive_first_prompt_title(first_user_message or "")

    async def list_metadata_async(self, working_path: str) -> list[NativeResumeSession]:
        raw_sessions = await self._list_raw_sessions(working_path)
        return self._format_sessions(raw_sessions, working_path)

    def list_metadata(self, working_path: str) -> list[NativeResumeSession]:
        self._ensure_no_running_loop("list_metadata")
        return asyncio.run(self.list_metadata_async(working_path))

    def hydrate_preview(self, item: NativeResumeSession) -> NativeResumeSession:
        preview = str(item.locator.get("title") or "").strip()
        item.last_agent_message = preview
        item.last_agent_tail = build_tail_preview(preview or item.native_session_id)
        return item

    @classmethod
    def is_placeholder_title(cls, title: str) -> bool:
        return title.strip().lower() in cls._PLACEHOLDER_TITLES

    async def get_title_async(
        self,
        *,
        working_path: str,
        native_session_id: str,
        first_user_message: str | None = None,
    ) -> BackendSessionTitle | None:
        if not native_session_id:
            return derive_first_prompt_title(first_user_message or "")
        items = await self.list_metadata_async(working_path)
        return self._resolve_title(
            items,
            native_session_id=native_session_id,
            first_user_message=first_user_message,
        )

    def get_title(
        self,
        *,
        native_session_id: str,
        working_path: str,
        first_user_message: str = "",
    ) -> BackendSessionTitle | None:
        if not native_session_id:
            return derive_first_prompt_title(first_user_message)
        items = self.list_metadata(working_path)
        return self._resolve_title(
            items,
            native_session_id=native_session_id,
            first_user_message=first_user_message,
        )
