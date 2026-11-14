"""Content-addressed Skill catalogs with event-owned reference lifetime.

Epoch-3 writers intern entries in the event transaction. The migration's indexed
GC triggers cover both new and released bridge deletion/update paths. Raw SQL
tables remain physical storage; skill_events is the explicit hydrated projection.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection

from storage.models import agent_events, skill_catalogs


def compact_event(conn: Connection, event_id: str, content: dict[str, Any]) -> None:
    """Only after a unique receipt was inserted, in the same writer transaction.

    Duplicates never allocate snapshots or change event bytes. Daily counters
    still consume the original validated inline observation.
    """
    from storage.compatibility import permits_compact_writes

    if not permits_compact_writes(conn):
        return
    entries = json.dumps(content["entries"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(entries.encode("utf-8")).hexdigest()
    conn.execute(insert(skill_catalogs).values(digest=digest, entries_json=entries).on_conflict_do_nothing(
        index_elements=["digest"],
    ))
    stored = conn.execute(select(skill_catalogs.c.entries_json).where(skill_catalogs.c.digest == digest)).scalar_one()
    if stored != entries:
        raise CatalogReferenceError("Skill catalog snapshot content conflict")
    body = {key: value for key, value in content.items() if key != "entries"}
    body["entries_ref"] = digest
    conn.execute(agent_events.update().where(agent_events.c.id == event_id).values(
        content_json=json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    ))


class CatalogReferenceError(ValueError):
    """A persisted catalog reference cannot be resolved without losing entries."""


def _reference(content: Any, event_type: str, visibility: str) -> str | None:
    if (
        event_type != "skill.catalog_result"
        or visibility != "trace"
        or not isinstance(content, dict)
        or "entries" in content
        or "entries_ref" not in content
    ):
        return None
    digest = content["entries_ref"]
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise CatalogReferenceError("invalid Skill catalog reference")
    return digest


def hydrate_content(content_json: str, event_type: str, visibility: str, entries_json: str | None) -> str:
    """Resolve a reference; preserve legacy JSON bytes and inline precedence."""
    try:
        content = json.loads(content_json)
    except (TypeError, ValueError):
        return content_json
    digest = _reference(content, event_type, visibility)
    if digest is None:
        return content_json
    if entries_json is None:
        raise CatalogReferenceError("missing Skill catalog snapshot")
    try:
        entries = json.loads(entries_json)
        canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise CatalogReferenceError("invalid Skill catalog snapshot") from None
    if not isinstance(entries, list) or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != digest:
        raise CatalogReferenceError("Skill catalog snapshot digest mismatch")
    del content["entries_ref"]
    content["entries"] = entries
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_content(conn: Connection, row: dict[str, Any]) -> str:
    """One primary-key lookup only for a compact catalog, on the caller's snapshot."""
    content_json = row.get("content_json") or "{}"
    if row.get("event_type") != "skill.catalog_result" or row.get("visibility") != "trace":
        return content_json
    try:
        content = json.loads(content_json)
    except (TypeError, ValueError):
        return content_json
    digest = _reference(content, row.get("event_type"), row.get("visibility"))
    if digest is None:
        return content_json
    entries_json = conn.execute(
        select(skill_catalogs.c.entries_json).where(skill_catalogs.c.digest == digest)
    ).scalar_one_or_none()
    return hydrate_content(content_json, row.get("event_type"), row.get("visibility"), entries_json)


# Keep the table's exact column shape; only content_json is hydrated. Selecting
# e.id/session_id/date remains a direct indexed event scan, not a materialized
# history-wide projection. The raw agent_events table is intentionally unchanged.
def events_view(*, catalogs_available: bool = True) -> str:
    """Also read an explicitly supplied pre-migration database without modifying it."""
    entries = "c.entries_json" if catalogs_available else "NULL"
    return (
        "SELECT "
        + ", ".join(
            f"skill_catalog_content(e.content_json, e.event_type, e.visibility, {entries}) AS content_json"
            if column.name == "content_json" else f"e.{column.name}"
            for column in agent_events.columns
        )
        + " FROM agent_events e"
        + (
            " LEFT JOIN skill_catalogs c ON c.digest = "
            "CASE WHEN json_valid(e.content_json) THEN json_extract(e.content_json, '$.entries_ref') END"
            if catalogs_available else ""
        )
        + " WHERE e.visibility = 'trace' AND e.event_type IN ('skill.catalog_result', 'skill.load_result')"
    )


SKILL_EVENTS_VIEW = events_view()
