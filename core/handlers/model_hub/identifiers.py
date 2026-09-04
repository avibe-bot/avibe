"""Stable Model Hub model identifiers shared by API and backend lanes."""

from __future__ import annotations

import hashlib

# The longest model identifier the hub admits at a boundary that can still refuse
# one: manual add, upstream discovery, and a client-declared model on source
# creation. It is deliberately not a load rule — per the persisted-shape rule a
# file an older release wrote must keep loading — so a live, routable identifier
# is not always within it, and no surface downstream of config may treat this
# bound as the set of identifiers that exist.
MODEL_ID_MAX_LENGTH = 256

# Usage rows shipped with a 200-character readable head. Keep that threshold
# stable even when a later API admits longer model identifiers, or one upgrade
# would start writing a second key for the same model's historical usage.
USAGE_LEDGER_VERBATIM_MAX_LENGTH = 200

# The longest key a usage ledger row can carry: the stable readable head, one
# separator, and one hex digest. At 265 characters it remains outside the model
# identifier admission namespace, so an admitted literal cannot collide with a
# folded key.
USAGE_LEDGER_KEY_MAX_LENGTH = (
    USAGE_LEDGER_VERBATIM_MAX_LENGTH + 1 + 2 * hashlib.sha256().digest_size
)

OPENCODE_PROVIDER_BY_NATIVE_PROTOCOL = {
    "openai_responses": "avibe-openai",
    "anthropic": "avibe-anthropic",
}


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
    applies on load; `usage_ledger_key` is how a metered call gets a bounded key
    without being refused one.
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

    The bound therefore folds instead of refusing. A value within the ledger's
    stable verbatim threshold is its own key; anything longer is keyed by its
    readable head plus a digest of the whole value, which is bounded, identical
    across restarts, and separated from other identities by that digest rather
    than by a prefix an adversary can pad.

    Where the fold starts is the whole of why two distinct models cannot share one
    row. A verbatim key is at most `USAGE_LEDGER_VERBATIM_MAX_LENGTH` and a folded
    key is always exactly `USAGE_LEDGER_KEY_MAX_LENGTH`: two populations a length
    test tells apart. The folded length also remains above `MODEL_ID_MAX_LENGTH`,
    so no newly admitted model can occupy it literally. Fold later and the
    populations overlap, and the folded form stops being ours — a config may hold
    any legacy string, including the literal `<head>~<digest>` of another model it
    also holds, and that second model would then be keyed verbatim onto the first
    model's row and billed for its calls. No marker can close that gap, because a
    legacy identifier can carry any marker too.

    Only a value carrying no identity at all — not text, or empty — has no key,
    and that is exactly the set no config can hold.
    """

    if not isinstance(value, str) or not value:
        return None
    canonical = normalized_model_id(value)
    if len(canonical) <= USAGE_LEDGER_VERBATIM_MAX_LENGTH:
        return canonical
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{canonical[:USAGE_LEDGER_VERBATIM_MAX_LENGTH]}~{digest}"


def persisted_ledger_key(value: object) -> str | None:
    """Accept one key a ledger row already carries, or refuse the row.

    The other direction, and the reason deriving a key is not idempotent: a folded
    key is past the admission bound by construction, so feeding it back through
    `usage_ledger_key` would fold it a second time and orphan the row it came from.
    The read path must therefore recognize a stored key rather than re-derive it.

    It may refuse where `usage_ledger_key` may not, and the asymmetry is the same
    one admission and metering have. A call that already happened cannot be
    un-billed, so deriving may only fold. A row is not a call — it is what a
    previous write claims about calls — so a row bigger than any key this ledger
    can produce is a corrupt row, and dropping it loses a claim rather than a call.

    Whitespace still normalizes, because a row written by a release that spelled an
    identity with padding names the same model as one that did not, and two rows for
    one model would double it in the tab.
    """

    if not isinstance(value, str) or not value:
        return None
    canonical = normalized_model_id(value)
    if len(canonical) > USAGE_LEDGER_KEY_MAX_LENGTH:
        return None
    return canonical
