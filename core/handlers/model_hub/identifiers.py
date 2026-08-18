"""Stable Model Hub model identifiers shared by API and backend lanes."""

from __future__ import annotations

import hashlib

# The longest model identifier the hub admits at a boundary that can still refuse
# one: manual add, upstream discovery, and a client-declared model on source
# creation. It is deliberately not a load rule — per the persisted-shape rule a
# file an older release wrote must keep loading — so a live, routable identifier
# is not always within it, and no surface downstream of config may treat this
# bound as the set of identifiers that exist.
MODEL_ID_MAX_LENGTH = 200

# The longest key a usage ledger row can carry, so a persisted row cannot grow
# without limit. Longer than any identifier stored verbatim by construction: the
# fold below appends a full digest, so a folded key can never collide with a
# verbatim one and folding a key twice returns it unchanged.
USAGE_LEDGER_KEY_MAX_LENGTH = MODEL_ID_MAX_LENGTH + 1 + 2 * hashlib.sha256().digest_size


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
    returns. An identifier past the bound is refused here, at the one moment a
    request can still be answered with an error instead of carried forever.

    Deliberately not applied when loading persisted config, and not applied by the
    usage ledger: per the persisted-shape rule a legacy value must disable nothing
    and fail nothing, and a call already made under one cannot be un-billed by
    refusing its identifier afterwards. `normalized_model_id` is the half that
    applies on load; `usage_ledger_key` is the bound that applies to a row.
    """

    if not isinstance(value, str):
        return None
    canonical = normalized_model_id(value)
    if not canonical.strip() or len(canonical) > MODEL_ID_MAX_LENGTH:
        return None
    return canonical


def usage_ledger_key(value: object) -> str | None:
    """Key one metered call by the identity it ran under, whatever its length.

    Admission and metering ask different questions about the same identifier, and
    only admission may answer no. A request naming a 4KB model is refused and
    nothing is lost; a call that already reached an upstream was already billed,
    and the only identity it can be attributed to is the one config holds. So
    `canonical_model_id` is the wrong question here — a legacy model that
    `ModelHubModelConfig.from_payload` deliberately keeps loadable and routable
    would have every one of its calls dropped, and the tab would report an
    upgraded install as quieter than it actually is.

    The bound therefore folds instead of refusing. A value that fits is its own
    key; a longer one is keyed by its readable head plus a digest of the whole
    value, which is bounded, identical across restarts, and separated from other
    identities by that digest rather than by a prefix an adversary can pad. The
    result is longer than any verbatim key, so it cannot collide with one and
    cannot fold again — which is why the read path and the write path can share
    this one function.

    Only a value carrying no identity at all — not text, or empty — has no key,
    and that is exactly the set no config can hold.
    """

    if not isinstance(value, str) or not value:
        return None
    canonical = normalized_model_id(value)
    if len(canonical) <= USAGE_LEDGER_KEY_MAX_LENGTH:
        return canonical
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{canonical[:MODEL_ID_MAX_LENGTH]}~{digest}"

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
