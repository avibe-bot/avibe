import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field, fields
from datetime import datetime
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

# Absolute-time backstop for evicting a Codex transport whose turn is stuck
# "active" forever (e.g. the ``codex app-server`` wedged or silently
# disconnected after ``turn/start`` but before ``turn/completed``, so
# ``_active_turns`` is never cleared). Without this, ``evict_idle_transports``
# treats an active turn as an ABSOLUTE veto and the wedged app-server process
# leaks until service restart (mirrors the Claude leak in #622/#623).
#
# A transport with an active turn is force-evicted once it has been idle for
# ``max(idle_timeout * MULTIPLIER, FLOOR_SECONDS)``. Set the multiplier <= 0 to
# disable the backstop entirely.
#
# TRADE-OFF: this cap is driven purely by ``last_activity`` (refreshed on every
# Codex notification), so it CANNOT distinguish a genuinely wedged turn from a
# legitimately long, fully-silent one. A single tool/MCP run or model "thinking"
# phase that emits no notifications for longer than the cap will be misjudged as
# stuck and have its transport torn down. The multiplier defaults higher than a
# typical tool-run assumption, and the floor guarantees a >= 30 min window even
# when ``idle_timeout`` is configured small, to keep that false-positive rare.
DEFAULT_CODEX_STUCK_ACTIVE_IDLE_EVICTION_MULTIPLIER = 3
DEFAULT_CODEX_STUCK_ACTIVE_IDLE_EVICTION_FLOOR_SECONDS = 1800

# Absolute-age backstop for idle eviction. A Claude session that is still
# flagged ``active`` (its per-turn receiver never released the flag, e.g. a
# long-lived receiver blocked on ``receive_messages`` with no stream EOF) is
# force-evicted once its ``last_activity`` is older than
# ``max(idle_timeout * STUCK_ACTIVE_IDLE_EVICTION_MULTIPLIER, FLOOR_SECONDS)``.
# This decouples eviction from the receiver's flag-release logic, so a
# stuck-active session can no longer pin its ~220MB ``claude`` subprocess until
# the next service restart. A genuine in-flight turn keeps touching
# ``last_activity`` (assistant/tool messages), so it normally stays well under
# this cap. Set the multiplier to 0 to disable the backstop.
#
# Trade-off: ``last_activity`` is only refreshed when an SDK message arrives.
# Because a stuck (blocked-receiver) session and a session running a single
# silent tool call are indistinguishable from ``last_activity`` alone, a real
# turn whose ONE tool invocation runs silently for longer than
# the cap would be force-evicted mid-turn. The default cap is at least 30min
# because Claude Code's Bash tool caps at 10min, so a single 30min-silent turn
# is not expected in practice; raise the multiplier if your deployment runs
# longer silent tools (e.g. long builds via custom/MCP tools that emit no
# intermediate messages).
DEFAULT_STUCK_ACTIVE_IDLE_EVICTION_MULTIPLIER = 3
DEFAULT_STUCK_ACTIVE_IDLE_EVICTION_FLOOR_SECONDS = 1800
DEFAULT_OPENCODE_ERROR_RETRY_LIMIT = 1
DEFAULT_CHAT_MESSAGE_FONT_SIZE_PX = 14
MIN_CHAT_MESSAGE_FONT_SIZE_PX = 12
MAX_CHAT_MESSAGE_FONT_SIZE_PX = 20
DEFAULT_AGENT_PROGRESS_STYLE = "off"


def _validate_optional_datetime(value: object, field_path: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Config '{field_path}' must be a date-time string or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Config '{field_path}' must be a valid date-time") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Config '{field_path}' must include a timezone")
    return value


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
    # Global default for the per-channel require_bind gate (allowed users).
    # False=any channel member may drive the agent, True=only bound users.
    # Channels whose per-channel require_bind is None inherit this value.
    require_bind: bool = False
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
    # Global default for the per-channel require_bind gate (allowed users).
    require_bind: bool = False
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
    # Global default for the per-channel require_bind gate (allowed users).
    require_bind: bool = False
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
    # Global default for the per-channel require_bind gate (allowed users).
    require_bind: bool = False
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
    require_bind: bool = False  # unused for WeChat DM-only, kept for interface compat

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
    # Linux/cgroup v2 best-effort resource governance for aggregate agent
    # workload. "auto" enables it only when Avibe can create and write the
    # delegated cgroup; unsupported systems silently fall back to legacy spawn.
    resource_governance: dict = field(default_factory=lambda: {"mode": "auto"})


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
class AVaultConfig:
    cli_path: str = "avault"


