"""Read-only assembly of the Agents · 运行图 (run graph) payload.

This service is the single place that turns the persisted agent state into the
graph the Agents → 运行 tab renders. It is intentionally read-only: it opens a
SQLite connection, reads ``agent_sessions``/``agent_runs``/``scopes``/
``run_definitions``, and merges in liveness that the caller passes in from the
controller's running-agents snapshot. It never writes and never touches the
controller directly, so it stays pure w.r.t. its inputs and unit-testable with
fixture rows + a fake ``live_agents`` list.

Wire shape is the frozen contract in
``docs/plans/agents-run-graph-contract.md`` §3. The only addition over that doc
is the per-node ``runs`` array (recent runs for the detail-panel timeline);
it is additive and only the graph frontend consumes it.

Graph semantics (all from existing columns):

- **Node** = an ``agent_sessions`` row. ``status``/``live`` come from the
  running-agents snapshot when the session has a live process, else from the
  session's latest run outcome.
- **Spawn edge** (caller → callee): runs with ``source_kind='agent'`` and
  ``source_actor`` set, aggregated per (caller session, callee session).
- **Callback edge** (callee → report target): runs with ``callback_session_id``
  set; status from ``callback_status``. Its delivery child has
  ``source_kind='callback'`` and never suppresses an agent-authored spawn.
- **Trigger edge** (definition → carrying session): runs with
  ``run_type in ('scheduled','watch')`` grouped by ``definition_id``.

Scope is joined LEFT (``scope_id`` is nullable): a NULL scope renders as the
``独立`` (standalone) bucket.
"""

from __future__ import annotations

import json
import sqlite3
from time import perf_counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import and_, func, literal_column, or_, select, text
from sqlalchemy.engine import Engine

from storage.agent_session_rows import session_openable_in_chat
from storage.background import _status_query_values, normalize_run_status
from storage.db import create_sqlite_engine
from storage.models import agents, agent_runs, agent_sessions, run_definitions, scope_settings, scopes

# History window → lookback seconds. ``24h`` is the default (contract §3).
WINDOW_SECONDS: dict[str, int] = {
    "1h": 3600,
    "6h": 21600,
    "24h": 86400,
    "7d": 604800,
}
DEFAULT_WINDOW = "24h"

# Server-side node cap; when exceeded we keep the most recent 300 and set
# ``truncated`` so the client can surface it (contract §3).
NODE_CAP = 300

# Recent runs embedded per node for the detail-panel timeline (contract A1:
# newest first, capped at 10).
RUNS_PER_NODE = 10

# Sort sentinel for runs whose created_at is missing/unparseable (oldest).
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

# Trigger definition types that produce a trigger chip.
_TRIGGER_RUN_TYPES = {"scheduled", "watch"}



# ── timestamp helpers ────────────────────────────────────────────────────────
# Stored ``*_at`` strings come in two ISO-8601 flavors depending on the writer
# (``…Z`` second-granularity vs ``.isoformat()`` with ``+00:00`` microseconds).
# Parse either and re-emit the canonical ``…Z`` the contract asks for.

def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_z(value: Any) -> Optional[str]:
    """Normalize a stored timestamp (or datetime) to ``YYYY-MM-DDTHH:MM:SSZ``."""
    if isinstance(value, datetime):
        dt: Optional[datetime] = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        dt = _parse_iso(value)
    if dt is None:
        return value if isinstance(value, str) and value else None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── status resolution ────────────────────────────────────────────────────────

def _node_status(live_state: Optional[str], runs: list[dict[str, Any]]) -> tuple[str, bool]:
    """Return ``(status, live)`` for a node.

    Live sessions carry the running-agents state (``active``/``idle``/
    ``orphan``). Otherwise the node reflects its runs: any non-terminal run
    (``queued``/``running`` — including a stale ``running`` with no live process,
    or an older queued/running run left behind a newer finished one on a reused
    or stuck session) surfaces as ``queued`` so the Active view keeps its pending
    work; else the latest run's terminal outcome; else ``idle``.
    """
    if live_state in ("active", "idle", "orphan"):
        return live_state, True
    for run in runs:
        if normalize_run_status(run["status"]) in ("queued", "running"):
            return "queued", False
    if runs:
        return normalize_run_status(runs[0]["status"]), False
    return "idle", False


def _merge_live_state(states: Iterable[str]) -> str:
    """Collapse a session's per-backend live rows into one state.

    A session can appear once per backend in the snapshot; ``active`` wins over
    ``orphan`` over ``idle`` so the node reflects its most significant activity.
    """
    rank = {"active": 0, "orphan": 1, "idle": 2}
    best: Optional[str] = None
    for state in states:
        if best is None or rank.get(state, 3) < rank.get(best, 3):
            best = state
    return best or "idle"


# ── liveness index ───────────────────────────────────────────────────────────

