from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from .base import (
    NativeSessionProvider,
    build_tail_preview,
    dt_from_ts,
    normalize_title_text,
    normalize_preview_text,
    read_json_lines,
)
from .types import BackendSessionTitle, NativeResumeSession

logger = logging.getLogger(__name__)


def _norm_path(p: str) -> str:
    """Normalize path for comparison: resolve case and separators."""
    return os.path.normcase(os.path.normpath(p))


def encode_project_path(working_path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", working_path)


def _legacy_project_path(working_path: str) -> str:
    return working_path.replace("/", "-")


class ClaudeNativeSessionProvider(NativeSessionProvider):
    agent_name = "claude"

    def __init__(self, root: str | None = None, history_path: str | None = None):
        self.root = Path(root or Path.home() / ".claude" / "projects")
        self.history_path = Path(history_path or Path.home() / ".claude" / "history.jsonl")

    def _project_dir(self, working_path: str) -> Path:
        return self.root / encode_project_path(working_path)

    @staticmethod
    def _candidate_project_names(working_path: str) -> set[str]:
        collapsed = re.sub(r"[^A-Za-z0-9]+", "-", working_path).strip("-")
        legacy = _legacy_project_path(working_path)
        names: set[str] = {
            legacy,
            legacy.replace("_", "-"),
            encode_project_path(working_path),
            encode_project_path(working_path).replace("_", "-"),
        }
        if collapsed:
            names.add(f"-{collapsed}")
        # On Windows, backslashes and colons are each replaced with a
        # separate dash by Claude Code (e.g. "C:\Users" → "C--Users"),
        # whereas the collapsed regex above merges consecutive non-alnum
        # chars into one dash ("C:\Users" → "C-Users").  Add the
        # per-character encoding so the actual directory name is found.
        per_char = re.sub(r"[^A-Za-z0-9]", "-", working_path)
        names.add(per_char)
        names.add(per_char.replace("_", "-"))
        return names

    @staticmethod
    def _merge_session(
        results: dict[str, NativeResumeSession],
        item: NativeResumeSession,
    ) -> None:
        existing = results.get(item.native_session_id)
        if existing is None:
            results[item.native_session_id] = item
            return

        if item.created_at and (existing.created_at is None or item.created_at < existing.created_at):
            existing.created_at = item.created_at
        if item.updated_at and (existing.updated_at is None or item.updated_at > existing.updated_at):
            existing.updated_at = item.updated_at
        existing.sort_ts = max(existing.sort_ts, item.sort_ts)

        for key, value in item.locator.items():
            if value and not existing.locator.get(key):
                existing.locator[key] = value

    @staticmethod
    def _parse_iso(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
        except Exception:
            return None

    def _existing_candidate_dirs(self, working_path: str) -> list[Path]:
        dirs: list[Path] = []
        seen: set[Path] = set()
        for name in self._candidate_project_names(working_path):
            path = self.root / name
            if path.exists() and path.is_dir() and path not in seen:
                dirs.append(path)
                seen.add(path)
        return dirs

    def _load_project_index(self, project_dir: Path) -> list[dict]:
        index_path = project_dir / "sessions-index.json"
        if not index_path.is_file():
            return []
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse Claude sessions index %s: %s", index_path, exc)
            return []
        entries = payload.get("entries", []) if isinstance(payload, dict) else []
        return [entry for entry in entries if isinstance(entry, dict)]

    def _merge_index_entry(
        self,
        results: dict[str, NativeResumeSession],
        *,
        working_path: str,
        project_dir: Path,
        entry: dict,
    ) -> str | None:
        session_id = str(entry.get("sessionId") or "").strip()
        project_path = str(entry.get("projectPath") or "").strip()
        if not session_id or _norm_path(project_path) != _norm_path(working_path):
            return None
        created_at = self._parse_iso(entry.get("created"))
        updated_at = self._parse_iso(entry.get("modified"))
        self._merge_session(
            results,
            NativeResumeSession(
                agent="claude",
                agent_prefix="cc",
                native_session_id=session_id,
                working_path=working_path,
                created_at=created_at,
                updated_at=updated_at,
                sort_ts=(updated_at or created_at).timestamp() if (updated_at or created_at) else 0.0,
                locator={
                    "full_path": entry.get("fullPath") or str(project_dir / f"{session_id}.jsonl"),
                    "first_prompt": str(entry.get("firstPrompt") or ""),
                },
            ),
        )
        return session_id

    def _history_sessions(
        self, working_path: str
    ) -> dict[str, dict[str, str | float | datetime | None]]:
        latest_history: dict[str, dict[str, str | float | datetime | None]] = {}
        if not self.history_path.exists():
            return latest_history
        for row in read_json_lines(self.history_path):
            project_path = str(row.get("project") or "").strip()
            session_id = str(row.get("sessionId") or "").strip()
            if project_path != working_path or not session_id:
                continue
            updated_at = dt_from_ts(row.get("timestamp"), millis=True)
            sort_ts = updated_at.timestamp() if updated_at else 0.0
            current = latest_history.get(session_id)
            if current and sort_ts <= float(current.get("sort_ts") or 0.0):
                continue
            latest_history[session_id] = {
                "updated_at": updated_at,
                "sort_ts": sort_ts,
                "display": str(row.get("display") or "").strip(),
            }
        return latest_history

    def _merge_history_sessions(
        self,
        results: dict[str, NativeResumeSession],
        *,
        working_path: str,
        history_sessions: dict[str, dict[str, str | float | datetime | None]],
    ) -> None:
        for session_id, payload in history_sessions.items():
            updated_at = payload.get("updated_at")
            self._merge_session(
                results,
                NativeResumeSession(
                    agent="claude",
                    agent_prefix="cc",
                    native_session_id=session_id,
                    working_path=working_path,
                    created_at=updated_at if isinstance(updated_at, datetime) else None,
                    updated_at=updated_at if isinstance(updated_at, datetime) else None,
                    sort_ts=float(payload.get("sort_ts") or 0.0),
                    locator={"history_display": str(payload.get("display") or "")},
                ),
            )

    def _merge_session_file(
        self,
        results: dict[str, NativeResumeSession],
        *,
        working_path: str,
        jsonl_path: Path,
    ) -> bool:
        rows = read_json_lines(jsonl_path)
        if not rows:
            return False
        created_at = None
        updated_at = None
        first_prompt = ""
        matched_working_path = False
        inferred_working_path = ""
        for row in rows:
            row_cwd = str(row.get("cwd") or row.get("projectPath") or "").strip()
            if row_cwd:
                inferred_working_path = row_cwd
            if _norm_path(row_cwd) == _norm_path(working_path):
                matched_working_path = True
            if row.get("type") == "user" and not first_prompt:
                first_prompt = str((row.get("message") or {}).get("content") or "")
            timestamp = self._parse_iso(row.get("timestamp"))
            if timestamp and created_at is None:
                created_at = timestamp
            if timestamp:
                updated_at = timestamp
        if not matched_working_path and inferred_working_path and _norm_path(inferred_working_path) != _norm_path(working_path):
            return False
        if not (created_at or updated_at):
            stat = jsonl_path.stat()
            created_at = datetime.fromtimestamp(stat.st_ctime)
            updated_at = datetime.fromtimestamp(stat.st_mtime)
        self._merge_session(
            results,
            NativeResumeSession(
                agent="claude",
                agent_prefix="cc",
                native_session_id=jsonl_path.stem,
                working_path=working_path,
                created_at=created_at,
                updated_at=updated_at,
                sort_ts=(updated_at or created_at).timestamp() if (updated_at or created_at) else 0.0,
                locator={"full_path": str(jsonl_path), "first_prompt": first_prompt},
            ),
        )
        return True

    def list_metadata(self, working_path: str) -> list[NativeResumeSession]:
        results: dict[str, NativeResumeSession] = {}
        candidate_dirs = self._existing_candidate_dirs(working_path)
        history_sessions = self._history_sessions(working_path)
        known_session_ids = set(history_sessions)
        session_file_paths: dict[str, Path] = {}

        for project_dir in candidate_dirs:
            for entry in self._load_project_index(project_dir):
                session_id = self._merge_index_entry(results, working_path=working_path, project_dir=project_dir, entry=entry)
                if not session_id:
                    continue
                known_session_ids.add(session_id)
                full_path = str(entry.get("fullPath") or "").strip()
                if full_path:
                    session_file_paths[session_id] = Path(full_path)

        if not results and self.root.exists():
            for project_dir in sorted(self.root.iterdir()):
                if not project_dir.is_dir() or project_dir in candidate_dirs:
                    continue
                for entry in self._load_project_index(project_dir):
                    session_id = self._merge_index_entry(
                        results, working_path=working_path, project_dir=project_dir, entry=entry
                    )
                    if not session_id:
                        continue
                    known_session_ids.add(session_id)
                    full_path = str(entry.get("fullPath") or "").strip()
                    if full_path:
                        session_file_paths[session_id] = Path(full_path)

        self._merge_history_sessions(results, working_path=working_path, history_sessions=history_sessions)

        for session_id, item in list(results.items()):
            full_path_raw = str(item.locator.get("full_path") or "").strip()
            if full_path_raw:
                session_file_paths.setdefault(session_id, Path(full_path_raw))

        for session_id in known_session_ids:
            if session_id in session_file_paths:
                continue
            for project_dir in candidate_dirs:
                candidate_path = project_dir / f"{session_id}.jsonl"
                if candidate_path.is_file():
                    session_file_paths[session_id] = candidate_path
                    break

        for session_id, jsonl_path in session_file_paths.items():
            if not jsonl_path.is_file():
                continue
            self._merge_session_file(results, working_path=working_path, jsonl_path=jsonl_path)

        # Always scan candidate dirs for .jsonl files not yet discovered
        # via index or history (e.g. sessions created by SDK/agent that
        # don't write to sessions-index.json or history.jsonl).
        known_ids = set(results)
        for project_dir in candidate_dirs:
            for jsonl_path in sorted(project_dir.glob("*.jsonl")):
                if jsonl_path.stem not in known_ids:
                    self._merge_session_file(results, working_path=working_path, jsonl_path=jsonl_path)

        items = list(results.values())
        items.sort(key=lambda item: (-item.sort_ts, item.native_session_id))
        return items

    def hydrate_preview(self, item: NativeResumeSession) -> NativeResumeSession:
        preview = ""
        full_path_raw = str(item.locator.get("full_path") or "").strip()
        full_path = Path(full_path_raw) if full_path_raw else None
        if full_path and full_path.exists():
            rows = read_json_lines(full_path)
            for row in reversed(rows):
                if row.get("type") != "assistant":
                    continue
                message = row.get("message") or {}
                content = message.get("content")
                if isinstance(content, str):
                    preview = content
                    break
                if isinstance(content, list):
                    parts: list[str] = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text = str(part.get("text") or "").strip()
                            if text:
                                parts.append(text)
                    if parts:
                        preview = "\n".join(parts)
                        break
        if not preview:
            preview = str(
                item.locator.get("first_prompt") or item.locator.get("history_display") or ""
            )
        item.last_agent_message = normalize_preview_text(preview)
        item.last_agent_tail = build_tail_preview(item.last_agent_message or preview or item.native_session_id)
        return item

    def get_title(
        self,
        *,
        native_session_id: str,
        working_path: str,
        first_user_message: str = "",
    ) -> BackendSessionTitle | None:
        title = normalize_title_text(first_user_message, limit=10)
        if not title:
            for item in self.list_metadata(working_path):
                if item.native_session_id != native_session_id:
                    continue
                title = normalize_title_text(str(item.locator.get("first_prompt") or ""), limit=10)
                break
        if not title:
            return None
        return BackendSessionTitle(title=title, source="derived_first_prompt", confidence="low")
