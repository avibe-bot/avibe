from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from .base import NativeSessionProvider, build_tail_preview, dt_from_ts, normalize_title_text, read_json_lines
from .types import BackendSessionTitle, NativeResumeSession

logger = logging.getLogger(__name__)


class CodexNativeSessionProvider(NativeSessionProvider):
    agent_name = "codex"
    _PLACEHOLDER_TITLES = {"new session", "untitled session", "未命名会话"}

    def __init__(self, db_path: str | None = None):
        codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
        self.db_path = Path(db_path) if db_path is not None else codex_home / "state_5.sqlite"

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)

    def list_metadata(self, working_path: str) -> list[NativeResumeSession]:
        if not self.db_path.exists():
            return []
        items: list[NativeResumeSession] = []
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    SELECT id, created_at, updated_at, title, first_user_message, rollout_path
                    FROM threads
                    WHERE cwd = ? AND archived = 0
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (working_path,),
                )
                for session_id, created_ts, updated_ts, title, first_user_message, rollout_path in cursor.fetchall():
                    created_at = dt_from_ts(created_ts)
                    updated_at = dt_from_ts(updated_ts)
                    items.append(
                        NativeResumeSession(
                            agent="codex",
                            agent_prefix="cx",
                            native_session_id=session_id,
                            working_path=working_path,
                            created_at=created_at,
                            updated_at=updated_at,
                            sort_ts=(updated_at or created_at).timestamp() if (updated_at or created_at) else 0.0,
                            locator={
                                "title": title or "",
                                "first_user_message": first_user_message or "",
                                "rollout_path": rollout_path or "",
                            },
                        )
                    )
        except Exception as exc:
            logger.warning("Failed to list Codex sessions for %s: %s", working_path, exc)
        return items

    def hydrate_preview(self, item: NativeResumeSession) -> NativeResumeSession:
        preview = ""
        rollout_path_raw = str(item.locator.get("rollout_path") or "").strip()
        rollout_path = Path(rollout_path_raw) if rollout_path_raw else None
        if rollout_path and rollout_path.is_file():
            rows = read_json_lines(rollout_path)
            for row in reversed(rows):
                if row.get("type") != "response_item":
                    continue
                payload = row.get("payload") or {}
                if payload.get("type") != "message" or payload.get("role") != "assistant":
                    continue
                parts = payload.get("content") or []
                texts: list[str] = []
                if isinstance(parts, list):
                    for part in parts:
                        if isinstance(part, dict) and part.get("type") == "output_text":
                            text = str(part.get("text") or "").strip()
                            if text:
                                texts.append(text)
                if texts:
                    preview = "\n".join(texts)
                    break
        if not preview:
            preview = str(item.locator.get("title") or item.locator.get("first_user_message") or "")
        item.last_agent_message = preview
        item.last_agent_tail = build_tail_preview(preview or item.native_session_id)
        return item

    @classmethod
    def is_placeholder_title(cls, title: str) -> bool:
        return title.strip().lower() in cls._PLACEHOLDER_TITLES

    def get_title(
        self,
        *,
        native_session_id: str,
        working_path: str,
        first_user_message: str = "",
    ) -> BackendSessionTitle | None:
        if not self.db_path.exists():
            return None
        row: tuple[str | None, str | None] | None
        has_first_user_message_column = True
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT title, first_user_message
                    FROM threads
                    WHERE id = ? AND cwd = ? AND archived = 0
                    LIMIT 1
                    """,
                    (native_session_id, working_path),
                ).fetchone()
        except Exception as exc:
            if "no such column: first_user_message" in str(exc):
                has_first_user_message_column = False
                try:
                    with self._connect() as conn:
                        title_row = conn.execute(
                            """
                            SELECT title
                            FROM threads
                            WHERE id = ? AND cwd = ? AND archived = 0
                            LIMIT 1
                            """,
                            (native_session_id, working_path),
                        ).fetchone()
                    row = (title_row[0], None) if title_row else None
                except Exception as fallback_exc:
                    logger.warning("Failed to read Codex thread title %s: %s", native_session_id, fallback_exc)
                    return None
            else:
                logger.warning("Failed to read Codex thread title %s: %s", native_session_id, exc)
                return None
        if row is None:
            return None
        try:
            title_value, first_user_value = row
        except ValueError:
            title_value = row[0]
            first_user_value = None
        stored_first_user_message = normalize_title_text(str(first_user_value or ""))
        if has_first_user_message_column and stored_first_user_message:
            fallback_first_user_message = normalize_title_text(first_user_message)
            derived = normalize_title_text(first_user_message or stored_first_user_message, limit=10)
            if derived:
                return BackendSessionTitle(title=derived, source="derived_first_prompt", confidence="low")

        title = normalize_title_text(str(title_value or ""))
        if not title or self.is_placeholder_title(title):
            return None
        return BackendSessionTitle(title=title, source="backend", confidence="high")
