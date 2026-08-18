"""Stable Model Hub model identifiers shared by API and backend lanes."""

from __future__ import annotations

# The longest model identifier the hub accepts. One constant so the boundaries
# that admit an identifier (manual add and upstream discovery) and the usage
# ledger that keys rows by it cannot disagree: a model config accepts is always
# a model usage can meter, and a persisted row can never grow without limit.
MODEL_ID_MAX_LENGTH = 200


def normalized_model_id(value: str) -> str:
    """Spell one identifier the one way this product spells it.

    The identity half of the canonical form, split out because it is the half
    that is always safe. Surrounding whitespace is not part of a model's
    identity — accepting both ``"model-x"`` and ``" model-x"`` would put two rows
    in the tab for one model — so every identifier that enters a config object
    goes through here, including one read from a file an older release wrote.
    The bound below cannot follow it there: rejecting a persisted value would
    fail config load, and per the persisted-shape rule a legacy file must load.

    A value that is nothing but padding therefore comes back unchanged rather
    than emptied. A normalization that turns a loadable file into an unloadable
    one is a migration, and this is not one.
    """

    return value.strip() or value


def canonical_model_id(value: object) -> str | None:
    """Admit one candidate identifier in its canonical form, or reject it.

    A model ID is an identity: it is compared, stored in config, sent upstream,
    and used as a usage-ledger key. Those uses only agree if every one of them
    means the same thing by "the same model", so admission is decided once here
    and the surfaces that accept a client-declared identifier store what this
    returns. An identifier past the bound is rejected outright rather than
    admitted somewhere it cannot be metered.

    Deliberately not applied when loading persisted config: per the
    persisted-shape rule a legacy value must disable nothing and fail nothing.
    `normalized_model_id` is the half that does apply there.
    """

    if not isinstance(value, str):
        return None
    canonical = normalized_model_id(value)
    if not canonical.strip() or len(canonical) > MODEL_ID_MAX_LENGTH:
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
