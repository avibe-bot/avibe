"""Durable owners of an agent session, for backend idle-eviction interlocks.

A backend runtime (a Claude SDK client, a Codex app-server transport) is a
*projection* of a session, not its owner. The durable owners are the rows shipped
by #1134: a nonterminal ``message_deliveries`` row owns submitted input, a
nonterminal ``session_turns`` row owns native execution, and a nonterminal
execution-bearing ``agent_runs`` row owns Harness work that has not yet reserved
a Delivery. Idle eviction destroys the projection, so it must not run while one
of those owners still legitimately holds the session.

Two rules shape everything here:

* **Waiting is not activity.** This provider only answers "does durable work own
  this session right now?". It never touches ``session_last_activity``, so a
  claim, a queue wait, or a provider lookup cannot keep a stuck session alive.
  The caller keeps the real-progress inactivity clock and the stuck-active
  threshold as the outer bound.
* **Missing safety data is not evidence that eviction is safe.** A binding that
  is *positively* dangling (its session row is gone) fails open for that binding
  alone, so a deleted target cannot pin an unrelated runtime forever. Any other
  failure marks the whole snapshot unresolved and the caller skips the cycle.

The union is read inside ONE SQLite read transaction. All four tables live in the
same state database and the same engine, so a single ``BEGIN DEFERRED`` snapshot
is enough: an ownership handoff committed mid-read (bare Run to reserved
Delivery, Delivery to Turn) is either wholly before or wholly after the snapshot,
and can never fall between two reads.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from storage.background import RUN_STATUS_ALIASES, TERMINAL_RUN_STATUSES, normalize_run_status
from storage.db import get_cached_sqlite_engine
from storage.delivery_states import DELIVERY_STATE_MATRIX
from storage.models import agent_runs, agent_sessions, message_deliveries, session_turns

logger = logging.getLogger(__name__)

# The ordering roles that still own their input. Derived from the state policy
# rather than spelled out, so a new Delivery state inherits the right answer:
# terminal ``accepted`` / ``retired`` history does not pin a session, while an
# unresolved fence or a turn-owned row does.
PINNING_DELIVERY_ROLES = frozenset({"claimable", "fence", "turn_owned"})
TERMINAL_DELIVERY_STATES = tuple(
    state for state, policy in DELIVERY_STATE_MATRIX.items() if policy.ordering == "terminal"
)
PINNING_DELIVERY_STATES = tuple(
    state for state, policy in DELIVERY_STATE_MATRIX.items() if policy.ordering in PINNING_DELIVERY_ROLES
)
# ``session_turns.state`` is constrained to these four values; only the last is
# terminal. Queried as "not terminal" so an unrecognized state pins (fail closed)
# instead of being silently skipped.
TERMINAL_TURN_STATES = ("terminal",)
PINNING_TURN_STATES = ("waiting", "starting", "active")

# Run-type classification is explicit and closed on purpose. A watch supervisor
# heartbeat is an ``agent_runs`` row that stays ``running`` for the whole life of
# the waiter; treating it as execution would pin every watched session forever.
# Anything not deliberately classified pins, so a future run type cannot silently
# lose its interlock.
SUPERVISOR_RUN_TYPES = frozenset({"watch_runtime"})
EXECUTION_BEARING_RUN_TYPES = frozenset({"agent_run", "scheduled", "watch"})
NONTERMINAL_RUN_STATUSES = tuple(
    sorted({raw for raw, public in RUN_STATUS_ALIASES.items() if public not in TERMINAL_RUN_STATUSES})
)

_REQUIRED_TABLES = ("message_deliveries", "session_turns", "agent_sessions", "agent_runs")


def normalize_workdir(value: Any) -> str:
    """Match ``agent_run_target``'s stored form so both sides compare equal."""

    text = str(value or "").strip()
    if not text:
        return ""
    return os.path.abspath(os.path.expanduser(text))


def split_runtime_key(runtime_key: str) -> tuple[str, str]:
    """Split ``f"{base_session_id}:{workdir}"`` into its two parts.

    A subagent base itself contains a colon (``{platform}_{thread}:{agent}``) and
    a workdir is an absolute path with none, so the split is on the LAST colon —
    the same rule ``core/services/running_agents.py`` uses.
    """

    base, sep, workdir = str(runtime_key or "").rpartition(":")
    if not sep:
        return str(runtime_key or ""), ""
    return base, workdir