@dataclass
class AgentsConfig:
    default_backend: str = DEFAULT_AGENT_BACKEND
    opencode: OpenCodeConfig = field(default_factory=OpenCodeConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    avault: AVaultConfig = field(default_factory=AVaultConfig)


@dataclass
class ModelHubModelConfig:
    id: str
    provenance: Literal["discovered", "manual"]
    display_name: Optional[str] = None
    discovered_at: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: dict) -> "ModelHubModelConfig":
        if not isinstance(payload, dict):
            raise ValueError("Config 'model_hub.sources.models' entries must be objects")
        model_id = payload.get("id")
        provenance = payload.get("provenance")
        display_name = payload.get("display_name")
        discovered_at = payload.get("discovered_at")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("Config 'model_hub.sources.models.id' must be a non-empty string")
        if provenance not in {"discovered", "manual"}:
            raise ValueError("Config 'model_hub.sources.models.provenance' is invalid")
        if display_name is not None and not isinstance(display_name, str):
            raise ValueError("Config 'model_hub.sources.models.display_name' must be a string or null")
        return cls(
            id=model_id,
            provenance=provenance,
            display_name=display_name,
            discovered_at=_validate_optional_datetime(
                discovered_at,
                "model_hub.sources.models.discovered_at",
            ),
        )

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "provenance": self.provenance,
            "discovered_at": self.discovered_at,
        }


@dataclass
class ModelHubSourceStateConfig:
    status: Literal["active", "standby", "cooldown", "error"] = "standby"
    retry_at: Optional[str] = None
    detail_key: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: dict) -> "ModelHubSourceStateConfig":
        if not isinstance(payload, dict):
            raise ValueError("Config 'model_hub.sources.state' must be an object")
        status = payload.get("status")
        retry_at = payload.get("retry_at")
        detail_key = payload.get("detail_key")
        if status not in {"active", "standby", "cooldown", "error"}:
            raise ValueError("Config 'model_hub.sources.state.status' is invalid")
        if detail_key is not None and not isinstance(detail_key, str):
            raise ValueError("Config 'model_hub.sources.state.detail_key' must be a string or null")
        return cls(
            status=status,
            retry_at=_validate_optional_datetime(retry_at, "model_hub.sources.state.retry_at"),
            detail_key=detail_key,
        )

    def to_payload(self) -> dict:
        return {"status": self.status, "retry_at": self.retry_at, "detail_key": self.detail_key}


@dataclass
class ModelHubSourceUsageConfig:
    cycle_used_pct: Optional[float] = None
    month_spend_cents: Optional[int] = None
    currency: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: dict) -> "ModelHubSourceUsageConfig":
        if not isinstance(payload, dict):
            raise ValueError("Config 'model_hub.sources.usage' must be an object")
        cycle_used_pct = payload.get("cycle_used_pct")
        if cycle_used_pct is not None and (
            isinstance(cycle_used_pct, bool)
            or not isinstance(cycle_used_pct, (int, float))
            or not 0 <= cycle_used_pct <= 100
        ):
            raise ValueError("Config 'model_hub.sources.usage.cycle_used_pct' must be between 0 and 100")
        month_spend_cents = payload.get("month_spend_cents")
        currency = payload.get("currency")
        if month_spend_cents is not None and (
            isinstance(month_spend_cents, bool) or not isinstance(month_spend_cents, int) or month_spend_cents < 0
        ):
            raise ValueError("Config 'model_hub.sources.usage.month_spend_cents' must be a non-negative integer")
        if currency is not None and not isinstance(currency, str):
            raise ValueError("Config 'model_hub.sources.usage.currency' must be a string or null")
        return cls(
            cycle_used_pct=cycle_used_pct,
            month_spend_cents=month_spend_cents,
            currency=currency,
        )

    def to_payload(self) -> dict:
        return {
            "cycle_used_pct": self.cycle_used_pct,
            "month_spend_cents": self.month_spend_cents,
            "currency": self.currency,
        }


