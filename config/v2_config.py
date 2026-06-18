import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import List, Literal, Optional, Union

from config import paths
from config.platform_registry import (
    WORKBENCH_PLATFORM_ID,
    get_platform_descriptor,
    is_workbench_platform,
    platform_catalog_payload,
    platform_descriptors,
    supported_platform_ids,
    supported_platform_set,
)
from modules.agents.catalog import DEFAULT_AGENT_BACKEND
from modules.im.base import BaseIMConfig
from vibe.i18n import normalize_language

logger = logging.getLogger(__name__)

CONFIG_LOCK = threading.RLock()

DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS = 600
DEFAULT_OPENCODE_ERROR_RETRY_LIMIT = 1
DEFAULT_CHAT_MESSAGE_FONT_SIZE_PX = 14
MIN_CHAT_MESSAGE_FONT_SIZE_PX = 12
MAX_CHAT_MESSAGE_FONT_SIZE_PX = 20


def _filter_dataclass_fields(dc_class, payload: dict) -> dict:
    """Filter payload to only include fields defined in dataclass."""
    valid_fields = {f.name for f in fields(dc_class)}
    return {k: v for k, v in payload.items() if k in valid_fields}


@dataclass
class SlackConfig(BaseIMConfig):
    bot_token: str = ""
    app_token: Optional[str] = None
    signing_secret: Optional[str] = None
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    app_id: Optional[str] = None
    require_mention: bool = False
    disable_link_unfurl: bool = False

    def validate(self) -> None:
        # Allow empty token for initial setup
        if self.bot_token and not self.bot_token.startswith("xoxb-"):
            raise ValueError("Invalid Slack bot token format (should start with xoxb-)")
        if self.app_token and not self.app_token.startswith("xapp-"):
            raise ValueError("Invalid Slack app token format (should start with xapp-)")


@dataclass
class DiscordConfig(BaseIMConfig):
    bot_token: str = ""
    application_id: Optional[str] = None
    # Legacy input fields. Runtime server access is stored in settings.json
    # under scopes.guild.discord so it stays with channel/user scope settings.
    guild_allowlist: Optional[List[str]] = None
    guild_denylist: Optional[List[str]] = None
    require_mention: bool = False
    # Auto-archive duration (minutes) for threads created by vibe-remote.
    # Discord only accepts 60, 1440, 4320, or 10080 (1h / 1d / 3d / 7d).
    # Defaults to 10080 (7d) to match Discord's longest native inactivity window
    # rather than aggressively archiving idle sessions after 1 hour.
    thread_auto_archive_minutes: int = 10080

    def validate(self) -> None:
        # Allow empty token for initial setup
        if self.bot_token and len(self.bot_token.strip()) < 10:
            raise ValueError("Invalid Discord bot token format")
        allowed_archive = {60, 1440, 4320, 10080}
        if self.thread_auto_archive_minutes not in allowed_archive:
            raise ValueError(
                "Invalid Discord thread_auto_archive_minutes "
                f"{self.thread_auto_archive_minutes!r}; must be one of "
                f"{sorted(allowed_archive)}"
            )


@dataclass
class TelegramConfig(BaseIMConfig):
    bot_token: str = ""
    require_mention: bool = True
    forum_auto_topic: bool = True
    use_webhook: bool = False
    webhook_url: Optional[str] = None
    webhook_secret_token: Optional[str] = None
    allowed_chat_ids: Optional[List[str]] = None
    allowed_user_ids: Optional[List[str]] = None

    def validate(self) -> None:
        # Allow empty token for initial setup
        if self.bot_token and ":" not in self.bot_token:
            raise ValueError("Invalid Telegram bot token format")


