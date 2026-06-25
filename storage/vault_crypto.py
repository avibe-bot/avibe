"""Vaults secret-name validation and the on-disk envelope shape.

P1 moved **all value cryptography out of Python** into the ``avault`` custody core
(design: avibe ``docs/plans/avault-custody-core.md`` §18). This module is now
intentionally tiny: it owns only the ENV-style secret-name rule and the
:class:`Sealed` envelope shape that the ``vault_secrets`` table stores and that the
avault client (``vibe/api.py``) produces (``avault seal``) and hands back to avault
to deliver. No keys, no plaintext, and no cryptography ever live here anymore — the
daemon never holds the machine key and never decrypts.

Wire format (the ``wrap_meta`` column is JSON text produced by avault):
    {"v": 1, "scheme": "machine-aesgcm-v1", "wrapped_dek": <b64>, "dek_nonce": <b64>}
``ciphertext`` and ``nonce`` are base64 text (the DB stores text, not blobs).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The wrap scheme avault stamps into wrap_meta. Kept here only as a documented
# reference for the stored envelope; nothing in Python reads or enforces it.
WRAP_SCHEME = "machine-aesgcm-v1"
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class VaultCryptoError(Exception):
    """Retained for backwards compatibility. Value crypto now lives in the avault
    client (``vibe/api.py``), which raises ``api.AvaultError`` instead."""


@dataclass(frozen=True)
class Sealed:
    """An envelope-encrypted value as stored in ``vault_secrets`` (all text columns).

    Produced by the avault client (``avault seal``) on create, and handed back to
    avault to deliver/fetch/inject. This process never decrypts it.
    """

    ciphertext: str  # base64
    nonce: str  # base64
    wrap_meta: str  # JSON text


def is_valid_secret_name(name: str | None) -> bool:
    """ENV-style names only: an uppercase letter then uppercase/digit/underscore."""
    return bool(name and _NAME_RE.match(name))
