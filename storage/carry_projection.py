"""The model view of inputs handed to a later Turn, independent of queue state.

The carry edge, not a failed terminal outcome or matching user text, supersedes
one materialized occurrence. Read these edges inside the transcript snapshot.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Iterable

from sqlalchemy import select, text
from sqlalchemy.engine import Connection

from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import UserMessage
from storage.models import agent_sessions, message_deliveries, messages

logger = logging.getLogger(__name__)


@lru_cache(maxsize=None)
def _opaque(row_id: str) -> None:
    # Once per opaque historical row in this process. Never emit an id, text or attachment.
    logger.warning("Legacy carry projection retained an inseparable mixed input (count=1)")


@dataclass
class CarryProjection:
    # Only occurrences actually changed by the projection contribute to its epoch.
    occurrences: dict[str, tuple[int, tuple[str, ...]]] = field(default_factory=dict)
    replacements: dict[str, UserMessage] = field(default_factory=dict)

    @property
    def identity(self) -> str:
        return self.before(None)

    def before(self, seq: int | None) -> str:
        rows = sorted((mid, parts) for mid, (at, parts) in self.occurrences.items() if seq is None or at < seq)
        return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest() if rows else ""

    def clean_checkpoint(self, seq, payload) -> bool:
        return payload.get("carry_projection", "") == self.before(seq)

    def apply(self, entries: Iterable[ContextEntry]):
        for row in entries:
            if row.kind == "compaction" and not self.clean_checkpoint(row.context_seq, row.payload):
                continue  # Its opaque summary and/or pinned inputs may contain superseded work.
            if row.kind == "input" and row.row_id in self.occurrences:
                message = self.replacements.get(row.row_id)
                if message is None:
                    continue
                row = replace(row, message=message)
            yield row


# Drive the lookup from the small partial index on carry children. A predecessor's
# id is immediate; original_id in the event refers instead to the root of a chain.
_CARRY_EDGES = text("""
    SELECT d.id, d.message_id, m.context_seq, json_extract(e.value,'$.at') AS created
    FROM message_deliveries AS c INDEXED BY ix_message_deliveries_session_carry
    CROSS JOIN message_deliveries AS d ON d.id=substr(c.id,1,length(c.id)-6) AND d.session_id=c.session_id
    CROSS JOIN messages AS m ON m.id=d.message_id AND m.session_id=d.session_id
    CROSS JOIN json_each(c.delivery_history_json,'$.events') AS e
    WHERE c.session_id=:member AND c.id GLOB '*~carry' AND m.context_seq IS NOT NULL
    AND (:bound IS NULL OR m.context_seq<=:bound)
    AND json_extract(e.value,'$.kind')='turn_carry'
    AND json_extract(e.value,'$.reason') IN ('failed','failed_user')
    AND json_extract(e.value,'$.source_turn_id')=d.turn_id
""")


def fork_mask(conn: Connection, member: str, bound: int) -> list[str]:
    """Freeze this immediate ancestor's edge membership with the fork point."""
    return sorted(conn.execute(_CARRY_EDGES, {"member": member, "bound": bound}).scalars())


def read_projection(conn: Connection, chain) -> CarryProjection:
    from storage.agent_transcript import _message_entry

    projection = CarryProjection()
    child = None
    for member, bound in chain:
        frozen = None
        cutoff = None
        if child is not None:
            meta, created = conn.execute(select(agent_sessions.c.metadata_json, agent_sessions.c.created_at)
                                         .where(agent_sessions.c.id == child)).one()
            data = json.loads(meta or "{}")
            if isinstance(data.get("fork_source_carry_mask"), list):
                frozen = set(data["fork_source_carry_mask"])
            else:  # Released forks: carry event time, not the child's current queue state.
                cutoff = data.get("fork_created_at") or created
        child = member
        removed: dict[str, list[str]] = {}
        seqs = {}
        for delivery, mid, seq, created in conn.execute(_CARRY_EDGES, {"member": member, "bound": bound}):
            if frozen is not None and delivery not in frozen:
                continue
            if cutoff and created and _epoch(created) > _epoch(cutoff):
                continue
            removed.setdefault(mid, []).append(delivery)
            seqs[mid] = seq
        for mid, ids in removed.items():
            members = tuple(conn.execute(select(message_deliveries.c.id).where(message_deliveries.c.message_id == mid)
                            .order_by(message_deliveries.c.turn_position, message_deliveries.c.id)).scalars())
            hidden = set(ids)
            if hidden != set(members):
                from storage.carry_members import surviving_input

                row = conn.execute(select(messages).where(messages.c.id == mid)).mappings().one()
                original = _message_entry(member, row).message
                replacement = surviving_input(conn, mid, members, hidden, original)
                if replacement is None:
                    _opaque(mid)
                    continue
                projection.replacements[mid] = replacement
            projection.occurrences[mid] = (seqs[mid], tuple(sorted(hidden)))
    return projection


def _epoch(value):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