@dataclass
class LarkConfig(BaseIMConfig):
    app_id: str = ""
    app_secret: str = ""
    require_mention: bool = False
    domain: str = "feishu"  # "feishu" for domestic (open.feishu.cn), "lark" for international (open.larksuite.com)

    def validate(self) -> None:
        if self.domain not in ("feishu", "lark"):
            raise ValueError(f"Invalid lark domain: {self.domain!r}. Must be 'feishu' or 'lark'.")

    @property
    def api_base_url(self) -> str:
        """Return the base API URL for the configured domain."""
        if self.domain == "lark":
            return "https://open.larksuite.com"
        return "https://open.feishu.cn"


@dataclass
class WeChatConfig(BaseIMConfig):
    bot_token: str = ""
    base_url: str = "https://ilinkai.weixin.qq.com"
    cdn_base_url: str = "https://novac2c.cdn.weixin.qq.com/c2c"
    require_mention: bool = False  # unused for WeChat DM-only, kept for interface compat

    def validate(self) -> None:
        # bot_token can be empty during setup wizard (filled after QR login)
        pass


@dataclass
class AvibeConfig(BaseIMConfig):
    """Avibe — Vibe Remote's own Web UI surfaced as a first-class IM platform.

    Runs in-process; no remote credentials. ``enabled`` lets headless
    deployments skip the workbench surface entirely while keeping the
    other IM-bridge platforms (Slack/Discord/...) wired up.
    """

    enabled: bool = True

    def validate(self) -> None:
        return None


@dataclass
class GatewayConfig:
    relay_url: Optional[str] = None
    workspace_token: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    last_connected_at: Optional[str] = None


@dataclass
class AudioAsrConfig:
    enabled: bool = True
    echo_transcript: bool = True
    enabled_configured: bool = False
    timeout_seconds: float = 60.0
    endpoint_path: str = "/v1/audio/transcriptions"
    model: str = "qwen3-asr-flash"
    max_file_bytes: Optional[int] = None


@dataclass
class RuntimeConfig:
    default_cwd: str
    log_level: str = "INFO"


@dataclass
class OpenCodeConfig:
    enabled: bool = True
    cli_path: str = "opencode"
    default_agent: Optional[str] = None
    default_model: Optional[str] = None
    default_reasoning_effort: Optional[str] = None
    error_retry_limit: int = DEFAULT_OPENCODE_ERROR_RETRY_LIMIT  # Max retries on LLM stream errors (0 = no retry)
    # Provider the user picked in Settings → Backends → OpenCode. The provider
    # catalog itself lives in ~/.config/opencode/opencode.json (OpenCode's own
    # state file). Stays ``None`` until the user explicitly chooses so legacy
    # installs (e.g. Ollama/OpenAI users) keep falling back to OpenCode's own
    # routing for bare-model strings instead of being silently rerouted to
    # Anthropic on upgrade.
    default_provider: Optional[str] = None


@dataclass
class ClaudeConfig:
    enabled: bool = True
    cli_path: str = "claude"
    default_model: Optional[str] = None
    idle_timeout_seconds: int = DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS
    # Auth model: "oauth" relies on Claude Code's own credential storage;
    # "api_key" injects ANTHROPIC_API_KEY (and optionally ANTHROPIC_BASE_URL)
    # at CLI launch time for API gateway / proxy setups.
    auth_mode: Literal["oauth", "api_key"] = "oauth"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    # ``True`` once the user has saved a Claude auth choice through the
    # Settings UI (or removed the API key, or signed out). Legacy installs
    # — V2 configs that predate the Settings page or have never touched
    # it — load with ``False`` because the field defaults to ``False`` and
    # isn't in their on-disk JSON. ``build_claude_subprocess_env`` reads
    # this to decide whether to honor ``auth_mode`` strictly (strip
    # inherited ``ANTHROPIC_*`` env in OAuth mode) or preserve the
    # legacy env-var-only auth path. Without this flag the schema's
    # ``auth_mode == "oauth"`` default is indistinguishable between
    # "explicit OAuth pick" and "user has never opened Settings".
    auth_mode_set: bool = False


