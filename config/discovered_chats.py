from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from config import paths
from config.atomic_io import write_atomic

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DiscoveredChat:
    chat_id: str
    platform: str
    name: str = ""
    username: str = ""
    chat_type: str = ""
    is_private: bool = False
    is_forum: bool = False
    supports_topics: bool = False
    last_seen_at: str = ""


@dataclass
class DiscoveredChatsState:
    chats: Dict[str, Dict[str, DiscoveredChat]] = field(default_factory=dict)


class DiscoveredChatsStore:
    _instance: Optional["DiscoveredChatsStore"] = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls, storage_path: Optional[Path] = None) -> "DiscoveredChatsStore":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(storage_path)
            else:
                cls._instance.maybe_reload()
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        with cls._instance_lock:
            cls._instance = None

    def __init__(self, storage_path: Optional[Path] = None):
        self.storage_path = storage_path or paths.get_discovered_chats_path()
        self.state = DiscoveredChatsState()
        self._file_mtime: float = 0
        self._lock = threading.RLock()
        self._load()

    def maybe_reload(self) -> None:
        with self._lock:
            try:
                mtime = self.storage_path.stat().st_mtime
                if mtime > self._file_mtime:
                    self._load()
            except FileNotFoundError:
                pass

    def _load(self) -> None:
        with self._lock:
            if not self.storage_path.exists():
                return
            try:
                payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.error("Failed to load discovered chats: %s", exc)
                return
            if not isinstance(payload, dict):
                logger.error("Failed to load discovered chats: invalid format")
                return

            platforms: Dict[str, Dict[str, DiscoveredChat]] = {}
            raw_platforms = payload.get("platforms") or {}
            if isinstance(raw_platforms, dict):
                for platform, chats in raw_platforms.items():
                    if not isinstance(chats, dict):
                        continue
                    platform_chats: Dict[str, DiscoveredChat] = {}
                    for chat_id, chat_payload in chats.items():
                        if not isinstance(chat_payload, dict):
                            continue
                        platform_chats[str(chat_id)] = DiscoveredChat(
                            chat_id=str(chat_id),
                            platform=str(platform),
                            name=chat_payload.get("name", ""),
                            username=chat_payload.get("username", ""),
                            chat_type=chat_payload.get("chat_type", ""),
                            is_private=bool(chat_payload.get("is_private", False)),
                            is_forum=bool(chat_payload.get("is_forum", False)),
                            supports_topics=bool(chat_payload.get("supports_topics", False)),
                            last_seen_at=chat_payload.get("last_seen_at", ""),
                        )
                    platforms[str(platform)] = platform_chats

            self.state = DiscoveredChatsState(chats=platforms)
            try:
                self._file_mtime = self.storage_path.stat().st_mtime
            except FileNotFoundError:
                self._file_mtime = 0

    def save(self) -> None:
        with self._lock:
            paths.ensure_data_dirs()
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": SCHEMA_VERSION,
                "platforms": {
                    platform: {chat_id: asdict(chat) for chat_id, chat in chats.items()}
                    for platform, chats in self.state.chats.items()
                },
            }
            write_atomic(self.storage_path, json.dumps(payload, indent=2) + "\n")
            try:
                self._file_mtime = self.storage_path.stat().st_mtime
            except FileNotFoundError:
                self._file_mtime = 0

    def remember_chat(
        self,
        *,
        platform: str,
        chat_id: str,
        name: str = "",
        username: str = "",
        chat_type: str = "",
        is_private: bool = False,
        is_forum: bool = False,
        supports_topics: bool = False,
    ) -> DiscoveredChat:
        platform_key = str(platform)
        chat_key = str(chat_id)
        with self._lock:
            self.maybe_reload()
            platform_chats = self.state.chats.setdefault(platform_key, {})
            existing = platform_chats.get(chat_key)
            if existing is None:
                existing = DiscoveredChat(chat_id=chat_key, platform=platform_key)
                platform_chats[chat_key] = existing

            if name:
                existing.name = name
            if username:
                existing.username = username
            if chat_type:
                existing.chat_type = chat_type
            existing.is_private = existing.is_private or is_private
            existing.is_forum = existing.is_forum or is_forum
            existing.supports_topics = existing.supports_topics or supports_topics
            existing.last_seen_at = _now_iso()
            self.save()
            return existing

    def list_chats(self, platform: str, *, include_private: bool = True) -> list[DiscoveredChat]:
        with self._lock:
            self.maybe_reload()
            chats = list(self.state.chats.get(str(platform), {}).values())
            if not include_private:
                chats = [chat for chat in chats if not chat.is_private]
            chats.sort(
                key=lambda chat: (
                    chat.last_seen_at or "",
                    chat.name.lower() if chat.name else "",
                    chat.chat_id,
                ),
                reverse=True,
            )
            return chats
