import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import paths

logger = logging.getLogger(__name__)


def _optional_str_dict(value: Any) -> Optional[Dict[str, str]]:
    if not isinstance(value, dict):
        return None
    out = {str(key): str(item) for key, item in value.items() if isinstance(key, str) and isinstance(item, str)}
    return out or None


@dataclass
class ActivePollInfo:
    """Information about an active poll that needs to be restored on restart."""

    opencode_session_id: str
    base_session_id: str
    channel_id: str
    thread_id: str
    settings_key: str
    working_path: str
    baseline_message_ids: List[str] = field(default_factory=list)
    seen_tool_calls: List[str] = field(default_factory=list)
    emitted_assistant_messages: List[str] = field(default_factory=list)
    started_at: float = 0.0
    prompt_started_at: Optional[float] = None
    model_dict: Optional[Dict[str, str]] = None
    reasoning_effort: Optional[str] = None
    # Ack reaction info for cleanup on restore
    ack_reaction_message_id: Optional[str] = None
    ack_reaction_emoji: Optional[str] = None
    # Typing indicator info for cleanup on restore
    typing_indicator_active: bool = False
    context_token: str = ""
    processing_indicator: Dict[str, Any] = field(default_factory=dict)
    # User identity for restoring question UI context
    user_id: str = ""
    platform: str = ""
    session_key: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "opencode_session_id": self.opencode_session_id,
            "base_session_id": self.base_session_id,
            "channel_id": self.channel_id,
            "thread_id": self.thread_id,
            "settings_key": self.settings_key,
            "working_path": self.working_path,
            "baseline_message_ids": self.baseline_message_ids,
            "seen_tool_calls": self.seen_tool_calls,
            "emitted_assistant_messages": self.emitted_assistant_messages,
            "started_at": self.started_at,
            "prompt_started_at": self.prompt_started_at,
            "model_dict": self.model_dict,
            "reasoning_effort": self.reasoning_effort,
            "ack_reaction_message_id": self.ack_reaction_message_id,
            "ack_reaction_emoji": self.ack_reaction_emoji,
            "typing_indicator_active": self.typing_indicator_active,
            "context_token": self.context_token,
            "processing_indicator": self.processing_indicator,
            "user_id": self.user_id,
            "platform": self.platform,
            "session_key": self.session_key,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ActivePollInfo":
        processing_indicator = data.get("processing_indicator") or {}
        if not processing_indicator:
            processing_indicator = {
                "platform": data.get("platform", ""),
                "user_id": data.get("user_id", ""),
                "channel_id": data.get("channel_id", ""),
                "thread_id": data.get("thread_id", ""),
                "context_token": data.get("context_token", ""),
                "ack_reaction_message_id": data.get("ack_reaction_message_id"),
                "ack_reaction_emoji": data.get("ack_reaction_emoji"),
                "typing_indicator_active": bool(data.get("typing_indicator_active", False)),
            }
        return cls(
            opencode_session_id=data.get("opencode_session_id", ""),
            base_session_id=data.get("base_session_id", ""),
            channel_id=data.get("channel_id", ""),
            thread_id=data.get("thread_id", ""),
            settings_key=data.get("settings_key", ""),
            working_path=data.get("working_path", ""),
            baseline_message_ids=data.get("baseline_message_ids", []),
            seen_tool_calls=data.get("seen_tool_calls", []),
            emitted_assistant_messages=data.get("emitted_assistant_messages", []),
            started_at=data.get("started_at", 0.0),
            prompt_started_at=data.get("prompt_started_at"),
            model_dict=_optional_str_dict(data.get("model_dict")),
            reasoning_effort=data.get("reasoning_effort") if isinstance(data.get("reasoning_effort"), str) else None,
            ack_reaction_message_id=data.get("ack_reaction_message_id"),
            ack_reaction_emoji=data.get("ack_reaction_emoji"),
            typing_indicator_active=bool(data.get("typing_indicator_active", False)),
            context_token=data.get("context_token", ""),
            processing_indicator=processing_indicator,
            user_id=data.get("user_id", ""),
            platform=data.get("platform", ""),
            session_key=data.get("session_key", ""),
        )


