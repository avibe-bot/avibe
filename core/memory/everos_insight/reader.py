"""Authorization-scoped, best-effort diagnostics projections.

Provider Call Log and Processing Record remain independent sources.  Memory
delivery state is volatile and is deliberately absent from this reader.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeAlias

from core.memory.processing_record import ProcessingSourceObservations, SourceObservation
from core.memory.store import is_principal_id

MemoryReadScope: TypeAlias = tuple[str, str]


@dataclass(frozen=True, slots=True)
class MemoryInsightPaths:
    everos_root: Path
    capture_db_path: Path
    call_log_db_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "everos_root", Path(self.everos_root))
        object.__setattr__(self, "capture_db_path", Path(self.capture_db_path))
        object.__setattr__(self, "call_log_db_path", Path(self.call_log_db_path))

    @property
    def system_db_path(self) -> Path:
        return self.everos_root / ".index" / "sqlite" / "system.db"

    @property
    def ome_db_path(self) -> Path:
        return self.everos_root / ".index" / "sqlite" / "ome.db"


class MemoryInsightReader:
    """Read diagnostics without joining or inferring from volatile captures."""

    def __init__(
        self,
        paths: MemoryInsightPaths,
        *,
        provider_base_urls: tuple[str, ...] | list[str] = (),
        exact_redaction_values: tuple[str, ...] | list[str] = (),
    ) -> None:
        if isinstance(provider_base_urls, str) or any(not isinstance(v, str) for v in provider_base_urls):
            raise TypeError("provider_base_urls must be a sequence of strings")
        if isinstance(exact_redaction_values, str) or any(not isinstance(v, str) for v in exact_redaction_values):
            raise TypeError("exact_redaction_values must be a sequence of strings")
        self._paths = paths
        self._provider_base_urls = tuple(provider_base_urls)
        self._redactions = tuple(sorted((v for v in exact_redaction_values if v), key=len, reverse=True))

    def source_observation(self) -> ProcessingSourceObservations:
        observed = _utc_now()
        return ProcessingSourceObservations(
            everos=_source_status(self._paths.everos_root, observed),
            capture=SourceObservation("unavailable", observed, "volatile_delivery_state"),
            calls=_call_source_status(self._paths.call_log_db_path, observed),
        )

    def installation_preflight_calls(self) -> tuple[dict[str, Any], ...]:
        with _read_only(self._paths.call_log_db_path) as conn:
            if conn is None:
                return ()
            try:
                rows = conn.execute(
                    "SELECT id, started_at_ms, duration_ms, kind, stage, model, status, error "
                    "FROM provider_call WHERE stage = 'processing_preflight' "
                    "ORDER BY started_at_ms DESC, id DESC LIMIT 20"
                ).fetchall()
            except sqlite3.Error:
                return ()
        return tuple(dict(row) for row in rows)

    def list_unlinked_calls(self, scope: MemoryReadScope, limit: int) -> dict[str, Any]:
        _validated_scope(scope)
        return _empty_projection(self._paths.call_log_db_path, limit)

    def list_admin_unlinked_calls(self, limit: int) -> dict[str, Any]:
        return _empty_projection(self._paths.call_log_db_path, limit)

    def list_entries(self, scope: MemoryReadScope, cursor: str | None, limit: int) -> dict[str, Any]:
        _validated_scope(scope)
        _validate_limit(limit, 50)
        return _empty_entries(self._paths)

    def list_admin_entries(self, cursor: str | None, limit: int) -> dict[str, Any]:
        _validate_limit(limit, 50)
        return _empty_entries(self._paths)

    def entry_detail(self, scope: MemoryReadScope, memcell_id: str) -> dict[str, Any]:
        _validated_scope(scope)
        _validate_id(memcell_id)
        return {"status": "not_found", "sections": _sections(self._paths)}

    def admin_entry_detail(self, memcell_id: str) -> dict[str, Any]:
        _validate_id(memcell_id)
        return {"status": "not_found", "sections": _sections(self._paths)}


def _validated_scope(scope: MemoryReadScope) -> MemoryReadScope:
    if (
        not isinstance(scope, tuple)
        or len(scope) != 2
        or not is_principal_id(scope[0])
        or not isinstance(scope[1], str)
        or not scope[1]
    ):
        raise ValueError("invalid Memory scope")
    return scope


def _validate_limit(limit: int, maximum: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")


def _validate_id(value: str) -> None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256:
        raise ValueError("invalid memcell id")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _source_status(root: Path, observed: str) -> SourceObservation:
    try:
        root.stat()
    except OSError:
        return SourceObservation("unavailable", observed, "provider_root_unavailable")
    return SourceObservation("available", observed)


def _call_source_status(path: Path, observed: str) -> SourceObservation:
    with _read_only(path) as conn:
        if conn is None:
            return SourceObservation("unavailable", observed, "provider_call_log_unavailable")
    try:
        path.stat()
    except OSError:
        return SourceObservation("unavailable", observed, "provider_call_log_unavailable")
    return SourceObservation("available", observed)


def _sections(paths: MemoryInsightPaths) -> dict[str, dict[str, str]]:
    observed = _utc_now()
    everos = _source_status(paths.everos_root, observed)
    calls = _call_source_status(paths.call_log_db_path, observed)
    return {
        "everos": _source_payload(everos),
        "capture": {"status": "unavailable", "reason": "volatile_delivery_state"},
        "calls": _source_payload(calls),
    }


def _source_payload(source: SourceObservation) -> dict[str, str]:
    payload = {"status": source.status}
    if source.observed_at is not None:
        payload["observed_at"] = source.observed_at
    if source.reason is not None:
        payload["reason"] = source.reason
    return payload


def _empty_entries(paths: MemoryInsightPaths) -> dict[str, Any]:
    return {"status": "ok", "entries": [], "next_cursor": None, "sections": _sections(paths)}


def _empty_projection(path: Path, limit: int) -> dict[str, Any]:
    _validate_limit(limit, 20)
    observed = _utc_now()
    return {
        "status": "ok",
        "calls": [],
        "truncated": False,
        "sections": {
            "capture": {"status": "unavailable", "reason": "volatile_delivery_state", "observed_at": observed},
            "calls": _source_payload(_call_source_status(path, observed)),
        },
    }


@contextmanager
def _read_only(path: Path):
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        yield None
        return
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