def _index_live_agents(live_agents: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index running-agents rows by ``session_id``.

    Keeps the merged live ``state`` and the ``elapsed_seconds`` of the row whose
    state *won* the merge — the header shows the winning state (e.g. ``active``),
    so its elapsed must come from an ``active`` row, not from an unrelated idle
    row that happened to have a larger elapsed.
    """
    by_session: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in live_agents or ():
        session_id = row.get("session_id")
        if session_id:
            grouped.setdefault(session_id, []).append(row)
    for session_id, rows in grouped.items():
        state = _merge_live_state(str(r.get("state") or "idle") for r in rows)
        winning_elapsed = [
            r.get("elapsed_seconds")
            for r in rows
            if str(r.get("state") or "idle") == state and r.get("elapsed_seconds") is not None
        ]
        by_session[session_id] = {
            "state": state,
            "elapsed_seconds": max(winning_elapsed) if winning_elapsed else None,
        }
    return by_session


# ── scope helpers ────────────────────────────────────────────────────────────

def _scope_label(display_name: Optional[str], native_id: Optional[str]) -> Optional[str]:
    if display_name and display_name.strip():
        return display_name
    return native_id or None


def _project_id(platform: Optional[str], scope_type: Optional[str], native_id: Optional[str]) -> Optional[str]:
    # A "project" is an avibe scope of type project; its public id is the
    # scope's native_id (proj_<hex>). IM scopes are not projects.
    if platform == "avibe" and scope_type == "project":
        return native_id
    return None


# ── trigger schedule label ───────────────────────────────────────────────────

def _schedule_label(row: Mapping[str, Any]) -> Optional[str]:
    if row.get("definition_type") == "scheduled":
        cron = row.get("cron")
        if cron:
            return f"cron {cron}"
        run_at = row.get("run_at")
        if run_at:
            return _iso_z(run_at)
    return None


# ── main assembly ────────────────────────────────────────────────────────────

class _GraphReader:
    """Time SQL execution and row retrieval, not the Python that consumes rows."""

    def __init__(self, conn, timings: dict[str, float]):
        self.conn = conn
        self.timings = timings
        raw = conn.connection.driver_connection
        self.bind_limit = raw.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) if hasattr(raw, "getlimit") else 999

    def batches(self, ids, *, reserved=0, copies=1):
        # The node cap does not bound lineage, candidate count, trigger IDs,
        # or tied timeline rows. Honor a connection's configured bind limit.
        size = min(2000, (self.bind_limit - reserved) // copies)
        values = list(ids)
        for start in range(0, len(values), size):
            yield values[start:start + size]

    def rows(self, stmt, *, mappings=False):
        started = perf_counter()
        result = self.conn.execute(stmt)
        executed = perf_counter()
        self.timings["execute"] += executed - started
        keys = tuple(result.keys()) if mappings else ()
        for batch in iter(lambda: self._fetch(result), []):
            if mappings:
                yield from (dict(zip(keys, row)) for row in batch)
            else:
                yield from batch

    def _fetch(self, result):
        started = perf_counter()
        batch = result.fetchmany(512)
        self.timings["fetch"] += perf_counter() - started
        return batch


def build_graph(
    *,
    live_agents: Optional[Sequence[Mapping[str, Any]]] = None,
    window: str = DEFAULT_WINDOW,
    project: str = "all",
    include_ended: bool = True,
    include_background: bool = True,
    live_unreachable: bool = False,
    now: Optional[datetime] = None,
    node_cap: int = NODE_CAP,
    engine: Optional[Engine] = None,
    timings: Optional[dict[str, float]] = None,
) -> dict[str, Any]:
    """Assemble the run-graph payload (contract §3 + amendments A1/A2).

    ``live_agents`` is the controller's running-agents snapshot list (each row
    has at least ``session_id`` + ``state``); the route injects it so this stays
    testable. ``live_unreachable`` (A2) is set by the route when the controller
    is down and ``live_agents`` is empty. ``now`` is injectable for tests.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    window = window if window in WINDOW_SECONDS else DEFAULT_WINDOW
    cutoff = now - timedelta(seconds=WINDOW_SECONDS[window])
    cutoff_iso = cutoff.isoformat()

    live_by_session = _index_live_agents(live_agents or ())
    live_ids = set(live_by_session)

    timings = timings if timings is not None else {}
    timings.update(execute=0.0, fetch=0.0, candidates=0.0, lineage=0.0, histories=0.0, edges=0.0)
    opening = perf_counter()
    owned_engine = engine is None
    engine = engine or create_sqlite_engine()
    try:
        with engine.connect() as connection:
            timings["open"] = perf_counter() - opening
            # Pin one SQLite snapshot: compact index passes use implicit rowids
            # to join facts from the same run without rereading its payload.
            if not connection.connection.driver_connection.in_transaction:
                connection.exec_driver_sql("BEGIN")
            conn = _GraphReader(connection, timings)
            # The candidate source differs by view. History (include_ended) uses
            # the window scan. Active (contract A5 = live + queued) uses the live
            # set plus every session with a non-terminal run, independent of the
            # window and of the raw status alias — so a stuck-queued job older
            # than the cutoff, or a controller-down stale ``running`` row, can't
            # disappear from the active view.
            stage = perf_counter()
            if include_ended:
                candidate_ids = _resolve_candidates(conn, live_ids, cutoff_iso)
            else:
                nonterminal = _active_run_session_ids(conn)
                candidate_ids = live_ids | nonterminal
            if not candidate_ids:
                timings["candidates"] = perf_counter() - stage
                return _empty_payload(now, window, live_unreachable)

            # Bound the expensive run-history load by choosing which candidate
            # SESSIONS to load runs for (cheap — no runs) before ``_load_runs``,
            # after the session-level visibility filters (project, background,
            # archived/disabled) so hidden rows can't consume the cap and starve
            # visible ones. A busy install can have far more candidates than
            # survive the response, and the graph refetches on SSE/poll.
            candidate_rows = _load_sessions(conn, candidate_ids)
            eligible = _prefilter_candidate_rows(
                candidate_rows, project=project,
                include_background=include_background, live_ids=live_ids,
            )
            ranked = sorted(eligible, key=lambda r: _session_sort_key(r, live_ids))
            candidate_truncated = len(ranked) > node_cap
            session_by_id = {r["id"]: r for r in ranked[:node_cap]}
            loaded_ids = set(session_by_id)
            timings["candidates"] = perf_counter() - stage
            stage = perf_counter()

            # Discover lineage using only reference columns. The cap is on the
            # final visible nodes, not on depth: a hidden or missing intermediate
            # can still lead to a visible ancestor. Keep discovery waves so edge
            # ordering stays the same as when full histories were read per wave.
            # A non-truncated Active seed already contains every visible live
            # or nonterminal session. An ended ancestor can't survive that
            # filter or contribute an edge. Use the status index to check the
            # sole exception: an imported spelling normalized only in Python
            # (e.g. ' pending '). Such an ancestor still needs discovery.
            check_imported = not include_ended and _has_noncanonical_pending_status(conn)
            follow_refs = include_ended or candidate_truncated or check_imported
            waves = [loaded_ids]
            while follow_refs and waves[-1]:
                extra = _lineage_refs(conn, waves[-1]) - loaded_ids
                if not extra:
                    break
                waves.append(extra)
                loaded_ids = loaded_ids | extra
                for row in _load_sessions(conn, extra):
                    session_by_id[row["id"]] = row

            # Construct and rank the nodes before fetching the expensive run
            # projections (timestamps and metadata can follow large result blobs
            # in SQLite overflow pages). History visibility doesn't need runs at
            # all. Active visibility needs only whether any run is nonterminal.
            nodes = _build_nodes(list(session_by_id.values()), live_by_session)
            if not include_ended:
                # Normalize only statuses on discovered sessions, using the
                # covering session/status index. Missing/hidden intermediates
                # don't need status checks, though their lineage must be read.
                if check_imported:
                    for wave in waves:
                        for batch in conn.batches(s for s in wave if s in session_by_id and s not in nonterminal):
                            stmt = select(agent_runs.c.session_id, agent_runs.c.status).where(
                                agent_runs.c.session_id.in_(batch)
                            ).distinct()
                            nonterminal.update(sid for sid, status in conn.rows(stmt)
                                               if normalize_run_status(status) in {"queued", "running"})
                for node in nodes:
                    if not node["live"] and node["session_id"] in nonterminal:
                        node["status"] = "queued"
            nodes = _filter_nodes(nodes, project=project, include_ended=include_ended,
                                  include_background=include_background)
            nodes, cap_truncated = _cap_nodes(nodes, node_cap)
            node_ids = {n["session_id"] for n in nodes}
            timings["lineage"] = perf_counter() - stage
            stage = perf_counter()
            runs_by_session = {}
            counts_by_session = _run_totals(conn, node_ids)
            for wave in waves:
                if visible := wave & node_ids:
                    runs_by_session.update(_load_runs(conn, visible, node_ids, cutoff))
            for node in nodes:
                sid = node["session_id"]
                _add_run_details(node, runs_by_session.get(sid, []), counts_by_session.get(sid, (0, 0, False)))
            timings["histories"] = perf_counter() - stage
            stage = perf_counter()
            edges, trigger_ids = _build_edges(runs_by_session, node_ids, cutoff)
            trigger_nodes = _load_trigger_nodes(conn, trigger_ids)
            timings["edges"] = perf_counter() - stage
    finally:
        if owned_engine:
            engine.dispose()

    truncated = candidate_truncated or cap_truncated

    edges = _filter_edges(edges, node_ids, {t["definition_id"] for t in trigger_nodes})
    trigger_nodes = _prune_triggers(trigger_nodes, edges)

    return {
        "ok": True,
        "generated_at": _iso_z(now),
        "window": window,
        "live_unreachable": live_unreachable,
        "counts": _counts(nodes),
        "nodes": nodes,
        "trigger_nodes": trigger_nodes,
        "edges": edges,
        "truncated": truncated,
    }


