"""Durable Memory admission-metadata keys and their read semantics.

Kept as a leaf module so storage and lightweight Memory-runtime checks can
share the vocabulary without importing CaptureAdmission (attachments, IM
leases, aiohttp).
"""

from __future__ import annotations


MEMORY_USER_ID_METADATA = "_memory_user_id"
MEMORY_ORDINARY_TEXT_METADATA = "_memory_ordinary_text"
MEMORY_CLI_ADMITTED_METADATA = "_memory_cli_admitted"


def _admission_metadata(metadata: object) -> dict:
    return metadata if isinstance(metadata, dict) else {}


def admitted_user_id(metadata: object) -> str | None:
    """Return the Memory principal, or None when the stored value is unusable.

    Only a non-empty string is usable. Surrounding whitespace is stripped;
    a blank or non-string value is treated as missing.
    """

    memory_user_id = _admission_metadata(metadata).get(MEMORY_USER_ID_METADATA)
    if not isinstance(memory_user_id, str) or not memory_user_id.strip():
        return None
    if memory_user_id != memory_user_id.strip():
        return memory_user_id.strip()
    return memory_user_id


def is_ordinary_text(metadata: object) -> bool:
    """True only when the surface stored a literal JSON/Python ``True``."""

    return _admission_metadata(metadata).get(MEMORY_ORDINARY_TEXT_METADATA) is True


def is_cli_admitted(metadata: object) -> bool:
    """True only when the surface stored a literal JSON/Python ``True``."""

    return _admission_metadata(metadata).get(MEMORY_CLI_ADMITTED_METADATA) is True


def merge_identity(metadata: object) -> tuple[str | None, bool, bool]:
    """Return the Memory facts that one dispatch context must keep singular.

    ``author_id`` is the web-push ownership identity, not the Memory principal.
    Keep the durable Memory identity alongside the two admission flags so a
    merged turn can never cross principals.
    """

    metadata = _admission_metadata(metadata)
    return (
        admitted_user_id(metadata),
        is_ordinary_text(metadata),
        is_cli_admitted(metadata),
    )
