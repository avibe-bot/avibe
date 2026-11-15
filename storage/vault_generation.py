"""Value-free provenance projection and additive generation commits.

Custody is deliberately absent: callers hand in only an avault Sealed envelope.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from storage import vault_crypto, vault_service as vs
from storage.agent_session_rows import reserve_write_lock
from storage.models import agent_sessions, vault_secrets
from storage.vault_crypto import Sealed


class GenerateTargetError(vs.InvalidRequestError):
    """Generation cannot replace a value or cross a kind/protection boundary."""


def generated_metadata(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Project only strictly typed, current-field provenance, never nested metadata."""
    if row.get("kind") != "static":
        return []
    raw = vs._public_meta(row.get("public_meta")).get("generated")
    if not isinstance(raw, list):
        return []
    fields = set(vs.secret_field_names(row))
    result = []
    seen = set()
    required = {"field", "by", "session_id", "session_title", "at", "type"}
    for item in raw:
        if not isinstance(item, dict) or set(item) - (required | {"length", "bytes"}) or not required <= set(item):
            continue
        field = item["field"]
        if not isinstance(field, str) or field not in fields or field in seen or item["by"] not in ("agent", "cli"):
            continue
        session_id = item["session_id"]
        if item["by"] == "agent" and (not isinstance(session_id, str) or not session_id.strip()):
            continue
        if item["by"] == "cli" and (session_id is not None or item["session_title"] is not None):
            continue
        if item["session_title"] is not None and not isinstance(item["session_title"], str):
            continue
        if not isinstance(item["at"], str) or not isinstance(item["type"], str):
            continue
        try:
            at = datetime.fromisoformat(item["at"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if at.tzinfo is None:
            continue
        parameter = "length" if item["type"] == "password" else "bytes"
        lower, upper = (12, 128) if parameter == "length" else (16, 64)
        if item["type"] not in {"password", "hex", "base64"} or set(item) != required | {parameter}:
            continue
        if type(item[parameter]) is not int or not lower <= item[parameter] <= upper:
            continue
        result.append({key: item[key] for key in (*sorted(required), parameter)})
        seen.add(field)
    return result


def clear_generated_field(meta: dict[str, Any], field: str) -> None:
    raw = meta.get("generated")
    if isinstance(raw, list):
        remaining = [item for item in raw if isinstance(item, dict) and item.get("field") != field]
        if remaining:
            meta["generated"] = remaining
        else:
            meta.pop("generated", None)


def move_generated_field(meta: dict[str, Any], old: str, new: str) -> None:
    raw = meta.get("generated")
    if isinstance(raw, list):
        meta["generated"] = [
            {**item, "field": new} if isinstance(item, dict) and item.get("field") == old else item
            for item in raw
        ]


def capture_target(conn: Connection, name: str, field: str | None) -> dict[str, Any]:
    """Guard before custody; commit repeats these guards under a short writer lock."""
    if not vault_crypto.is_valid_secret_name(name):
        raise vs.InvalidSecretNameError("invalid secret name")
    if field is not None:
        vs._validate_field_name(field)
    vs.require_secret_create_access()
    existing = vs._find_secret_name_case_insensitive(conn, name)
    if existing is not None and existing != name:
        raise vs.SecretNameCaseConflictError(name, existing)
    if existing is None:
        return {"name": name, "field": field or "value", "record": name, "row": None}
    row = vs.require_secret_management(conn, name)
    if row.get("kind") != "static" or row.get("protection") != "standard":
        raise GenerateTargetError("generation requires a static Standard target; use vibe vault request instead")
    if field is None or field in vs.secret_field_names(row):
        raise GenerateTargetError("generation never replaces an existing value; use vibe vault request instead")
    extras = vs._extra_field_rows(row, strict=True)
    if len(extras) + 1 >= vs.MAX_SECRET_FIELDS:
        raise vs.TooManyFieldsError(f"a vault secret can have at most {vs.MAX_SECRET_FIELDS} fields")
    return {"name": name, "field": field, "record": f"{name}.{field}", "row": row}


def normalize_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate all metadata before custody, using the existing edit validators."""
    result = dict(payload)
    if "description" in result:
        result["description"] = vs._optional_string(result["description"], field="description")
    if "tags" in result:
        result["tags"] = vs._normalize_tags(vs._string_list(result["tags"], field="tags"))
    if "policy" in result:
        result["policy"] = vs._normalize_fetch_policy(result["policy"])
    return result


def commit_generated(
    conn: Connection, snapshot: dict[str, Any], sealed: Sealed, *, recipe: dict[str, Any],
    metadata: dict[str, Any], session_id: str | None, release_scopes: list[dict[str, str]],
) -> dict[str, Any]:
    """Atomically add one opaque record and its storage-owned provenance/audit."""
    reserve_write_lock(conn)
    with conn.begin_nested():
        previous = snapshot["row"]
        current = capture_target(conn, snapshot["name"], snapshot["field"] if previous else None)
        row = current["row"]
        if (row is None) != (previous is None) or (row is not None and any(
            row.get(key) != previous.get(key) for key in ("id", "updated_at", "public_meta", "tags", "policy")
        )):
            raise vs.SecretConflictError("generation target changed before commit")
        session = conn.execute(select(agent_sessions.c.id, agent_sessions.c.title).where(agent_sessions.c.id == session_id)).mappings().first() if session_id else None
        session_id = str(session["id"]) if session is not None else None
        title = session["title"] if session is not None else None
        if row is None:
            vs.create_secret(conn, name=snapshot["name"], sealed=sealed, source="agent" if session_id else "cli",
                             default_field=snapshot["field"], **metadata)
        else:
            vs.update_secret_field(conn, snapshot["name"], snapshot["field"], sealed,
                                   record=snapshot["record"], expected_updated_at=row["updated_at"],
                                   release_scopes=release_scopes)
            if metadata:
                vs.update_secret_metadata(conn, snapshot["name"], release_scopes=release_scopes, **metadata)
        row = vs._require_row(conn, snapshot["name"])
        parameter = "length" if recipe["type"] == "password" else "bytes"
        summary = {"by": "agent" if session_id else "cli", "session_id": session_id, "type": recipe["type"], parameter: recipe[parameter]}
        provenance = {"field": snapshot["field"], **summary, "session_title": title, "at": vs._now()}
        meta = vs._public_meta(row.get("public_meta"))
        meta["generated"] = [*generated_metadata(row), provenance]
        conn.execute(vault_secrets.update().where(vault_secrets.c.id == row["id"]).values(public_meta=json.dumps(meta)))
        vs.audit(conn, "generated", secret_name=row["name"], requester={"source": "agent-cli", "session_id": session_id},
                 delivery=summary)
        return {"schema_version": 1, "ok": True, "kind": "vault_secret",
                "secret": vs._meta_payload(vs._require_row(conn, row["name"])), "generated": summary}