@dataclass
class ModelHubSourceConfig:
    id: str
    kind: Literal["subscription", "api_key"]
    vendor: str
    display_name: str
    protocol: Literal["anthropic", "openai_responses", "openai_chat", "openai_compatible"]
    supply_channel: Literal["native_cli", "hub"]
    billing: Literal["monthly", "metered"]
    state: ModelHubSourceStateConfig
    models: list[ModelHubModelConfig]
    base_url: Optional[str] = None
    experimental_consent_at: Optional[str] = None
    usage: Optional[ModelHubSourceUsageConfig] = None
    credential_ref: Optional[str] = None
    account_label: Optional[str] = None
    masked_credential: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: dict) -> "ModelHubSourceConfig":
        if not isinstance(payload, dict):
            raise ValueError("Config 'model_hub.sources' entries must be objects")
        source_id = payload.get("id")
        kind = payload.get("kind")
        vendor = payload.get("vendor")
        display_name = payload.get("display_name")
        protocol = payload.get("protocol")
        supply_channel = payload.get("supply_channel")
        billing = payload.get("billing")
        if not isinstance(source_id, str) or re.fullmatch(r"src_[a-z0-9]{8,}", source_id) is None:
            raise ValueError("Config 'model_hub.sources.id' is invalid")
        if kind not in {"subscription", "api_key"}:
            raise ValueError("Config 'model_hub.sources.kind' is invalid")
        if not isinstance(vendor, str) or not vendor:
            raise ValueError("Config 'model_hub.sources.vendor' must be a non-empty string")
        if not isinstance(display_name, str) or not display_name or len(display_name) > 64:
            raise ValueError("Config 'model_hub.sources.display_name' is invalid")
        if protocol not in {"anthropic", "openai_responses", "openai_chat", "openai_compatible"}:
            raise ValueError("Config 'model_hub.sources.protocol' is invalid")
        if supply_channel not in {"native_cli", "hub"}:
            raise ValueError("Config 'model_hub.sources.supply_channel' is invalid")
        if billing not in {"monthly", "metered"}:
            raise ValueError("Config 'model_hub.sources.billing' is invalid")
        models_payload = payload.get("models")
        if not isinstance(models_payload, list):
            raise ValueError("Config 'model_hub.sources.models' must be an array")
        usage_payload = payload.get("usage")
        base_url = payload.get("base_url")
        consent_at = payload.get("experimental_consent_at")
        credential_ref = payload.get("credential_ref")
        account_label = payload.get("account_label")
        masked_credential = payload.get("masked_credential")
        if base_url is not None and not isinstance(base_url, str):
            raise ValueError("Config 'model_hub.sources.base_url' is invalid")
        if credential_ref is not None and not isinstance(credential_ref, str):
            raise ValueError("Config 'model_hub.sources.credential_ref' is invalid")
        if account_label is not None and not isinstance(account_label, str):
            raise ValueError("Config 'model_hub.sources.account_label' is invalid")
        if masked_credential is not None and not isinstance(masked_credential, str):
            raise ValueError("Config 'model_hub.sources.masked_credential' is invalid")
        return cls(
            id=source_id,
            kind=kind,
            vendor=vendor,
            display_name=display_name,
            protocol=protocol,
            supply_channel=supply_channel,
            billing=billing,
            state=ModelHubSourceStateConfig.from_payload(payload.get("state")),
            models=[ModelHubModelConfig.from_payload(model) for model in models_payload],
            base_url=base_url,
            experimental_consent_at=_validate_optional_datetime(
                consent_at,
                "model_hub.sources.experimental_consent_at",
            ),
            usage=ModelHubSourceUsageConfig.from_payload(usage_payload) if usage_payload is not None else None,
            credential_ref=credential_ref,
            account_label=account_label,
            masked_credential=masked_credential,
        )

    def to_payload(self) -> dict:
        payload = {
            "id": self.id,
            "kind": self.kind,
            "vendor": self.vendor,
            "display_name": self.display_name,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "supply_channel": self.supply_channel,
            "billing": self.billing,
            "state": self.state.to_payload(),
            "models": [model.to_payload() for model in self.models],
            "credential_ref": self.credential_ref,
            "account_label": self.account_label,
            "masked_credential": self.masked_credential,
        }
        if self.usage is not None:
            payload["usage"] = self.usage.to_payload()
        if self.experimental_consent_at is not None:
            payload["experimental_consent_at"] = self.experimental_consent_at
        return payload