def _empty_payload(now: datetime, window: str, live_unreachable: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "generated_at": _iso_z(now),
        "window": window,
        "live_unreachable": live_unreachable,
        "counts": _counts([]),
        "nodes": [],
        "trigger_nodes": [],
        "edges": [],
        "truncated": False,
    }


def _has_noncanonical_pending_status(conn) -> bool:
    indexed = set(_status_query_values("queued") + _status_query_values("running"))
    # Jump to the next distinct key. SELECT DISTINCT status still walks the
    # entire status index on each Active poll, even when no malformed spelling
    # exists. The empty string is a real historical spelling, not a sentinel.
    statuses = text("""
        WITH RECURSIVE status_keys(value) AS (
            SELECT min(status) FROM agent_runs WHERE status >= ''
            UNION ALL
            SELECT (SELECT min(status) FROM agent_runs WHERE status > status_keys.value)
            FROM status_keys WHERE value IS NOT NULL
        ) SELECT value FROM status_keys WHERE value IS NOT NULL
    """)
    return any(status not in indexed and normalize_run_status(status) in {"queued", "running"}
               for (status,) in conn.rows(statuses))


def _active_run_session_ids(conn) -> set[str]:
    """Sessions with a non-terminal run — the non-live half of the active view
    (contract A5). Uses the run store's own normalized status family
    (``queued``/``running`` → raw ``pending``/``queued``/``processing``/
    ``running``) so aliases and a controller-down stale ``running`` row still
    count, and is NOT window-limited so a stuck-queued job older than the
    history cutoff can't disappear from the active view. Non-terminal runs are
    few (in-flight work drains), so the set stays small."""
    raw_statuses = _status_query_values("queued") + _status_query_values("running")
    stmt = select(agent_runs.c.session_id).where(agent_runs.c.status.in_(raw_statuses)).distinct()
    return {row[0] for row in conn.rows(stmt) if row[0]}


