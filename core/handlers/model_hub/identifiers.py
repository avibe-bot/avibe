"""Stable Model Hub model identifiers shared by API and backend lanes."""

from __future__ import annotations

import hashlib
import re
from typing import Container

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

# The shape a Source prefix is minted in: ``avibe-`` plus twelve random bytes.
# Only ever used to recognise an address in a record that does not say which one
# it carries; wherever the owning prefix is known, it is compared literally.
_CREDENTIAL_ADDRESS_SHAPE = re.compile(r"avibe-[0-9a-f]{24}")


def model_id_without_credential_address(value: str, prefix: str | None = None) -> str:
    """Return the identity ``value`` names, with no credential address around it.

    The engine addresses a credential through the model name and no other field,
    so an outbound call spells one as ``<source prefix>/<model>``. That prefix is
    the address of a credential, never part of a model's identity.

    ``prefix`` is the address of the credential this identity belongs to. Given
    one, only that exact address is removed, so nothing is claimed about any
    other name: reserving the whole ``avibe-<hex>/`` namespace would rewrite an
    upstream identity that happened to be spelled that way, and an identity is
    not ours to rename. Callers that hold the owning Source or credential always
    pass it.

    Without one, the address is recognised by its minted shape. That is for the
    one caller holding a credential whose metadata records no prefix, and it is
    a fallback: it fails visibly, at the same place this bug did, rather than
    renaming a vendor's model quietly.

    Unwrapping repeatedly rather than once makes the result total: whatever this
    returns carries no address, so a caller never has to ask how many one
    identity accumulated across releases.
    """

    if prefix is not None:
        return model_id_without_credential_addresses(value, (prefix,))
    identity = value
    while True:
        address, separator, remainder = identity.partition("/")
        if not separator or not remainder:
            return identity
        if not _CREDENTIAL_ADDRESS_SHAPE.fullmatch(address):
            return identity
        identity = remainder


def model_id_without_credential_addresses(
    value: str,
    addresses: Container[str],
) -> str:
    """Return the identity ``value`` names, with no *known* address around it.

    The same removal as above, for a caller holding more than one credential.
    ``addresses`` are proven addresses — each one minted for a credential this
    installation holds — so membership is the proof of ownership, and a leading
    segment that is not one of them belongs to whoever named the model.

    This is what lets an identity be repaired outside the Source that owns it.
    A backend menu row, a route key, or a hidden-model entry records no Source,
    but an address names exactly one credential and a credential binds to
    exactly one Source, so an id carrying a proven address came from there.
    """

    identity = value
    while True:
        address, separator, remainder = identity.partition("/")
        if not separator or not remainder or address not in addresses:
            return identity
        identity = remainder


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
