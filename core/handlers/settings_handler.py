"""Settings and configuration handlers"""

import logging
from typing import Optional

from modules.agents import get_agent_display_name
from modules.im import MessageContext, InlineKeyboard, InlineButton
from core.modals import RoutingModalData, RoutingModalSelection

from .base import BaseHandler

logger = logging.getLogger(__name__)

_UNSET = object()


class SettingsHandler(BaseHandler):
    """Handles settings and configuration operations"""

    def __init__(self, controller):
        """Initialize with reference to main controller"""
        super().__init__(controller)

    def _message_type_display_names(self) -> dict:
        return {
            "assistant": self._t("messageType.assistant"),
            "toolcall": self._t("messageType.toolcall"),
        }

    def _format_settings_update_message(
        self,
        show_message_types: list,
        require_mention: Optional[bool],
        language: Optional[str],
        language_saved: bool,
    ) -> str:
        display_names = self._message_type_display_names()
        selected_names = [display_names.get(msg_type, msg_type) for msg_type in show_message_types]
        selected_text = ", ".join(selected_names) if selected_names else "-"

        if require_mention is None:
            slack_cfg = getattr(self.config, "slack", None)
            default_require = bool(getattr(slack_cfg, "require_mention", False))
            default_state = self._t("common.on") if default_require else self._t("common.off")
            mention_text = f"{self._t('common.default')} {default_state}"
        else:
            mention_text = self._t("common.on") if require_mention else self._t("common.off")

        language_text = self._t(f"language.{language}") if language else self._t("language.systemDefault")
        if language and language_text == f"language.{language}":
            language_text = language

        lines = [
            f"✅ {self._t('success.settingsUpdated')}",
            f"{self._t('modal.settings.showMessageTypes')} **{selected_text}**",
            f"{self._t('modal.settings.requireMention')} **{mention_text}**",
            f"{self._t('modal.settings.language')} **{language_text}**",
        ]
        if not language_saved:
            lines.append(f"⚠️ {self._t('error.languageUpdateFailed')}")
        return "\n".join(lines)

    def _get_agent_display_name(self, context: MessageContext) -> str:
        """Return a friendly agent name for the current context."""
        agent_name = self.controller.resolve_agent_for_context(context)
        default_agent = getattr(self.controller.agent_service, "default_agent", None)
        return get_agent_display_name(agent_name, fallback=default_agent)

    async def handle_settings(self, context: MessageContext, args: str = ""):
        """Handle settings command - show settings menu"""
        try:
            platform = context.platform or (context.platform_specific or {}).get("platform") or self.config.platform
            # For Slack, use modal dialog
            if platform == "slack":
                await self._handle_settings_slack(context)
            elif platform == "discord":
                await self._handle_settings_discord(context)
            elif platform == "telegram":
                await self._handle_settings_telegram(context)
            elif platform == "lark":
                await self._handle_settings_lark(context)
            else:
                # For other platforms, use inline keyboard
                await self._handle_settings_traditional(context)

        except Exception as e:
            logger.error(f"Error showing settings: {e}")
            await self._get_im_client(context).send_message(
                context, f"❌ {self._t('error.showSettings', error=str(e))}"
            )

    async def _handle_settings_traditional(self, context: MessageContext):
        """Handle settings for non-Slack platforms"""
        im_client = self._get_im_client(context)
        # Get current settings
        settings_key = self._get_settings_key(context)
        settings_manager = self._get_settings_manager(context)
        user_settings = settings_manager.get_user_settings(settings_key)

        # Get available message types and display names
        message_types = settings_manager.get_available_message_types()
        display_names = self._message_type_display_names()

        # Create inline keyboard buttons in 2x2 layout
        buttons = []
        row = []

        for i, msg_type in enumerate(message_types):
            is_shown = msg_type in user_settings.show_message_types
            checkbox = "☑️" if is_shown else "⬜"
            display_name = display_names.get(msg_type, msg_type)
            button = InlineButton(
                text=f"{checkbox} {self._t('settings.showMessageType', name=display_name)}",
                callback_data=f"toggle_msg_{msg_type}",
            )
            row.append(button)

            # Create 2x2 layout
            if len(row) == 2 or i == len(message_types) - 1:
                buttons.append(row)
                row = []

        # Add info button on its own row
        buttons.append([InlineButton(f"ℹ️ {self._t('button.aboutMessageTypes')}", callback_data="info_msg_types")])

        keyboard = InlineKeyboard(buttons=buttons)

        # Send settings message with escaped dash
        agent_label = self._get_agent_display_name(context)
        await im_client.send_message_with_buttons(
            context,
            f"⚙️ *{self._t('settings.visibilityTitle')}*\n\n{self._t('settings.visibilityDesc', agent=agent_label)}",
            keyboard,
        )

    async def _handle_settings_slack(self, context: MessageContext):
        """Handle settings for Slack using modal dialog"""
        im_client = self._get_im_client(context)
        # For slash commands or direct triggers, we might have trigger_id
        trigger_id = context.platform_specific.get("trigger_id") if context.platform_specific else None

        if trigger_id and hasattr(im_client, "open_settings_modal"):
            # We have trigger_id, open modal directly
            settings_key = self._get_settings_key(context)
            settings_manager = self._get_settings_manager(context)
            user_settings = settings_manager.get_user_settings(settings_key)
            message_types = settings_manager.get_available_message_types()
            display_names = self._message_type_display_names()

            # Get current require_mention override for this channel
            current_require_mention = settings_manager.get_require_mention_override(settings_key)
            global_require_mention = self.config.slack.require_mention

            # Get current language from global config
            current_language = self.config.language

            try:
                await im_client.run_on_client_loop(
                    im_client.open_settings_modal(
                        trigger_id,
                        user_settings,
                        message_types,
                        display_names,
                        context.channel_id,
                        current_require_mention=current_require_mention,
                        global_require_mention=global_require_mention,
                        current_language=current_language,
                    )
                )
            except Exception as e:
                logger.error(f"Error opening settings modal: {e}")
                await im_client.send_message(context, f"❌ {self._t('error.settingsFailed')}")
        else:
            # No trigger_id, show button to open modal
            buttons = [[InlineButton(text=f"🛠️ {self._t('button.settings')}", callback_data="open_settings_modal")]]

            keyboard = InlineKeyboard(buttons=buttons)

            await im_client.send_message_with_buttons(
                context,
                f"⚙️ *{self._t('settings.personalizationTitle')}*\n\n{self._t('settings.personalizationDesc', agent=self._get_agent_display_name(context))}",
                keyboard,
            )

    async def _handle_settings_discord(self, context: MessageContext):
        im_client = self._get_im_client(context)
        interaction = context.platform_specific.get("interaction") if context.platform_specific else None
        settings_key = self._get_settings_key(context)
        settings_manager = self._get_settings_manager(context)
        user_settings = settings_manager.get_user_settings(settings_key)
        message_types = settings_manager.get_available_message_types()
        display_names = self._message_type_display_names()

        current_require_mention = settings_manager.get_require_mention_override(settings_key)
        global_require_mention = self.config.discord.require_mention if self.config.discord else False
        current_language = self.config.language

        if hasattr(im_client, "open_settings_modal"):
            await im_client.run_on_client_loop(
                im_client.open_settings_modal(
                    interaction,
                    user_settings,
                    message_types,
                    display_names,
                    context.channel_id,
                    current_require_mention=current_require_mention,
                    global_require_mention=global_require_mention,
                    current_language=current_language,
                    owner_user_id=context.user_id,
                )
            )
            return
        await self._handle_settings_traditional(context)

    async def _handle_settings_telegram(self, context: MessageContext):
        im_client = self._get_im_client(context)
        settings_key = self._get_settings_key(context)
        settings_manager = self._get_settings_manager(context)
        user_settings = settings_manager.get_user_settings(settings_key)
        message_types = settings_manager.get_available_message_types()
        display_names = self._message_type_display_names()
        current_require_mention = settings_manager.get_require_mention_override(settings_key)
        global_require_mention = self.config.telegram.require_mention if self.config.telegram else True
        current_language = self.config.language

        if hasattr(im_client, "open_settings_modal"):
            await im_client.run_on_client_loop(
                im_client.open_settings_modal(
                    trigger_id=context,
                    user_settings=user_settings,
                    message_types=message_types,
                    display_names=display_names,
                    channel_id=context.channel_id,
                    current_require_mention=current_require_mention,
                    global_require_mention=global_require_mention,
                    current_language=current_language,
                    owner_user_id=context.user_id,
                )
            )
            return
        await self._handle_settings_traditional(context)

    async def _handle_settings_lark(self, context: MessageContext):
        """Handle settings for Lark/Feishu using interactive form card."""
        im_client = self._get_im_client(context)
        settings_key = self._get_settings_key(context)
        settings_manager = self._get_settings_manager(context)
        user_settings = settings_manager.get_user_settings(settings_key)
        message_types = settings_manager.get_available_message_types()
        display_names = self._message_type_display_names()

        current_require_mention = settings_manager.get_require_mention_override(settings_key)
        global_require_mention = self.config.lark.require_mention if self.config.lark else False
        current_language = self.config.language

        if hasattr(im_client, "open_settings_modal"):
            try:
                await im_client.run_on_client_loop(
                    im_client.open_settings_modal(
                        trigger_id=context,
                        user_settings=user_settings,
                        message_types=message_types,
                        display_names=display_names,
                        channel_id=context.channel_id,
                        current_require_mention=current_require_mention,
                        global_require_mention=global_require_mention,
                        current_language=current_language,
                    )
                )
            except Exception as e:
                logger.error(f"Error opening settings card for Lark: {e}", exc_info=True)
                await im_client.send_message(context, f"❌ {self._t('error.settingsFailed')}")
        else:
            await self._handle_settings_traditional(context)

    async def handle_toggle_message_type(self, context: MessageContext, msg_type: str):
        """Handle toggle for message type visibility"""
        try:
            im_client = self._get_im_client(context)
            settings_manager = self._get_settings_manager(context)
            # Toggle message type visibility
            settings_key = self._get_settings_key(context)
            message_types = settings_manager.get_available_message_types()
            if msg_type not in message_types:
                return
            is_shown = settings_manager.toggle_show_message_type(settings_key, msg_type)

            # Update the keyboard
            user_settings = settings_manager.get_user_settings(settings_key)
            display_names = self._message_type_display_names()

            buttons = []
            row = []

            for i, mt in enumerate(message_types):
                is_shown_now = mt in user_settings.show_message_types
                checkbox = "☑️" if is_shown_now else "⬜"
                display_name = display_names.get(mt, mt)
                button = InlineButton(
                    text=f"{checkbox} {self._t('settings.showMessageType', name=display_name)}",
                    callback_data=f"toggle_msg_{mt}",
                )
                row.append(button)

                # Create 2x2 layout
                if len(row) == 2 or i == len(message_types) - 1:
                    buttons.append(row)
                    row = []

            buttons.append([InlineButton(f"ℹ️ {self._t('button.aboutMessageTypes')}", callback_data="info_msg_types")])

            keyboard = InlineKeyboard(buttons=buttons)

            # Update message
            if context.message_id:
                await im_client.edit_message(context, context.message_id, keyboard=keyboard)

            # Answer callback (for Telegram)
            display_name = display_names.get(msg_type, msg_type)
            action = self._t("settings.visibilityShown") if is_shown else self._t("settings.visibilityHidden")

            # Platform-specific callback answering
            await im_client.send_message(
                context,
                self._t("settings.messageTypeStatus", name=display_name, action=action),
            )

        except Exception as e:
            logger.error(f"Error toggling message type {msg_type}: {e}")
            await self._get_im_client(context).send_message(
                context,
                self._get_formatter(context).format_error(self._t("error.toggleSettingFailed", error=str(e))),
            )

    async def handle_info_message_types(self, context: MessageContext):
        """Show information about different message types"""
        try:
            formatter = self._get_formatter(context)

            # Use the new format_info_message method for clean, platform-agnostic formatting
            info_text = formatter.format_info_message(
                title=self._t("info.messageTypesTitle"),
                emoji="📋",
                items=[
                    (self._t("messageType.toolcall"), self._t("messageType.toolcallDesc")),
                    (self._t("messageType.assistant"), self._t("messageType.assistantDesc")),
                    (self._t("messageType.result"), self._t("messageType.resultDesc")),
                ],
                footer=self._t("info.messageTypesFooter"),
            )

            # Send as new message
            await self._get_im_client(context).send_message(context, info_text)
            logger.info(f"Sent info_msg_types message to user {context.user_id}")

        except Exception as e:
            logger.error(f"Error in info_msg_types handler: {e}", exc_info=True)
            await self._get_im_client(context).send_message(context, f"❌ {self._t('error.messageTypesInfoFailed')}")

    async def handle_info_how_it_works(self, context: MessageContext):
        """Show information about how the bot works"""
        try:
            formatter = self._get_formatter(context)
            agent_label = self._get_agent_display_name(context)

            # Use format_info_message for clean, platform-agnostic formatting
            info_text = formatter.format_info_message(
                title=self._t("info.howItWorksTitle"),
                emoji="✨",
                items=[
                    (
                        self._t("info.howItWorksRealtimeLabel"),
                        self._t("info.howItWorksRealtimeDesc", agent=agent_label),
                    ),
                    (self._t("info.howItWorksPersistentLabel"), self._t("info.howItWorksPersistentDesc")),
                    (self._t("info.howItWorksCommandsLabel"), self._t("info.howItWorksCommandsDesc")),
                    (self._t("info.howItWorksResumeLabel"), self._t("info.howItWorksResumeDesc")),
                    (self._t("info.howItWorksWorkDirLabel"), self._t("info.howItWorksWorkDirDesc")),
                    (self._t("info.howItWorksTasksLabel"), self._t("info.howItWorksTasksDesc")),
                    (self._t("info.howItWorksSettingsLabel"), self._t("info.howItWorksSettingsDesc")),
                ],
                footer=self._t("info.howItWorksFooter", agent=agent_label),
            )

            # Send as new message
            await self._get_im_client(context).send_message(context, info_text)
            logger.info(f"Sent how_it_works info to user {context.user_id}")

        except Exception as e:
            logger.error(f"Error in handle_info_how_it_works: {e}", exc_info=True)
            await self._get_im_client(context).send_message(context, f"❌ {self._t('error.helpInfoFailed')}")

    async def handle_routing(self, context: MessageContext):
        """Handle routing command - show agent/model selection"""
        try:
            platform = context.platform or (context.platform_specific or {}).get("platform") or self.config.platform
            # Only Slack has modal support for now
            if platform == "slack":
                await self._handle_routing_slack(context)
            elif platform == "discord":
                await self._handle_routing_discord(context)
            elif platform == "telegram":
                await self._handle_routing_telegram(context)
            elif platform == "lark":
                await self._handle_routing_lark(context)
            else:
                # For other platforms, show a simple message
                await self._get_im_client(context).send_message(
                    context,
                    self._t("routing.slackOnly"),
                )
        except Exception as e:
            logger.error(f"Error showing routing settings: {e}", exc_info=True)
            await self._get_im_client(context).send_message(
                context, f"❌ {self._t('error.routingFailed', error=str(e))}"
            )

    async def _gather_routing_modal_data(
        self,
        context: MessageContext,
        selected_backend: Optional[str] = None,
        include_all_backend_data: bool = False,
    ) -> RoutingModalData:
        """Collect backend/agent/model data for routing modal renderers."""
        settings_key = self._get_settings_key(context)
        current_routing = self._get_settings_manager(context).get_channel_routing(settings_key)

        all_backends = list(self.controller.agent_service.agents.keys())
        current_backend = self.controller.resolve_agent_for_context(context)
        enabled_backends = [backend for backend in all_backends if self._is_backend_enabled(backend)]
        if not enabled_backends and current_backend:
            enabled_backends = [current_backend]
        registered_backends = sorted(enabled_backends, key=lambda x: (x != "opencode", x))
        active_backend = registered_backends[0] if registered_backends else current_backend
        if current_backend in registered_backends:
            active_backend = current_backend
        if selected_backend in registered_backends:
            active_backend = selected_backend
        visible_current_backend = current_backend if current_backend in registered_backends else active_backend
        backends_to_load = set(registered_backends) if include_all_backend_data else {active_backend}

        opencode_agents = []
        opencode_models = {}
        opencode_default_config = {}
        claude_agents = []
        claude_models = []
        codex_agents = []
        codex_models = []

        if "opencode" in backends_to_load:
            try:
                opencode_agent = self.controller.agent_service.agents.get("opencode")
                if opencode_agent and hasattr(opencode_agent, "_get_server"):
                    server = await opencode_agent._get_server()  # type: ignore[attr-defined]
                    await server.ensure_running()

                    cwd = self.controller.get_cwd(context)
                    opencode_agents = await server.get_available_agents(cwd)
                    opencode_models = await server.get_available_models(cwd)
                    opencode_default_config = await server.get_default_config(cwd)
            except Exception as e:
                logger.warning(f"Failed to fetch OpenCode data: {e}")

        if "claude" in backends_to_load:
            try:
                from vibe.api import claude_agents as get_claude_agents, claude_models as get_claude_models

                cwd = self.controller.get_cwd(context)
                agents_result = get_claude_agents(cwd)
                if agents_result.get("ok"):
                    claude_agents = agents_result.get("agents", [])
                models_result = get_claude_models()
                if models_result.get("ok"):
                    claude_models = models_result.get("models", [])
            except Exception as e:
                logger.warning(f"Failed to fetch Claude data: {e}")

        if "codex" in backends_to_load:
            try:
                from vibe.api import codex_agents as get_codex_agents, codex_models as get_codex_models

                models_result = get_codex_models()
                if models_result.get("ok"):
                    codex_models = models_result.get("models", [])
                cwd = self.controller.get_cwd(context)
                agents_result = get_codex_agents(cwd)
                if agents_result.get("ok"):
                    codex_agents = agents_result.get("agents", [])
            except Exception as e:
                logger.warning(f"Failed to fetch Codex data: {e}")

        return RoutingModalData(
            registered_backends=registered_backends,
            current_backend=visible_current_backend,
            current_routing=current_routing,
            opencode_agents=opencode_agents,
            opencode_models=opencode_models,
            opencode_default_config=opencode_default_config,
            claude_agents=claude_agents,
            claude_models=claude_models,
            codex_agents=codex_agents,
            codex_models=codex_models,
        )

    def _is_backend_enabled(self, backend: str) -> bool:
        backend_config = getattr(self.config, backend, None)
        if backend_config is None:
            return False
        return bool(getattr(backend_config, "enabled", True))

    def _resolve_route_backend(self, agent_name: Optional[str]) -> Optional[str]:
        if not agent_name:
            return None
        name = str(agent_name)
        if name in {"opencode", "claude", "codex"}:
            return name
        store = getattr(self.controller, "vibe_agent_store", None)
        if store is None:
            return None
        try:
            agent = store.get(name)
        except Exception:
            return None
        backend = getattr(agent, "backend", None) if agent else None
        return str(backend) if backend else None

    @staticmethod
    def _routing_target_from_row(row: dict) -> tuple[str, str, str, str, str]:
        backend = str(row.get("agent_backend") or "").strip()
        agent_name = str(row.get("agent_name") or "").strip()
        if not agent_name and backend in {"opencode", "claude", "codex"}:
            agent_name = backend
        variant = str(row.get("agent_variant") or "").strip()
        if variant == backend and agent_name == backend:
            variant = ""
        return (
            agent_name,
            backend,
            variant,
            str(row.get("model") or "").strip(),
            str(row.get("reasoning_effort") or "").strip(),
        )

    def _routing_target_from_settings(self, routing) -> tuple[str, str, str, str, str]:
        agent_name = str(getattr(routing, "agent_name", None) or "").strip()
        backend = str(self._resolve_route_backend(agent_name) or agent_name).strip()
        variant = ""
        if backend == "opencode":
            variant = str(getattr(routing, "opencode_agent", None) or "").strip()
        elif backend == "claude":
            variant = str(getattr(routing, "claude_agent", None) or "").strip()
        elif backend == "codex":
            variant = str(getattr(routing, "codex_agent", None) or "").strip()
        return (
            agent_name,
            backend,
            variant,
            str(getattr(routing, "model", None) or "").strip(),
            str(getattr(routing, "reasoning_effort", None) or "").strip(),
        )

    def _routing_update_needs_new_session_hint(self, context: MessageContext, routing) -> bool:
        row = self._current_flat_scope_session_row_for_hint(context, log_context="routing update hint")
        if not row:
            return False
        current_target = self._routing_target_from_row(row)
        next_target = self._routing_target_from_settings(routing)
        current_backend = current_target[1]
        next_backend = next_target[1]
        return bool(current_backend and next_backend and current_backend != next_backend)

    async def _handle_routing_slack(self, context: MessageContext):
        """Handle routing for Slack using modal dialog"""
        im_client = self._get_im_client(context)
        trigger_id = context.platform_specific.get("trigger_id") if context.platform_specific else None

        if not trigger_id:
            # No trigger_id, show button to open modal
            buttons = [
                [
                    InlineButton(
                        text=f"🤖 {self._t('button.agentSettings')}",
                        callback_data="open_routing_modal",
                    )
                ]
            ]
            keyboard = InlineKeyboard(buttons=buttons)
            await im_client.send_message_with_buttons(
                context,
                f"🤖 *{self._t('routing.introTitle')}*\n\n{self._t('routing.introDesc')}",
                keyboard,
            )
            return

        routing_data = await self._gather_routing_modal_data(context)

        # Open modal
        try:
            await im_client.run_on_client_loop(
                im_client.open_routing_modal(
                    trigger_id=trigger_id,
                    channel_id=context.channel_id,
                    **routing_data.as_kwargs(),
                )
            )
        except Exception as e:
            logger.error(f"Error opening routing modal: {e}", exc_info=True)
            await im_client.send_message(context, f"❌ {self._t('error.routingModalFailed')}")

    async def _handle_routing_discord(self, context: MessageContext):
        im_client = self._get_im_client(context)
        interaction = context.platform_specific.get("interaction") if context.platform_specific else None
        routing_data = await self._gather_routing_modal_data(context, include_all_backend_data=True)

        try:
            await im_client.run_on_client_loop(
                im_client.open_routing_modal(
                    trigger_id=interaction or context,
                    channel_id=context.channel_id,
                    **routing_data.as_kwargs(),
                )
            )
        except Exception as e:
            logger.error(f"Error opening routing modal: {e}", exc_info=True)
            await im_client.send_message(context, f"❌ {self._t('error.routingModalFailed')}")

    async def _handle_routing_telegram(self, context: MessageContext):
        im_client = self._get_im_client(context)
        routing_data = await self._gather_routing_modal_data(context, include_all_backend_data=True)
        try:
            await im_client.run_on_client_loop(
                im_client.open_routing_modal(
                    trigger_id=context,
                    channel_id=context.channel_id,
                    **routing_data.as_kwargs(),
                )
            )
        except Exception as e:
            logger.error(f"Error opening Telegram routing flow: {e}", exc_info=True)
            await im_client.send_message(context, f"❌ {self._t('error.routingModalFailed')}")

    async def _handle_routing_lark(self, context: MessageContext):
        """Handle routing for Lark/Feishu using interactive form card.

        Gathers the same backend/model/agent data as Slack/Discord so the
        Feishu card can display selectors for all available options.
        """
        im_client = self._get_im_client(context)
        routing_data = await self._gather_routing_modal_data(context, include_all_backend_data=True)

        try:
            await im_client.run_on_client_loop(
                im_client.open_routing_modal(
                    trigger_id=context,
                    channel_id=context.channel_id,
                    **routing_data.as_kwargs(),
                )
            )
        except Exception as e:
            logger.error(f"Error opening routing card for Lark: {e}", exc_info=True)
            await im_client.send_message(context, f"❌ {self._t('error.routingModalFailed')}")

    async def handle_settings_update(
        self,
        user_id: str,
        show_message_types: list,
        channel_id: Optional[str] = None,
        require_mention: Optional[bool] = None,
        language: Optional[str] = None,
        notify_user: bool = True,
        is_dm: bool = False,
        platform: Optional[str] = None,
    ):
        """Handle settings update (typically from modal submissions)."""
        try:
            context = MessageContext(
                user_id=user_id,
                channel_id=channel_id if channel_id else user_id,
                platform=platform or self.config.platform,
                platform_specific={"is_dm": is_dm, "platform": platform or self.config.platform},
            )
            settings_key = self._get_settings_key(context)
            im_client = self._get_im_client(context)
            settings_manager = self._get_settings_manager(context)

            user_settings = settings_manager.get_user_settings(settings_key)
            user_settings.show_message_types = show_message_types
            settings_manager.update_user_settings(settings_key, user_settings)

            if not is_dm:
                settings_manager.set_require_mention(settings_key, require_mention)

            language_saved = True
            if language is not None and language != self.config.language:
                try:
                    from config.v2_config import V2Config

                    v2_config = V2Config.load()
                    v2_config.language = language
                    v2_config.save()
                    self.config.language = language
                except Exception as err:
                    language_saved = False
                    logger.error(f"Failed to persist language setting: {err}")

            logger.info(
                f"Updated settings for {settings_key}: show types = {show_message_types}, "
                f"require_mention = {require_mention}, language = {language}"
            )

            if notify_user:
                message = self._format_settings_update_message(
                    show_message_types=show_message_types,
                    require_mention=require_mention,
                    language=language,
                    language_saved=language_saved,
                )
                await im_client.send_message(context, message, parse_mode="markdown")

        except Exception as e:
            logger.error(f"Error updating settings: {e}")
            if notify_user:
                context = MessageContext(
                    user_id=user_id,
                    channel_id=channel_id if channel_id else user_id,
                    platform=platform or self.config.platform,
                    platform_specific={"is_dm": is_dm, "platform": platform or self.config.platform},
                )
                await self._get_im_client(context).send_message(
                    context,
                    f"❌ {self._t('error.settingsUpdateFailed', error=str(e))}",
                )
            else:
                raise

    async def handle_routing_modal_update(
        self,
        user_id: str,
        channel_id: str,
        view_id: str,
        view_hash: str,
        selection: RoutingModalSelection,
        is_dm: bool = False,
        platform: Optional[str] = None,
    ) -> None:
        """Handle routing modal updates using normalized selection data."""
        try:
            if not view_id or not view_hash:
                logger.warning("Routing modal update missing view id/hash")
                return

            resolved_channel_id = channel_id if channel_id else user_id
            context = MessageContext(
                user_id=user_id,
                channel_id=resolved_channel_id,
                platform=platform or self.config.platform,
                platform_specific={"is_dm": is_dm, "platform": platform or self.config.platform},
            )
            im_client = self._get_im_client(context)

            resolved_backend = self.controller.resolve_agent_for_context(context)
            selected_backend = selection.selected_backend or resolved_backend
            routing_data = await self._gather_routing_modal_data(context, selected_backend=selected_backend)
            current_routing = routing_data.current_routing
            registered_backends = routing_data.registered_backends
            current_backend = routing_data.current_backend
            visible_selected_backend = (
                selected_backend if selected_backend in registered_backends else current_backend
            )

            if hasattr(im_client, "update_routing_modal"):
                await im_client.update_routing_modal(  # type: ignore[attr-defined]
                    view_id=view_id,
                    view_hash=view_hash,
                    channel_id=resolved_channel_id,
                    registered_backends=registered_backends,
                    current_backend=current_backend,
                    current_routing=current_routing,
                    opencode_agents=routing_data.opencode_agents,
                    opencode_models=routing_data.opencode_models,
                    opencode_default_config=routing_data.opencode_default_config,
                    claude_agents=routing_data.claude_agents,
                    claude_models=routing_data.claude_models,
                    codex_agents=routing_data.codex_agents,
                    codex_models=routing_data.codex_models,
                    selected_backend=visible_selected_backend,
                    selected_opencode_agent=selection.selected_opencode_agent,
                    selected_opencode_model=selection.selected_opencode_model,
                    selected_opencode_reasoning=selection.selected_opencode_reasoning,
                    selected_claude_agent=selection.selected_claude_agent,
                    selected_claude_model=selection.selected_claude_model,
                    selected_claude_reasoning=selection.selected_claude_reasoning,
                    selected_codex_agent=selection.selected_codex_agent,
                    selected_codex_model=selection.selected_codex_model,
                    selected_codex_reasoning=selection.selected_codex_reasoning,
                )
        except Exception as e:
            logger.error(f"Error updating routing modal: {e}", exc_info=True)

    async def handle_routing_update(
        self,
        user_id: str,
        channel_id: str,
        backend: str,
        opencode_agent: Optional[str],
        opencode_model: Optional[str],
        opencode_reasoning_effort: Optional[str] = None,
        claude_agent: Optional[str] = None,
        claude_model: Optional[str] = None,
        claude_reasoning_effort: Optional[str] = None,
        codex_agent: object = _UNSET,
        codex_model: Optional[str] = None,
        codex_reasoning_effort: Optional[str] = None,
        notify_user: bool = True,
        is_dm: bool = False,
        platform: Optional[str] = None,
    ):
        """Handle routing update submission (from modal)."""
        from config.v2_settings import RoutingSettings
        from modules.agents.opencode.utils import normalize_claude_reasoning_effort

        try:
            context = MessageContext(
                user_id=user_id,
                channel_id=channel_id if channel_id else user_id,
                platform=platform or self.config.platform,
                platform_specific={"is_dm": is_dm, "platform": platform or self.config.platform},
            )
            settings_key = self._get_settings_key(context)
            im_client = self._get_im_client(context)
            settings_manager = self._get_settings_manager(context)
            existing_routing = settings_manager.get_channel_routing(settings_key)
            normalized_claude_reasoning_effort = normalize_claude_reasoning_effort(
                claude_model,
                claude_reasoning_effort,
            )
            if backend == "codex" and codex_agent is _UNSET:
                resolved_codex_agent = existing_routing.codex_agent if existing_routing else None
            else:
                resolved_codex_agent = codex_agent

            routing = RoutingSettings(
                agent_name=backend,
                model=(
                    opencode_model
                    if backend == "opencode"
                    else claude_model
                    if backend == "claude"
                    else codex_model
                    if backend == "codex"
                    else None
                ),
                reasoning_effort=(
                    opencode_reasoning_effort
                    if backend == "opencode"
                    else normalized_claude_reasoning_effort
                    if backend == "claude"
                    else codex_reasoning_effort
                    if backend == "codex"
                    else None
                ),
                opencode_agent=opencode_agent
                if backend == "opencode"
                else (existing_routing.opencode_agent if existing_routing else None),
                claude_agent=claude_agent
                if backend == "claude"
                else (existing_routing.claude_agent if existing_routing else None),
                codex_agent=resolved_codex_agent
                if backend == "codex"
                else (existing_routing.codex_agent if existing_routing else None),
            )

            settings_manager.set_channel_routing(settings_key, routing)
            needs_new_session_hint = self._routing_update_needs_new_session_hint(context, routing)

            parts = [f"{self._t('routing.label.backend')}: **{backend}**"]
            if backend == "opencode":
                if opencode_agent:
                    parts.append(f"{self._t('routing.label.agent')}: **{opencode_agent}**")
                if opencode_model:
                    parts.append(f"{self._t('routing.label.model')}: **{opencode_model}**")
                if opencode_reasoning_effort:
                    parts.append(f"{self._t('routing.label.reasoningEffort')}: **{opencode_reasoning_effort}**")
            elif backend == "claude":
                if claude_agent:
                    parts.append(f"{self._t('routing.label.agent')}: **{claude_agent}**")
                if claude_model:
                    parts.append(f"{self._t('routing.label.model')}: **{claude_model}**")
                if normalized_claude_reasoning_effort:
                    parts.append(
                        f"{self._t('routing.label.reasoningEffort')}: **{normalized_claude_reasoning_effort}**"
                    )
            elif backend == "codex":
                if resolved_codex_agent:
                    parts.append(f"{self._t('routing.label.agent')}: **{resolved_codex_agent}**")
                if codex_model:
                    parts.append(f"{self._t('routing.label.model')}: **{codex_model}**")
                if codex_reasoning_effort:
                    parts.append(f"{self._t('routing.label.reasoningEffort')}: **{codex_reasoning_effort}**")
            if needs_new_session_hint:
                parts.extend(["", self._t("success.routingUpdateNeedsNewSession")])

            if notify_user:
                await im_client.send_message(
                    context,
                    f"✅ {self._t('success.routingUpdated')}\n" + "\n".join(parts),
                    parse_mode="markdown",
                )

            logger.info(
                f"Routing updated for {settings_key}: backend={backend}, "
                f"opencode_agent={opencode_agent}, opencode_model={opencode_model}, "
                f"claude_agent={claude_agent}, claude_model={claude_model}, "
                f"claude_reasoning_effort={normalized_claude_reasoning_effort}, "
                f"codex_agent={resolved_codex_agent}, codex_model={codex_model}, "
                f"codex_reasoning_effort={codex_reasoning_effort}"
            )

        except Exception as e:
            logger.error(f"Error updating routing: {e}")
            context = MessageContext(
                user_id=user_id,
                channel_id=channel_id if channel_id else user_id,
                platform=platform or self.config.platform,
                platform_specific={"is_dm": is_dm, "platform": platform or self.config.platform},
            )
            if notify_user:
                await self._get_im_client(context).send_message(
                    context,
                    f"❌ {self._t('error.routingUpdateFailed', error=str(e))}",
                )
            else:
                raise