@dataclass
class ModelHubMappingConfig:
    builtin_id: str
    target_model_id: str
    enabled: bool

    @classmethod
    def from_payload(cls, payload: dict) -> "ModelHubMappingConfig":
        if not isinstance(payload, dict):
            raise ValueError("Config 'model_hub.agents.mappings' entries must be objects")
        builtin_id = payload.get("builtin_id")
        target_model_id = payload.get("target_model_id")
        enabled = payload.get("enabled")
        if not isinstance(builtin_id, str) or not builtin_id:
            raise ValueError("Config 'model_hub.agents.mappings.builtin_id' is invalid")
        if not isinstance(target_model_id, str) or not target_model_id:
            raise ValueError("Config 'model_hub.agents.mappings.target_model_id' is invalid")
        if not isinstance(enabled, bool):
            raise ValueError("Config 'model_hub.agents.mappings.enabled' must be a boolean")
        return cls(builtin_id=builtin_id, target_model_id=target_model_id, enabled=enabled)

    def to_payload(self) -> dict:
        return {
            "builtin_id": self.builtin_id,
            "target_model_id": self.target_model_id,
            "enabled": self.enabled,
        }


@dataclass
class ModelHubMenuConfig:
    view: Literal["featured", "full"] = "featured"
    checked: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict) -> "ModelHubMenuConfig":
        if not isinstance(payload, dict):
            raise ValueError("Config 'model_hub.agents.menu' must be an object")
        view = payload.get("view")
        checked = payload.get("checked")
        if view not in {"featured", "full"}:
            raise ValueError("Config 'model_hub.agents.menu.view' is invalid")
        if not isinstance(checked, list) or not all(isinstance(item, str) for item in checked):
            raise ValueError("Config 'model_hub.agents.menu.checked' must be an array of strings")
        if len(set(checked)) != len(checked):
            raise ValueError("Config 'model_hub.agents.menu.checked' must be unique")
        return cls(view=view, checked=list(checked))

    def to_payload(self) -> dict:
        return {"view": self.view, "checked": list(self.checked)}


@dataclass
class ModelHubAgentSupplyConfig:
    backend: Literal["claude", "codex", "opencode"]
    mode: Literal["hub", "direct"]
    menu_kind: Literal["fixed", "open"]
    mappings: list[ModelHubMappingConfig] = field(default_factory=list)
    menu: Optional[ModelHubMenuConfig] = None

    @classmethod
    def default(cls, backend: str, *, mode: Literal["hub", "direct"]) -> "ModelHubAgentSupplyConfig":
        if backend == "opencode":
            return cls(backend="opencode", mode=mode, menu_kind="open", menu=ModelHubMenuConfig())
        if backend not in {"claude", "codex"}:
            raise ValueError(f"Unsupported Model Hub backend: {backend}")
        return cls(backend=backend, mode=mode, menu_kind="fixed")

    @classmethod
    def from_payload(cls, payload: dict, *, expected_backend: Optional[str] = None) -> "ModelHubAgentSupplyConfig":
        if not isinstance(payload, dict):
            raise ValueError("Config 'model_hub.agents' entries must be objects")
        backend = payload.get("backend") or expected_backend
        mode = payload.get("mode")
        menu_kind = payload.get("menu_kind")
        if backend not in {"claude", "codex", "opencode"} or (
            expected_backend is not None and backend != expected_backend
        ):
            raise ValueError("Config 'model_hub.agents.backend' is invalid")
        if mode not in {"hub", "direct"}:
            raise ValueError("Config 'model_hub.agents.mode' is invalid")
        expected_menu_kind = "open" if backend == "opencode" else "fixed"
        if menu_kind != expected_menu_kind:
            raise ValueError("Config 'model_hub.agents.menu_kind' is invalid for backend")
        mappings_payload = payload.get("mappings") or []
        if not isinstance(mappings_payload, list):
            raise ValueError("Config 'model_hub.agents.mappings' must be an array")
        menu_payload = payload.get("menu")
        if backend == "opencode" and menu_payload is None:
            menu_payload = {"view": "featured", "checked": []}
        if backend != "opencode" and menu_payload is not None:
            raise ValueError("Config 'model_hub.agents.menu' is only valid for opencode")
        return cls(
            backend=backend,
            mode=mode,
            menu_kind=menu_kind,
            mappings=[ModelHubMappingConfig.from_payload(mapping) for mapping in mappings_payload],
            menu=ModelHubMenuConfig.from_payload(menu_payload) if menu_payload is not None else None,
        )

    def to_payload(self) -> dict:
        return {
            "backend": self.backend,
            "mode": self.mode,
            "menu_kind": self.menu_kind,
            "mappings": [mapping.to_payload() for mapping in self.mappings],
            "menu": self.menu.to_payload() if self.menu else None,
        }


