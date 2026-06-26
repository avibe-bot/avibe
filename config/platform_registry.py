"""Single source of truth for IM platform metadata and capabilities."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from importlib import import_module
from typing import Any


@dataclass(frozen=True)
class PlatformCapabilities:
    supports_channels: bool
    supports_threads: bool
    supports_buttons: bool
    supports_quick_replies: bool
    supports_message_editing: bool
    # Platform can delete a previously-sent message. Used so the status bubble can
    # be removed (rather than edited-in-place) when the final result is posted as a
    # NEW message, which is what makes IM fire a push notification for the result.
    supports_message_deletion: bool = False
    markdown_upload_returns_message_id: bool = False
    quick_reply_single_column: bool = False
    supports_typing_indicator: bool = False
    typing_indicator_requires_clear: bool = False
    typing_indicator_best_effort: bool = False
    supports_reaction_indicator: bool = False
    supports_message_indicator: bool = True
    supports_message_indicator_delete: bool = False
    preferred_processing_indicator: str = "typing"
    force_preferred_processing_indicator: bool = False
    # Platform can host the concise single status bubble (edit-in-place + subtext
    # footer) that becomes the result. One capability shared by the dispatcher and
    # the processing indicator instead of a hardcoded {"slack","discord"} literal.
    supports_status_bubble: bool = False


@dataclass(frozen=True)
class PlatformDescriptor:
    id: str
    config_key: str
    config_module: str
    config_class: str
    client_module: str
    client_class: str
    formatter_module: str
    formatter_class: str
    credential_fields: tuple[str, ...]
    capabilities: PlatformCapabilities
    runtime_reconcile_fields: tuple[str, ...] = ()
    # Structural distinction between real IM transports ("im") and the
    # always-on in-process Avibe Workbench ("workbench"), which lives in the
    # same registry but is never a configurable IM platform. IM-only code paths
    # should select via ``im_platform_descriptors()`` / ``im_platform_ids()``
    # rather than re-deriving an ad-hoc workbench exclude.
    kind: str = "im"

    @property
    def title_key(self) -> str:
        return f"platform.{self.id}.title"

    @property
    def description_key(self) -> str:
        return f"platform.{self.id}.desc"

    def get_config(self, app_config: Any) -> Any:
        platform_configs = getattr(app_config, "platform_configs", None)
        if isinstance(platform_configs, dict) and self.id in platform_configs:
            return platform_configs[self.id]
        return getattr(app_config, self.config_key, None)

    def get_config_class(self) -> type[Any]:
        return _load_attr(self.config_module, self.config_class)

    def create_config(self, payload: dict[str, Any]) -> Any:
        config_cls = self.get_config_class()
        valid_fields = {field.name for field in fields(config_cls)}
        platform_config = config_cls(**{key: value for key, value in payload.items() if key in valid_fields})
        validate = getattr(platform_config, "validate", None)
        if callable(validate):
            validate()
        return platform_config

    def has_credentials(self, app_config: Any) -> bool:
        platform_config = self.get_config(app_config)
        if platform_config is None:
            return False
        return all(bool(getattr(platform_config, field, None)) for field in self.credential_fields)

    def create_client(self, app_config: Any) -> Any:
        platform_config = self.get_config(app_config)
        if platform_config is None:
            raise ValueError(f"{self.id.title()} configuration not found")
        client_cls = _load_attr(self.client_module, self.client_class)
        return client_cls(platform_config)

    def create_formatter(self) -> Any:
        formatter_cls = _load_attr(self.formatter_module, self.formatter_class)
        return formatter_cls()

    def runtime_reconcile_field_names(self) -> tuple[str, ...]:
        return self.runtime_reconcile_fields or self.credential_fields

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "config_key": self.config_key,
            "title_key": self.title_key,
            "description_key": self.description_key,
            "credential_fields": list(self.credential_fields),
            "capabilities": asdict(self.capabilities),
            "kind": self.kind,
        }


def _load_attr(module_name: str, attr_name: str) -> Any:
    module = import_module(module_name)
    return getattr(module, attr_name)


# The in-process Avibe Workbench (Web UI), surfaced as a peer platform so it
# shares scopes/agent_sessions/routing with Slack/Discord/etc. It has no remote
# credentials and is wired by the controller, never built as an IM transport.
# Single source of truth, mirrored by the frontend (ui/src/lib/platforms.ts).
WORKBENCH_PLATFORM_ID = "avibe"


def is_workbench_platform(platform: str) -> bool:
    return platform == WORKBENCH_PLATFORM_ID


PLATFORM_REGISTRY: dict[str, PlatformDescriptor] = {
    "slack": PlatformDescriptor(
        id="slack",
        config_key="slack",
        config_module="config.v2_config",
        config_class="SlackConfig",
        client_module="modules.im.slack",
        client_class="SlackBot",
        formatter_module="modules.im.formatters",
        formatter_class="SlackFormatter",
        credential_fields=("bot_token",),
        runtime_reconcile_fields=("bot_token", "app_token", "proxy_url"),
        capabilities=PlatformCapabilities(
            supports_channels=True,
            supports_threads=True,
            supports_buttons=True,
            supports_quick_replies=True,
            supports_message_editing=True,
            supports_message_deletion=True,
            supports_typing_indicator=True,
            typing_indicator_best_effort=True,
            supports_reaction_indicator=True,
            supports_status_bubble=True,
        ),
    ),
    "discord": PlatformDescriptor(
        id="discord",
        config_key="discord",
        config_module="config.v2_config",
        config_class="DiscordConfig",
        client_module="modules.im.discord",
        client_class="DiscordBot",
        formatter_module="modules.im.formatters",
        formatter_class="DiscordFormatter",
        credential_fields=("bot_token",),
        runtime_reconcile_fields=("bot_token", "proxy_url"),
        capabilities=PlatformCapabilities(
            supports_channels=True,
            supports_threads=True,
            supports_buttons=True,
            supports_quick_replies=True,
            supports_message_editing=True,
            supports_message_deletion=True,
            markdown_upload_returns_message_id=True,
            supports_typing_indicator=True,
            supports_reaction_indicator=True,
            supports_status_bubble=True,
        ),
    ),
    "telegram": PlatformDescriptor(
        id="telegram",
        config_key="telegram",
        config_module="config.v2_config",
        config_class="TelegramConfig",
        client_module="modules.im.telegram",
        client_class="TelegramBot",
        formatter_module="modules.im.formatters",
        formatter_class="TelegramFormatter",
        credential_fields=("bot_token",),
        runtime_reconcile_fields=("bot_token", "use_webhook", "webhook_url", "webhook_secret_token", "proxy_url"),
        capabilities=PlatformCapabilities(
            supports_channels=True,
            supports_threads=False,
            supports_buttons=True,
            supports_quick_replies=True,
            supports_message_editing=True,
            supports_message_deletion=True,
            markdown_upload_returns_message_id=True,
            quick_reply_single_column=True,
            supports_typing_indicator=True,
            supports_reaction_indicator=True,
            supports_message_indicator_delete=True,
        ),
    ),
    "lark": PlatformDescriptor(
        id="lark",
        config_key="lark",
        config_module="config.v2_config",
        config_class="LarkConfig",
        client_module="modules.im.feishu",
        client_class="FeishuBot",
        formatter_module="modules.im.formatters",
        formatter_class="FeishuFormatter",
        credential_fields=("app_id", "app_secret"),
        runtime_reconcile_fields=("app_id", "app_secret", "domain", "proxy_url"),
        capabilities=PlatformCapabilities(
            supports_channels=True,
            supports_threads=True,
            supports_buttons=True,
            supports_quick_replies=True,
            supports_message_editing=True,
            markdown_upload_returns_message_id=True,
            quick_reply_single_column=True,
            supports_reaction_indicator=True,
            preferred_processing_indicator="reaction",
        ),
    ),
    "wechat": PlatformDescriptor(
        id="wechat",
        config_key="wechat",
        config_module="config.v2_config",
        config_class="WeChatConfig",
        client_module="modules.im.wechat",
        client_class="WeChatBot",
        formatter_module="modules.im.formatters",
        formatter_class="WeChatFormatter",
        credential_fields=("bot_token",),
        runtime_reconcile_fields=("bot_token", "base_url", "cdn_base_url", "proxy_url"),
        capabilities=PlatformCapabilities(
            supports_channels=False,
            supports_threads=False,
            supports_buttons=False,
            supports_quick_replies=False,
            supports_message_editing=False,
            supports_typing_indicator=True,
            typing_indicator_requires_clear=True,
            supports_reaction_indicator=False,
            preferred_processing_indicator="typing",
            force_preferred_processing_indicator=True,
        ),
    ),
    # The Vibe Remote Web UI itself, surfaced as a peer platform so the
    # workbench shares ``scopes`` / ``agent_sessions`` / routing machinery
    # with Slack/Discord/etc. The adapter has no remote credentials —
    # transport is REST + SSE hosted by ``vibe/ui_server.py`` (wired in
    # later commits).
    "avibe": PlatformDescriptor(
        id="avibe",
        config_key="avibe",
        config_module="config.v2_config",
        config_class="AvibeConfig",
        client_module="modules.im.avibe",
        client_class="AvibeBot",
        formatter_module="modules.im.formatters",
        formatter_class="AvibeFormatter",
        kind="workbench",
        credential_fields=(),
        capabilities=PlatformCapabilities(
            supports_channels=True,
            supports_threads=True,
            supports_buttons=True,
            supports_quick_replies=True,
            supports_message_editing=True,
            markdown_upload_returns_message_id=True,
            supports_typing_indicator=True,
            supports_reaction_indicator=True,
        ),
    ),
}


def platform_descriptors() -> list[PlatformDescriptor]:
    return list(PLATFORM_REGISTRY.values())


def im_platform_descriptors() -> list[PlatformDescriptor]:
    """Descriptors for real IM transports only (excludes the workbench)."""
    return [descriptor for descriptor in PLATFORM_REGISTRY.values() if descriptor.kind == "im"]


def im_platform_ids() -> list[str]:
    """Ids of real IM transports only (excludes the workbench)."""
    return [descriptor.id for descriptor in im_platform_descriptors()]


def supported_platform_ids() -> list[str]:
    return list(PLATFORM_REGISTRY.keys())


def supported_platform_set() -> set[str]:
    return set(PLATFORM_REGISTRY)


def get_platform_descriptor(platform: str) -> PlatformDescriptor:
    try:
        return PLATFORM_REGISTRY[platform]
    except KeyError as err:
        raise ValueError(f"Unsupported platform: {platform}") from err


def platform_catalog_payload() -> list[dict[str, Any]]:
    return [descriptor.to_public_dict() for descriptor in platform_descriptors()]