def _resolve_candidates(conn, live_ids: set[str], cutoff_iso: str) -> set[str]:
    """The set of session ids that become nodes.

    Always the live sessions, plus every session with a run in the window and
    the sessions those runs reference as caller (``source_actor``) or callback
    target — so tree roots, report targets, and (per contract A5) queued work
    render. ``_filter_nodes`` then narrows to the active (non-terminal) or full
    (history) view; this stays broad so a queued-but-not-live session is a
    candidate the active filter can keep.
    """
    candidates: set[str] = set(live_ids)
    # Window match = any recent activity, not just creation: a long run created
    # before the cutoff but finished/failed/canceled inside the window must
    # still surface. ``updated_at`` alone captures all of these — it is set to
    # ``created_at`` on insert and bumped to the transition time (equal to
    # ``completed_at`` on terminal states) on every status change, so it is a
    # superset of the created/completed timestamps. Using the single column lets
    # this frequently-polled scan ride the ``ix_agent_runs_updated`` index
    # instead of a full table scan (the other agent_runs indexes all lead with a
    # non-timestamp column and cannot serve a bare range predicate).
    r = agent_runs.c
    rowid = literal_column("agent_runs.rowid")
    stmt = select(rowid, r.session_id, r.source_kind, r.source_actor).where(r.updated_at >= cutoff_iso)
    recent_rowids = set()
    sessions = set()
    for rid, session_id, source_kind, source_actor in conn.rows(stmt):
        recent_rowids.add(rid)
        sessions.add(session_id)
        if session_id:
            candidates.add(session_id)
        if source_kind == "agent" and source_actor:
            candidates.add(source_actor)

    # A callback target earns a candidate only for a run touched in-window.
    # These two columns follow the result blobs: read them from the covering
    # session/callback index, not the table once per recent run. A repaired
    # run with no session of its own may still point to a real callback target.
    callback = select(rowid, r.callback_session_id).where(
        r.callback_session_id.is_not(None), r.callback_status.is_not(None), r.callback_status != "",
    )
    batches = [r.session_id.in_(batch) for batch in conn.batches((s for s in sessions if s), reserved=1)]
    if None in sessions:
        batches.append(r.session_id.is_(None))
    if "" in sessions:
        batches.append(r.session_id == "")
    for belongs in batches:
        for rid, target in conn.rows(callback.where(belongs)):
            if rid in recent_rowids and target:
                candidates.add(target)
    return candidates


def _load_sessions(conn, candidate_ids: set[str]) -> list[dict[str, Any]]:
    cols = [
        agent_sessions.c.id,
        agent_sessions.c.scope_id,
        agent_sessions.c.agent_name,
        agent_sessions.c.agent_backend,
        agent_sessions.c.model,
        agent_sessions.c.reasoning_effort,
        agent_sessions.c.workdir,
        agent_sessions.c.title,
        agent_sessions.c.status,
        agent_sessions.c.visibility,
        agent_sessions.c.created_at,
        agent_sessions.c.updated_at,
        agent_sessions.c.last_active_at,
        scopes.c.display_name.label("scope_display_name"),
        scopes.c.native_id.label("scope_native_id"),
        scopes.c.platform.label("scope_platform"),
        scopes.c.scope_type.label("scope_scope_type"),
        scopes.c.native_type.label("scope_native_type"),
        scope_settings.c.enabled.label("scope_enabled"),
        agents.c.name.label("catalog_agent_name"),
        agents.c.archived_at.label("catalog_agent_archived_at"),
        agents.c.metadata_json.label("catalog_agent_metadata_json"),
    ]
    stmt = (
        select(*cols)
        .select_from(
            agent_sessions
            .outerjoin(scopes, agent_sessions.c.scope_id == scopes.c.id)
            .outerjoin(scope_settings, agent_sessions.c.scope_id == scope_settings.c.scope_id)
            .outerjoin(
                agents,
                or_(
                    agents.c.id == agent_sessions.c.agent_id,
                    and_(
                        agent_sessions.c.agent_id.is_(None),
                        agents.c.name == agent_sessions.c.agent_name,
                    ),
                ),
            )
        )
    )
    return [row for batch in conn.batches(candidate_ids)
            for row in conn.rows(stmt.where(agent_sessions.c.id.in_(batch)), mappings=True)]