@dataclass
class SessionState:
    # session_mappings: user_id -> agent_name -> thread_id -> session_id
    session_mappings: Dict[str, Dict[str, Dict[str, str]]] = field(default_factory=dict)
    active_slack_threads: Dict[str, Dict[str, Dict[str, float]]] = field(default_factory=dict)
    # active_polls: opencode_session_id -> ActivePollInfo
    active_polls: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # processed_message_ts: channel_id -> thread_ts -> list of processed message IDs
    # (set-based dedup, supports all platforms including Feishu non-monotonic IDs)
    processed_message_ts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    last_activity: Optional[str] = None


def parse_session_payload(payload: dict[str, Any]) -> SessionState:
    """Parse current or legacy sessions JSON into SessionState."""
    if not isinstance(payload, dict):
        raise ValueError("sessions payload must be an object")
    return SessionState(
        session_mappings=payload.get("session_mappings", {}),
        active_slack_threads=payload.get("active_slack_threads", {}),
        active_polls=payload.get("active_polls", {}),
        processed_message_ts=payload.get("processed_message_ts", {}),
        last_activity=payload.get("last_activity"),
    )


def load_session_state_from_json(sessions_path: Path) -> SessionState:
    if not sessions_path.exists():
        return SessionState()
    payload = json.loads(sessions_path.read_text(encoding="utf-8"))
    return parse_session_payload(payload)


def infer_platform_from_thread_ids(agent_maps: Dict[str, Dict[str, str]]) -> Optional[str]:
    """Infer platform from thread ID prefixes within a legacy mapping."""
    platforms: set[str] = set()
    for thread_map in agent_maps.values():
        for thread_id in thread_map:
            if "_" in thread_id:
                prefix = thread_id.split("_", 1)[0]
                if prefix.isalpha():
                    platforms.add(prefix)
    if len(platforms) == 1:
        return platforms.pop()
    return None


def migrate_session_state_active_polls(state: SessionState, default_platform: str) -> bool:
    migrated = False
    for _sid, data in state.active_polls.items():
        if not isinstance(data, dict):
            continue
        sk = data.get("settings_key", "")
        if sk and "::" in sk:
            prefix, raw = sk.split("::", 1)
            if not data.get("platform") and prefix:
                data["platform"] = prefix
            data["settings_key"] = raw
            migrated = True
        if not data.get("platform"):
            data["platform"] = default_platform
            migrated = True
    return migrated


def migrate_session_state_mappings(state: SessionState, default_platform: str) -> tuple[int, int, int]:
    """Migrate legacy raw session keys to platform-prefixed keys.

    Returns ``(migrated_entries, legacy_keys, empty_keys_removed)``.
    """
    mappings = state.session_mappings
    old_keys = [
        k
        for k in list(mappings.keys())
        if "::" not in k and mappings[k]
    ]
    if not old_keys:
        empty_keys = [k for k in list(mappings.keys()) if not mappings[k]]
        for key in empty_keys:
            del mappings[key]
        return 0, 0, len(empty_keys)

    migrated_count = 0
    for old_key in old_keys:
        old_agents = mappings[old_key]
        inferred = infer_platform_from_thread_ids(old_agents)
        platform = inferred or default_platform
        if not inferred:
            logger.warning(
                "Could not infer platform for legacy key %s, falling back to default_platform=%s",
                old_key,
                default_platform,
            )
        new_key = f"{platform}::{old_key}"

        if new_key not in mappings:
            mappings[new_key] = {}
        for agent_name, thread_map in old_agents.items():
            if agent_name not in mappings[new_key]:
                mappings[new_key][agent_name] = {}
            for thread_id, session_id in thread_map.items():
                if thread_id not in mappings[new_key][agent_name]:
                    mappings[new_key][agent_name][thread_id] = session_id
                    migrated_count += 1

        del mappings[old_key]

    empty_keys = [k for k in list(mappings.keys()) if not mappings[k]]
    for key in empty_keys:
        del mappings[key]

    return migrated_count, len(old_keys), len(empty_keys)