@dataclass(frozen=True)
class OwnerBinding:
    """One durable row that owns a session, kept for loggable provenance."""

    kind: str  # "delivery" | "turn" | "run"
    row_id: str
    session_id: str
    detail: str

    def describe(self) -> str:
        return f"{self.kind}:{self.row_id}({self.detail})"


@dataclass(frozen=True)
class SessionOwnershipSnapshot:
    """One consistent read of who owns which session.

    ``resolved`` is the fail-closed switch: ``False`` means the union could not be
    established and the caller must skip its eviction cycle rather than assume
    nothing is owned.
    """

    resolved: bool
    bindings: tuple[OwnerBinding, ...] = ()
    pinned_session_ids: frozenset[str] = frozenset()
    # Runtime keys observed by the runtime itself (``session_turns.runtime_key``),
    # bound at native start. Authoritative when present.
    observed_runtime_keys: frozenset[str] = frozenset()
    # ``(session_anchor, normalized workdir, session_id)`` for pinned sessions, so
    # a session whose turn never reached native start (or has no turn at all)
    # still resolves to the runtime key the backend composes from the same two
    # values.
    pinned_targets: tuple[tuple[str, str, str], ...] = ()
    pinned_workdirs: frozenset[str] = frozenset()
    # Bindings whose session row is positively gone: recorded, never pinned.
    dangling_session_ids: frozenset[str] = frozenset()

    @property
    def failed(self) -> bool:
        return not self.resolved

    def pins_runtime_key(self, runtime_key: str) -> bool:
        """True when a durable owner holds the session behind this runtime key."""

        return bool(runtime_key) and bool(self.session_ids_for_runtime_key(runtime_key))

    def pins_workdir(self, workdir: str) -> bool:
        """True for a runtime keyed only by working directory (Codex transports)."""

        return bool(self.pinned_workdirs) and normalize_workdir(workdir) in self.pinned_workdirs

    def session_ids_for_runtime_key(self, runtime_key: str) -> frozenset[str]:
        """Which pinned sessions this backend runtime key projects."""

        if not runtime_key or not self.pinned_session_ids:
            return frozenset()
        matched: set[str] = set()
        for session_id in self._runtime_key_owners.get(runtime_key, ()):
            matched.add(session_id)
        base, workdir = split_runtime_key(runtime_key)
        normalized = normalize_workdir(workdir)
        for anchor, session_workdir, session_id in self.pinned_targets:
            if session_workdir and normalized and session_workdir != normalized:
                continue
            # A subagent runs under its parent session's anchor with an
            # ``:{agent_name}`` suffix, so a live parent turn pins it too.
            if base == anchor or base.startswith(f"{anchor}:"):
                matched.add(session_id)
        return frozenset(matched)

    def reasons_for_runtime_key(self, runtime_key: str) -> tuple[str, ...]:
        """Loggable provenance: which durable rows pin this runtime key."""

        owners = self.session_ids_for_runtime_key(runtime_key)
        return tuple(binding.describe() for binding in self.bindings if binding.session_id in owners)

    def session_ids_for_workdir(self, workdir: str) -> frozenset[str]:
        """Which pinned sessions a workdir-keyed runtime projects."""

        normalized = normalize_workdir(workdir)
        if not normalized:
            return frozenset()
        return frozenset(
            session_id
            for _anchor, session_workdir, session_id in self.pinned_targets
            if session_workdir == normalized
        )

    def reasons_for_workdir(self, workdir: str) -> tuple[str, ...]:
        """Loggable provenance for a workdir-keyed runtime."""

        owners = self.session_ids_for_workdir(workdir)
        return tuple(binding.describe() for binding in self.bindings if binding.session_id in owners)

    # Derived by the provider; carried so lookups stay O(1) per runtime key.
    _runtime_key_owners: Mapping[str, frozenset[str]] = MappingProxyType({})


UNRESOLVED_SNAPSHOT = SessionOwnershipSnapshot(resolved=False)
EMPTY_SNAPSHOT = SessionOwnershipSnapshot(resolved=True)


