"""Typed Skill observations and their transactionally maintained projection."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import func, select, text
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection

from storage.models import agent_events, agent_sessions, scopes, session_turns, skill_usage_daily, state_meta

EVENT_TYPES = ("skill.catalog_result", "skill.load_result")
# Literal allowlist matches the partial index; bound IN values do not let
# SQLite prove that a query is covered by this index.
SKILL_TRACE_FILTER = text("visibility = 'trace' and event_type in ('skill.catalog_result', 'skill.load_result')")
RAW_RETENTION_DAYS = 90
DAILY_RETENTION_DAYS = 365
CLEARED_THROUGH_KEY = "skill_observability.cleared_through"
FIRST_OBSERVED_KEY = "skill_observability.first_observed_at"
NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
SOURCES = frozenset({"builtin", "project", "global", "unresolved"})
TRIGGERS = frozenset({"human", "task", "watch", "agent_run", "callback", "standalone", "unknown"})
COUNTERS = (
    "catalog_offer_count",
    "load_success_count",
    "load_failure_count",
    "load_duration_samples",
    "load_duration_ms_sum",
    "loaded_body_bytes_sum",
)


class ObservationError(ValueError):
    """An invalid observation; exception text is a bounded reason code."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _string(value: Any, *, limit: int = 200, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ObservationError("invalid_string")
    return value


def _identifier(value: Any, *, optional: bool = False) -> str | None:
    result = _string(value, optional=optional)
    if result is not None and not ID_RE.fullmatch(result):
        raise ObservationError("invalid_identifier")
    return result