def _session_agent_display_name(row: Mapping[str, Any]) -> Optional[str]:
    internal_name = str(row.get("agent_name") or "").strip()
    catalog_name = str(row.get("catalog_agent_name") or "").strip()
    if row.get("catalog_agent_archived_at"):
        try:
            metadata = json.loads(row.get("catalog_agent_metadata_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        archive = metadata.get("_avibe_archive") if isinstance(metadata, dict) else None
        if isinstance(archive, dict):
            original_name = str(archive.get("original_name") or "").strip()
            if original_name:
                return original_name
    return catalog_name or internal_name or None


def _run_totals(conn, session_ids):
    """Full-history counts and nonterminal status, from the covering status index."""
    r = agent_runs.c
    stmt = select(r.session_id, r.status, func.count()).group_by(r.session_id, r.status)
    totals = {}
    rows = (row for batch in conn.batches(session_ids) for row in conn.rows(stmt.where(r.session_id.in_(batch))))
    for sid, status, count in rows:
        total, running, pending = totals.get(sid, (0, 0, False))
        status = normalize_run_status(status)
        totals[sid] = (total + count, running + (count if status == "running" else 0),
                       pending or status in {"queued", "running"})
    return totals


def _load_runs(conn, session_ids: set[str], visible: set[str], cutoff: datetime) -> dict[str, list[dict[str, Any]]]:
    """Hydrate runs that can contribute a timeline or an edge in this wave.

    `created_at` and the callback fields follow unbounded text/JSON on the
    table record. Reading them from every row (even without selecting the
    blobs) walks overflow pages. Read creation times and endpoint membership
    from covering indexes; touch later table columns only when needed.
    """
    r = agent_runs.c
    rowid = literal_column("agent_runs.rowid").label("rid")
    times = {}
    by_session = {}
    needed = set()
    times_stmt = select(rowid, r.session_id, r.created_at)
    for rid, sid, raw in (row for b in conn.batches(session_ids) for row in conn.rows(times_stmt.where(r.session_id.in_(b)))):
        instant = _parse_iso(raw) or _EPOCH
        times[rid] = (raw, instant)
        by_session.setdefault(sid, []).append(rid)
        if instant >= cutoff:
            needed.add(rid)  # potential trigger; definition/type are before payloads
    for ids in by_session.values():
        # Keep all ties at the timeline boundary until we have the real run
        # IDs to break them. The raw maximum separately decides session/edge
        # insertion order (the legacy SQL ORDER BY created_at DESC, id DESC).
        boundary = sorted((times[k][1] for k in ids), reverse=True)[min(RUNS_PER_NODE, len(ids)) - 1]
        raw_max = max(times[k][0] for k in ids)
        needed.update(k for k in ids if times[k][1] >= boundary or times[k][0] == raw_max)

    spawn = select(rowid, r.source_actor).where(r.source_kind == "agent")
    cb = select(rowid, r.callback_session_id, r.callback_status).where(
        r.callback_session_id.is_not(None), r.callback_status.is_not(None), r.callback_status != "",
    )
    callbacks = {}
    for batch in conn.batches(session_ids, reserved=1):
        needed.update(k for k, actor in conn.rows(spawn.where(r.session_id.in_(batch))) if actor in visible)
        callbacks.update((k, (target, status)) for k, target, status in conn.rows(cb.where(r.session_id.in_(batch)))
                         if target in visible)
    needed.update(callbacks)

    grouped = {}
    for batch in conn.batches(needed):
        head = select(rowid, r.session_id, r.id, r.status, r.run_type, r.definition_id, r.source_kind, r.source_actor)
        for data in conn.rows(head.where(rowid.in_(batch)), mappings=True):
            k = data["rid"]
            data["created_at"], data["instant"] = times[k]
            if k in callbacks:
                data["callback_session_id"], data["callback_status"] = callbacks[k]
            grouped.setdefault(data["session_id"], []).append(data)

    def raw_order(item):
        return max((row["created_at"], row["id"]) for row in item[1])

    grouped = dict(sorted(grouped.items(), key=raw_order, reverse=True))
    timeline = {}
    for runs in grouped.values():
        runs.sort(key=lambda r: (r["instant"], r["id"]), reverse=True)
        timeline.update((r["rid"], r) for r in runs[:RUNS_PER_NODE])
    for batch in conn.batches(timeline):
        for k, started, completed in conn.rows(select(rowid, r.started_at, r.completed_at).where(rowid.in_(batch))):
            timeline[k].update(started_at=started, completed_at=completed)

    # A sent callback always draws an edge, even in failure-only mode. For
    # the other candidates evaluate the JSON predicate only on their own rows.
    if callbacks:
        by_callback = {run["rid"]: run for runs in grouped.values() for run in runs if run["rid"] in callbacks}
        for batch in conn.batches((k for k, (_, status) in callbacks.items() if status != "sent"), reserved=1):
            mode = func.json_extract(r.metadata_json, "$.callback_mode")
            for k, callback_mode in conn.rows(select(rowid, mode).where(rowid.in_(batch))):
                by_callback[k]["callback_mode"] = callback_mode
        # Only the newest drawable callback per (sender, target) contributes
        # its delivery ID. Reading that trailing field on all historical sent
        # callbacks can otherwise cost as much as reading all the payloads.
        latest = {}
        for sid, runs in grouped.items():
            for run in runs:
                if _draws_callback_edge(run):
                    latest.setdefault((sid, run["callback_session_id"]), run)
        for batch in conn.batches(run["rid"] for run in latest.values()):
            for k, callback_id in conn.rows(select(rowid, r.callback_run_id).where(rowid.in_(batch))):
                by_callback[k]["callback_run_id"] = callback_id
    return grouped


def _draws_callback_edge(run: Mapping[str, Any]) -> bool:
    """A callback edge needs a recorded status (A4). A run that asked for no callback only reports a failure to its
    sender (``docs/plans/bots.md`` D4), so it draws one only once that notice was sent."""
    if not (run.get("callback_session_id") and run.get("callback_status")):
        return False
    return run.get("callback_mode") != "failure" or run.get("callback_status") == "sent"


def _lineage_refs(conn, frontier: set[str]) -> set[str]:
    """Advance one wave, including old delegations. The two probes use the
    source/session and session/callback indexes; only an unsent callback can
    require touching the run's metadata to decide if it draws an edge."""
    r = agent_runs.c
    # Group in index order, including the session. DISTINCT source_actor
    # alone invites SQLite to walk *all* agent-authored runs via the actor
    # index once per wave, filtering session_id from the table afterwards.
    callers = select(r.session_id, r.source_actor).where(r.source_kind == "agent").group_by(r.session_id, r.source_actor)
    targets = select(r.session_id, r.callback_session_id).where(
        r.callback_session_id.is_not(None),
        r.callback_status.is_not(None), r.callback_status != "",
        or_(r.callback_status == "sent",
            func.json_extract(r.metadata_json, "$.callback_mode").is_distinct_from("failure")),
    ).group_by(r.session_id, r.callback_session_id)
    refs = set()
    for batch in conn.batches(frontier, reserved=5, copies=2):
        wave = r.session_id.in_(batch)
        refs.update(sid for _, sid in conn.rows(callers.where(wave).union_all(targets.where(wave))) if sid)
    return refs


def _build_edges(
    runs_by_session: dict[str, list[dict[str, Any]]],
    candidate_ids: set[str],
    cutoff: datetime,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Aggregate spawn / callback / trigger edges from the candidate runs.

    Trigger edges are window-scoped (contract A10): a candidate session can carry
    OLD trigger runs (it stays in scope via other in-window activity or lineage),
    but a definition only earns a ``trigger`` edge — and thus a chip — from a run
    that fired within the window. Spawn/callback stay full-lineage so an older
    delegation's endpoints survive.
    """
    spawn: dict[tuple[str, str], dict[str, Any]] = {}
    callback: dict[tuple[str, str], dict[str, Any]] = {}
    trigger: dict[tuple[str, str], dict[str, Any]] = {}
    trigger_ids: set[str] = set()

    def _newer(existing: Optional[str], candidate: Optional[str]) -> bool:
        # Compare parsed instants, not raw strings: stored run times mix `...Z`
        # (second precision) and `.isoformat()` (microseconds/`+00:00`), which
        # sort wrong lexicographically within the same second.
        cand = _parse_iso(candidate)
        if cand is None:
            return False
        prev = _parse_iso(existing)
        return prev is None or prev < cand

    for session_id, runs in runs_by_session.items():
        for run in runs:
            created = run.get("created_at")
            # Callback deliveries use source_kind='callback'. Every agent child
            # remains independent delegation, even when it targets the parent
            # run's callback session.
            if run.get("source_kind") == "agent" and run.get("source_actor"):
                key = (run["source_actor"], session_id)
                agg = spawn.setdefault(key, {"run_count": 0, "last_run_id": None, "last_at": None})
                agg["run_count"] += 1
                if agg["last_at"] is None or _newer(agg["last_at"], created):
                    agg["last_at"] = created
                    agg["last_run_id"] = run.get("id")
            # callback: this session → report target
            # Contract A4: only emit a callback edge once a callback_status is
            # recorded. A run that merely routes a callback (sync-delegated,
            # null status — nothing will be delivered) produces no edge.
            if _draws_callback_edge(run):
                key = (session_id, run["callback_session_id"])
                agg = callback.setdefault(key, {"status": None, "last_run_id": None, "last_at": None})
                if agg["last_at"] is None or _newer(agg["last_at"], created):
                    agg["last_at"] = created
                    agg["last_run_id"] = run.get("callback_run_id") or run.get("id")
                    agg["status"] = run.get("callback_status")
            # trigger: definition → this session. A10: only an IN-WINDOW trigger
            # run earns a chip. A candidate session can carry old trigger runs (it
            # stays in scope via other in-window activity or lineage); a historical
            # or since-disabled definition must not resurface just because its old
            # session lingers.
            if (
                run.get("run_type") in _TRIGGER_RUN_TYPES
                and run.get("definition_id")
                and (run_at := _parse_iso(created)) is not None
                and run_at >= cutoff
            ):
                definition_id = run["definition_id"]
                trigger_ids.add(definition_id)
                key = (definition_id, session_id)
                agg = trigger.setdefault(key, {"run_count": 0, "last_at": None})
                agg["run_count"] += 1
                if agg["last_at"] is None or _newer(agg["last_at"], created):
                    agg["last_at"] = created

    edges: list[dict[str, Any]] = []
    for (src, dst), agg in spawn.items():
        edges.append({
            "kind": "spawn", "from": src, "to": dst,
            "run_count": agg["run_count"],
            "last_run_id": agg["last_run_id"], "last_at": _iso_z(agg["last_at"]),
        })
    for (src, dst), agg in callback.items():
        edges.append({
            "kind": "callback", "from": src, "to": dst,
            "status": agg["status"],
            "last_run_id": agg["last_run_id"], "last_at": _iso_z(agg["last_at"]),
        })
    for (definition_id, dst), agg in trigger.items():
        edges.append({
            "kind": "trigger", "from": f"def:{definition_id}", "to": dst,
            "run_count": agg["run_count"], "last_at": _iso_z(agg["last_at"]),
        })
    return edges, trigger_ids


def _load_trigger_nodes(conn, trigger_ids: set[str]) -> list[dict[str, Any]]:
    if not trigger_ids:
        return []
    stmt = select(
        run_definitions.c.id,
        run_definitions.c.definition_type,
        run_definitions.c.name,
        run_definitions.c.cron,
        run_definitions.c.run_at,
        run_definitions.c.enabled,
    )
    nodes: list[dict[str, Any]] = []
    batches = conn.batches(sorted(trigger_ids))
    for data in (row for b in batches for row in conn.rows(stmt.where(run_definitions.c.id.in_(b)), mappings=True)):
        nodes.append({
            "definition_id": data["id"],
            "definition_type": data["definition_type"],
            "name": data.get("name"),
            "schedule_label": _schedule_label(data),
            "enabled": bool(data.get("enabled")),
        })
    return nodes


def _add_run_details(node, runs, totals) -> None:
    total, running, pending = totals
    if not node["live"]:
        node["status"] = "queued" if pending else _node_status(None, runs[:1])[0]
    node["run_counts"] = {"total": total, "running": running}
    node["runs"] = [
        {"id": r["id"], "status": normalize_run_status(r["status"]), "run_type": r.get("run_type"),
         "created_at": _iso_z(r.get("created_at")), "started_at": _iso_z(r.get("started_at")),
         "completed_at": _iso_z(r.get("completed_at"))}
        for r in runs[:RUNS_PER_NODE]
    ]


def _build_nodes(
    session_rows: list[dict[str, Any]],
    live_by_session: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for row in session_rows:
        session_id = row["id"]
        live = live_by_session.get(session_id)
        status, is_live = _node_status(live["state"] if live else None, [])

        # Hide user-deleted chats (archived session) and sessions under an
        # archived project (avibe project scope with scope_settings.enabled=0) —
        # other session lists hide both, so they must not reappear as non-live
        # history nodes. A live row still shows so its runtime can be ended.
        is_archived_project = row.get("scope_scope_type") == "project" and row.get("scope_enabled") == 0
        if (row.get("status") == "archived" or is_archived_project) and not is_live:
            continue


        # A private ``vibe agent run`` session hangs on the legacy
        # ``private_agent_run`` pseudo-scope (placeholder platform, no delivery).
        # Mirror the running-agents enrichment: render it as an internal run
        # (no platform/scope label/project) and not openable — never label it as
        # an IM channel session or offer a chat link. M1 re-parents sessions off
        # this pseudo-scope, so this branch is a no-op post-migration.
        is_private_run = row.get("scope_native_type") == "private_agent_run"
        platform = None if is_private_run else row.get("scope_platform")
        scope_label = None if is_private_run else _scope_label(
            row.get("scope_display_name"), row.get("scope_native_id")
        )
        project_id = None if is_private_run else _project_id(
            row.get("scope_platform"), row.get("scope_scope_type"), row.get("scope_native_id")
        )

        node: dict[str, Any] = {
            "session_id": session_id,
            "title": row.get("title"),
            "agent_name": row.get("agent_name"),
            "agent_display_name": _session_agent_display_name(row),
            "agent_backend": row.get("agent_backend"),
            "model": row.get("model"),
            "reasoning_effort": row.get("reasoning_effort"),
            "status": status,
            "live": is_live,
            "scope_id": row.get("scope_id"),
            "project_id": project_id,
            "scope_label": scope_label,
            "platform": platform,
            "workdir": row.get("workdir"),
            "openable_in_chat": session_openable_in_chat(
                session_id=session_id, scope_native_type=row.get("scope_native_type")
            ),
            "created_at": _iso_z(row.get("created_at")),
            "last_active_at": _iso_z(row.get("last_active_at")),
            "elapsed_seconds": (live or {}).get("elapsed_seconds") if is_live else None,
            "run_counts": {},
            "runs": [],
            # Legacy rows backfill to foreground (contract §1).
            "visibility": row.get("visibility") or "foreground",
        }
        nodes.append(node)
    return nodes


def _filter_nodes(
    nodes: list[dict[str, Any]],
    *,
    project: str,
    include_ended: bool,
    include_background: bool,
) -> list[dict[str, Any]]:
    result = nodes
    if not include_ended:
        # Active view (contract A5) = non-terminal work: live sessions plus
        # queued (accepted-but-not-started) runs. Only terminal non-live nodes
        # (succeeded/failed/canceled) are dropped.
        result = [n for n in result if n["live"] or n["status"] == "queued"]
    if project and project != "all":
        if project == "standalone":
            result = [n for n in result if not n["scope_id"]]
        else:
            target = f"avibe::project::{project}"
            result = [n for n in result if n["scope_id"] == target]
    # A ``system`` session is the RUNTIME's own row (today: the workspace-notifications
    # sink that harness failure notices fall back to — see
    # ``storage.agent_session_rows.resolve_workspace_notice_session``). It is not agent
    # work, has no backend and runs no turns, so it is not a node under EITHER toggle;
    # ``include_background`` is a user preference about the user's own hidden sessions and
    # does not mean "show me the runtime's". Excluded EXPLICITLY rather than left to the
    # ``!= 'background'`` test below, which admits by default: today the row owns no runs
    # so it never even becomes a candidate, but a single run pointing at it (a repaired
    # ``session_id``, an imported row, a future self-heal run) would otherwise make it a
    # History-view node counted as ``foreground``. Pinned by
    # ``tests/test_workspace_system_session.py``.
    result = [n for n in result if n.get("visibility") != "system"]
    if not include_background:
        result = [n for n in result if n.get("visibility") != "background"]
    return result


def _node_sort_key(node: dict[str, Any]) -> tuple[int, str]:
    # Live first, then most-recently-active; deterministic tie-break on id.
    activity = node.get("last_active_at") or node.get("created_at") or ""
    return (0 if node["live"] else 1, _invert_iso(activity) + node["session_id"])


def _invert_iso(value: str) -> str:
    # Sort ISO timestamps descending under an ascending sort by inverting bytes.
    return "".join(chr(0x10FFFF - ord(c)) if ord(c) < 0x10FFFF else c for c in value)


def _session_sort_key(row: dict[str, Any], live_ids: set[str]) -> tuple[int, str]:
    # Mirror _node_sort_key on a raw session row so candidates can be ranked (and
    # capped) before their run histories are loaded.
    activity = row.get("last_active_at") or row.get("created_at") or ""
    sid = row["id"]
    return (0 if sid in live_ids else 1, _invert_iso(activity) + sid)


def _prefilter_candidate_rows(
    rows: list[dict[str, Any]],
    *,
    project: str,
    include_background: bool,
    live_ids: set[str],
) -> list[dict[str, Any]]:
    # The session-level visibility filters that _build_nodes / _filter_nodes also
    # apply, hoisted ahead of the run-load cap so hidden rows (archived chats,
    # sessions under a disabled project, backgrounds, other projects) can't
    # consume the cap and starve visible sessions. include_ended is deliberately
    # excluded — it needs the run-derived status, so it stays a post-load filter.
    def _hidden(r: dict[str, Any]) -> bool:
        # Mirror _build_nodes: archived chat or archived (disabled) project,
        # unless the session is live.
        if r["id"] in live_ids:
            return False
        archived_project = r.get("scope_scope_type") == "project" and r.get("scope_enabled") == 0
        return r.get("status") == "archived" or archived_project

    result = [r for r in rows if not _hidden(r)]
    # Mirror _filter_nodes' unconditional ``system`` exclusion here too, so a runtime row
    # cannot consume the run-load cap on its way to being dropped.
    result = [r for r in result if (r.get("visibility") or "foreground") != "system"]
    if not include_background:
        result = [r for r in result if (r.get("visibility") or "foreground") != "background"]
    if project and project != "all":
        if project == "standalone":
            result = [r for r in result if not r.get("scope_id")]
        else:
            target = f"avibe::project::{project}"
            result = [r for r in result if r.get("scope_id") == target]
    return list(result)


def _cap_nodes(nodes: list[dict[str, Any]], node_cap: int) -> tuple[list[dict[str, Any]], bool]:
    ordered = sorted(nodes, key=_node_sort_key)
    if len(ordered) > node_cap:
        return ordered[:node_cap], True
    return ordered, False


def _filter_edges(
    edges: list[dict[str, Any]],
    node_ids: set[str],
    trigger_ids: set[str],
) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for edge in edges:
        if edge["kind"] == "trigger":
            definition_id = edge["from"][len("def:"):]
            if definition_id in trigger_ids and edge["to"] in node_ids:
                kept.append(edge)
        else:
            if edge["from"] in node_ids and edge["to"] in node_ids:
                kept.append(edge)
    return kept


def _prune_triggers(
    trigger_nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    referenced = {e["from"][len("def:"):] for e in edges if e["kind"] == "trigger"}
    return [t for t in trigger_nodes if t["definition_id"] in referenced]


def _counts(nodes: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "active": 0, "idle": 0, "orphan": 0, "queued": 0,
        "succeeded": 0, "failed": 0, "canceled": 0,
        "live": 0, "ended": 0, "background": 0, "foreground": 0, "total": len(nodes),
    }
    for node in nodes:
        status = node["status"]
        if status in counts:
            counts[status] += 1
        if node["live"]:
            counts["live"] += 1
        else:
            counts["ended"] += 1
        if node.get("visibility") == "background":
            counts["background"] += 1
        else:
            counts["foreground"] += 1
    return counts
