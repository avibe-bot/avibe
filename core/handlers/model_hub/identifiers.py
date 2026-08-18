"""Stable Model Hub model identifiers shared by API and backend lanes."""

from __future__ import annotations

# The longest model identifier the hub accepts. One constant so the boundaries
# that admit an identifier (manual add and upstream discovery) and the usage
# ledger that keys rows by it cannot disagree: a model config accepts is always
# a model usage can meter, and a persisted row can never grow without limit.
MODEL_ID_MAX_LENGTH = 200

# Vendors with native, stable OpenCode provider identifiers. Compatible relays
# and unrecognized vendors share the frozen contract's single custom/ prefix.
STANDARD_OPENCODE_VENDOR_IDS = frozenset(
    {
        "anthropic",
        "deepseek",
        "github-copilot",
        "google",
        "groq",
        "kimi",
        "minimax",
        "mistral",
        "moonshot",
        "openai",
        "openrouter",
        "together",
        "xai",
        "zhipuai",
    }
)


def opencode_provider_id(vendor: str) -> str:
    return vendor if vendor in STANDARD_OPENCODE_VENDOR_IDS else "custom"


def opencode_model_id(vendor: str, model_id: str) -> str:
    return f"{opencode_provider_id(vendor)}/{model_id}"


def parse_opencode_model_id(identifier: str) -> tuple[str, str]:
    provider, separator, model_id = identifier.partition("/")
    if not separator or not provider or not model_id:
        raise ValueError("Invalid OpenCode model identifier")
    return provider, model_id