@dataclass
class ModelHubConfig:
    sources: list[ModelHubSourceConfig] = field(default_factory=list)
    priority_order: list[str] = field(default_factory=list)
    agents: dict[str, ModelHubAgentSupplyConfig] = field(
        default_factory=lambda: {
            backend: ModelHubAgentSupplyConfig.default(backend, mode="direct")
            for backend in ("claude", "codex", "opencode")
        }
    )
    subscription_hub_experimental: bool = False

    @classmethod
    def fresh(cls) -> "ModelHubConfig":
        return cls(
            agents={
                backend: ModelHubAgentSupplyConfig.default(backend, mode="hub")
                for backend in ("claude", "codex", "opencode")
            }
        )

    @classmethod
    def from_payload(cls, payload: dict) -> "ModelHubConfig":
        if not isinstance(payload, dict):
            raise ValueError("Config 'model_hub' must be an object")
        sources_payload = payload.get("sources") or []
        priority_order = payload.get("priority_order") or []
        agents_payload = payload.get("agents") or {}
        experimental = payload.get("subscription_hub_experimental", False)
        if not isinstance(sources_payload, list):
            raise ValueError("Config 'model_hub.sources' must be an array")
        if not isinstance(priority_order, list) or not all(isinstance(item, str) for item in priority_order):
            raise ValueError("Config 'model_hub.priority_order' must be an array of strings")
        if not isinstance(agents_payload, dict):
            raise ValueError("Config 'model_hub.agents' must be an object")
        if not isinstance(experimental, bool):
            raise ValueError("Config 'model_hub.subscription_hub_experimental' must be a boolean")
        sources = [ModelHubSourceConfig.from_payload(source) for source in sources_payload]
        source_ids = [source.id for source in sources]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("Config 'model_hub.sources' contains duplicate ids")
        if len(set(priority_order)) != len(priority_order) or set(priority_order) != set(source_ids):
            raise ValueError("Config 'model_hub.priority_order' must be a permutation of source ids")
        agents = {
            backend: ModelHubAgentSupplyConfig.from_payload(
                agents_payload.get(backend)
                or ModelHubAgentSupplyConfig.default(backend, mode="direct").to_payload(),
                expected_backend=backend,
            )
            for backend in ("claude", "codex", "opencode")
        }
        for source in sources:
            if source.kind == "subscription" and source.supply_channel == "hub":
                if not experimental or not source.experimental_consent_at:
                    raise ValueError("Config hub-held subscription source requires recorded experimental consent")
            elif source.experimental_consent_at is not None:
                raise ValueError("Config experimental consent is only valid for hub-held subscription sources")
        return cls(
            sources=sources,
            priority_order=list(priority_order),
            agents=agents,
            subscription_hub_experimental=experimental,
        )

    def to_payload(self) -> dict:
        return {
            "sources": [source.to_payload() for source in self.sources],
            "priority_order": list(self.priority_order),
            "agents": {backend: self.agents[backend].to_payload() for backend in ("claude", "codex", "opencode")},
            "subscription_hub_experimental": self.subscription_hub_experimental,
        }