def _digest(value: Any, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise ObservationError("invalid_digest")
    return value


def _integer(value: Any, *, maximum: int, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if type(value) is not int or not 0 <= value <= maximum:
        raise ObservationError("invalid_integer")
    return value


def _fields(value: Any, allowed: set[str], required: set[str] | None = None) -> dict:
    if not isinstance(value, dict) or set(value) - allowed or (required or allowed) - set(value):
        raise ObservationError("invalid_fields")
    return value


def _skill_fields(value: dict, *, unresolved: bool = False) -> None:
    _digest(value["skill_key"], optional=unresolved)
    name = value["skill_name"]
    if name is not None or not unresolved:
        if not isinstance(name, str) or not 1 <= len(name) <= 64 or not NAME_RE.fullmatch(name):
            raise ObservationError("invalid_skill_name")
    if (value["skill_key"] is None) != (name is None):
        raise ObservationError("invalid_skill_identity")
    if value["source_kind"] not in SOURCES:
        raise ObservationError("invalid_skill_source")
    _digest(value["descriptor_sha256"], optional=unresolved)


def validate(observation: Any, *, now: datetime) -> dict[str, Any]:
    envelope = _fields(observation, {"id", "session_id", "turn_id", "event_type", "content", "metadata"})
    _identifier(envelope["id"])
    _identifier(envelope["session_id"], optional=True)
    _identifier(envelope["turn_id"], optional=True)
    if envelope["event_type"] not in EVENT_TYPES:
        raise ObservationError("invalid_event_type")
    metadata = _fields(
        envelope["metadata"],
        {
            "schema_version",
            "observed_at",
            "observation_channel",
            "trigger_kind",
            "backend",
            "model",
            "avibe_version",
        },
    )
    if type(metadata["schema_version"]) is not int or metadata["schema_version"] != 1:
        raise ObservationError("unsupported_schema")
    value = _string(metadata["observed_at"], limit=27)
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp(observed) != value:
            raise ValueError
    except (ValueError, TypeError):
        raise ObservationError("invalid_observed_at") from None
    if observed > now or observed < now - timedelta(hours=24):
        raise ObservationError("observation_expired_or_future")
    if metadata["observation_channel"] not in {"avibe_cli", "runtime_prompt"}:
        raise ObservationError("invalid_channel")
    if metadata["trigger_kind"] not in TRIGGERS:
        raise ObservationError("invalid_trigger")
    _string(metadata["model"], optional=True)
    _identifier(metadata["backend"], optional=True)
    _string(metadata["avibe_version"], limit=80)
    content = envelope["content"]
    if envelope["event_type"] == "skill.load_result":
        content = _fields(
            content,
            {
                "skill_key",
                "skill_name",
                "source_kind",
                "skill_revision",
                "descriptor_sha256",
                "outcome",
                "error_code",
                "duration_ms",
                "body_bytes",
            },
        )
        _skill_fields(content, unresolved=True)
        _digest(content["skill_revision"], optional=True)
        _integer(content["duration_ms"], maximum=86_400_000, optional=True)
        _integer(content["body_bytes"], maximum=32 * 1024 * 1024, optional=True)
        errors = {"invalid_name", "not_found", "unreadable_or_invalid", "output_interrupted", "internal_error"}
        if content["outcome"] == "success":
            if any(content[key] is None for key in ("skill_key", "skill_revision", "descriptor_sha256", "body_bytes")):
                raise ObservationError("incomplete_success")
            if content["source_kind"] == "unresolved" or content["error_code"] is not None:
                raise ObservationError("invalid_success")
        elif content["outcome"] != "failure" or content["error_code"] not in errors:
            raise ObservationError("invalid_load_outcome")
        if metadata["observation_channel"] != "avibe_cli":
            raise ObservationError("invalid_channel")
    else:
        content = _fields(
            content, {"entry_point", "outcome", "error_code", "page", "has_more", "catalog_digest", "entries"}
        )
        if content["entry_point"] not in {"cli_list", "runtime_prompt"}:
            raise ObservationError("invalid_entry_point")
        if (content["entry_point"] == "runtime_prompt") != (metadata["observation_channel"] == "runtime_prompt"):
            raise ObservationError("invalid_channel")
        _integer(content["page"], maximum=1_000_000)
        if (
            type(content["has_more"]) is not bool
            or not isinstance(content["entries"], list)
            or len(content["entries"]) > 25
        ):
            raise ObservationError("invalid_catalog")
        _digest(content["catalog_digest"], optional=content["outcome"] == "failure")
        if content["outcome"] == "failure":
            if content["entries"] or content["error_code"] not in {
                "invalid_page",
                "output_interrupted",
                "internal_error",
            }:
                raise ObservationError("invalid_catalog_failure")
        elif content["outcome"] != "success" or content["error_code"] is not None or content["page"] < 1:
            raise ObservationError("invalid_catalog_outcome")
        seen = set()
        for position, entry in enumerate(content["entries"], 1):
            entry = _fields(entry, {"skill_key", "skill_name", "source_kind", "descriptor_sha256", "position"})
            _skill_fields(entry)
            if (
                entry["source_kind"] == "unresolved"
                or type(entry["position"]) is not int
                or entry["position"] != position
            ):
                raise ObservationError("invalid_catalog_entry")
            if entry["skill_key"] in seen:
                raise ObservationError("duplicate_catalog_entry")
            seen.add(entry["skill_key"])
    return envelope


def record(
    conn: Connection,
    observation: dict,
    *,
    enabled: Callable[[], bool],
    trusted_runtime: bool = False,
    now: datetime | None = None,
) -> str:
    """Caller owns the transaction. Accepted event and buckets commit together."""
    now = now or utc_now()
    event = validate(observation, now=now)
    # Acquire SQLite's writer reservation before reading clear/disable state.
    conn.execute(text("update state_meta set updated_at = updated_at where key = :key"), {"key": CLEARED_THROUGH_KEY})
    if not enabled():
        return "disabled"
    cleared = conn.execute(
        select(state_meta.c.value_json).where(state_meta.c.key == CLEARED_THROUGH_KEY)
    ).scalar_one_or_none()
    if cleared is not None and event["metadata"]["observed_at"] <= json.loads(cleared):
        raise ObservationError("observation_cleared")
    if not trusted_runtime and event["metadata"]["observation_channel"] != "avibe_cli":
        raise ObservationError("runtime_observation_required")

    session = None
    platform = "unknown"
    if event["session_id"] is not None:
        session = (
            conn.execute(select(agent_sessions).where(agent_sessions.c.id == event["session_id"]))
            .mappings()
            .one_or_none()
        )
        if session is None:
            raise ObservationError("session_missing")
        platform = (
            conn.execute(select(scopes.c.platform).where(scopes.c.id == session["scope_id"])).scalar_one_or_none()
            or "unknown"
        )
    turn_id = None
    if trusted_runtime and event["turn_id"] and session:
        turn_id = conn.execute(
            select(session_turns.c.id).where(
                session_turns.c.id == event["turn_id"],
                session_turns.c.session_id == session["id"],
            )
        ).scalar_one_or_none()
    meta = dict(event["metadata"])
    backend = meta.pop("backend")
    meta["correlation_level"] = "exact_turn" if turn_id else ("session_only" if session else "unattributed")
    # A Session-stable shell may outlive its originating Turn and author.
    if not trusted_runtime:
        meta["model"] = None
        meta["trigger_kind"] = "standalone" if not session and meta["trigger_kind"] == "standalone" else "unknown"
    meta["context_ref"] = turn_id
    body = json.dumps(event["content"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    meta_json = json.dumps(meta, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload = {
        "id": event["id"],
        "session_id": event["session_id"],
        "scope_id": session["scope_id"] if session else None,
        "turn_id": turn_id,
        "run_id": None,
        "platform": platform,
        "agent_name": session["agent_name"] if session else None,
        "backend": backend,
        "event_type": event["event_type"],
        "visibility": "trace",
        "content_text": None,
        "content_json": body,
        "metadata_json": meta_json,
        "source": "runtime",
        "created_at": timestamp(now),
        "updated_at": timestamp(now),
    }
    inserted = conn.execute(
        insert(agent_events).values(**payload).on_conflict_do_nothing(index_elements=["id"])
    ).rowcount
    if not inserted:
        previous = conn.execute(select(agent_events).where(agent_events.c.id == event["id"])).mappings().one()
        # Scope/agent labels may have changed since the first receipt. Compare
        # immutable submitted content and identity, not today's mutable joins.
        if any(
            previous[key] != payload[key]
            for key in (
                "session_id",
                "turn_id",
                "backend",
                "event_type",
                "content_json",
                "metadata_json",
                "source",
                "visibility",
            )
        ):
            raise ObservationError("observation_id_conflict")
        return "duplicate"

    entries = event["content"]["entries"] if event["event_type"] == "skill.catalog_result" else [event["content"]]
    for entry in entries:
        if entry["skill_key"] is None:
            continue
        counts = dict.fromkeys(COUNTERS, 0)
        maximum = 0
        revision = ""
        if event["event_type"] == "skill.catalog_result":
            counts["catalog_offer_count"] = 1
        else:
            success = entry["outcome"] == "success"
            counts["load_success_count" if success else "load_failure_count"] = 1
            duration = entry["duration_ms"]
            if duration is not None:
                counts["load_duration_samples"] = 1
                counts["load_duration_ms_sum"] = maximum = duration
            if success:
                counts["loaded_body_bytes_sum"] = entry["body_bytes"]
            revision = entry["skill_revision"] or ""
        observed = meta["observed_at"]
        values = {
            "day": observed[:10],
            "scope_id": payload["scope_id"],
            "session_id": payload["session_id"],
            "skill_key": entry["skill_key"],
            "skill_name": entry["skill_name"],
            "source_kind": entry["source_kind"],
            "skill_revision": revision,
            "backend": payload["backend"] or "",
            "model": meta["model"] or "",
            "trigger_kind": meta["trigger_kind"],
            "platform": platform,
            "avibe_version": meta["avibe_version"],
            **counts,
            "load_duration_ms_max": maximum,
            "first_observed_at": observed,
            "last_observed_at": observed,
        }
        stmt = insert(skill_usage_daily).values(**values)
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[
                    skill_usage_daily.c.day,
                    text("coalesce(scope_id, '')"),
                    text("coalesce(session_id, '')"),
                    *[
                        skill_usage_daily.c[key]
                        for key in (
                            "skill_key",
                            "skill_revision",
                            "backend",
                            "model",
                            "trigger_kind",
                            "platform",
                            "avibe_version",
                        )
                    ],
                ],
                set_={
                    **{key: skill_usage_daily.c[key] + stmt.excluded[key] for key in COUNTERS},
                    "load_duration_ms_max": func.max(
                        skill_usage_daily.c.load_duration_ms_max, stmt.excluded.load_duration_ms_max
                    ),
                    "first_observed_at": func.min(
                        skill_usage_daily.c.first_observed_at, stmt.excluded.first_observed_at
                    ),
                    "last_observed_at": func.max(skill_usage_daily.c.last_observed_at, stmt.excluded.last_observed_at),
                },
            )
        )
    conn.execute(
        insert(state_meta)
        .values(
            key=FIRST_OBSERVED_KEY,
            value_json=json.dumps(timestamp(now)),
            updated_at=timestamp(now),
        )
        .on_conflict_do_nothing(index_elements=["key"])
    )
    return "accepted"


def clear(conn: Connection, *, now: datetime | None = None) -> dict[str, int]:
    instant = timestamp(now or utc_now())
    conn.execute(
        insert(state_meta)
        .values(
            key=CLEARED_THROUGH_KEY,
            value_json=json.dumps(instant),
            updated_at=instant,
        )
        .on_conflict_do_update(index_elements=["key"], set_={"value_json": json.dumps(instant), "updated_at": instant})
    )
    events = conn.execute(agent_events.delete().where(SKILL_TRACE_FILTER)).rowcount
    daily = conn.execute(skill_usage_daily.delete()).rowcount
    return {"events": events, "daily_rows": daily}


def status(conn: Connection) -> dict:
    """Bounded local diagnostics, without copying transcript content."""
    markers = dict(
        conn.execute(
            select(state_meta.c.key, state_meta.c.value_json).where(
                state_meta.c.key.in_((FIRST_OBSERVED_KEY, CLEARED_THROUGH_KEY)),
            )
        ).all()
    )
    return {
        "raw_retention_days": RAW_RETENTION_DAYS,
        "daily_retention_days": DAILY_RETENTION_DAYS,
        "first_observed_at": json.loads(markers[FIRST_OBSERVED_KEY]) if FIRST_OBSERVED_KEY in markers else None,
        "cleared_through": json.loads(markers[CLEARED_THROUGH_KEY]) if CLEARED_THROUGH_KEY in markers else None,
        "events": conn.execute(
            select(func.count())
            .select_from(agent_events)
            .where(
                SKILL_TRACE_FILTER,
            )
        ).scalar_one(),
        "daily_rows": conn.execute(select(func.count()).select_from(skill_usage_daily)).scalar_one(),
    }
