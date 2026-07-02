"""Facade for runtime session/thread/dedup/poll state.

This facade centralizes runtime conversation state operations that are
backed by ``config.v2_sessions.SessionsStore``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Union

from config.v2_sessions import ActivePollInfo, SessionsStore

logger = logging.getLogger(__name__)


class SessionsFacade:
    """High-level APIs for session and runtime state operations."""

    def __init__(self, sessions_store: SessionsStore):
        self.sessions_store = sessions_store

    def _normalize_user_id(self, user_id: Union[int, str]) -> str:
        return str(user_id)

    def _ensure_agent_namespace(self, user_id: Union[int, str], agent_name: str) -> Dict[str, str]:
        user_key = self._normalize_user_id(user_id)
        return self.sessions_store.get_agent_map(user_key, agent_name)

    def set_agent_session_mapping(
        self,
        user_id: Union[int, str],
        agent_name: str,
        thread_id: str,
        session_id: str,
        *,
        vibe_agent_id: str | None = None,
        vibe_agent_name: str | None = None,
    ) -> None:
        self.sessions_store.bind_agent_session(
            self._normalize_user_id(user_id),
            agent_name,
            thread_id,
            session_id,
            vibe_agent_id=vibe_agent_id,
            vibe_agent_name=vibe_agent_name,
        )
        logger.info("Stored %s session mapping for %s: %s -> %s", agent_name, user_id, thread_id, session_id)

    def get_agent_session_id(
        self,
        user_id: Union[int, str],
        thread_id: str,
        agent_name: str,
    ) -> Optional[str]:
        user_key = self._normalize_user_id(user_id)
        agent_map = self.sessions_store.get_agent_map(user_key, agent_name)
        return agent_map.get(thread_id)

    def get_agent_session_row_id(
        self,
        user_id: Union[int, str],
        thread_id: str,
        agent_name: str,
    ) -> Optional[str]:
        user_key = self._normalize_user_id(user_id)
        getter = getattr(self.sessions_store, "get_agent_session_row_id", None)
        if not callable(getter):
            return None
        return getter(user_key, agent_name, thread_id)

    def find_session_for_anchor(self, user_id: Union[int, str], session_anchor: str) -> Optional[dict]:
        """Latest session row for ``(scope, anchor)`` regardless of backend, or
        ``None``. Lets a turn pin a thread to its OWN backend instead of the
        scope's current routing. Read-only; tolerates stores without support."""
        user_key = self._normalize_user_id(user_id)
        finder = getattr(self.sessions_store, "find_session_for_anchor", None)
        if not callable(finder):
            return None
        return finder(user_key, session_anchor)

    def ensure_agent_session_id(
        self,
        user_id: Union[int, str],
        agent_name: str,
        thread_id: str,
        *,
        workdir: str | None = None,
        vibe_agent_id: str | None = None,
        vibe_agent_name: str | None = None,
    ) -> Optional[str]:
        user_key = self._normalize_user_id(user_id)
        ensure = getattr(self.sessions_store, "ensure_agent_session_id", None)
        if callable(ensure):
            return ensure(
                user_key,
                agent_name,
                thread_id,
                workdir=workdir,
                vibe_agent_id=vibe_agent_id,
                vibe_agent_name=vibe_agent_name,
            )
        return self.get_agent_session_row_id(user_key, thread_id, agent_name)

    def bind_agent_session(
        self,
        user_id: Union[int, str],
        agent_name: str,
        thread_id: str,
        session_id: Any,
        *,
        workdir: str | None = None,
        vibe_agent_id: str | None = None,
        vibe_agent_name: str | None = None,
    ) -> Optional[str]:
        user_key = self._normalize_user_id(user_id)
        return self.sessions_store.bind_agent_session(
            user_key,
            agent_name,
            thread_id,
            session_id,
            workdir=workdir,
            vibe_agent_id=vibe_agent_id,
            vibe_agent_name=vibe_agent_name,
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
        binder = getattr(self.sessions_store, "bind_agent_session_by_id", None)
        if not callable(binder):
            return None
        return binder(
            agent_session_id,
            native_session_id,
            workdir=workdir,
            vibe_agent_id=vibe_agent_id,
            vibe_agent_name=vibe_agent_name,
            vibe_agent_backend=vibe_agent_backend,
        )

    def clear_agent_session_mapping(
        self,
        user_id: Union[int, str],
        agent_name: str,
        thread_id: str,
    ) -> None:
        self.remove_agent_session(user_id, agent_name, thread_id)

    def remove_agent_session(
        self,
        user_id: Union[int, str],
        agent_name: str,
        thread_id: str,
    ) -> bool:
        user_key = self._normalize_user_id(user_id)
        removed = self.sessions_store.remove_agent_session(user_key, agent_name, thread_id)
        if removed:
            logger.info("Cleared %s session mapping for user %s: %s", agent_name, user_id, thread_id)
        return bool(removed)

    def clear_agent_sessions(self, user_id: Union[int, str], agent_name: str) -> None:
        user_key = self._normalize_user_id(user_id)
        cleared = self.sessions_store.clear_agent_sessions(user_key, agent_name)
        if cleared:
            logger.info("Cleared all %s session namespaces for user %s", agent_name, user_id)

    def clear_all_session_mappings(self, user_id: Union[int, str]) -> None:
        user_key = self._normalize_user_id(user_id)
        count = self.sessions_store.clear_agent_sessions(user_key)
        if count:
            logger.info("Cleared all session mappings (%s bases) for user %s", count, user_id)

    def list_agent_sessions(self, user_id: Union[int, str], agent_name: str) -> Dict[str, str]:
        user_key = self._normalize_user_id(user_id)
        agent_map = self.sessions_store.get_agent_map(user_key, agent_name)
        return dict(agent_map)

    def list_all_agent_sessions(self, user_id: Union[int, str]) -> Dict[str, Dict[str, str]]:
        user_key = self._normalize_user_id(user_id)
        self.sessions_store._ensure_user_namespace(user_key)
        agent_maps = self.sessions_store.state.session_mappings.get(user_key, {})
        return {agent: dict(mapping) for agent, mapping in agent_maps.items()}

    @staticmethod
    def _matches_base_prefix(mapping_key: str, base_session_id: str) -> bool:
        return mapping_key == base_session_id or mapping_key.startswith(f"{base_session_id}:")

    def has_any_agent_session_base(self, user_id: Union[int, str], base_session_id: str) -> bool:
        user_key = self._normalize_user_id(user_id)
        self.sessions_store._ensure_user_namespace(user_key)
        agent_maps = self.sessions_store.state.session_mappings.get(user_key, {})
        for agent_map in agent_maps.values():
            for mapping_key in agent_map.keys():
                if self._matches_base_prefix(mapping_key, base_session_id):
                    return True
        return False

    def alias_session_base(
        self,
        user_id: Union[int, str],
        source_base_session_id: str,
        alias_base_session_id: str,
    ) -> bool:
        user_key = self._normalize_user_id(user_id)
        self.sessions_store._ensure_user_namespace(user_key)
        agent_maps = self.sessions_store.state.session_mappings.get(user_key, {})
        changed = False

        for agent_name, agent_map in agent_maps.items():
            additions: Dict[str, str] = {}
            for mapping_key, native_session_id in list(agent_map.items()):
                if not self._matches_base_prefix(mapping_key, source_base_session_id):
                    continue
                suffix = mapping_key[len(source_base_session_id) :]
                alias_key = f"{alias_base_session_id}{suffix}"
                if alias_key in agent_map or alias_key in additions:
                    continue
                additions[alias_key] = native_session_id
            if additions:
                agent_map.update(additions)
                changed = True
                logger.info(
                    "Aliased %s session base for %s: %s -> %s (%s keys)",
                    agent_name,
                    user_key,
                    source_base_session_id,
                    alias_base_session_id,
                    len(additions),
                )

        if changed:
            for agent_name, agent_map in agent_maps.items():
                for mapping_key, native_session_id in agent_map.items():
                    if self._matches_base_prefix(mapping_key, alias_base_session_id):
                        self.sessions_store.bind_agent_session(user_key, agent_name, mapping_key, native_session_id)
        return changed

    def alias_session_base_across_scopes(
        self,
        source_user_id: Union[int, str],
        target_user_id: Union[int, str],
        source_base_session_id: str,
        alias_base_session_id: str,
    ) -> bool:
        source_key = self._normalize_user_id(source_user_id)
        target_key = self._normalize_user_id(target_user_id)
        self.sessions_store._ensure_user_namespace(source_key)
        self.sessions_store._ensure_user_namespace(target_key)

        source_agent_maps = self.sessions_store.state.session_mappings.get(source_key, {})
        aliases_to_bind: List[tuple[str, str, Any]] = []
        changed = False

        for agent_name, source_agent_map in source_agent_maps.items():
            target_agent_map = self.sessions_store.get_agent_map(target_key, agent_name)
            additions: Dict[str, str] = {}
            for mapping_key, native_session_id in list(source_agent_map.items()):
                if not self._matches_base_prefix(mapping_key, source_base_session_id):
                    continue
                suffix = mapping_key[len(source_base_session_id) :]
                alias_key = f"{alias_base_session_id}{suffix}"
                if alias_key in target_agent_map or alias_key in additions:
                    continue
                additions[alias_key] = native_session_id
            if additions:
                target_agent_map.update(additions)
                aliases_to_bind.extend(
                    (agent_name, alias_key, native_session_id)
                    for alias_key, native_session_id in additions.items()
                )
                changed = True
                logger.info(
                    "Aliased %s session base across scopes: %s/%s -> %s/%s (%s keys)",
                    agent_name,
                    source_key,
                    source_base_session_id,
                    target_key,
                    alias_base_session_id,
                    len(additions),
                )

        if changed:
            for agent_name, mapping_key, native_session_id in aliases_to_bind:
                self.sessions_store.bind_agent_session(target_key, agent_name, mapping_key, native_session_id)
        return changed

    def clear_session_base(self, user_id: Union[int, str], base_session_id: str) -> int:
        user_key = self._normalize_user_id(user_id)
        cleared = self.sessions_store.clear_session_base(user_key, base_session_id)
        if cleared:
            logger.info("Cleared session base for %s: %s (%s keys)", user_key, base_session_id, cleared)
        return cleared

    def get_all_session_mappings(self) -> Dict[str, Dict[str, Dict[str, str]]]:
        """Return all persisted session mappings grouped by user and agent."""
        mappings = self.sessions_store.state.session_mappings
        return {
            user_id: {agent: dict(agent_map) for agent, agent_map in (agents or {}).items()}
            for user_id, agents in mappings.items()
        }

    def set_session_mapping(self, user_id: Union[int, str], thread_id: str, claude_session_id: str) -> None:
        self.set_agent_session_mapping(user_id, "claude", thread_id, claude_session_id)

    def get_claude_session_id(self, user_id: Union[int, str], thread_id: str) -> Optional[str]:
        return self.get_agent_session_id(user_id, thread_id, agent_name="claude")

    def clear_session_mapping(self, user_id: Union[int, str], thread_id: str) -> None:
        self.clear_agent_session_mapping(user_id, "claude", thread_id)

    def mark_thread_active(self, user_id: Union[int, str], channel_id: str, thread_ts: str) -> None:
        user_key = self._normalize_user_id(user_id)
        self.sessions_store.mark_thread_active(user_key, channel_id, thread_ts, time.time())
        logger.info("Marked thread active for user %s: channel=%s, thread=%s", user_id, channel_id, thread_ts)

    def is_thread_active(self, user_id: Union[int, str], channel_id: str, thread_ts: str) -> bool:
        user_key = self._normalize_user_id(user_id)
        self._cleanup_expired_threads_for_channel(user_id, channel_id)
        channel_map = self.sessions_store.get_thread_map(user_key, channel_id)
        if thread_ts in channel_map:
            return True
        return self._is_thread_active_for_any_user(channel_id, thread_ts)

    def is_thread_active_for_user(self, user_id: Union[int, str], channel_id: str, thread_ts: str) -> bool:
        user_key = self._normalize_user_id(user_id)
        self._cleanup_expired_threads_for_channel(user_id, channel_id)
        channel_map = self.sessions_store.get_thread_map(user_key, channel_id)
        return thread_ts in channel_map

    def _is_thread_active_for_any_user(self, channel_id: str, thread_ts: str) -> bool:
        """Return whether a channel thread is active for any participant.

        Thread activation gates whether replies can be routed to the agent; once
        the bot is invited into a thread, all participants should be able to
        continue the conversation without mentioning the bot again.
        """
        cutoff = time.time() - (24 * 60 * 60)
        changed = False

        for user_key, channels in list(self.sessions_store.state.active_slack_threads.items()):
            if not isinstance(channels, dict):
                continue
            channel_map = channels.get(channel_id)
            if not isinstance(channel_map, dict):
                continue

            last_active = channel_map.get(thread_ts)
            if last_active is None:
                continue
            if last_active < cutoff:
                self.sessions_store.remove_active_thread(user_key, channel_id, thread_ts)
                changed = True
                continue

            return True

        return False

    def _cleanup_expired_threads_for_channel(self, user_id: Union[int, str], channel_id: str) -> None:
        user_key = self._normalize_user_id(user_id)
        channel_map = self.sessions_store.get_thread_map(user_key, channel_id)
        if not channel_map:
            return

        current_time = time.time()
        twenty_four_hours_ago = current_time - (24 * 60 * 60)
        expired_threads = [
            thread_ts for thread_ts, last_active in channel_map.items() if last_active < twenty_four_hours_ago
        ]

        if not expired_threads:
            return

        for thread_ts in expired_threads:
            self.sessions_store.remove_active_thread(user_key, channel_id, thread_ts)
        logger.info("Cleaned up %s expired threads for channel %s", len(expired_threads), channel_id)

    def cleanup_all_expired_threads(self, user_id: Union[int, str]) -> None:
        user_key = self._normalize_user_id(user_id)
        channel_map = self.sessions_store.state.active_slack_threads.get(user_key, {})
        if not channel_map:
            return
        for channel_id in list(channel_map.keys()):
            self._cleanup_expired_threads_for_channel(user_id, channel_id)

    def is_message_already_processed(self, channel_id: str, thread_ts: str, message_ts: str) -> bool:
        return self.sessions_store.is_message_in_processed_set(channel_id, thread_ts, message_ts)

    def record_processed_message(self, channel_id: str, thread_ts: str, message_ts: str) -> None:
        self.sessions_store.add_to_processed_set(channel_id, thread_ts, message_ts)
        logger.debug("Recorded processed message: channel=%s, thread=%s, message=%s", channel_id, thread_ts, message_ts)

    def try_record_processed_message(self, channel_id: str, thread_ts: str, message_ts: str) -> bool:
        recorded = self.sessions_store.try_add_to_processed_set(channel_id, thread_ts, message_ts)
        if recorded:
            logger.debug(
                "Recorded processed message: channel=%s, thread=%s, message=%s",
                channel_id,
                thread_ts,
                message_ts,
            )
        return recorded

    def try_record_runtime_event(
        self,
        record_type: str,
        record_key: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        ttl_seconds: Optional[int] = None,
    ) -> bool:
        recorder = getattr(self.sessions_store, "try_record_runtime_event", None)
        if not callable(recorder):
            return True
        return bool(
            recorder(
                record_type,
                record_key,
                payload,
                ttl_seconds=ttl_seconds,
            )
        )

    def add_active_poll(
        self,
        opencode_session_id: str,
        base_session_id: str,
        channel_id: str,
        thread_id: str,
        settings_key: str,
        working_path: str,
        baseline_message_ids: List[str],
        ack_reaction_message_id: Optional[str] = None,
        ack_reaction_emoji: Optional[str] = None,
        typing_indicator_active: bool = False,
        context_token: str = "",
        processing_indicator: Optional[Dict[str, Any]] = None,
        user_id: str = "",
        platform: str = "",
        prompt_started_at: Optional[float] = None,
        model_dict: Optional[Dict[str, str]] = None,
        reasoning_effort: Optional[str] = None,
        session_key: str = "",
    ) -> None:
        poll_info = ActivePollInfo(
            opencode_session_id=opencode_session_id,
            base_session_id=base_session_id,
            channel_id=channel_id,
            thread_id=thread_id,
            settings_key=settings_key,
            working_path=working_path,
            baseline_message_ids=baseline_message_ids,
            seen_tool_calls=[],
            emitted_assistant_messages=[],
            started_at=time.time(),
            prompt_started_at=prompt_started_at,
            ack_reaction_message_id=ack_reaction_message_id,
            ack_reaction_emoji=ack_reaction_emoji,
            typing_indicator_active=typing_indicator_active,
            context_token=context_token,
            processing_indicator=processing_indicator or {},
            user_id=user_id,
            platform=platform,
            model_dict=model_dict,
            reasoning_effort=reasoning_effort,
            session_key=session_key,
        )
        self.sessions_store.add_active_poll(poll_info)
        logger.debug("Added active poll: session=%s, thread=%s", opencode_session_id, thread_id)

    def remove_active_poll(self, opencode_session_id: str) -> None:
        self.sessions_store.remove_active_poll(opencode_session_id)
        logger.debug("Removed active poll: session=%s", opencode_session_id)

    def update_active_poll_state(
        self,
        opencode_session_id: str,
        seen_tool_calls: Optional[List[str]] = None,
        emitted_assistant_messages: Optional[List[str]] = None,
    ) -> None:
        poll_info = self.sessions_store.get_active_poll(opencode_session_id)
        if poll_info:
            if seen_tool_calls is not None:
                poll_info.seen_tool_calls = seen_tool_calls
            if emitted_assistant_messages is not None:
                poll_info.emitted_assistant_messages = emitted_assistant_messages
            self.sessions_store.update_active_poll(poll_info)

    def get_all_active_polls(self) -> Dict[str, Any]:
        return self.sessions_store.get_all_active_polls()