@dataclass
class UiConfig:
    setup_host: str = "127.0.0.1"
    setup_port: int = 5123
    open_browser: bool = True
    chat_message_font_size: int = DEFAULT_CHAT_MESSAGE_FONT_SIZE_PX
    # When true, the Web Chat renders each agent turn's intermediate activity
    # (interim ``assistant`` messages + ``tool_call`` summaries) as a collapsible
    # group, and the message mirror streams those rows live. Default off: a strict
    # no-op — the live stream and transcript stay exactly as they are today.
    show_agent_activity: bool = False
    # When true (default), the Activity panel renders tool-call rows; when false,
    # only assistant narration rows show. Pure display filter — step counts,
    # durations, and data collection are unaffected. Independent of
    # ``show_agent_activity`` (which gates the whole panel); this only filters rows
    # within it. Default on so the panel keeps today's full detail unless hidden.
    show_tool_calls: bool = True
    trusted_public_origins: List[str] = field(default_factory=list)
    # Display name appended to the browser tab title ("Avibe - <name>"). When
    # blank the UI falls back to the read-only ``default_instance_name`` field
    # in the /api/config payload (remote-access tunnel name when available,
    # otherwise the machine's system hostname).
    instance_name: str = ""


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
    model_hub: ModelHubConfig = field(default_factory=ModelHubConfig)
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
    # Progress UX for editing platforms (Slack/Discord):
    #   "off" (default) no process bubble, "concise" one self-updating bubble,
    #   "verbose" legacy append/split process log.
    agent_progress_style: str = DEFAULT_AGENT_PROGRESS_STYLE
    agent_status_heartbeat_ms: int = 8000  # status-bubble elapsed-timer heartbeat
    agent_status_no_output_ms: int = 180000  # "no output for N min" hint threshold
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

        avault_payload = agents_payload.get("avault") or {}
        if not isinstance(avault_payload, dict):
            raise ValueError("Config 'agents.avault' must be an object")

        opencode = OpenCodeConfig(**_filter_dataclass_fields(OpenCodeConfig, opencode_payload))
        claude = ClaudeConfig(**_filter_dataclass_fields(ClaudeConfig, claude_payload))
        codex = CodexConfig(**_filter_dataclass_fields(CodexConfig, codex_payload))
        avault = AVaultConfig(**_filter_dataclass_fields(AVaultConfig, avault_payload))

        agents = AgentsConfig(
            opencode=opencode,
            claude=claude,
            codex=codex,
            avault=avault,
        )

        model_hub_payload = payload.get("model_hub")
        if model_hub_payload is None:
            # Existing installs predate Model Hub and must remain in Direct mode
            # until the user explicitly migrates. Fresh defaults seed Hub mode.
            model_hub = ModelHubConfig()
        else:
            model_hub = ModelHubConfig.from_payload(model_hub_payload)

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
        # Accept real booleans; parse known string forms explicitly (a config file
        # or API client may supply "false"/"0", and ``bool("false")`` is True).
        raw_show_activity = ui.show_agent_activity
        if isinstance(raw_show_activity, str):
            ui.show_agent_activity = raw_show_activity.strip().lower() in ("1", "true", "yes", "on")
        else:
            ui.show_agent_activity = bool(raw_show_activity)
        # ``show_tool_calls`` defaults true; a string "false"/"0" must coerce to False
        # (``bool("false")`` is True) — same parse as above, applied when a string form
        # is supplied; a missing key keeps the ``UiConfig`` default (True).
        raw_show_tools = ui.show_tool_calls
        if isinstance(raw_show_tools, str):
            ui.show_tool_calls = raw_show_tools.strip().lower() in ("1", "true", "yes", "on")
        else:
            ui.show_tool_calls = bool(raw_show_tools)

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

        agent_progress_style = payload.get("agent_progress_style", DEFAULT_AGENT_PROGRESS_STYLE)
        if agent_progress_style not in ("concise", "verbose", "off"):
            agent_progress_style = DEFAULT_AGENT_PROGRESS_STYLE

        def _positive_int(value, default, maximum):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > maximum:
                return default
            return value

        # Cap to sane upper bounds so a fat-fingered value can't silence the
        # heartbeat (heartbeat ≤ 1h, no-output hint ≤ 24h); out-of-range → default.
        agent_status_heartbeat_ms = _positive_int(payload.get("agent_status_heartbeat_ms"), 8000, 3_600_000)
        agent_status_no_output_ms = _positive_int(payload.get("agent_status_no_output_ms"), 180000, 86_400_000)

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
            model_hub=model_hub,
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
            agent_progress_style=agent_progress_style,
            agent_status_heartbeat_ms=agent_status_heartbeat_ms,
            agent_status_no_output_ms=agent_status_no_output_ms,
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
                "resource_governance": self.runtime.resource_governance,
            },
            "agents": {
                "opencode": self.agents.opencode.__dict__,
                "claude": self.agents.claude.__dict__,
                "codex": self.agents.codex.__dict__,
                "avault": self.agents.avault.__dict__,
            },
            "model_hub": self.model_hub.to_payload(),
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
            "agent_progress_style": self.agent_progress_style,
            "agent_status_heartbeat_ms": self.agent_status_heartbeat_ms,
            "agent_status_no_output_ms": self.agent_status_no_output_ms,
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
