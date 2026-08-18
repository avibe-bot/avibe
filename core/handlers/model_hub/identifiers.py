"""Stable Model Hub model identifiers shared by API and backend lanes."""

from __future__ import annotations

# The longest model identifier the hub accepts. One constant so the boundaries
# that admit an identifier (manual add and upstream discovery) and the usage
# ledger that keys rows by it cannot disagree: a model config accepts is always
# a model usage can meter, and a persisted row can never grow without limit.
MODEL_ID_MAX_LENGTH = 200


def canonical_model_id(value: object) -> str | None:
    """Reduce one candidate identifier to its canonical form, or reject it.

    A model ID is an identity: it is compared, stored in config, sent upstream,
    and used as a usage-ledger key. Those uses only agree if every one of them
    means the same thing by "the same model", so the canonical form is decided
    once here and the boundaries that admit an identifier store what this
    returns. Surrounding whitespace is not part of the identity — accepting both
    ``"model-x"`` and ``" model-x"`` would put two rows in the tab for one model
    — and an identifier past the bound is rejected outright rather than admitted
    somewhere it cannot be metered.

    Deliberately not applied when loading persisted config: per the
    persisted-shape rule a legacy value must disable nothing and fail nothing.
    """

    if not isinstance(value, str):
        return None
    canonical = value.strip()
    if not canonical or len(canonical) > MODEL_ID_MAX_LENGTH:
        return None
    return canonical

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
