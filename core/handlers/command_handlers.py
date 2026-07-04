"""Command handlers for bot commands like /start, /new, /cwd, etc."""

import logging
import os
import time
from typing import Any, Optional
from config.platform_registry import get_platform_descriptor
from core.bind_security import BindAttemptLimiter
from core.message_context import requires_typed_user_session_key
from modules.agents import get_agent_display_name
from modules.agents.native_sessions.types import NativeResumeSession
from modules.agents.base import AgentRequest
from modules.im import MessageContext, InlineKeyboard, InlineButton

from .base import BaseHandler

logger = logging.getLogger(__name__)


class CommandHandlers(BaseHandler):
    """Handles all bot command operations"""

    def __init__(self, controller):
        """Initialize with reference to main controller"""
        super().__init__(controller)
        self.session_manager = controller.session_manager
        self._resume_snapshots: dict[str, dict[str, Any]] = {}
        self._resume_snapshot_ttl_seconds = 600.0
        self._wechat_resume_page_size = 5
        self._bind_attempt_limiter = BindAttemptLimiter()

    def _get_channel_context(self, context: MessageContext) -> MessageContext:
        """Get context for channel messages (no thread)"""
        # Send command responses directly to channel, not in thread/topic
        platform = context.platform or (context.platform_specific or {}).get("platform") or self.config.platform
        if get_platform_descriptor(platform).capabilities.supports_threads:
            return MessageContext(
                user_id=context.user_id,
                channel_id=context.channel_id,
                platform=context.platform,
                thread_id=None,  # No thread for command responses
                platform_specific=context.platform_specific,
            )
        # For other platforms, keep original context
        return context

    def _build_non_interactive_start_message(
        self,
        *,
        platform_name: str,
        agent_display_name: str,
        user_name: str,
        show_channel: bool,
        channel_name: str,
        supports_threads: bool = False,
    ) -> str:
        lines = [
            f"{self._t('command.start.welcome')}",
            "",
            self._t("command.start.greeting", name=user_name),
            self._t("command.start.platform", platform=platform_name),
            self._t("command.start.agent", agent=agent_display_name),
        ]
        if show_channel:
            lines.append(self._t("command.start.channel", channel=channel_name))

        commands = [
            "",
            self._t("command.start.commandsTitle"),
            self._t("command.start.commandStart"),
            self._t("command.start.commandCwd"),
            self._t("command.start.commandSetCwd"),
            self._t("command.start.commandResume"),
            self._t("command.start.commandSetup"),
            self._t("command.start.commandStop", agent=agent_display_name),
        ]
        if not supports_threads:
            commands.append(self._t("command.start.commandNew"))
        lines.extend(commands)
        return "\n".join(line for line in lines if line)

    def _build_bind_success_message(self, *, name: str, is_admin: bool, platform: str) -> str:
        key = "bind.successAdmin" if is_admin else "bind.success"
        message = self._t(key, name=name)
        try:
            supports_buttons = get_platform_descriptor(platform).capabilities.supports_buttons
        except ValueError:
            supports_buttons = False

        hint_key = "bind.menuHintButtons" if supports_buttons else "bind.menuHintStart"
        return f"{message}\n\n{self._t(hint_key)}"

    def _normalize_resume_agent(self, value: str) -> Optional[str]:
        normalized = (value or "").strip().lower()
        aliases = {
            "oc": "opencode",
            "opencode": "opencode",
            "open-code": "opencode",
            "cc": "claude",
            "claude": "claude",
            "claudecode": "claude",
            "claude-code": "claude",
            "cx": "codex",
            "codex": "codex",
        }
        return aliases.get(normalized)

    def _get_resume_snapshot_key(self, context: MessageContext) -> str:
        platform = context.platform or (context.platform_specific or {}).get("platform") or self.config.platform
        return f"{platform}::{self._get_settings_key(context)}"

    def _session_anchor_for_new(self, context: MessageContext) -> str:
        session_handler = getattr(self.controller, "session_handler", None)
        getter = getattr(session_handler, "get_base_session_id", None)
        if callable(getter):
            try:
                return getter(context)
            except Exception:
                logger.debug("Failed to resolve session anchor for /new", exc_info=True)
        platform = context.platform or (context.platform_specific or {}).get("platform") or self.config.platform
        payload = context.platform_specific or {}
        if payload.get("is_dm", False):
            base_id = context.thread_id or context.channel_id or context.user_id
        else:
            base_id = context.thread_id or context.message_id or context.channel_id or context.user_id
        return f"{platform}_{base_id}"

    def _compat_session_keys_for_new(self, context: MessageContext, session_key: str) -> list[str]:
        keys = [session_key]
        platform = context.platform or (context.platform_specific or {}).get("platform") or self.config.platform
        payload = context.platform_specific or {}
        if payload.get("is_dm", False) and requires_typed_user_session_key(context) and context.channel_id:
            keys.append(f"{platform}::channel::{context.channel_id}")
            keys.append(f"{platform}::{context.channel_id}")
        return list(dict.fromkeys(keys))

    def _store_resume_snapshot(
        self,
        context: MessageContext,
        *,
        items: list[NativeResumeSession],
        page: int,
        total: int,
        working_path: str,
    ) -> None:
        self._resume_snapshots[self._get_resume_snapshot_key(context)] = {
            "stored_at": time.time(),
            "items": list(items),
            "page": page,
            "total": total,
            "working_path": working_path,
        }

    def _get_resume_snapshot(self, context: MessageContext) -> Optional[dict[str, Any]]:
        snapshot = self._resume_snapshots.get(self._get_resume_snapshot_key(context))
        if not snapshot:
            return None
        if time.time() - float(snapshot.get("stored_at", 0.0)) > self._resume_snapshot_ttl_seconds:
            self._resume_snapshots.pop(self._get_resume_snapshot_key(context), None)
            return None
        if snapshot.get("working_path") != self.controller.get_cwd(context):
            self._resume_snapshots.pop(self._get_resume_snapshot_key(context), None)
            return None
        return snapshot

    def _list_recent_native_sessions(
        self,
        context: MessageContext,
        *,
        limit: int = 100,
    ) -> tuple[str, list[NativeResumeSession]]:
        working_path = self.controller.get_cwd(context)
        service_getter = getattr(self.controller, "get_native_session_service", None)
        if callable(service_getter):
            native_session_service = service_getter()
        else:
            native_session_service = getattr(self.controller, "native_session_service", None)
        if native_session_service is None:
            return working_path, []
        sessions = native_session_service.list_recent_sessions(working_path, limit=limit)
        agent_service = getattr(self.controller, "agent_service", None)
        registered_agents = getattr(agent_service, "agents", None)
        if isinstance(registered_agents, dict) and registered_agents:
            allowed_agents = set(registered_agents.keys())
            sessions = [item for item in sessions if item.agent in allowed_agents]
        return working_path, sessions

    @staticmethod
    def _format_resume_time(item: NativeResumeSession) -> str:
        dt = item.updated_at or item.created_at
        if dt is None:
            return "--"
        return dt.strftime("%m-%d %H:%M")

    @staticmethod
    def _format_resume_tail(item: NativeResumeSession) -> str:
        preview = (item.last_agent_tail or item.last_agent_message or "").strip()
        if preview:
            return preview
        suffix = item.native_session_id[-15:] if len(item.native_session_id) > 15 else item.native_session_id
        return f"...{suffix}"

    async def _send_wechat_resume_usage(self, context: MessageContext, *, include_snapshot_expired: bool = False) -> None:
        channel_context = self._get_channel_context(context)
        lines = []
        if include_snapshot_expired:
            lines.append(f"⏮️ {self._t('command.resume.snapshotExpired')}")
            lines.append("")
        lines.extend(
            [
                self._t("command.resume.usageTitle"),
                self._t("command.resume.usageList"),
                self._t("command.resume.usagePick"),
                self._t("command.resume.usageMore"),
                self._t("command.resume.usageLatest"),
                self._t("command.resume.usageManual"),
            ]
        )
        await self._get_im_client(channel_context).send_message(channel_context, "\n".join(lines))

    async def _send_wechat_resume_page(self, context: MessageContext, *, page: int) -> None:
        page_size = self._wechat_resume_page_size
        working_path, sessions = self._list_recent_native_sessions(context, limit=100)
        channel_context = self._get_channel_context(context)
        if not sessions:
            await self._get_im_client(channel_context).send_message(
                channel_context,
                "\n".join(
                    [
                        f"⏮️ {self._t('command.resume.noStoredSessions')}",
                        "",
                        self._t("command.resume.usageManual"),
                    ]
                ),
            )
            return

        total = len(sessions)
        page_count = max(1, (total + page_size - 1) // page_size)
        if page < 1:
            page = 1
        if page > page_count:
            await self._get_im_client(channel_context).send_message(
                channel_context,
                f"⏮️ {self._t('command.resume.noMorePages')}",
            )
            return

        start = (page - 1) * page_size
        page_items = sessions[start : start + page_size]
        self._store_resume_snapshot(context, items=page_items, page=page, total=total, working_path=working_path)

        lines = [
            f"⏮️ {self._t('command.resume.listTitle')}",
            self._t("command.resume.listPage", page=page, total=page_count),
            "",
        ]
        for idx, item in enumerate(page_items, start=1):
            lines.append(
                self._t(
                    "command.resume.listItemTitle",
                    index=idx,
                    prefix=item.agent_prefix,
                    preview=self._format_resume_tail(item),
                )
            )
            lines.append(self._t("command.resume.listItemTime", time=self._format_resume_time(item)))
            lines.append("")

        if lines and not lines[-1]:
            lines.pop()

        lines.extend(
            [
                "",
                self._t("command.resume.usagePick"),
                self._t("command.resume.usageMore"),
                self._t("command.resume.usageLatest"),
                self._t("command.resume.usageManual"),
            ]
        )

        await self._get_im_client(channel_context).send_message(channel_context, "\n".join(lines))

    async def _handle_wechat_resume_selection(self, context: MessageContext, *, selection: int) -> None:
        snapshot = self._get_resume_snapshot(context)
        if not snapshot:
            await self._send_wechat_resume_usage(context, include_snapshot_expired=True)
            return

        items = snapshot.get("items") or []
        if not isinstance(items, list) or selection < 1 or selection > len(items):
            channel_context = self._get_channel_context(context)
            await self._get_im_client(channel_context).send_message(
                channel_context,
                f"⏮️ {self._t('command.resume.invalidSelection')}",
            )
            return

        item = items[selection - 1]
        if not isinstance(item, NativeResumeSession):
            await self._send_wechat_resume_usage(context, include_snapshot_expired=True)
            return

        await self.controller.session_handler.handle_resume_session_submission(
            user_id=context.user_id,
            channel_id=context.channel_id,
            thread_id=context.thread_id,
            agent=item.agent,
            session_id=item.native_session_id,
            host_message_ts=context.message_id,
            is_dm=bool((context.platform_specific or {}).get("is_dm", False)),
            platform=context.platform or (context.platform_specific or {}).get("platform") or self.config.platform,
        )

    async def _handle_wechat_resume_latest(self, context: MessageContext, *, agent: Optional[str] = None) -> None:
        _, sessions = self._list_recent_native_sessions(context, limit=100)
        if agent:
            sessions = [item for item in sessions if item.agent == agent]
        if not sessions:
            channel_context = self._get_channel_context(context)
            key = "command.resume.noStoredSessionsForBackend" if agent else "command.resume.noStoredSessions"
            kwargs = {"backend": agent} if agent else {}
            await self._get_im_client(channel_context).send_message(
                channel_context,
                f"⏮️ {self._t(key, **kwargs)}",
            )
            return

        item = sessions[0]
        await self.controller.session_handler.handle_resume_session_submission(
            user_id=context.user_id,
            channel_id=context.channel_id,
            thread_id=context.thread_id,
            agent=item.agent,
            session_id=item.native_session_id,
            host_message_ts=context.message_id,
            is_dm=bool((context.platform_specific or {}).get("is_dm", False)),
            platform=context.platform or (context.platform_specific or {}).get("platform") or self.config.platform,
        )

    async def _handle_wechat_resume(self, context: MessageContext, args: str) -> None:
        stripped_args = args.strip()
        if not stripped_args:
            await self._send_wechat_resume_page(context, page=1)
            return

        parts = stripped_args.split()
        head = parts[0].lower()

        if head.isdigit():
            await self._handle_wechat_resume_selection(context, selection=int(head))
            return

        if head == "more":
            snapshot = self._get_resume_snapshot(context)
            if not snapshot:
                await self._send_wechat_resume_usage(context, include_snapshot_expired=True)
                return
            page = int(snapshot.get("page", 1)) + 1
            await self._send_wechat_resume_page(context, page=page)
            return

        if head == "latest":
            agent = self._normalize_resume_agent(parts[1]) if len(parts) > 1 else None
            if len(parts) > 1 and not agent:
                channel_context = self._get_channel_context(context)
                await self._get_im_client(channel_context).send_message(
                    channel_context,
                    f"⏮️ {self._t('command.resume.unknownBackend')}",
                )
                return
            await self._handle_wechat_resume_latest(context, agent=agent)
            return

        agent = self._normalize_resume_agent(head)
        if agent:
            session_id = stripped_args[len(parts[0]) :].strip()
            if not session_id:
                await self._send_wechat_resume_usage(context)
                return
            await self.controller.session_handler.handle_resume_session_submission(
                user_id=context.user_id,
                channel_id=context.channel_id,
                thread_id=context.thread_id,
                agent=agent,
                session_id=session_id,
                host_message_ts=context.message_id,
                is_dm=bool((context.platform_specific or {}).get("is_dm", False)),
                platform=context.platform or (context.platform_specific or {}).get("platform") or self.config.platform,
            )
            return

        channel_context = self._get_channel_context(context)
        await self._get_im_client(channel_context).send_message(
            channel_context,
            f"⏮️ {self._t('command.resume.unknownCommand')}",
        )
        await self._send_wechat_resume_usage(context)

    async def _send_resume_menu_prompt(self, context: MessageContext) -> None:
        channel_context = self._get_channel_context(context)
        await self._get_im_client(channel_context).send_message(
            channel_context,
            f"⏮️ {self._t('command.resume.clickButton')}",
        )
        await self.handle_start(channel_context)

    async def handle_start(self, context: MessageContext, args: str = ""):
        """Handle /start command with interactive buttons"""
        im_client = self._get_im_client(context)
        platform = context.platform or (context.platform_specific or {}).get("platform") or self.config.platform
        platform_capabilities = get_platform_descriptor(platform).capabilities
        platform_name = str(platform).capitalize()

        is_dm = bool((context.platform_specific or {}).get("is_dm", False))

        # Get user and channel info
        try:
            user_info = await im_client.get_user_info(context.user_id)
        except Exception as e:
            logger.warning(f"Failed to get user info: {e}")
            user_info = {"id": context.user_id}

        channel_info = None
        if platform == "slack" and is_dm:
            channel_info = {
                "id": context.channel_id,
                "name": self._t("command.start.directMessage"),
            }
        else:
            try:
                channel_info = await im_client.get_channel_info(context.channel_id)
            except Exception as e:
                logger.warning(f"Failed to get channel info: {e}")
                channel_info = {
                    "id": context.channel_id,
                    "name": (
                        self._t("command.start.directMessage")
                        if context.channel_id.startswith("D")
                        else context.channel_id
                    ),
                }

        agent_name = self.controller.resolve_agent_for_context(context)
        default_agent = getattr(self.controller.agent_service, "default_agent", None)
        agent_display_name = get_agent_display_name(agent_name, fallback=default_agent or "Unknown")

        # Determine whether this conversation supports threads.
        # If it does, each new thread is already a fresh session, so the
        # "New Session" button/command is unnecessary.
        supports_threads = (
            getattr(im_client, "should_use_thread_for_dm_session", lambda: False)()
            if is_dm
            else (
                getattr(im_client, "should_use_thread_for_reply", lambda: False)()
                and getattr(im_client, "should_use_message_id_for_channel_session", lambda _context=None: True)(context)
            )
        )

        # For non-interactive platforms, use traditional text message
        if not platform_capabilities.supports_buttons:
            user_name = self._resolve_user_display_name(user_info, self._t("command.start.userFallback"))
            message_text = self._build_non_interactive_start_message(
                platform_name=platform_name,
                agent_display_name=agent_display_name,
                user_name=user_name,
                show_channel=platform_capabilities.supports_channels,
                channel_name=channel_info.get("name", "Unknown"),
                supports_threads=supports_threads,
            )
            channel_context = self._get_channel_context(context)
            await im_client.send_message(channel_context, message_text)
            return

        # For Slack/Discord/Telegram, create interactive buttons
        user_name = self._resolve_user_display_name(user_info, "User")

        # Create interactive buttons for commands
        session_row = []
        if not supports_threads:
            session_row.append(InlineButton(text=f"🆕 {self._t('button.newSession')}", callback_data="cmd_new"))
        session_row.append(InlineButton(text=f"⚙️ {self._t('button.settings')}", callback_data="cmd_settings"))

        buttons = [
            # Row 1: Directory management
            [
                InlineButton(text=f"📁 {self._t('button.currentDir')}", callback_data="cmd_cwd"),
                InlineButton(text=f"📂 {self._t('button.changeDir')}", callback_data="cmd_change_cwd"),
            ],
            # Row 2: Session and/or Settings
            session_row,
            # Row 3: Resume + Agent/Model switching
            [
                InlineButton(text=f"⏮️ {self._t('button.resumeSession')}", callback_data="cmd_resume"),
                InlineButton(text=f"🤖 {self._t('button.agentSettings')}", callback_data="cmd_routing"),
            ],
            # Row 4: Features
            [InlineButton(text=f"✨ {self._t('button.howItWorks')}", callback_data="info_how_it_works")],
        ]

        keyboard = InlineKeyboard(buttons=buttons)

        welcome_text = f"""🎉 **{self._t("command.start.welcome")}**

👋 {self._t("command.start.greeting", name=user_name)}
🔧 {self._t("command.start.platform", platform=platform_name)}
🤖 {self._t("command.start.agent", agent=agent_display_name)}
📍 {self._t("command.start.channel", channel=channel_info.get("name", "Unknown"))}

**{self._t("command.start.quickActions")}**
{self._t("command.start.quickActionsDesc", agent=agent_display_name)}"""

        # Send command response to channel (not in thread)
        channel_context = self._get_channel_context(context)
        await im_client.send_message_with_buttons(channel_context, welcome_text, keyboard)

    async def handle_new(self, context: MessageContext, args: str = ""):
        """Handle /new command - reset active session state for a fresh start."""
        try:
            im_client = self._get_im_client(context)
            platform = context.platform or (context.platform_specific or {}).get("platform") or self.config.platform
            if platform == "telegram" and hasattr(im_client, "start_new_topic_session"):
                topic_context = await im_client.start_new_topic_session(context)
                if topic_context is not None:
                    await im_client.send_message(topic_context, f"🆕 {self._t('command.new.started')}")
                    logger.info("Started new Telegram topic session for user %s", context.user_id)
                    return
            session_key = self._get_session_key(context)
            session_anchor = self._session_anchor_for_new(context)
            sessions = getattr(self.controller, "sessions", None)
            clear_base = getattr(sessions, "clear_session_base", None)
            for key in self._compat_session_keys_for_new(context, session_key):
                await self.controller.agent_service.clear_sessions(key)
                if callable(clear_base):
                    try:
                        clear_base(key, session_anchor)
                    except Exception:
                        logger.debug("Failed to clear session base for %s:%s", key, session_anchor, exc_info=True)
            full_response = f"🆕 {self._t('command.new.started')}"

            channel_context = self._get_channel_context(context)
            await im_client.send_message(channel_context, full_response)
            logger.info("Started fresh session for user %s", context.user_id)

        except Exception as e:
            logger.error(f"Error starting new session: {e}", exc_info=True)
            try:
                channel_context = self._get_channel_context(context)
                await im_client.send_message(channel_context, f"❌ {self._t('error.clearSession', error=str(e))}")
            except Exception as send_error:
                logger.error(f"Failed to send error message: {send_error}", exc_info=True)

    async def handle_clear(self, context: MessageContext, args: str = ""):
        """Backward-compatible alias for older interactive callbacks."""

        await self.handle_new(context, args)

    async def handle_cwd(self, context: MessageContext, args: str = ""):
        """Handle cwd command - show current working directory"""
        try:
            im_client = self._get_im_client(context)
            # Get CWD based on context (channel/chat)
            absolute_path = self.controller.get_cwd(context)

            # Build response using formatter to avoid escaping issues
            formatter = self._get_formatter(context)

            # Format path properly with code block
            path_line = f"📁 {self._t('command.cwd.current')}\n{formatter.format_code_inline(absolute_path)}"

            # Build status lines
            status_lines = []
            if os.path.exists(absolute_path):
                status_lines.append(f"✅ {self._t('command.cwd.exists')}")
            else:
                status_lines.append(f"⚠️ {self._t('command.cwd.notExists')}")

            status_lines.append(f"💡 {self._t('command.cwd.hint')}")

            # Combine all parts
            response_text = path_line + "\n" + "\n".join(status_lines)

            channel_context = self._get_channel_context(context)
            await im_client.send_message(channel_context, response_text)
        except Exception as e:
            logger.error(f"Error getting cwd: {e}")
            channel_context = self._get_channel_context(context)
            await im_client.send_message(channel_context, f"❌ {self._t('error.cwdGetFailed', error=str(e))}")

    async def handle_set_cwd(self, context: MessageContext, args: str):
        """Handle set_cwd command - change working directory"""
        try:
            im_client = self._get_im_client(context)
            settings_manager = self._get_settings_manager(context)
            if not args:
                channel_context = self._get_channel_context(context)
                await im_client.send_message(channel_context, self._t("command.cwd.usage"))
                return

            new_path = args.strip()

            # Expand user path and get absolute path
            expanded_path = os.path.expanduser(new_path)
            absolute_path = os.path.abspath(expanded_path)

            # Check if directory exists
            if not os.path.exists(absolute_path):
                # Try to create it
                try:
                    os.makedirs(absolute_path, exist_ok=True)
                    logger.info(f"Created directory: {absolute_path}")
                except Exception as e:
                    channel_context = self._get_channel_context(context)
                    await im_client.send_message(
                        channel_context, f"❌ {self._t('error.cwdCreateFailed', error=str(e))}"
                    )
                    return

            if not os.path.isdir(absolute_path):
                formatter = self._get_formatter(context)
                error_text = f"❌ {self._t('error.cwdNotDirectory', path=formatter.format_code_inline(absolute_path))}"
                channel_context = self._get_channel_context(context)
                await im_client.send_message(channel_context, error_text)
                return

            # Save to user settings
            settings_key = self._get_settings_key(context)
            settings_manager.set_custom_cwd(settings_key, absolute_path)

            logger.info(f"User {context.user_id} changed cwd to: {absolute_path}")

            formatter = self._get_formatter(context)
            response_text = f"✅ {self._t('success.cwdChanged', path=formatter.format_code_inline(absolute_path))}"
            if self._flat_scope_needs_new_session_hint(context, log_context="cwd update hint"):
                response_text = f"{response_text}\n\n{self._t('success.routingUpdateNeedsNewSession')}"
            channel_context = self._get_channel_context(context)
            await im_client.send_message(channel_context, response_text)

        except Exception as e:
            logger.error(f"Error setting cwd: {e}")
            channel_context = self._get_channel_context(context)
            await im_client.send_message(channel_context, f"❌ {self._t('error.cwdSetFailed', error=str(e))}")

    async def handle_change_cwd_submission(
        self,
        user_id: str,
        new_cwd: str,
        channel_id: Optional[str] = None,
        is_dm: bool = False,
        platform: Optional[str] = None,
    ):
        """Handle working directory change submission from modal."""
        try:
            context = MessageContext(
                user_id=user_id,
                channel_id=channel_id if channel_id else user_id,
                platform=platform or self.config.platform,
                platform_specific={"is_dm": is_dm},
            )
            await self.handle_set_cwd(context, new_cwd.strip())
        except Exception as e:
            logger.error(f"Error changing working directory: {e}")
            context = MessageContext(
                user_id=user_id,
                channel_id=channel_id if channel_id else user_id,
                platform=platform or self.config.platform,
                platform_specific={"is_dm": is_dm},
            )
            await self._get_im_client(context).send_message(
                context,
                f"❌ {self._t('error.cwdSetFailed', error=str(e))}",
            )

    async def handle_change_cwd_modal(self, context: MessageContext):
        """Handle Change Work Dir button - open modal for Slack"""
        platform = context.platform or (context.platform_specific or {}).get("platform") or self.config.platform
        im_client = self._get_im_client(context)
        if platform == "discord":
            interaction = context.platform_specific.get("interaction") if context.platform_specific else None
            if interaction and hasattr(im_client, "open_change_cwd_modal"):
                try:
                    current_cwd = self.controller.get_cwd(context)
                    await im_client.run_on_client_loop(
                        im_client.open_change_cwd_modal(interaction, current_cwd, context.channel_id)
                    )
                    return
                except Exception as e:
                    logger.error(f"Error opening change CWD modal: {e}")
            channel_context = self._get_channel_context(context)
            await im_client.send_message(
                channel_context,
                f"📂 {self._t('command.cwd.changeInstructions')}",
            )
            return
        if platform == "lark":
            if hasattr(im_client, "open_change_cwd_modal"):
                try:
                    current_cwd = self.controller.get_cwd(context)
                    await im_client.run_on_client_loop(
                        im_client.open_change_cwd_modal(
                            trigger_id=context,
                            current_cwd=current_cwd,
                            channel_id=context.channel_id,
                        )
                    )
                    return
                except Exception as e:
                    logger.error(f"Error opening change CWD card for Lark: {e}")
            channel_context = self._get_channel_context(context)
            await im_client.send_message(
                channel_context,
                f"📂 {self._t('command.cwd.changeInstructions')}",
            )
            return
        if platform == "telegram":
            if hasattr(im_client, "open_change_cwd_modal"):
                try:
                    current_cwd = self.controller.get_cwd(context)
                    await im_client.run_on_client_loop(
                        im_client.open_change_cwd_modal(context, current_cwd, context.channel_id)
                    )
                    return
                except Exception as e:
                    logger.error(f"Error opening Telegram change CWD flow: {e}")
            channel_context = self._get_channel_context(context)
            await im_client.send_message(
                channel_context,
                f"📂 {self._t('command.cwd.changeInstructions')}",
            )
            return

        if platform not in {"slack"}:
            channel_context = self._get_channel_context(context)
            await im_client.send_message(
                channel_context,
                f"📂 {self._t('command.cwd.changeInstructions')}",
            )
            return

        # For Slack, open a modal dialog
        trigger_id = context.platform_specific.get("trigger_id") if context.platform_specific else None

        if trigger_id and hasattr(im_client, "open_change_cwd_modal"):
            try:
                # Get current CWD based on context
                current_cwd = self.controller.get_cwd(context)

                await im_client.run_on_client_loop(
                    im_client.open_change_cwd_modal(trigger_id, current_cwd, context.channel_id)
                )
            except Exception as e:
                logger.error(f"Error opening change CWD modal: {e}")
                channel_context = self._get_channel_context(context)
                await im_client.send_message(
                    channel_context,
                    f"❌ {self._t('error.cwdChangeFailed')}",
                )
        else:
            # No trigger_id, show instructions
            channel_context = self._get_channel_context(context)
            await im_client.send_message(
                channel_context,
                f"📂 {self._t('command.cwd.clickButton')}",
            )

    async def handle_resume(self, context: MessageContext, args: str = ""):
        """Open resume-session modal (Slack) or explain availability."""
        platform = context.platform or (context.platform_specific or {}).get("platform") or self.config.platform
        im_client = self._get_im_client(context)
        if platform == "wechat":
            await self._handle_wechat_resume(context, args)
            return

        # /resume is only allowed at the SCOPE level (channel/DM), never inside an
        # existing thread/topic: binding there would rebind that thread's session,
        # mutating an existing record. Reject in-thread invocations so resume always
        # opens a FRESH scope-level session (maintainer decision; preserves the
        # native_session_id write-once invariant). WeChat above has no threads.
        if context.thread_id and context.thread_id != context.message_id:
            channel_context = self._get_channel_context(context)
            await im_client.send_message(channel_context, self._t("command.resume.scopeOnly"))
            return

        if platform == "discord":
            interaction = context.platform_specific.get("interaction") if context.platform_specific else None
            if interaction and hasattr(im_client, "open_resume_session_modal"):
                try:
                    _, sessions = self._list_recent_native_sessions(context, limit=25)
                    await im_client.run_on_client_loop(
                        im_client.open_resume_session_modal(
                            trigger_id=interaction,
                            sessions=sessions,
                            channel_id=context.channel_id,
                            thread_id=context.thread_id or context.message_id or "",
                            host_message_ts=context.message_id,
                        )
                    )
                    return
                except Exception as e:
                    logger.error(f"Error opening resume modal: {e}")
            channel_context = self._get_channel_context(context)
            await self._send_resume_menu_prompt(context)
            return
        if platform == "telegram":
            if hasattr(im_client, "open_resume_session_modal"):
                try:
                    _, sessions = self._list_recent_native_sessions(context, limit=25)
                    await im_client.run_on_client_loop(
                        im_client.open_resume_session_modal(
                            trigger_id=context,
                            sessions=sessions,
                            channel_id=context.channel_id,
                            thread_id=context.thread_id or context.message_id or "",
                            host_message_ts=context.message_id,
                        )
                    )
                    return
                except Exception as e:
                    logger.error(f"Error opening Telegram resume flow: {e}")
            channel_context = self._get_channel_context(context)
            await im_client.send_message(
                channel_context,
                f"⏮️ {self._t('command.resume.clickButton')}",
            )
            return
        if platform == "lark":
            if hasattr(im_client, "open_resume_session_modal"):
                try:
                    _, sessions = self._list_recent_native_sessions(context, limit=100)
                    await im_client.run_on_client_loop(
                        im_client.open_resume_session_modal(
                            trigger_id=context,
                            sessions=sessions,
                            channel_id=context.channel_id,
                            thread_id=context.thread_id or context.message_id or "",
                            host_message_ts=context.message_id,
                        )
                    )
                    return
                except Exception as e:
                    logger.error(f"Error opening resume session card for Lark: {e}")
            channel_context = self._get_channel_context(context)
            await self._send_resume_menu_prompt(context)
            return

        if platform not in {"slack"}:
            channel_context = self._get_channel_context(context)
            await im_client.send_message(
                channel_context,
                f"⏮️ {self._t('command.resume.slackOnly')}",
            )
            return

        trigger_id = context.platform_specific.get("trigger_id") if context.platform_specific else None
        if not trigger_id:
            await self._send_resume_menu_prompt(context)
            return

        try:
            _, sessions = self._list_recent_native_sessions(context, limit=100)
            await im_client.run_on_client_loop(
                im_client.open_resume_session_modal(
                    trigger_id=trigger_id,
                    sessions=sessions,
                    channel_id=context.channel_id,
                    thread_id=context.thread_id or context.message_id or "",
                    host_message_ts=context.message_id,
                )
            )
        except Exception as e:
            logger.error(f"Error opening resume modal: {e}")
            channel_context = self._get_channel_context(context)
            await im_client.send_message(channel_context, f"❌ {self._t('error.resumeFailed')}")

    async def handle_setup(self, context: MessageContext, args: str = ""):
        """Start or continue backend OAuth setup via IM."""
        await self.controller.agent_auth_service.handle_setup_command(context, args)

    async def handle_bind(self, context: MessageContext, args: str = ""):
        """Handle bind command - bind a user to this avibe instance via bind code.

        Only allowed in DM context. In channels, instructs the user to DM the bot.
        """
        try:
            im_client = self._get_im_client(context)
            settings_manager = self._get_settings_manager(context)
            platform = context.platform or (context.platform_specific or {}).get("platform") or self.config.platform

            def _is_bound_user() -> bool:
                try:
                    return settings_manager.is_bound_user(context.user_id, platform=platform)
                except TypeError:
                    return settings_manager.is_bound_user(context.user_id)

            def _bind_user(display_name: str):
                try:
                    return settings_manager.bind_user_with_code(
                        context.user_id,
                        display_name,
                        code,
                        dm_chat_id=context.channel_id,
                        platform=platform,
                    )
                except TypeError:
                    return settings_manager.bind_user_with_code(
                        context.user_id,
                        display_name,
                        code,
                        dm_chat_id=context.channel_id,
                    )

            # Check if this is a DM context (settings_key == user_id means DM)
            settings_key = self._get_settings_key(context)
            if settings_key != context.user_id:
                # Not a DM — instruct user to use DM
                channel_context = self._get_channel_context(context)
                await im_client.send_message(channel_context, self._t("bind.dmOnly"))
                return

            code = args.strip()
            if not code:
                channel_context = self._get_channel_context(context)
                await im_client.send_message(channel_context, self._t("bind.usage"))
                return

            # Check if user is already bound
            if _is_bound_user():
                channel_context = self._get_channel_context(context)
                await im_client.send_message(channel_context, self._t("bind.alreadyBound"))
                return

            limit_decision = self._bind_attempt_limiter.check(
                platform=platform,
                user_id=context.user_id,
                channel_id=context.channel_id,
            )
            if not limit_decision.allowed:
                channel_context = self._get_channel_context(context)
                await im_client.send_message(
                    channel_context,
                    self._t("bind.rateLimited", seconds=limit_decision.retry_after_seconds),
                )
                return

            # Fetch user info for display name
            try:
                user_info = await im_client.get_user_info(context.user_id)
            except Exception as e:
                logger.warning(f"Failed to get user info during bind: {e}")
                user_info = {"id": context.user_id}

            display_name = self._resolve_user_display_name(user_info, context.user_id)

            # Atomic bind: validate code + create user + consume code in one operation
            success, is_admin = _bind_user(display_name)

            if not success:
                # Could be already bound (race) or invalid code
                if _is_bound_user():
                    self._bind_attempt_limiter.reset(
                        platform=platform,
                        user_id=context.user_id,
                        channel_id=context.channel_id,
                    )
                    channel_context = self._get_channel_context(context)
                    await im_client.send_message(channel_context, self._t("bind.alreadyBound"))
                else:
                    failure_decision = self._bind_attempt_limiter.record_failure(
                        platform=platform,
                        user_id=context.user_id,
                        channel_id=context.channel_id,
                    )
                    channel_context = self._get_channel_context(context)
                    if failure_decision.allowed:
                        await im_client.send_message(channel_context, self._t("bind.invalidCode"))
                    else:
                        await im_client.send_message(
                            channel_context,
                            self._t("bind.rateLimited", seconds=failure_decision.retry_after_seconds),
                        )
                return

            self._bind_attempt_limiter.reset(
                platform=platform,
                user_id=context.user_id,
                channel_id=context.channel_id,
            )
            msg = self._build_bind_success_message(
                name=display_name,
                is_admin=is_admin,
                platform=platform,
            )

            channel_context = self._get_channel_context(context)
            await im_client.send_message(channel_context, msg)
            logger.info(f"User {context.user_id} ({display_name}) bound successfully (admin={is_admin})")

        except Exception as e:
            logger.error(f"Error handling bind command: {e}", exc_info=True)
            try:
                channel_context = self._get_channel_context(context)
                await im_client.send_message(channel_context, self._t("bind.error", error=str(e)))
            except Exception as send_error:
                logger.error(f"Failed to send bind error message: {send_error}", exc_info=True)

    async def handle_stop(self, context: MessageContext, args: str = ""):
        """Handle /stop command - send interrupt message to the active agent"""
        try:
            im_client = self._get_im_client(context)
            session_handler = self.controller.session_handler
            base_session_id, working_path, composite_key = session_handler.get_session_info(context)
            payload = context.platform_specific or {}
            backend_base_session_id = str(payload.get("backend_base_session_id") or "").strip()
            backend_composite_session_id = str(payload.get("backend_composite_session_id") or "").strip()
            if backend_base_session_id:
                base_session_id = backend_base_session_id
            if backend_composite_session_id:
                composite_key = backend_composite_session_id
            session_key = self._get_session_key(context)
            # Stop the backend the turn actually ran on. The web Chat cancel path
            # hands in the turn's captured context, which carries the session's
            # chosen agent in ``agent_session_target``; honor that backend before
            # the generic per-context resolution. Otherwise, if the Chat header
            # switched the session's agent mid-stream, Stop would resolve the new
            # (or default) backend and ``handle_stop`` returns False while the
            # real turn keeps running. IM ``/stop`` carries no
            # ``agent_session_target`` and falls back to the generic resolver, so
            # its behavior is unchanged.
            session_target = payload.get("agent_session_target")
            session_agent_backend = (
                str(session_target["agent_backend"])
                if isinstance(session_target, dict) and session_target.get("agent_backend")
                else None
            )
            agent_name = session_agent_backend or self.controller.resolve_agent_for_context(context)
            request = AgentRequest(
                context=context,
                message="stop",
                working_path=working_path,
                base_session_id=base_session_id,
                composite_session_id=composite_key,
                session_key=session_key,
            )

            handled = await self.controller.agent_service.handle_stop(agent_name, request)
            if not handled and request.stop_failure_reason:
                if context.platform_specific is None:
                    context.platform_specific = {}
                context.platform_specific["stop_failure_reason"] = request.stop_failure_reason
            if not handled and not payload.get("suppress_stop_no_active_notice"):
                channel_context = self._get_channel_context(context)
                await im_client.send_message(channel_context, f"ℹ️ {self._t('command.stop.noActiveSession')}")
            # Return whether the backend actually interrupted a turn, so callers
            # like web ``send-now`` can confirm the stop before cutting in a queued
            # turn (a falsy result means nothing was interrupted).
            return bool(handled)

        except Exception as e:
            logger.error(f"Error sending stop command: {e}", exc_info=True)
            # For errors, still use original context to maintain thread consistency
            await im_client.send_message(
                context,  # Use original context
                f"❌ {self._t('error.stopFailed', error=str(e))}",
            )
            return False
