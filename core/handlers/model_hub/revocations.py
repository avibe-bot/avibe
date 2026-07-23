"""Crash-recoverable journal for engine-owned credential revocation."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PendingCredentialRevocation:
    source_id: str
    credential_ref: str


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
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        return [
            PendingCredentialRevocation(
                source_id=item["source_id"],
                credential_ref=item["credential_ref"],
            )
            for item in payload
            if isinstance(item, dict)
            and isinstance(item.get("source_id"), str)
            and isinstance(item.get("credential_ref"), str)
        ]

    def _write(self, entries: list[PendingCredentialRevocation]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {"source_id": entry.source_id, "credential_ref": entry.credential_ref}
            for entry in entries
        ]
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_name = tmp.name
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, self.path)

    def add(self, source_id: str, credential_ref: str) -> None:
        with self._lock:
            entries = [entry for entry in self._read() if entry.source_id != source_id]
            entries.append(PendingCredentialRevocation(source_id, credential_ref))
            self._write(entries)

    def remove(self, source_id: str) -> None:
        with self._lock:
            entries = self._read()
            remaining = [entry for entry in entries if entry.source_id != source_id]
            if len(remaining) != len(entries):
                self._write(remaining)

    def list(self) -> list[PendingCredentialRevocation]:
        with self._lock:
            return self._read()
