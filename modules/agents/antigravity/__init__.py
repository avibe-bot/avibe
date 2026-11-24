"""Antigravity agent backend over stdio ACP."""

from modules.agents.antigravity.agent import (
    AGY_CLI_DISABLE_AUTO_UPDATE_ENV,
    AntigravityAgent,
    AntigravityLaunchSpec,
    AntigravityResumeUnavailableError,
    auth_config_fingerprint,
    build_antigravity_command,
    build_antigravity_env,
)
from modules.agents.antigravity.models import (
    AntigravityModelSelectionError,
    AppliedModelConfig,
    apply_model_and_effort,
    resolve_requested_model_and_effort,
)
from modules.agents.antigravity.rules import (
    MAX_RULE_FILE_BYTES,
    MAX_RULE_TOTAL_BYTES,
    MAX_RULE_TOTAL_TOKENS,
    RenderedRules,
    render_session_rules,
    split_rule_content,
)

__all__ = [
    "AGY_CLI_DISABLE_AUTO_UPDATE_ENV",
    "MAX_RULE_FILE_BYTES",
    "MAX_RULE_TOTAL_BYTES",
    "MAX_RULE_TOTAL_TOKENS",
    "AntigravityAgent",
    "AntigravityLaunchSpec",
    "AntigravityModelSelectionError",
    "AntigravityResumeUnavailableError",
    "AppliedModelConfig",
    "RenderedRules",
    "apply_model_and_effort",
    "auth_config_fingerprint",
    "build_antigravity_command",
    "build_antigravity_env",
    "render_session_rules",
    "resolve_requested_model_and_effort",
    "split_rule_content",
]