@dataclass
class SessionsStore:
    sessions_path: Path = field(default_factory=paths.get_sessions_path)
    state: SessionState = field(default_factory=SessionState)

    def __post_init__(self) -> None:
        self.sessions_path = Path(self.sessions_path)
        self.db_path: Path | None = None
        self._service = None
        self._ensure_service()
        self.load()
        self._service.has_external_write()

    def _ensure_service(self) -> None:
        target_db = Path(self.sessions_path).with_name("vibe.sqlite")
        if self._service is not None and self.db_path == target_db:
            return
        if self._service is not None:
            self._service.close()
        from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config
        from storage.sessions_service import SQLiteSessionsService

        ensure_sqlite_state(
            db_path=target_db,
            state_dir=Path(self.sessions_path).parent,
            primary_platform=resolve_primary_platform_from_config(Path(self.sessions_path).parent),
        )
        self.db_path = target_db
        self._service = SQLiteSessionsService(target_db)

    def close(self) -> None:
        service = getattr(self, "_service", None)
        if service is not None:
            service.close()

    def load(self) -> None:
        self._ensure_service()
        self.state = self._service.load_state()

    def maybe_reload(self) -> None:
        self._ensure_service()
        if self._service.has_external_write():
            self.load()

    def migrate_active_polls(self, default_platform: str) -> None:
        """Migrate legacy active_polls that lack ``platform`` or use scoped settings_key.

        Should be called once after load() when the runtime knows the primary
        platform.  For pre-multi-platform installs every active poll was
        created under a single platform, so ``default_platform`` is safe to
        use as the backfill value.
        """
        migrated = migrate_session_state_active_polls(self.state, default_platform)
        if migrated:
            self.save()
            logger.info("Migrated legacy active_polls (default_platform=%s)", default_platform)

    @staticmethod
    def _infer_platform_from_thread_ids(agent_maps: Dict[str, Dict[str, str]]) -> Optional[str]:
        """Infer the platform from thread ID prefixes within a legacy mapping.

        Thread IDs are formatted as ``<platform>_<ts>`` or
        ``<platform>_<ts>:<working_path>``, e.g. ``slack_1774074591.762089``
        or ``discord_1485641561998889093:/work``.  Returns the platform if
        all thread IDs agree, otherwise ``None``.
        """
        return infer_platform_from_thread_ids(agent_maps)

    def migrate_session_mappings(self, default_platform: str) -> None:
        """Migrate legacy session_mappings stored under raw keys to prefixed keys.

        Before the settings_key/session_key split (commit 674e24d), session
        mappings were stored under raw channel/user IDs (e.g. ``C0A6U2GH6P5``).
        After the split, they are stored under platform-prefixed keys
        (e.g. ``slack::C0A6U2GH6P5``).  This method merges old-format entries
        into their prefixed counterparts so that existing sessions are not
        orphaned on upgrade.

        The platform is inferred from thread ID prefixes where possible,
        falling back to ``default_platform`` only when inference fails.

        Also removes empty orphan keys left behind by the migration.
        """
        previous_keys = set(self.state.session_mappings.keys())
        migrated_count, old_key_count, empty_key_count = migrate_session_state_mappings(self.state, default_platform)
        removed_keys = previous_keys - set(self.state.session_mappings.keys())
        if migrated_count == 0 and old_key_count == 0:
            if empty_key_count:
                self._delete_session_scope_keys(removed_keys)
                self.save()
                logger.info("Cleaned up %d empty session_mapping keys", empty_key_count)
            return
        self.save()
        self._delete_session_scope_keys(removed_keys)
        logger.info(
            "Migrated %d session entries from %d legacy keys; removed %d empty keys",
            migrated_count,
            old_key_count,
            empty_key_count,
        )

    def _delete_session_scope_keys(self, scope_keys: set[str]) -> int:
        self._ensure_service()
        deleted = 0
        for scope_key in scope_keys:
            deleted += self._service.delete_agent_sessions(scope_key=str(scope_key))
        return deleted

    def _ensure_user_namespace(self, user_id: str) -> None:
        if user_id not in self.state.session_mappings:
            self.state.session_mappings[user_id] = {}
        if user_id not in self.state.active_slack_threads:
            self.state.active_slack_threads[user_id] = {}

    @staticmethod
    def _count_session_mappings(agent_maps: Dict[str, Any]) -> int:
        return sum(len(thread_map) for thread_map in agent_maps.values() if isinstance(thread_map, dict))

    def _sync_session_mappings_for_user(self, user_id: str) -> None:
        fresh_maps = self._service.load_state().session_mappings.get(user_id, {})
        self.state.session_mappings[user_id] = {
            str(agent_name): dict(thread_map)
            for agent_name, thread_map in fresh_maps.items()
            if isinstance(thread_map, dict)
        }
        self._ensure_user_namespace(user_id)

    def get_agent_map(self, user_id: str, agent_name: str) -> Dict[str, str]:
        """Get mapping of thread_id -> session_id for a user and agent."""
        self.maybe_reload()
        self._ensure_user_namespace(user_id)
        agent_map = self.state.session_mappings[user_id].get(agent_name)
        if agent_map is None:
            agent_map = {}
            self.state.session_mappings[user_id][agent_name] = agent_map
        return agent_map

    def get_agent_session_row_id(self, user_id: str, agent_name: str, thread_id: str) -> Optional[str]:
        self._ensure_service()
        return self._service.get_agent_session_row_id(
            scope_key=user_id,
            agent_name=agent_name,
            session_anchor=thread_id,
        )

    def ensure_agent_session_id(
        self,
        user_id: str,
        agent_name: str,
        thread_id: str,
        *,
        workdir: str | None = None,
        vibe_agent_id: str | None = None,
        vibe_agent_name: str | None = None,
    ) -> Optional[str]:
        self._ensure_service()
        agent_session_id = self._service.ensure_agent_session_id(
            scope_key=user_id,
            agent_name=agent_name,
            session_anchor=thread_id,
            workdir=workdir,
            vibe_agent_id=vibe_agent_id,
            vibe_agent_name=vibe_agent_name,
        )
        self.get_agent_map(user_id, agent_name).setdefault(thread_id, "")
        return agent_session_id

    def find_session_for_anchor(self, user_id: str, session_anchor: str):
        """Read-through to the SQLite service's ``(scope, anchor)`` lookup (latest
        row, any backend). Not cached — callers use it only to pin a thread's
        backend at resolution time."""
        self._ensure_service()
        finder = getattr(self._service, "find_session_for_anchor", None)
        if not callable(finder):
            return None
        return finder(scope_key=user_id, session_anchor=session_anchor)

    def bind_agent_session(
        self,
        user_id: str,
        agent_name: str,
        thread_id: str,
        session_id: Any,
        *,
        workdir: str | None = None,
        vibe_agent_id: str | None = None,
        vibe_agent_name: str | None = None,
    ) -> Optional[str]:
        self._ensure_service()
        agent_session_id = self._service.bind_agent_session(
            scope_key=user_id,
            agent_name=agent_name,
            session_anchor=thread_id,
            native_session_id=session_id,
            workdir=workdir,
            vibe_agent_id=vibe_agent_id,
            vibe_agent_name=vibe_agent_name,
        )
        # WRITE-ONCE in the in-memory cache too: the map mirrors the (write-once)
        # table, so don't overwrite an existing native — otherwise a later
        # ``save_state`` flush could reintroduce a changed native id.
        agent_map = self.get_agent_map(user_id, agent_name)
        if not agent_map.get(thread_id):
            agent_map[thread_id] = session_id
        return agent_session_id

    def materialize_agent_session_route(
        self,
        agent_session_id: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> bool:
        self._ensure_service()
        return self._service.materialize_agent_session_route(
            agent_session_id,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    def bind_agent_session_by_id(
        self,
        agent_session_id: str,
        native_session_id: Any,
        *,
        workdir: str | None = None,
        vibe_agent_id: str | None = None,
        vibe_agent_name: str | None = None,
        vibe_agent_backend: str | None = None,
    ) -> Optional[str]:
        self._ensure_service()
        bound_id = self._service.bind_agent_session_by_id(
            session_id=agent_session_id,
            native_session_id=native_session_id,
            workdir=workdir,
            vibe_agent_id=vibe_agent_id,
            vibe_agent_name=vibe_agent_name,
            vibe_agent_backend=vibe_agent_backend,
        )
        if bound_id:
            self.load()
        return bound_id

    def remove_agent_session(self, user_id: str, agent_name: str, thread_id: str) -> bool:
        self._ensure_service()
        self._ensure_user_namespace(user_id)
        before = self._count_session_mappings(self.state.session_mappings[user_id])
        removed = self._service.delete_agent_session(
            scope_key=user_id,
            agent_name=agent_name,
            session_anchor=thread_id,
        )
        self._sync_session_mappings_for_user(user_id)
        after = self._count_session_mappings(self.state.session_mappings[user_id])
        return bool(removed or after < before)

    def clear_agent_sessions(self, user_id: str, agent_name: str | None = None) -> int:
        self._ensure_service()
        self._ensure_user_namespace(user_id)
        before = self._count_session_mappings(self.state.session_mappings[user_id])
        removed = self._service.delete_agent_sessions(scope_key=user_id, agent_name=agent_name)
        self._sync_session_mappings_for_user(user_id)
        after = self._count_session_mappings(self.state.session_mappings[user_id])
        return max(removed, max(before - after, 0))

    def clear_session_base(self, user_id: str, base_session_id: str) -> int:
        self._ensure_service()
        removed = self._service.delete_agent_sessions(
            scope_key=user_id,
            session_anchor_prefix=base_session_id,
        )
        self._ensure_user_namespace(user_id)
        cleared = 0
        for agent_map in self.state.session_mappings[user_id].values():
            keys_to_remove = [
                mapping_key
                for mapping_key in list(agent_map.keys())
                if mapping_key == base_session_id or mapping_key.startswith(f"{base_session_id}:")
            ]
            for mapping_key in keys_to_remove:
                del agent_map[mapping_key]
                cleared += 1
        return max(removed, cleared)

    def get_thread_map(self, user_id: str, channel_id: str) -> Dict[str, float]:
        self.maybe_reload()
        self._ensure_user_namespace(user_id)
        channel_map = self.state.active_slack_threads[user_id].get(channel_id)
        if channel_map is None:
            channel_map = {}
            self.state.active_slack_threads[user_id][channel_id] = channel_map
        return channel_map

    def mark_thread_active(self, user_id: str, channel_id: str, thread_ts: str, last_active_at: float) -> None:
        self._ensure_service()
        self._service.mark_thread_active(user_id, channel_id, thread_ts, last_active_at)
        self._ensure_user_namespace(user_id)
        self.state.active_slack_threads[user_id].setdefault(channel_id, {})[thread_ts] = last_active_at

    def remove_active_thread(self, user_id: str, channel_id: str, thread_ts: str) -> bool:
        self._ensure_service()
        removed = self._service.delete_active_thread(user_id, channel_id, thread_ts)
        channel_map = self.get_thread_map(user_id, channel_id)
        if thread_ts in channel_map:
            del channel_map[thread_ts]
            removed = True
        if not channel_map:
            self.state.active_slack_threads[user_id].pop(channel_id, None)
        if not self.state.active_slack_threads[user_id]:
            self.state.active_slack_threads.pop(user_id, None)
        return removed

    # Max number of message IDs to keep per thread for dedup
    _DEDUP_SET_MAX = 200

    def _get_processed_set(self, channel_id: str, thread_ts: str) -> List[str]:
        """Get the processed message ID list for a thread.

        Handles backward-compat: old format stored a single string (high-water mark).
        New format stores a list of message IDs.
        """
        channel_map = self.state.processed_message_ts.get(channel_id)
        if not channel_map:
            return []
        value = channel_map.get(thread_ts)
        if value is None:
            return []
        # Backward compat: old format was a single string
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return value
        return []

    def is_message_in_processed_set(self, channel_id: str, thread_ts: str, message_ts: str) -> bool:
        """Check if a message ID is in the processed set."""
        return message_ts in self._get_processed_set(channel_id, thread_ts)

    def _remember_processed_message(self, channel_id: str, thread_ts: str, message_ts: str) -> None:
        if channel_id not in self.state.processed_message_ts:
            self.state.processed_message_ts[channel_id] = {}
        value = self.state.processed_message_ts[channel_id].get(thread_ts)
        # Backward compat: migrate old string format to list
        if isinstance(value, str):
            processed = [value]
        elif isinstance(value, list):
            processed = value
        else:
            processed = []

        if message_ts not in processed:
            processed.append(message_ts)
            # Trim to keep only the most recent entries
            if len(processed) > self._DEDUP_SET_MAX:
                processed = processed[-self._DEDUP_SET_MAX :]
        self.state.processed_message_ts[channel_id][thread_ts] = processed

    def try_add_to_processed_set(self, channel_id: str, thread_ts: str, message_ts: str) -> bool:
        """Atomically add a message ID to the processed set."""
        self._ensure_service()
        if not self._service.try_record_processed_message(channel_id, thread_ts, message_ts):
            self.maybe_reload()
            return False
        self._remember_processed_message(channel_id, thread_ts, message_ts)
        return True

    def try_record_runtime_event(
        self,
        record_type: str,
        record_key: str,
        payload: Dict[str, Any] | None = None,
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        """Atomically claim a short-lived runtime event."""
        self._ensure_service()
        return self._service.try_record_runtime_event(
            record_type,
            record_key,
            payload,
            ttl_seconds=ttl_seconds,
        )

    def add_to_processed_set(self, channel_id: str, thread_ts: str, message_ts: str) -> None:
        """Add a message ID to the processed set (bounded)."""
        self._ensure_service()
        self._service.upsert_processed_message(channel_id, thread_ts, message_ts)
        self._remember_processed_message(channel_id, thread_ts, message_ts)

    def add_active_poll(self, poll_info: ActivePollInfo) -> None:
        """Add an active poll to track."""
        self._ensure_service()
        self._service.upsert_active_poll(poll_info)
        self.state.active_polls[poll_info.opencode_session_id] = poll_info.to_dict()

    def remove_active_poll(self, opencode_session_id: str) -> bool:
        """Remove an active poll."""
        self._ensure_service()
        removed = self._service.delete_active_poll(opencode_session_id)
        if opencode_session_id in self.state.active_polls:
            del self.state.active_polls[opencode_session_id]
            removed = True
        return removed

    def get_active_poll(self, opencode_session_id: str) -> Optional[ActivePollInfo]:
        """Get active poll info by session ID."""
        self.maybe_reload()
        data = self.state.active_polls.get(opencode_session_id)
        if data:
            return ActivePollInfo.from_dict(data)
        return None

    def get_all_active_polls(self) -> Dict[str, ActivePollInfo]:
        """Get all active polls."""
        self.maybe_reload()
        return {sid: ActivePollInfo.from_dict(data) for sid, data in self.state.active_polls.items()}

    def update_active_poll(self, poll_info: ActivePollInfo) -> None:
        """Update an existing active poll."""
        self._ensure_service()
        self._service.upsert_active_poll(poll_info)
        self.state.active_polls[poll_info.opencode_session_id] = poll_info.to_dict()

    def save(self) -> None:
        self._ensure_service()
        self._service.save_state(self.state)
        self._service.has_external_write()
