"""Stable Model Hub model identifiers shared by API and backend lanes."""

from __future__ import annotations

import hashlib

# Usage rows shipped with a 200-character readable head. Keep that threshold
# stable even when a later API admits longer model identifiers, or one upgrade
# would start writing a second key for the same model's historical usage.
USAGE_LEDGER_VERBATIM_MAX_LENGTH = 200

# The longest key a usage ledger row can carry: the stable readable head, one
# separator, and one hex digest. A literal model ID spelling such a key is
# itself folded on live derivation, while persisted keys are read unchanged.
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
    New admission checks cannot follow it there: rejecting a persisted value
    would fail config load, and per the persisted-shape rule a legacy file must load.

    A value that is nothing but padding therefore comes back unchanged rather
    than emptied. A normalization that turns a loadable file into an unloadable
    one is a migration, and this is not one.
    """

    return value.strip() or value


def canonical_model_id(value: object) -> str | None:
    """Admit a new nonblank identity that storage and transport can represent.

    Length is not identity policy. Preserve every canonical code point, including
    case and Unicode composition, but refuse unpaired surrogates that UTF-8
    storage and metering cannot encode. Credential checks remain with callers.
    Loading uses `normalized_model_id`; metering derives `usage_ledger_key`.
    Neither applies this new-admission rule to historical data.
    """

    if not isinstance(value, str):
        return None
    canonical = normalized_model_id(value)
    if not canonical.strip():
        return None
    try:
        canonical.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return canonical


def usage_ledger_key(value: object) -> str | None:
    """Derive a live call's historical usage key without a length refusal.

    Verbatim keys have at most 200 characters; longer identities fold to a
    265-character head/digest key. A literal spelling another model's folded key
    is longer than 200 too, so deriving folds it again. Reading a persisted key
    must instead use `persisted_ledger_key`. The digest includes the entire
    canonical identity and its historical encoding must not change.

    This is not a guarantee over malformed legacy text. Unencodable surrogates
    and all-whitespace legacy IDs have existing encoding/attribution defects;
    see docs/plans/model-hub-identifier-boundary-assessment.md. Do not guess a
    migration of those rows while changing admission.
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
    key is past the verbatim threshold by construction, so feeding it back through
    `usage_ledger_key` would fold it a second time and orphan the row it came from.
    The read path must therefore recognize a stored key rather than re-derive it.

    It may refuse where `usage_ledger_key` may not, and the asymmetry is the same
    one admission and metering have. A call that already happened cannot be
    un-billed, so deriving may only fold. A row is not a call — it is what a
    previous write claims about calls — so a row bigger than any key this ledger
    can produce is a corrupt row, and dropping it loses a claim rather than a call.

    Whitespace still normalizes, because a row written by a release that spelled an
    identity with padding names the same model as one that did not. The historical
    all-whitespace exception is recorded in the identifier assessment; changing
    this reader cannot recover attribution from already merged counters.
    """

    if not isinstance(value, str) or not value:
        return None
    canonical = normalized_model_id(value)
    if len(canonical) > USAGE_LEDGER_KEY_MAX_LENGTH:
        return None
    return canonical
