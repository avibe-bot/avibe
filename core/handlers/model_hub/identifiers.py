"""Stable Model Hub model identifiers shared by API and backend lanes."""

from __future__ import annotations

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
