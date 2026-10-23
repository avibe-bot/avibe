"""Settings and configuration handlers"""

import asyncio
import logging
import sys
from typing import Optional

from modules.agents import get_agent_display_name
from modules.agents.catalog import (
    AGENT_BACKENDS,
    NATIVE_CLI_BACKENDS,
    display_name_for_backend,
    implicit_default_rank,
    is_builtin_backend,
)
from modules.im import MessageContext, InlineKeyboard, InlineButton
from config.v2_settings import normalize_routing_settings
from core.modals import (
    DEFAULT_OPTION_VALUE,
    STORED_OPTION_VALUE,
    BackendRouteMenu,
    RouteOption,
    RoutingModalData,
    RoutingModalSelection,
)
from vibe import backend_model_catalog

from .base import BaseHandler

logger = logging.getLogger(__name__)

_UNSET = object()


def _unique_options(values) -> list[RouteOption]:
    names = dict.fromkeys(str(value).strip() for value in values if value and str(value).strip())
    return [RouteOption(name, name) for name in names]


def _effort_options(entries) -> list[RouteOption]:
    """Catalog effort entries as route options, without the Default entry every select adds itself."""
    return [
        RouteOption(str(entry["value"]), str(entry.get("label") or entry["value"]))
        for entry in entries or []
        if entry.get("value") and entry["value"] != DEFAULT_OPTION_VALUE
    ]


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
            im_client = self._get_im_client(context)
            if not hasattr(im_client, "open_routing_modal"):
                await im_client.send_message(context, self._t("routing.slackOnly"))
                return
            platform_specific = context.platform_specific or {}
            if platform == "slack":
                # A Slack modal opens only from an interaction; a typed command gets a button that is one.
                trigger = platform_specific.get("trigger_id")
                if not trigger:
                    keyboard = InlineKeyboard(
                        buttons=[
                            [
                                InlineButton(
                                    text=f"🤖 {self._t('button.agentSettings')}",
                                    callback_data="open_routing_modal",
                                )
                            ]
                        ]
                    )
                    await im_client.send_message_with_buttons(
                        context,
                        f"🤖 *{self._t('routing.introTitle')}*\n\n{self._t('routing.introDesc')}",
                        keyboard,
                    )
                    return
            elif platform == "discord":
                trigger = platform_specific.get("interaction") or context
            else:
                trigger = context
            # A form refreshed through ``update_routing_modal`` loads each backend as it is shown; the others
            # render every backend up front.
            routing_data = await self._gather_routing_modal_data(
                context, include_all_backend_data=not hasattr(im_client, "update_routing_modal")
            )
            try:
                await im_client.run_on_client_loop(
                    im_client.open_routing_modal(
                        trigger_id=trigger,
                        channel_id=context.channel_id,
                        routing=routing_data,
                    )
                )
            except Exception as e:
                logger.error(f"Error opening routing modal: {e}", exc_info=True)
                await im_client.send_message(context, f"❌ {self._t('error.routingModalFailed')}")
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
        """Collect each shown backend's route menu for the routing form renderers."""
        settings_key = self._get_settings_key(context)
        current_routing = self._get_settings_manager(context).get_channel_routing(settings_key)

        all_backends = list(self.controller.agent_service.agents.keys())
        current_backend = self.controller.resolve_agent_for_context(context)
        enabled_backends = [backend for backend in all_backends if self._is_backend_enabled(backend)]
        if not enabled_backends and current_backend:
            enabled_backends = [current_backend]
        # Catalog order, the built-in backend first. As for the default Agent, the fallback
        # selection is the built-in backend only when no other backend is enabled.
        catalog_order = {backend: index for index, backend in enumerate(AGENT_BACKENDS)}
        registered_backends = sorted(enabled_backends, key=lambda x: catalog_order.get(x, len(catalog_order)))
        active_backend = min(registered_backends, key=implicit_default_rank, default=current_backend)
        if current_backend in registered_backends:
            active_backend = current_backend
        if selected_backend in registered_backends:
            active_backend = selected_backend
        visible_current_backend = current_backend if current_backend in registered_backends else active_backend
        backends_to_load = registered_backends if include_all_backend_data else [active_backend]

        data = RoutingModalData(
            registered_backends=registered_backends,
            current_backend=visible_current_backend,
            current_routing=current_routing,
        )
        im_client = self._get_im_client(context)
        for backend in backends_to_load:
            menu = BackendRouteMenu(
                backend,
                option_limit=getattr(im_client, "route_option_limit", None),
                value_limit=getattr(im_client, "route_option_value_limit", None),
                stored=data.stored_draft(backend),
            )
            loader = self._route_menu_loaders.get(backend)
            if loader is not None:
                try:
                    await loader(self, context, menu, menu.stored.model)
                except Exception as e:
                    logger.warning("Failed to load %s routing options: %s", backend, e)
            data.menus[backend] = menu
        return data

    # Where each backend's route lists come from: the only backend-specific part of the routing form. Each
    # loader fills ``menu`` in place; ``stored_model`` is the stored route's model when this is its backend.

    async def _load_opencode_route_menu(self, context, menu: BackendRouteMenu, stored_model: Optional[str]) -> None:
        from modules.agents.opencode.utils import (
            build_opencode_model_option_items,
            build_reasoning_effort_options,
            resolve_opencode_allowed_providers,
            resolve_opencode_provider_preferences,
        )

        opencode_agent = self.controller.agent_service.agents.get("opencode")
        if not opencode_agent or not hasattr(opencode_agent, "current_server"):
            return
        async with opencode_agent.current_server() as server:  # type: ignore[attr-defined]
            cwd = self.controller.get_cwd(context)
            agents = await server.get_available_agents(cwd)
            model_hub_service = getattr(self.controller, "model_hub_service", None)
            public_models = model_hub_service.opencode_public_models() if model_hub_service is not None else None
            if public_models is None:
                models = await server.get_available_models(cwd)
            else:
                models = await server.get_available_models(cwd, model_hub_models=public_models)
            default_config = await server.get_default_config(cwd)
        menu.variants = _unique_options(
            agent.get("name") if isinstance(agent, dict) else agent for agent in agents or []
        )
        def _entries(max_total: int) -> list[RouteOption]:
            return [
                RouteOption(str(entry["value"]), str(entry["label"]))
                for entry in build_opencode_model_option_items(
                    models,
                    max_total=max_total,
                    preferred_providers=resolve_opencode_provider_preferences(default_config, stored_model),
                    allowed_providers=resolve_opencode_allowed_providers(default_config, models),
                )
                if entry.get("value")
            ]

        # A platform without a cap (a paged picker) gets every model. With a cap, OpenCode spreads it over its
        # providers, so the list is built for the select, with room for the entries the platform cannot carry.
        every = _entries(sys.maxsize)
        if menu.option_limit:
            unfit = sum(1 for option in every if not menu.fits(option.value))
            every = _entries(menu.option_limit - 1 + unfit)
        menu.models = [option for option in every if menu.fits(option.value)]
        for model in {"", *(option.value for option in menu.models), stored_model or ""}:
            menu.efforts[model] = _effort_options(build_reasoning_effort_options(models, model or None))

    async def _load_claude_route_menu(self, context, menu: BackendRouteMenu, stored_model: Optional[str]) -> None:
        from modules.agents.opencode.utils import (
            build_claude_reasoning_options,
            format_claude_model_label,
            resolve_model_reasoning_options,
        )
        from vibe.api import claude_agents as get_claude_agents, claude_models as get_claude_models
        from vibe.claude_model_catalog import DEFAULT_CLAUDE_MODEL_ALIASES

        agents_result = get_claude_agents(self.controller.get_cwd(context))
        if agents_result.get("ok"):
            menu.variants = _unique_options(agent.get("id") for agent in agents_result.get("agents", []))
        models_result = await asyncio.to_thread(get_claude_models)
        if not models_result.get("ok"):
            return
        models = [str(model) for model in models_result.get("models", []) if model]
        # The bare aliases are always valid and the most useful, so a capped select trims dated snapshots first.
        aliases = [alias for alias in DEFAULT_CLAUDE_MODEL_ALIASES if alias in models]
        ordered = aliases + [model for model in models if model not in aliases]
        menu.models = [RouteOption(model, format_claude_model_label(model)) for model in ordered]
        catalog = models_result.get("reasoning_options")
        catalog = catalog if isinstance(catalog, dict) else None
        for model in {"", *ordered, stored_model or ""}:
            menu.efforts[model] = _effort_options(
                resolve_model_reasoning_options(catalog, model, build_claude_reasoning_options(model or None))
            )

    async def _load_codex_route_menu(self, context, menu: BackendRouteMenu, stored_model: Optional[str]) -> None:
        from modules.agents.opencode.utils import build_codex_reasoning_options, resolve_model_reasoning_options
        from vibe.api import codex_agents as get_codex_agents, codex_models as get_codex_models

        models_result = await asyncio.to_thread(get_codex_models)
        agents_result = get_codex_agents(self.controller.get_cwd(context))
        if agents_result.get("ok"):
            menu.variants = _unique_options(agent.get("id") for agent in agents_result.get("agents", []))
        if not models_result.get("ok"):
            return
        models = [str(model) for model in models_result.get("models", []) if model]
        menu.models = [RouteOption(model, model) for model in models]
        catalog = models_result.get("reasoning_options")
        catalog = catalog if isinstance(catalog, dict) else None
        for model in {"", *models, stored_model or ""}:
            menu.efforts[model] = _effort_options(
                resolve_model_reasoning_options(catalog, model, build_codex_reasoning_options())
            )

    async def _load_vibey_route_menu(self, context, menu: BackendRouteMenu, stored_model: Optional[str]) -> None:
        """Vibey's menu from Model Hub, as the Web route picker reads it.

        An effort Model Hub's ``effort_menu`` omits would only fail, so it is not offered. A route without its
        own model inherits the built-in Vibey Agent's, so ``""`` holds that model's efforts.
        """
        model_hub_service = getattr(self.controller, "model_hub_service", None)
        if model_hub_service is None:
            return
        catalog = (await model_hub_service.read_agent_sources("vibey")).get("catalog_models") or []
        for row in catalog:
            model_id = str(row.get("id") or "").strip()
            if not model_id or row.get("routeable") is False or model_id in menu.efforts:
                continue
            omitted = set((row.get("effort_menu") or {}).get("omitted") or [])
            menu.models.append(RouteOption(model_id, str(row.get("display_name") or model_id)))
            menu.efforts[model_id] = (
                []
                if row.get("supports_reasoning") is False
                else [
                    RouteOption(effort, effort)
                    for effort in dict.fromkeys(row.get("reasoning_efforts") or [])
                    if effort not in omitted
                ]
            )
        store = getattr(self.controller, "vibe_agent_store", None)
        agent = (
            await asyncio.to_thread(store.get_builtin_default_agent_for_backend, "vibey", enabled_only=False)
            if store
            else None
        )
        inherited = menu.efforts.get(str(getattr(agent, "model", None) or "").strip())
        if inherited is not None:
            menu.efforts[""] = inherited

    _route_menu_loaders = {
        "opencode": _load_opencode_route_menu,
        "claude": _load_claude_route_menu,
        "codex": _load_codex_route_menu,
        "vibey": _load_vibey_route_menu,
    }

    def _is_backend_enabled(self, backend: str) -> bool:
        if is_builtin_backend(backend):
            return True
        backend_config = getattr(self.config, backend, None)
        if backend_config is None:
            return False
        return bool(getattr(backend_config, "enabled", True))

    def _routing_agent_name(self, backend: str) -> str:
        """A backend choice routes to that backend's built-in Agent, under whatever name the row holds."""
        store = getattr(self.controller, "vibe_agent_store", None)
        return store.routing_name_for_backend(backend) if store is not None else backend

    def _resolve_route_backend(self, agent_name: Optional[str]) -> Optional[str]:
        if not agent_name:
            return None
        name = str(agent_name)
        if name in NATIVE_CLI_BACKENDS:
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
        if not agent_name and backend in NATIVE_CLI_BACKENDS:
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
        variant = getattr(routing, f"{backend}_agent", None) if backend in NATIVE_CLI_BACKENDS else None
        variant = str(variant or "").strip()
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

    async def handle_settings_update(
        self,
        user_id: str,
        show_message_types: list,
        *,
        channel_id: Optional[str] = None,
        thread_id: Optional[str] = None,
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
                thread_id=thread_id,
                platform=platform or self.config.platform,
                platform_specific={"is_dm": is_dm, "platform": platform or self.config.platform},
            )
            settings_key = self._get_settings_key(context)
            im_client = self._get_im_client(context)
            settings_manager = self._get_settings_manager(context)

            if not is_dm:
                settings_manager.set_require_mention(settings_key, require_mention)

            user_settings = settings_manager.get_user_settings(settings_key)
            user_settings.show_message_types = show_message_types
            settings_manager.update_user_settings(settings_key, user_settings)

            language_saved = True
            if language is not None and language != self.config.language:
                try:
                    from config.v2_config import update_config_fields

                    update_config_fields(lambda cfg: setattr(cfg, "language", language))
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
        *,
        thread_id: Optional[str] = None,
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
                thread_id=thread_id,
                platform=platform or self.config.platform,
                platform_specific={"is_dm": is_dm, "platform": platform or self.config.platform},
            )
            im_client = self._get_im_client(context)

            resolved_backend = self.controller.resolve_agent_for_context(context)
            selected_backend = selection.selected_backend or resolved_backend
            routing_data = await self._gather_routing_modal_data(context, selected_backend=selected_backend)
            visible_selected_backend = (
                selected_backend
                if selected_backend in routing_data.registered_backends
                else routing_data.current_backend
            )

            if hasattr(im_client, "update_routing_modal"):
                await im_client.update_routing_modal(  # type: ignore[attr-defined]
                    view_id=view_id,
                    view_hash=view_hash,
                    channel_id=resolved_channel_id,
                    routing=routing_data,
                    selected_backend=visible_selected_backend,
                    # A backend switch shows a section the form had no controls for: the stored route, not blanks.
                    draft=selection.draft if visible_selected_backend == selected_backend else None,
                )
        except Exception as e:
            logger.error(f"Error updating routing modal: {e}", exc_info=True)

    async def handle_routing_update(
        self,
        user_id: str,
        channel_id: str,
        backend: str,
        *,
        variant: object = _UNSET,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        thread_id: Optional[str] = None,
        notify_user: bool = True,
        is_dm: bool = False,
        platform: Optional[str] = None,
    ):
        """Save a routing form's choices for one backend.

        ``variant`` is the backend's native agent; omitted, the stored one stays. Other backends' native agents
        always stay, so switching back finds them.
        """
        from config.v2_settings import RoutingSettings
        from modules.agents.opencode.utils import normalize_claude_reasoning_effort

        try:
            context = MessageContext(
                user_id=user_id,
                channel_id=channel_id if channel_id else user_id,
                thread_id=thread_id,
                platform=platform or self.config.platform,
                platform_specific={"is_dm": is_dm, "platform": platform or self.config.platform},
            )
            settings_key = self._get_settings_key(context)
            im_client = self._get_im_client(context)
            settings_manager = self._get_settings_manager(context)
            existing_routing = settings_manager.get_channel_routing(settings_key)
            # A platform shows a stored choice it cannot carry as its own option value through a stand-in option;
            # saving that option keeps the stored choice.
            stored = normalize_routing_settings(existing_routing)
            if model == STORED_OPTION_VALUE:
                model = stored.model
            if reasoning_effort == STORED_OPTION_VALUE:
                reasoning_effort = stored.reasoning_effort
            if variant == STORED_OPTION_VALUE:
                variant = _UNSET
            variants = {
                native: getattr(existing_routing, f"{native}_agent", None) if existing_routing else None
                for native in NATIVE_CLI_BACKENDS
            }
            if backend in variants and variant is not _UNSET:
                variants[backend] = variant
            if backend == "claude":
                # Claude Code rejects an effort its model does not support.
                reasoning_effort = normalize_claude_reasoning_effort(
                    model,
                    reasoning_effort,
                    backend_model_catalog.catalog_reasoning_efforts_for_model("claude", model),
                )

            routing = RoutingSettings(
                agent_name=self._routing_agent_name(backend),
                model=model,
                reasoning_effort=reasoning_effort,
                **{f"{native}_agent": value for native, value in variants.items()},
            )

            settings_manager.set_channel_routing(settings_key, routing)
            needs_new_session_hint = self._routing_update_needs_new_session_hint(context, routing)

            parts = [f"{self._t('routing.label.backend')}: **{display_name_for_backend(backend)}**"]
            for label_key, value in (
                ("routing.label.agent", variants.get(backend)),
                ("routing.label.model", model),
                ("routing.label.reasoningEffort", reasoning_effort),
            ):
                if value:
                    parts.append(f"{self._t(label_key)}: **{value}**")
            if needs_new_session_hint:
                parts.extend(["", self._t("success.routingUpdateNeedsNewSession")])

            if notify_user:
                await im_client.send_message(
                    context,
                    f"✅ {self._t('success.routingUpdated')}\n" + "\n".join(parts),
                    parse_mode="markdown",
                )

            logger.info(
                f"Routing updated for {settings_key}: backend={backend}, variant={variants.get(backend)}, "
                f"model={model}, reasoning_effort={reasoning_effort}"
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