@dataclass
class CodexConfig:
    enabled: bool = True
    cli_path: str = "codex"
    default_model: Optional[str] = None
    idle_timeout_seconds: int = DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS
    # Auth model: "oauth" defers to whatever ~/.codex/config.toml already
    # has (typically `auth.method = "ChatGPT"`); "api_key" writes the
    # config.toml fields that point Codex at an API key + custom base URL.
    auth_mode: Literal["oauth", "api_key"] = "oauth"
    api_key: Optional[str] = None
    base_url: Optional[str] = None


@dataclass
class AgentsConfig:
    default_backend: str = DEFAULT_AGENT_BACKEND
    opencode: OpenCodeConfig = field(default_factory=OpenCodeConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)


@dataclass
class UiConfig:
    setup_host: str = "127.0.0.1"
    setup_port: int = 5123
    open_browser: bool = True
    chat_message_font_size: int = DEFAULT_CHAT_MESSAGE_FONT_SIZE_PX


@dataclass
class VibeCloudRemoteAccessConfig:
    enabled: bool = False
    backend_url: str = "https://avibe.bot"
    public_url: str = ""
    instance_id: str = ""
    client_id: str = ""
    issuer: str = ""
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    jwks_uri: str = ""
    redirect_uri: str = ""
    tunnel_token: str = ""
    instance_secret: str = ""
    session_secret: str = ""
    cloudflared_path: str = ""
    dev_login_hint: str = ""


@dataclass
class RemoteAccessConfig:
    provider: str = "vibe_cloud"
    vibe_cloud: VibeCloudRemoteAccessConfig = field(default_factory=VibeCloudRemoteAccessConfig)


@dataclass
class UpdateConfig:
    """Configuration for automatic update checking and installation."""

    auto_update: bool = True  # Auto-install updates when idle
    check_interval_minutes: int = 60  # How often to check for updates (0 = disable)
    idle_minutes: int = 30  # Minutes of inactivity before auto-update
    notify_admins: bool = True  # Send update notification to admins when update is available


@dataclass
class PlatformsConfig:
    """Multi-platform enablement metadata.

    ``primary`` remains the compatibility anchor for legacy single-platform
    code paths while ``enabled`` is the new source of truth.
    """

    enabled: list[str] = field(default_factory=lambda: ["slack"])
    primary: str = "slack"

    def validate(self) -> None:
        supported = supported_platform_set()
        normalized: list[str] = []
        for platform in self.enabled:
            if platform not in supported:
                raise ValueError(f"Unsupported enabled platform: {platform}")
            # The in-process workbench is never an enabled IM transport — the
            # controller wires it directly. Strip it from ``enabled`` so a
            # legacy/hand-edited config can't crash the IM factory or strand the
            # primary when a real IM is also enabled.
            if is_workbench_platform(platform):
                continue
            if platform not in normalized:
                normalized.append(platform)
        if not normalized:
            # Workbench-only install: no external IM platform is enabled. The
            # Avibe Workbench (in-process Web UI) is the sole inbound surface,
            # so anchor ``primary`` to it instead of force-inserting a real IM.
            # ``avibe`` is a registered platform but is intentionally NOT added
            # to ``enabled`` — it has no remote runtime and the controller wires
            # it as the in-process client directly (see ``_init_modules``).
            self.primary = WORKBENCH_PLATFORM_ID
            self.enabled = []
            return
        if is_workbench_platform(self.primary):
            # Real IM platforms are enabled, so the workbench can't be the
            # primary transport — retarget to the first real platform.
            self.primary = normalized[0]
        elif self.primary not in supported:
            supported_text = "', '".join(supported_platform_ids())
            raise ValueError(f"Config 'platforms.primary' must be one of: '{supported_text}'")
        elif self.primary not in normalized:
            # ``enabled`` is the source of truth and ``primary`` is now an
            # internal default with no user-facing control. A primary that is
            # not in the enabled set (e.g. a stale value surviving a deep config
            # merge after the platform was disabled) must FOLLOW enabled, not
            # resurrect a removed platform by forcing itself back into the list.
            self.primary = normalized[0]
        self.enabled = normalized


