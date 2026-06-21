from dataclasses import dataclass, field
from typing import Optional

from config.v2_config import (
    DEFAULT_AGENT_BACKEND,
    DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_OPENCODE_ERROR_RETRY_LIMIT,
    V2Config,
    SlackConfig,
    DiscordConfig,
    TelegramConfig,
    LarkConfig,
    WeChatConfig,
)


@dataclass
class ClaudeCompatConfig:
    enabled: bool
    permission_mode: str
    cwd: str
    system_prompt: Optional[str] = None
    default_model: Optional[str] = None
    cli_path: Optional[str] = None
    idle_timeout_seconds: int = DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS
    auth_mode: str = "oauth"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    # Mirrors ``ClaudeConfig.auth_mode_set`` — see the docstring there.
    # Defaults to ``False`` so legacy installs that never saved through
    # the Settings UI keep their inherited ``ANTHROPIC_*`` env-var auth.
    auth_mode_set: bool = False

    def __post_init__(self) -> None:
        self.permission_mode = str(self.permission_mode)
        self.cwd = str(self.cwd)
        if self.cli_path is not None:
            self.cli_path = str(self.cli_path)


@dataclass
class CodexCompatConfig:
    enabled: bool
    binary: str
    extra_args: list[str]
    default_model: Optional[str] = None
    idle_timeout_seconds: int = DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS


@dataclass
class OpenCodeCompatConfig:
    enabled: bool
    binary: str
    port: int
    request_timeout_seconds: int
    default_model: Optional[str] = None
    default_reasoning_effort: Optional[str] = None
    error_retry_limit: int = DEFAULT_OPENCODE_ERROR_RETRY_LIMIT  # Max retries on LLM stream errors (0 = no retry)
    # User's saved default provider from Settings → Backends → OpenCode.
    # Used as the ``providerID`` when a routed model string has no ``provider/``
    # prefix (most agents.opencode model entries are bare model IDs).
    default_provider: Optional[str] = None


@dataclass
class AppCompatConfig:
    platform: str
    slack: SlackConfig
    claude: ClaudeCompatConfig
    log_level: str
    ack_mode: str
    language: str
    platforms: dict = field(default_factory=lambda: {"enabled": ["slack"], "primary": "slack"})
    discord: Optional[DiscordConfig] = None
    telegram: Optional[TelegramConfig] = None
    lark: Optional[LarkConfig] = None
    wechat: Optional[WeChatConfig] = None
    codex: Optional[CodexCompatConfig] = None
    opencode: Optional[OpenCodeCompatConfig] = None
    show_duration: bool = False
    include_time_info: bool = True
    include_user_info: bool = True
    reply_enhancements: bool = True
    default_backend: str = DEFAULT_AGENT_BACKEND

    def enabled_platforms(self) -> list[str]:
        enabled = self.platforms.get("enabled") if isinstance(self.platforms, dict) else None
        # An explicit empty list is the workbench-only signal: no external IM
        # platform is enabled and the in-process Avibe surface is wired by the
        # controller (never by the IM factory, which has no AppCompatConfig for
        # "avibe"). Mirror ``V2Config.enabled_platforms`` and return ``[]`` so
        # ``create_clients`` produces no clients. Fall back to ``[self.platform]``
        # only for legacy configs that never populated ``enabled`` at all.
        if isinstance(enabled, list):
            return [str(platform) for platform in enabled]
        return [self.platform]


def to_app_config(v2: V2Config) -> AppCompatConfig:
    claude = ClaudeCompatConfig(
        enabled=v2.agents.claude.enabled,
        permission_mode="bypassPermissions",
        cwd=v2.runtime.default_cwd,
        system_prompt=None,
        default_model=v2.agents.claude.default_model,
        cli_path=v2.agents.claude.cli_path,
        idle_timeout_seconds=v2.agents.claude.idle_timeout_seconds,
        # Forward V2Config auth fields so ``session_handler`` can inject the
        # right ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` env vars when
        # launching the Claude CLI; without this the runtime ignores values
        # saved via ``/backend/claude/auth`` and falls back to ambient env.
        auth_mode=v2.agents.claude.auth_mode,
        api_key=v2.agents.claude.api_key,
        base_url=v2.agents.claude.base_url,
        auth_mode_set=v2.agents.claude.auth_mode_set,
    )
    codex = None
    if v2.agents.codex.enabled:
        codex = CodexCompatConfig(
            enabled=True,
            binary=v2.agents.codex.cli_path,
            extra_args=[],
            default_model=v2.agents.codex.default_model,
            idle_timeout_seconds=v2.agents.codex.idle_timeout_seconds,
        )
    opencode = None
    if v2.agents.opencode.enabled:
        opencode = OpenCodeCompatConfig(
            enabled=True,
            binary=v2.agents.opencode.cli_path,
            port=4096,
            request_timeout_seconds=60,
            default_model=v2.agents.opencode.default_model,
            default_reasoning_effort=v2.agents.opencode.default_reasoning_effort,
            error_retry_limit=v2.agents.opencode.error_retry_limit,
            # Surface the user's saved provider choice so the OpenCode agent
            # adapter can prepend it as ``providerID`` for bare-model strings.
            default_provider=v2.agents.opencode.default_provider,
        )
    slack = SlackConfig(**v2.slack.__dict__)
    return AppCompatConfig(
        platform=v2.platform,
        platforms={
            "enabled": v2.platforms.enabled,
            "primary": v2.platforms.primary,
        },
        slack=slack,
        discord=v2.discord,
        telegram=v2.telegram,
        lark=v2.lark,
        wechat=v2.wechat,
        claude=claude,
        codex=codex,
        opencode=opencode,
        log_level=v2.runtime.log_level,
        ack_mode=v2.ack_mode,
        language=v2.language,
        show_duration=v2.show_duration,
        include_time_info=v2.include_time_info,
        include_user_info=v2.include_user_info,
        reply_enhancements=v2.reply_enhancements,
    )
