"""Shared handler foundation for controller-owned handlers."""

import logging

from modules.im import MessageContext
from vibe.i18n import t as i18n_t

logger = logging.getLogger(__name__)


class BaseHandler:
    """Provide shared controller references and common helper methods."""

    def __init__(self, controller):
        self.controller = controller
        self.config = controller.config
        self.im_client = controller.im_client
        self.settings_manager = controller.settings_manager
        self.sessions = (
            getattr(controller, "sessions", None)
            or getattr(controller.settings_manager, "sessions", None)
            or controller.settings_manager
        )
        self.formatter = getattr(controller.im_client, "formatter", None)

    def _get_settings_key(self, context: MessageContext) -> str:
        return self.controller._get_settings_key(context)

    def _get_session_key(self, context: MessageContext) -> str:
        return self.controller._get_session_key(context)

    def _get_im_client(self, context: MessageContext):
        getter = getattr(self.controller, "get_im_client_for_context", None)
        if callable(getter):
            try:
                return getter(context)
            except AttributeError:
                pass
        return self.im_client

    def _get_settings_manager(self, context: MessageContext):
        getter = getattr(self.controller, "get_settings_manager_for_context", None)
        if callable(getter):
            try:
                return getter(context)
            except AttributeError:
                pass
        return self.settings_manager

    def _get_formatter(self, context: MessageContext):
        return getattr(self._get_im_client(context), "formatter", self.formatter)

    def _get_lang(self) -> str:
        if hasattr(self.controller, "_get_lang"):
            return self.controller._get_lang()
        return getattr(self.config, "language", "en")

    def _t(self, key: str, **kwargs) -> str:
        return i18n_t(key, self._get_lang(), **kwargs)

    def _context_can_start_fresh_session_without_reset(self, context: MessageContext) -> bool:
        im_client = self._get_im_client(context)
        is_dm = bool((context.platform_specific or {}).get("is_dm", False))
        if is_dm:
            return bool(getattr(im_client, "should_use_thread_for_dm_session", lambda: False)())
        uses_threads = bool(getattr(im_client, "should_use_thread_for_reply", lambda: False)())
        uses_message_sessions = bool(
            getattr(im_client, "should_use_message_id_for_channel_session", lambda _context=None: True)(context)
        )
        return uses_threads and uses_message_sessions

    def _session_anchor_for_context(self, context: MessageContext, *, log_context: str = "session hint") -> str:
        session_handler = getattr(self.controller, "session_handler", None)
        getter = getattr(session_handler, "get_base_session_id", None)
        if callable(getter):
            try:
                return getter(context)
            except Exception:
                logger.debug("Failed to resolve session anchor for %s", log_context, exc_info=True)
        platform = context.platform or (context.platform_specific or {}).get("platform") or self.config.platform
        payload = context.platform_specific or {}
        base_id = (context.channel_id or context.user_id) if payload.get("is_dm", False) else context.channel_id
        return f"{platform}_{base_id or context.user_id}"

    def _current_flat_scope_session_row_for_hint(self, context: MessageContext, *, log_context: str = "session hint"):
        if self._context_can_start_fresh_session_without_reset(context):
            return None
        finder = getattr(self.sessions, "find_session_for_anchor", None)
        if not callable(finder):
            return None
        session_key = self._get_session_key(context)
        session_anchor = self._session_anchor_for_context(context, log_context=log_context)
        try:
            return finder(session_key, session_anchor)
        except Exception:
            logger.debug("Failed to inspect current session for %s", log_context, exc_info=True)
            return None

    def _flat_scope_needs_new_session_hint(self, context: MessageContext, *, log_context: str = "session hint") -> bool:
        return bool(self._current_flat_scope_session_row_for_hint(context, log_context=log_context))

    @staticmethod
    def _resolve_user_display_name(user_info: dict, fallback: str) -> str:
        return (
            user_info.get("display_name")
            or user_info.get("display_name_normalized")
            or user_info.get("real_name")
            or user_info.get("real_name_normalized")
            or user_info.get("name")
            or fallback
        )