@dataclass
class V2Config:
    mode: str
    version: str
    slack: SlackConfig
    runtime: RuntimeConfig
    agents: AgentsConfig
    platform: str = "slack"
    platforms: PlatformsConfig = field(default_factory=PlatformsConfig)
    discord: Optional[DiscordConfig] = None
    telegram: Optional[TelegramConfig] = None
    lark: Optional[LarkConfig] = None
    wechat: Optional[WeChatConfig] = None
    # Always present: Avibe is in-process and has no credentials, so legacy
    # configs that pre-date the platform still get a usable adapter.
    avibe: AvibeConfig = field(default_factory=AvibeConfig)
    platform_configs: dict[str, BaseIMConfig] = field(default_factory=dict)
    gateway: Optional[GatewayConfig] = None
    ui: UiConfig = field(default_factory=UiConfig)
    remote_access: RemoteAccessConfig = field(default_factory=RemoteAccessConfig)
    audio_asr: AudioAsrConfig = field(default_factory=AudioAsrConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)
    ack_mode: str = "typing"
    show_duration: bool = False  # Show task duration in result messages
    include_time_info: bool = True  # Prepend current local time to agent messages
    include_user_info: bool = True  # Prepend user identity to agent messages
    reply_enhancements: bool = True  # Enable quick-reply buttons
    show_pages_prompt: bool = True  # Inject Show Pages capability guidance into agent prompts
    language: str = "en"  # Global language setting (see vibe/i18n)
    # True once the user has finished the setup wizard. This is the explicit
    # gate for ``setup_state().needs_setup`` — it replaces the old heuristic
    # that inferred "setup done" from having a mode plus configured platform
    # credentials (which forced credential-less / workbench-only installs back
    # into the wizard). Legacy configs that predate the flag have it derived in
    # ``from_payload`` from the old condition.
    setup_completed: bool = False

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "V2Config":
        paths.ensure_data_dirs()
        path = config_path or paths.get_config_path()
        with CONFIG_LOCK:
            if not path.exists():
                raise FileNotFoundError(f"Config not found: {path}")
            payload = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_payload(payload)

    @classmethod
    def from_payload(cls, payload: dict) -> "V2Config":
        if not isinstance(payload, dict):
            raise ValueError("Config payload must be an object")

        mode = payload.get("mode")
        if mode not in {"self_host", "saas"}:
            raise ValueError("Config 'mode' must be 'self_host' or 'saas'")

        platform = payload.get("platform") or "slack"
        try:
            get_platform_descriptor(platform)
        except ValueError as err:
            supported_text = "', '".join(supported_platform_ids())
            raise ValueError(f"Config 'platform' must be one of: '{supported_text}'") from err

        platforms_payload = payload.get("platforms")
        if platforms_payload is not None and not isinstance(platforms_payload, dict):
            raise ValueError("Config 'platforms' must be an object")
        if platforms_payload:
            platforms = PlatformsConfig(
                enabled=list(platforms_payload.get("enabled") or []),
                primary=platforms_payload.get("primary") or platform,
            )
        else:
            platforms = PlatformsConfig(enabled=[platform], primary=platform)
        # When the caller explicitly set 'platform' but did not provide
        # 'platforms', treat it as a legacy single-platform update and
        # sync the new structure so that the old field is not silently
        # overridden by a stale 'platforms' value from a prior merge.
        if "platform" in payload and "platforms" not in payload:
            platforms = PlatformsConfig(enabled=[platform], primary=platform)
        platforms.validate()
        platform = platforms.primary

        platform_configs: dict[str, Optional[BaseIMConfig]] = {}
        for descriptor in platform_descriptors():
            platform_payload = payload.get(descriptor.config_key)
            if descriptor.id == "slack":
                platform_payload = platform_payload or {}
                if isinstance(platform_payload, dict) and "require_mention" not in platform_payload:
                    platform_payload = dict(platform_payload)
                    platform_payload["require_mention"] = False
            if platform_payload is not None and not isinstance(platform_payload, dict):
                raise ValueError(f"Config '{descriptor.config_key}' must be an object")
            if platform_payload is None:
                platform_configs[descriptor.id] = None
                continue

            platform_configs[descriptor.id] = descriptor.create_config(platform_payload)

        # Avibe runs in-process with no credentials — auto-populate its
        # config when missing so legacy ``platforms.enabled`` lists that
        # mention "avibe" don't trip the validation loop below.
        if platform_configs.get("avibe") is None:
            platform_configs["avibe"] = AvibeConfig()

        # Validate that every enabled platform has its config section present.
        for _ep in platforms.enabled:
            descriptor = get_platform_descriptor(_ep)
            if platform_configs[descriptor.id] is None:
                raise ValueError(f"Config '{descriptor.config_key}' must be provided when {_ep} is enabled")

        gateway_payload = payload.get("gateway")
        if gateway_payload is not None and not isinstance(gateway_payload, dict):
            raise ValueError("Config 'gateway' must be an object")
        gateway = GatewayConfig(**_filter_dataclass_fields(GatewayConfig, gateway_payload)) if gateway_payload else None

        runtime_payload = payload.get("runtime")
        if not isinstance(runtime_payload, dict):
            raise ValueError("Config 'runtime' must be an object")
        runtime = RuntimeConfig(**_filter_dataclass_fields(RuntimeConfig, runtime_payload))

        agents_payload = payload.get("agents")
        if not isinstance(agents_payload, dict):
            raise ValueError("Config 'agents' must be an object")

        opencode_payload = agents_payload.get("opencode") or {}
        if not isinstance(opencode_payload, dict):
            raise ValueError("Config 'agents.opencode' must be an object")

        claude_payload = agents_payload.get("claude") or {}
        if not isinstance(claude_payload, dict):
            raise ValueError("Config 'agents.claude' must be an object")

        codex_payload = agents_payload.get("codex") or {}
        if not isinstance(codex_payload, dict):
            raise ValueError("Config 'agents.codex' must be an object")

        opencode = OpenCodeConfig(**_filter_dataclass_fields(OpenCodeConfig, opencode_payload))
        claude = ClaudeConfig(**_filter_dataclass_fields(ClaudeConfig, claude_payload))
        codex = CodexConfig(**_filter_dataclass_fields(CodexConfig, codex_payload))

        agents = AgentsConfig(
            opencode=opencode,
            claude=claude,
            codex=codex,
        )

        ui_payload = payload.get("ui") or {}
        if not isinstance(ui_payload, dict):
            raise ValueError("Config 'ui' must be an object")
        ui = UiConfig(**_filter_dataclass_fields(UiConfig, ui_payload))
        try:
            ui.chat_message_font_size = max(
                MIN_CHAT_MESSAGE_FONT_SIZE_PX,
                min(MAX_CHAT_MESSAGE_FONT_SIZE_PX, int(ui.chat_message_font_size)),
            )
        except (TypeError, ValueError):
            ui.chat_message_font_size = DEFAULT_CHAT_MESSAGE_FONT_SIZE_PX

        remote_access_payload = payload.get("remote_access") or {}
        if not isinstance(remote_access_payload, dict):
            raise ValueError("Config 'remote_access' must be an object")
        remote_access_provider = remote_access_payload.get("provider") or "vibe_cloud"
        if remote_access_provider != "vibe_cloud":
            raise ValueError("Config 'remote_access.provider' must be 'vibe_cloud'")
        vibe_cloud_payload = remote_access_payload.get("vibe_cloud") or {}
        if not isinstance(vibe_cloud_payload, dict):
            raise ValueError("Config 'remote_access.vibe_cloud' must be an object")
        remote_access = RemoteAccessConfig(
            provider=remote_access_provider,
            vibe_cloud=VibeCloudRemoteAccessConfig(
                **_filter_dataclass_fields(VibeCloudRemoteAccessConfig, vibe_cloud_payload)
            ),
        )

        audio_asr_payload = payload.get("audio_asr") or {}
        if not isinstance(audio_asr_payload, dict):
            raise ValueError("Config 'audio_asr' must be an object")
        audio_asr_enabled_present = "enabled" in audio_asr_payload
        audio_asr = AudioAsrConfig(**_filter_dataclass_fields(AudioAsrConfig, audio_asr_payload))
        if audio_asr_enabled_present and audio_asr.enabled is False and not audio_asr.enabled_configured:
            audio_asr.enabled_configured = True
        try:
            audio_asr.timeout_seconds = max(0.1, float(audio_asr.timeout_seconds))
        except (TypeError, ValueError):
            audio_asr.timeout_seconds = 60.0
        if audio_asr.max_file_bytes is not None:
            try:
                audio_asr.max_file_bytes = max(1, int(audio_asr.max_file_bytes))
            except (TypeError, ValueError):
                audio_asr.max_file_bytes = None
        if not isinstance(audio_asr.endpoint_path, str) or not audio_asr.endpoint_path.startswith("/"):
            audio_asr.endpoint_path = "/v1/audio/transcriptions"
        if not isinstance(audio_asr.model, str) or not audio_asr.model.strip():
            audio_asr.model = "qwen3-asr-flash"

        update_payload = payload.get("update") or {}
        if not isinstance(update_payload, dict):
            raise ValueError("Config 'update' must be an object")
        # Backward compat: rename legacy "notify_slack" → "notify_admins"
        if "notify_slack" in update_payload and "notify_admins" not in update_payload:
            update_payload["notify_admins"] = update_payload.pop("notify_slack")
        update = UpdateConfig(**_filter_dataclass_fields(UpdateConfig, update_payload))

        ack_mode = payload.get("ack_mode", "typing")
        if ack_mode not in {"reaction", "message", "typing"}:
            raise ValueError("Config 'ack_mode' must be 'reaction', 'message', or 'typing'")

        show_duration = payload.get("show_duration", False)
        if not isinstance(show_duration, bool):
            show_duration = False

        include_user_info = payload.get("include_user_info", True)
        if not isinstance(include_user_info, bool):
            include_user_info = True

        include_time_info = payload.get("include_time_info", True)
        if not isinstance(include_time_info, bool):
            include_time_info = True

        reply_enhancements = payload.get("reply_enhancements", True)
        if not isinstance(reply_enhancements, bool):
            reply_enhancements = True

        show_pages_prompt = payload.get("show_pages_prompt", True)
        if not isinstance(show_pages_prompt, bool):
            show_pages_prompt = True

        language = normalize_language(payload.get("language"), default="en")

        # ``setup_completed`` is the explicit setup gate. Read the stored value
        # when present; otherwise leave it ``None`` here and derive it below
        # from the legacy "setup done" heuristic so installs configured before
        # this flag existed are not bounced back into the wizard.
        setup_completed_raw = payload.get("setup_completed")
        setup_completed = setup_completed_raw if isinstance(setup_completed_raw, bool) else None

        config = cls(
            platform=platform,
            platforms=platforms,
            mode=mode,
            version=payload.get("version", "v2"),
            slack=platform_configs["slack"],
            discord=platform_configs["discord"],
            telegram=platform_configs["telegram"],
            lark=platform_configs["lark"],
            wechat=platform_configs["wechat"],
            # Default Avibe to an enabled instance when the payload is missing
            # the section — legacy configs predate the platform.
            avibe=platform_configs.get("avibe") or AvibeConfig(),
            platform_configs={key: value for key, value in platform_configs.items() if value is not None},
            runtime=runtime,
            agents=agents,
            gateway=gateway,
            ui=ui,
            remote_access=remote_access,
            audio_asr=audio_asr,
            update=update,
            ack_mode=ack_mode,
            show_duration=show_duration,
            include_time_info=include_time_info,
            include_user_info=include_user_info,
            reply_enhancements=reply_enhancements,
            show_pages_prompt=show_pages_prompt,
            language=language,
        )

        # Migration: when the payload predates ``setup_completed``, derive it
        # from the legacy heuristic (a mode plus at least one configured
        # platform). Only derive when the key is absent; an explicitly stored
        # value always wins.
        if setup_completed is None:
            setup_completed = bool(config.mode) and bool(config.configured_platforms())
        config.setup_completed = setup_completed

        return config

    def save(self, config_path: Optional[Path] = None) -> None:
        paths.ensure_data_dirs()
        path = config_path or paths.get_config_path()
        self.platforms.validate()
        self.platform = self.platforms.primary
        platform_payload = {}
        for descriptor in platform_descriptors():
            descriptor_config = descriptor.get_config(self)
            config_payload = descriptor_config.__dict__.copy() if descriptor_config else None
            if descriptor.id == "discord" and isinstance(config_payload, dict):
                if not config_payload.get("guild_allowlist") and not config_payload.get("guild_denylist"):
                    config_payload.pop("guild_allowlist", None)
                    config_payload.pop("guild_denylist", None)
            platform_payload[descriptor.config_key] = config_payload
        payload = {
            "platform": self.platform,
            "platforms": {
                "enabled": self.platforms.enabled,
                "primary": self.platforms.primary,
            },
            "mode": self.mode,
            "version": self.version,
            **platform_payload,
            "runtime": {
                "default_cwd": self.runtime.default_cwd,
                "log_level": self.runtime.log_level,
            },
            "agents": {
                "opencode": self.agents.opencode.__dict__,
                "claude": self.agents.claude.__dict__,
                "codex": self.agents.codex.__dict__,
            },
            "gateway": self.gateway.__dict__ if self.gateway else None,
            "ui": self.ui.__dict__,
            "remote_access": {
                "provider": self.remote_access.provider,
                "vibe_cloud": self.remote_access.vibe_cloud.__dict__,
            },
            "audio_asr": self.audio_asr.__dict__,
            "update": self.update.__dict__,
            "ack_mode": self.ack_mode,
            "show_duration": self.show_duration,
            "include_time_info": self.include_time_info,
            "include_user_info": self.include_user_info,
            "reply_enhancements": self.reply_enhancements,
            "show_pages_prompt": self.show_pages_prompt,
            "language": self.language,
            "setup_completed": self.setup_completed,
        }
        content = json.dumps(payload, indent=2)
        path.parent.mkdir(parents=True, exist_ok=True)
        with CONFIG_LOCK:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
                tmp.write(content)
                tmp.flush()
                os.fsync(tmp.fileno())
                temp_name = tmp.name
            os.replace(temp_name, path)

    def enabled_platforms(self) -> list[str]:
        return list(self.platforms.enabled)

    def platform_has_credentials(self, platform: str) -> bool:
        return get_platform_descriptor(platform).has_credentials(self)

    def configured_platforms(self) -> list[str]:
        return [platform for platform in self.enabled_platforms() if self.platform_has_credentials(platform)]

    def missing_platform_credentials(self) -> list[str]:
        return [platform for platform in self.enabled_platforms() if not self.platform_has_credentials(platform)]

    def has_configured_platform_credentials(self) -> bool:
        return bool(self.configured_platforms())

    def platform_catalog(self) -> list[dict]:
        return platform_catalog_payload()

    def setup_state(self) -> dict:
        configured = self.configured_platforms()
        missing = self.missing_platform_credentials()
        return {
            "needs_setup": not self.setup_completed,
            "configured_platforms": configured,
            "missing_credentials": missing,
        }