class DurableSessionOwnershipProvider:
    """Resolves the durable owners of every session in one read snapshot."""

    def __init__(self, engine_factory: Callable[[], Engine] = get_cached_sqlite_engine) -> None:
        self._engine_factory = engine_factory
        self._unknown_run_types_logged: set[str] = set()

    def snapshot(self) -> SessionOwnershipSnapshot:
        try:
            engine = self._engine_factory()
            with engine.connect() as conn:
                if not self._schema_available(conn):
                    # No durable tables means no durable owner can exist. That is
                    # positive proof, not missing data.
                    return EMPTY_SNAPSHOT
                conn.exec_driver_sql("BEGIN DEFERRED")
                try:
                    return self._read(conn)
                finally:
                    conn.exec_driver_sql("ROLLBACK")
        except Exception:
            logger.warning("Durable session ownership lookup failed; skipping eviction", exc_info=True)
            return UNRESOLVED_SNAPSHOT

    @staticmethod
    def _schema_available(conn: Connection) -> bool:
        """Whether every owner table exists, asked of this exact handle.

        Read from ``sqlite_master`` rather than through ``Dialect.has_table``,
        which is documented as internal and rejects anything but a real
        ``Connection`` — the probe has to work on whatever handle it is handed.
        """

        present = {
            str(row[0])
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        return all(table in present for table in _REQUIRED_TABLES)

    def _read(self, conn: Connection) -> SessionOwnershipSnapshot:
        bindings: list[OwnerBinding] = []
        nonterminal_delivery_ids: set[str] = set()

        for row in conn.execute(
            select(
                message_deliveries.c.id,
                message_deliveries.c.session_id,
                message_deliveries.c.state,
            ).where(message_deliveries.c.state.notin_(TERMINAL_DELIVERY_STATES))
        ).mappings():
            delivery_id = str(row["id"])
            nonterminal_delivery_ids.add(delivery_id)
            bindings.append(
                OwnerBinding(
                    kind="delivery",
                    row_id=delivery_id,
                    session_id=str(row["session_id"] or ""),
                    detail=str(row["state"] or ""),
                )
            )

        runtime_keys_by_session: dict[str, set[str]] = {}
        for row in conn.execute(
            select(
                session_turns.c.id,
                session_turns.c.session_id,
                session_turns.c.state,
                session_turns.c.runtime_key,
            ).where(session_turns.c.state.notin_(TERMINAL_TURN_STATES))
        ).mappings():
            session_id = str(row["session_id"] or "")
            bindings.append(
                OwnerBinding(
                    kind="turn",
                    row_id=str(row["id"]),
                    session_id=session_id,
                    detail=str(row["state"] or ""),
                )
            )
            runtime_key = str(row["runtime_key"] or "").strip()
            if runtime_key:
                runtime_keys_by_session.setdefault(session_id, set()).add(runtime_key)

        for row in conn.execute(
            select(
                agent_runs.c.id,
                agent_runs.c.session_id,
                agent_runs.c.run_type,
                agent_runs.c.status,
                agent_runs.c.delivery_id,
            )
            .where(agent_runs.c.session_id.is_not(None))
            .where(agent_runs.c.status.in_(NONTERMINAL_RUN_STATUSES))
        ).mappings():
            run_type = str(row["run_type"] or "").strip()
            if not self._run_bears_execution(run_type):
                continue
            session_id = str(row["session_id"] or "")
            delivery_id = str(row["delivery_id"] or "").strip()
            # Exact ownership already represented by a Delivery in THIS snapshot
            # needs no second binding. Because both reads share one snapshot, a
            # Run can never be dropped as "represented" by a row the snapshot
            # omitted.
            if delivery_id and delivery_id in nonterminal_delivery_ids:
                continue
            bindings.append(
                OwnerBinding(
                    kind="run",
                    row_id=str(row["id"]),
                    session_id=session_id,
                    detail=f"{run_type or 'unknown'}/{normalize_run_status(row['status'])}",
                )
            )

        candidate_session_ids = {binding.session_id for binding in bindings if binding.session_id}
        session_rows = self._session_rows(conn, candidate_session_ids)
        dangling = frozenset(candidate_session_ids - set(session_rows))
        if dangling:
            logger.debug("Ignoring dangling session ownership bindings: %s", sorted(dangling))

        pinned_session_ids = frozenset(candidate_session_ids & set(session_rows))
        pinned_targets = tuple(
            (str(row["session_anchor"] or ""), normalize_workdir(row["workdir"]), session_id)
            for session_id, row in sorted(session_rows.items())
            if session_id in pinned_session_ids and str(row["session_anchor"] or "")
        )
        runtime_key_owners: dict[str, set[str]] = {}
        for session_id, keys in runtime_keys_by_session.items():
            if session_id not in pinned_session_ids:
                continue
            for runtime_key in keys:
                runtime_key_owners.setdefault(runtime_key, set()).add(session_id)

        pinned_workdirs = {workdir for _anchor, workdir, _session_id in pinned_targets if workdir}
        pinned_workdirs |= {
            normalize_workdir(split_runtime_key(key)[1]) for key in runtime_key_owners
        }
        pinned_workdirs.discard("")

        return SessionOwnershipSnapshot(
            resolved=True,
            bindings=tuple(bindings),
            pinned_session_ids=pinned_session_ids,
            observed_runtime_keys=frozenset(runtime_key_owners),
            pinned_targets=pinned_targets,
            pinned_workdirs=frozenset(pinned_workdirs),
            dangling_session_ids=dangling,
            _runtime_key_owners=MappingProxyType(
                {key: frozenset(owners) for key, owners in runtime_key_owners.items()}
            ),
        )

    def _run_bears_execution(self, run_type: str) -> bool:
        if run_type in SUPERVISOR_RUN_TYPES:
            return False
        if run_type in EXECUTION_BEARING_RUN_TYPES:
            return True
        if run_type not in self._unknown_run_types_logged:
            self._unknown_run_types_logged.add(run_type)
            logger.warning(
                "Unclassified agent_runs.run_type=%r pins its session for eviction; classify it in "
                "core/session_ownership.py",
                run_type,
            )
        return True

    @staticmethod
    def _session_rows(conn: Connection, session_ids: Iterable[str]) -> dict[str, Mapping[str, Any]]:
        ids = [session_id for session_id in session_ids if session_id]
        if not ids:
            return {}
        rows: dict[str, Mapping[str, Any]] = {}
        # SQLite caps bound parameters per statement; chunk like storage does.
        chunk = 400
        for start in range(0, len(ids), chunk):
            batch = ids[start : start + chunk]
            for row in conn.execute(
                select(
                    agent_sessions.c.id,
                    agent_sessions.c.session_anchor,
                    agent_sessions.c.workdir,
                ).where(agent_sessions.c.id.in_(batch))
            ).mappings():
                rows[str(row["id"])] = dict(row)
        return rows


async def resolve_ownership_snapshot(controller: Any) -> SessionOwnershipSnapshot:
    """Read the owner union off the event loop, failing closed for the caller.

    The snapshot is a synchronous SQLite read, so it runs in a worker thread: an
    idle-sweep loop must never wait on a database lock to decide eviction.
    """

    provider = ownership_provider(controller)
    try:
        snapshot = await asyncio.to_thread(provider.snapshot)
    except Exception as exc:
        logger.warning("Durable session ownership lookup raised; skipping eviction cycle: %s", exc)
        return UNRESOLVED_SNAPSHOT
    if snapshot is None or snapshot.failed:
        logger.warning("Durable session ownership unresolved; skipping eviction cycle")
        return UNRESOLVED_SNAPSHOT
    return snapshot


def ownership_provider(controller: Any) -> DurableSessionOwnershipProvider:
    """One provider per controller, created on first use.

    Tests (and any future backend) can pre-set ``controller.session_ownership``
    to inject a stub; the attribute is the single wiring point.
    """

    provider = getattr(controller, "session_ownership", None)
    if provider is not None:
        return provider
    provider = DurableSessionOwnershipProvider()
    try:
        controller.session_ownership = provider
    except Exception:  # pragma: no cover - exotic controller stubs
        logger.debug("Controller rejected session_ownership attribute", exc_info=True)
    return provider
