"""Read a bounded runtime transcript from rebuildable checkpoint metadata.

The caller owns one explicit SQLite read snapshot and publishes the returned
pre-serialized metadata with a compare-and-swap *after* leaving that snapshot.
Canonical rows are never changed. A missing/invalid capsule costs one canonical
prefix read; subsequent loads resolve its original row references by primary key.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from typing import Any, Mapping, Sequence

from sqlalchemy import select, text
from sqlalchemy.engine import Connection

from core.agent_core.harness.cache_metrics import valid_record
from core.agent_core.harness.projection import ProjectionError, _index_results
from core.agent_core.harness.runtime import RunTranscript, compact_runtime, is_anchor_response
from core.agent_core.harness.store import ContextEntry
from storage.models import agent_events, messages

CAPSULE_KEY = "vibey_runtime"
CAPSULE_VERSION = 1
Publication = tuple[str, str | None, str]
Chain = Sequence[tuple[str, int | None]]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _ordered(rows: Sequence[ContextEntry]) -> list[ContextEntry]:
    from storage.agent_transcript import TranscriptError

    result = sorted(rows, key=lambda row: row.context_seq)
    for previous, current in zip(result, result[1:]):
        if previous.context_seq == current.context_seq:
            raise TranscriptError(f"rows {previous.row_id} and {current.row_id} share context_seq {current.context_seq}")
    return result


def _range(conn: Connection, chain: Chain, after: int = 0) -> list[ContextEntry]:
    from storage.agent_transcript import _event_entry, _message_entry

    entries = []
    for member, bound in chain:
        if bound is not None and bound <= after:
            continue
        for table, decode in ((messages, _message_entry), (agent_events, _event_entry)):
            conditions = [table.c.session_id == member, table.c.context_seq > after]
            if bound is not None:
                conditions.append(table.c.context_seq <= bound)
            for row in conn.execute(select(table).where(*conditions)).mappings():
                entries.append(decode(member, row))
    return _ordered(entries)


def _checkpoint(conn: Connection, chain: Chain, before: int | None = None) -> Mapping[str, Any] | None:
    """One descending indexed seek per ancestor; no union/global history sort."""
    newest = None
    for member, bound in chain:
        if before is not None:
            bound = min(bound, before - 1) if bound is not None else before - 1
        # SQLite may otherwise prefer the session/sequence unique index for an
        # upper-bounded predecessor seek and walk every intervening event. Pin
        # the checkpoint index so both latest and predecessor seeks are bounded.
        query = text(
            "SELECT * FROM agent_events INDEXED BY ix_agent_events_session_type_context_seq "
            "WHERE session_id = :member AND event_type = 'context_compaction' "
            "AND context_seq IS NOT NULL "
            + ("AND context_seq <= :bound " if bound is not None else "")
            + "ORDER BY context_seq DESC LIMIT 1"
        )
        row = conn.execute(query, {"member": member, "bound": bound}).mappings().first()
        if row is not None and (newest is None or row["context_seq"] > newest["context_seq"]):
            newest = row
    return newest


def _prefix_chain(conn: Connection, checkpoint: Mapping[str, Any]) -> list[tuple[str, int]]:
    from storage.agent_transcript import _ancestry

    seq = checkpoint["context_seq"]
    return [(member, min(bound, seq) if bound is not None else seq)
            for member, bound in _ancestry(conn, checkpoint["session_id"])]


def _binding(checkpoint: Mapping[str, Any], chain: Chain) -> dict[str, Any]:
    return {
        "checkpoint": [checkpoint["id"], checkpoint["session_id"], checkpoint["context_seq"]],
        "content_sha256": hashlib.sha256(checkpoint["content_json"].encode()).hexdigest(),
        "prefix": [list(member) for member in chain],
    }


def _ref(row: ContextEntry) -> list[Any]:
    return [row.kind, row.session_id, row.context_seq, row.row_id]


def _new_anchor(suffix: Sequence[ContextEntry]) -> bool:
    # A new checkpoint may itself discard a response in this suffix. Only an
    # anchor still eligible in the final projection can replace the old anchor.
    checkpoint = next((row for row in reversed(suffix) if row.kind == "compaction"), None)
    floor = checkpoint.payload["first_kept_seq"] if checkpoint is not None else 0
    return any(row.context_seq >= floor and is_anchor_response(row) for row in suffix)


def _metadata(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(checkpoint["metadata_json"] or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _cached(
    conn: Connection, checkpoint: Mapping[str, Any], chain: Chain, suffix: Sequence[ContextEntry], *,
    include_anchor: bool = True,
) -> RunTranscript | None:
    """Validate envelope/provenance; repair can explicitly load only base refs."""
    from storage.agent_transcript import TranscriptError, _event_entry, _message_entry

    try:
        capsule = _metadata(checkpoint)[CAPSULE_KEY]
        if not isinstance(capsule, dict):
            return None
        data = {key: value for key, value in capsule.items() if key != "digest"}
        if (set(data) != {"version", "binding", "base_refs", "anchor_refs", "response_clocks",
                          "cache_records", "change_seqs"}
                or type(data["version"]) is not int or data["version"] != CAPSULE_VERSION
                or data["binding"] != _binding(checkpoint, chain)
                or capsule.get("digest") != _digest(data)):
            return None
        bounds = dict(chain)
        seen_seqs, seen_ids = set(), set()
        groups = []
        for name in ("base_refs", "anchor_refs"):
            refs = data[name]
            if not isinstance(refs, list):
                return None
            for ref in refs:
                if not isinstance(ref, list) or len(ref) != 4:
                    return None
                kind, member, seq, row_id = ref
                if (kind not in {"input", "response", "tool_result", "compaction", "context_edit",
                                 "agent_state", "replay_refusal"}
                        or not isinstance(member, str) or member not in bounds
                        or type(seq) is not int or not 0 < seq <= bounds[member]
                        or not isinstance(row_id, str) or not row_id
                        or seq in seen_seqs or row_id in seen_ids):
                    return None
                seen_seqs.add(seq)
                seen_ids.add(row_id)
            groups.append(refs)
        if ["compaction", checkpoint["session_id"], checkpoint["context_seq"], checkpoint["id"]] not in groups[0]:
            return None
        clocks = {}
        if not isinstance(data["response_clocks"], list):
            return None
        for clock in data["response_clocks"]:
            if (not isinstance(clock, list) or len(clock) != 3
                    or not all(isinstance(value, str) for value in clock[:2])
                    or type(clock[2]) not in (int, float) or not math.isfinite(clock[2])
                    or tuple(clock[:2]) in clocks):
                return None
            clocks[tuple(clock[:2])] = clock[2]
        records = data["cache_records"]
        if not isinstance(records, list) or any(
            not valid_record(record) or record.get("input_tokens") is None
            or record["context_seq"] > checkpoint["context_seq"] for record in records
        ):
            return None
        changes = data["change_seqs"]
        if (not isinstance(changes, dict) or changes.get("compaction") != checkpoint["context_seq"] or any(
            kind not in {"compaction", "context_edit"} or type(seq) is not int
            or not 0 < seq <= checkpoint["context_seq"] for kind, seq in changes.items()
        )):
            return None
        refs = groups[0] if not include_anchor or _new_anchor(suffix) else [*groups[0], *groups[1]]
        entries = []
        for kind, member, seq, row_id in refs:
            table, decode = ((messages, _message_entry) if kind in {"input", "response"}
                             else (agent_events, _event_entry))
            row = conn.execute(select(table).where(table.c.id == row_id)).mappings().first()
            if row is None or row["session_id"] != member or row["context_seq"] != seq:
                return None
            entry = decode(member, row)
            if entry.kind != kind:
                return None
            entries.append(entry)
        return RunTranscript(tuple(_ordered(entries)), clocks, tuple(records), changes)
    except (KeyError, TypeError, ValueError, OverflowError, TranscriptError):
        # Optional metadata never converts canonical corruption into success:
        # the canonical rebuild below still validates the original rows.
        return None


def _capsule(
    checkpoint: Mapping[str, Any], chain: Chain, rows: Sequence[ContextEntry], *,
    prior: RunTranscript | None = None, extra: Sequence[ContextEntry] = (),
) -> tuple[RunTranscript, Publication]:
    full = compact_runtime(rows, prior=prior)
    base = compact_runtime(rows, prior=prior, preserve_anchor=False)
    base_rows = {row.context_seq: row for row in (*base.entries, *extra)}
    base_ids = set(base_rows)
    data = {
        "version": CAPSULE_VERSION,
        "binding": _binding(checkpoint, chain),
        "base_refs": [_ref(base_rows[seq]) for seq in sorted(base_rows)],
        "anchor_refs": [_ref(row) for row in full.entries if row.context_seq not in base_ids],
        "response_clocks": [[*route, clock] for route, clock in sorted(full.response_clocks.items())],
        "cache_records": list(full.cache_records),
        "change_seqs": dict(full.change_seqs),
    }
    metadata = {**_metadata(checkpoint), CAPSULE_KEY: {**data, "digest": _digest(data)}}
    expanded = {row.context_seq: row for row in (*full.entries, *extra)}
    return replace(full, entries=tuple(expanded[seq] for seq in sorted(expanded))), (
        checkpoint["id"], checkpoint["metadata_json"], _json(metadata)
    )


def _repair_dependencies(
    rows: Sequence[ContextEntry], checkpoint_seq: int
) -> list[ContextEntry]:
    """Certify suffix edit targets using ordered call instances, never reused IDs.

    Only immutable prefix rows enter the capsule. All this Session's suffix
    edits participate. The caller separately preserves certified base repairs
    shared by sibling forks, never promoting optional anchor references.
    """
    by_id = {row.row_id: row for row in rows}
    results, _ = _index_results(rows)
    targets = {
        by_id[row.payload["target_event_id"]].context_seq
        for row in rows if row.kind == "context_edit" and row.context_seq > checkpoint_seq
    }
    selected = set(targets)
    owners = {owner for (owner, _), result in results.items() if result.context_seq in selected}
    selected.update(owners)
    selected.update(result.context_seq for (owner, _), result in results.items() if owner in owners)
    selected.update(
        row.context_seq for row in rows if row.kind == "context_edit"
        and by_id[row.payload["target_event_id"]].context_seq in selected
    )
    return [row for row in rows if row.context_seq <= checkpoint_seq and row.context_seq in selected]


def read_runtime(conn: Connection, session_id: str) -> tuple[RunTranscript, list[Publication]]:
    """Read runtime rows and prepare capsule CAS publications in one read snapshot.

    This synchronous helper belongs off the controller event loop. The caller
    must not acquire a write lock until it has left this function/read snapshot.
    """
    from storage.agent_transcript import _ancestry, _context_entries

    chain = _ancestry(conn, session_id)
    checkpoint = _checkpoint(conn, chain)
    if checkpoint is None:
        return compact_runtime(_context_entries(conn, session_id)), []
    prefix_chain = _prefix_chain(conn, checkpoint)
    suffix = _range(conn, chain, checkpoint["context_seq"])
    prefix = _cached(conn, checkpoint, prefix_chain, suffix)
    publications = []
    if prefix is None:
        previous = _checkpoint(conn, prefix_chain, checkpoint["context_seq"])
        prior = None
        rows = None
        if previous is not None:
            delta = _range(conn, prefix_chain, previous["context_seq"])
            prior = _cached(conn, previous, _prefix_chain(conn, previous), delta)
            if prior is not None:
                rows = [*prior.entries, *delta]
        if rows is not None:
            try:
                prefix, publication = _capsule(checkpoint, prefix_chain, rows, prior=prior)
            except ProjectionError:
                rows = None
        if rows is None:
            rows = _range(conn, prefix_chain)
            prefix, publication = _capsule(checkpoint, prefix_chain, rows)
        publications.append(publication)
    try:
        return compact_runtime([*prefix.entries, *suffix], prior=prefix), publications
    except ProjectionError:
        # A newly appended edit can reach behind the certified closure. Validate
        # once against canonical history, then repair the prefix for future runs.
        canonical = _context_entries(conn, session_id)
        runtime = compact_runtime(canonical)
        prefix_rows = [row for row in canonical if row.context_seq <= checkpoint["context_seq"]]
        extra = _repair_dependencies(canonical, checkpoint["context_seq"])
        # Sibling forks share the checkpoint but not their suffixes. Preserve
        # every certified base repair so alternating children cannot evict one
        # another's dependencies. Optional anchor history must stay optional.
        certified_base = _cached(conn, checkpoint, prefix_chain, suffix, include_anchor=False)
        if certified_base is not None:
            extra.extend(certified_base.entries)
        _, publication = _capsule(checkpoint, prefix_chain, prefix_rows, extra=extra)
        return runtime, [publication]
