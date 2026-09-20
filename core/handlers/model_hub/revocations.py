"""Crash-recoverable journal for engine-owned credential revocation."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast, get_args

from .state_file import write_state_document


RevocationOperation = Literal[
    "revoke_credential",
    "cleanup_orphaned_oauth_material",
]
_REVOCATION_OPERATIONS = frozenset(get_args(RevocationOperation))


def _invalid_journal() -> OSError:
    return OSError("invalid credential revocation journal")


@dataclass(frozen=True)
class PendingCredentialRevocation:
    source_id: str
    credential_ref: str
    operation: RevocationOperation = "revoke_credential"


class CredentialRevocationJournal:
    """Persist opaque credential refs until their engine cleanup succeeds."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def _read(self) -> list[PendingCredentialRevocation]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise _invalid_journal() from None
        if not isinstance(payload, list):
            raise _invalid_journal()
        entries: list[PendingCredentialRevocation] = []
        identities: set[tuple[str, str]] = set()
        for item in payload:
            if not isinstance(item, dict) or set(item) != {
                "source_id",
                "credential_ref",
                "operation",
            }:
                raise _invalid_journal()
            source_id = item["source_id"]
            credential_ref = item["credential_ref"]
            operation = item["operation"]
            if (
                not isinstance(source_id, str)
                or not source_id
                or not isinstance(credential_ref, str)
                or not credential_ref
                or not isinstance(operation, str)
                or operation not in _REVOCATION_OPERATIONS
            ):
                raise _invalid_journal()
            identity = (source_id, credential_ref)
            if identity in identities:
                raise _invalid_journal()
            identities.add(identity)
            entries.append(
                PendingCredentialRevocation(
                    source_id=source_id,
                    credential_ref=credential_ref,
                    operation=cast(RevocationOperation, operation),
                )
            )
        return entries

    def _write(self, entries: list[PendingCredentialRevocation]) -> None:
        write_state_document(
            self.path,
            [
                {
                    "source_id": entry.source_id,
                    "credential_ref": entry.credential_ref,
                    "operation": entry.operation,
                }
                for entry in entries
            ],
        )

    def add(
        self,
        source_id: str,
        credential_ref: str,
        *,
        operation: RevocationOperation = "revoke_credential",
    ) -> None:
        with self._lock:
            if (
                not isinstance(source_id, str)
                or not source_id
                or not isinstance(credential_ref, str)
                or not credential_ref
                or not isinstance(operation, str)
                or operation not in _REVOCATION_OPERATIONS
            ):
                raise ValueError("invalid credential revocation entry")
            entries = self._read()
            entry = PendingCredentialRevocation(
                source_id,
                credential_ref,
                operation,
            )
            existing = next(
                (
                    pending
                    for pending in entries
                    if pending.source_id == source_id
                    and pending.credential_ref == credential_ref
                ),
                None,
            )
            if existing is not None and existing.operation != operation:
                raise ValueError("credential revocation entry has conflicting operation")
            if entry not in entries:
                entries.append(entry)
                self._write(entries)

    def remove(self, source_id: str, credential_ref: str) -> None:
        with self._lock:
            entries = self._read()
            remaining = [
                entry
                for entry in entries
                if not (
                    entry.source_id == source_id
                    and entry.credential_ref == credential_ref
                )
            ]
            if len(remaining) != len(entries):
                self._write(remaining)

    def list(self) -> list[PendingCredentialRevocation]:
        with self._lock:
            return self._read()
