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
  set; status from ``callback_status``.
- **Trigger edge** (definition → carrying session): runs with
  ``run_type in ('scheduled','watch')`` grouped by ``definition_id``.

Scope is joined LEFT (``scope_id`` is nullable): a NULL scope renders as the
``独立`` (standalone) bucket.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.engine import Engine

from storage.background import _status_query_values, normalize_run_status
from storage.db import create_sqlite_engine
from storage.models import agent_runs, agent_sessions, run_definitions, scope_settings, scopes

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

    owned_engine = engine is None
    engine = engine or create_sqlite_engine()
    try:
        with engine.connect() as conn:
            # The candidate source differs by view. History (include_ended) uses
            # the window scan. Active (contract A5 = live + queued) uses the live
            # set plus every session with a non-terminal run, independent of the
            # window and of the raw status alias — so a stuck-queued job older
            # than the cutoff, or a controller-down stale ``running`` row, can't
            # disappear from the active view.
            if include_ended:
                candidate_ids = _resolve_candidates(conn, live_ids, cutoff_iso)
            else:
                candidate_ids = set(live_ids) | _active_run_session_ids(conn)
            if not candidate_ids:
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

            runs_by_session = _load_runs(conn, loaded_ids)
            # Pull in lineage sessions referenced by a retained run — even a
            # spawn/callback older than the window (e.g. a long-lived live
            # session's original delegation) — so the edge and its "who started
            # it / reports to" endpoints survive. Fixed point, but track attempted
            # ids in ``loaded_ids`` (monotonic): a run can point at a session id
            # with no ``agent_sessions`` row (stale source_actor/callback from
            # imported or repaired state) which never lands in ``session_by_id``,
            # so keying the loop off that alone would spin forever.
            while True:
                extra = _lineage_refs(runs_by_session) - loaded_ids
                if not extra:
                    break
                loaded_ids |= extra
                runs_by_session.update(_load_runs(conn, extra))
                for row in _load_sessions(conn, extra):
                    session_by_id.setdefault(row["id"], row)
            session_rows = list(session_by_id.values())
            edges, trigger_ids = _build_edges(runs_by_session, loaded_ids)
            trigger_nodes = _load_trigger_nodes(conn, trigger_ids)
    finally:
        if owned_engine:
            engine.dispose()

    nodes = _build_nodes(session_rows, runs_by_session, live_by_session)
    nodes = _filter_nodes(nodes, project=project, include_ended=include_ended,
                          include_background=include_background)
    nodes, cap_truncated = _cap_nodes(nodes, node_cap)
    truncated = candidate_truncated or cap_truncated

    node_ids = {n["session_id"] for n in nodes}
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
    return {row[0] for row in conn.execute(stmt) if row[0]}


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
    stmt = select(
        agent_runs.c.session_id,
        agent_runs.c.source_kind,
        agent_runs.c.source_actor,
        agent_runs.c.callback_session_id,
        agent_runs.c.callback_status,
    ).where(agent_runs.c.updated_at >= cutoff_iso)
    for row in conn.execute(stmt).mappings():
        if row["session_id"]:
            candidates.add(row["session_id"])
        if row["source_kind"] == "agent" and row["source_actor"]:
            candidates.add(row["source_actor"])
        # Only a callback target that will draw an edge (A4: non-null status) —
        # a bare return route must not promote an unconnected root node.
        if row["callback_session_id"] and row["callback_status"]:
            candidates.add(row["callback_session_id"])
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
    ]
    stmt = (
        select(*cols)
        .select_from(
            agent_sessions
            .outerjoin(scopes, agent_sessions.c.scope_id == scopes.c.id)
            .outerjoin(scope_settings, agent_sessions.c.scope_id == scope_settings.c.scope_id)
        )
        .where(agent_sessions.c.id.in_(candidate_ids))
    )
    return [dict(row) for row in conn.execute(stmt).mappings()]


def _load_runs(conn, candidate_ids: set[str]) -> dict[str, list[dict[str, Any]]]:
    """Every run belonging to a candidate session, newest first, grouped by
    session. Full lineage (not window-limited) so aggregates + edges are
    accurate for a session once it is in scope."""
    stmt = (
        select(
            agent_runs.c.id,
            agent_runs.c.session_id,
            agent_runs.c.status,
            agent_runs.c.run_type,
            agent_runs.c.definition_id,
            agent_runs.c.source_kind,
            agent_runs.c.source_actor,
            agent_runs.c.parent_run_id,
            agent_runs.c.callback_session_id,
            agent_runs.c.callback_status,
            agent_runs.c.callback_run_id,
            agent_runs.c.created_at,
            agent_runs.c.started_at,
            agent_runs.c.completed_at,
        )
        .where(agent_runs.c.session_id.in_(candidate_ids))
        .order_by(agent_runs.c.created_at.desc(), agent_runs.c.id.desc())
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(stmt).mappings():
        data = dict(row)
        grouped.setdefault(data["session_id"], []).append(data)
    # Re-sort newest-first in Python: stored run times mix `...Z` (seconds) and
    # `.isoformat()` (`+00:00`, microseconds), so SQLite's text order_by can rank
    # two same-second runs wrong and leave a stale run at runs[0] — which drives
    # the node status, the recent-runs timeline, and the active-view filter.
    for runs in grouped.values():
        runs.sort(key=lambda r: (_parse_iso(r.get("created_at")) or _EPOCH, r["id"]), reverse=True)
    return grouped


def _lineage_refs(runs_by_session: dict[str, list[dict[str, Any]]]) -> set[str]:
    """Session ids referenced as a spawn caller or callback target by any run."""
    refs: set[str] = set()
    for runs in runs_by_session.values():
        for run in runs:
            if run.get("source_kind") == "agent" and run.get("source_actor"):
                refs.add(run["source_actor"])
            # Only follow a callback target that will actually draw an edge
            # (A4: callback edges need a non-null status). A run that merely
            # recorded a return route otherwise promotes an unconnected root that
            # consumes cap/count with no report edge.
            if run.get("callback_session_id") and run.get("callback_status"):
                refs.add(run["callback_session_id"])
    return refs


def _build_edges(
    runs_by_session: dict[str, list[dict[str, Any]]],
    candidate_ids: set[str],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Aggregate spawn / callback / trigger edges from the candidate runs."""
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

    # Callback target per run id — an explicit callback-delivery run
    # (source_kind='agent', parent_run_id → the delegated run) reports INTO the
    # delegated run's callback session. Such a report row must NOT be counted as
    # a spawn (it would draw a misleading callee→caller edge and make the caller
    # look "started by" the callee). Detect it: the run's session equals its
    # parent run's callback target.
    callback_target_by_run: dict[str, str] = {}
    for runs in runs_by_session.values():
        for run in runs:
            if run.get("callback_session_id"):
                callback_target_by_run[run["id"]] = run["callback_session_id"]

    for session_id, runs in runs_by_session.items():
        for run in runs:
            created = run.get("created_at")
            parent = run.get("parent_run_id")
            is_callback_delivery = bool(parent) and callback_target_by_run.get(parent) == session_id
            # spawn: caller (source_actor) → this session (excluding callback-
            # delivery reports, which run in the caller's session by design)
            if run.get("source_kind") == "agent" and run.get("source_actor") and not is_callback_delivery:
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
            if run.get("callback_session_id") and run.get("callback_status"):
                key = (session_id, run["callback_session_id"])
                agg = callback.setdefault(key, {"status": None, "last_run_id": None, "last_at": None})
                if agg["last_at"] is None or _newer(agg["last_at"], created):
                    agg["last_at"] = created
                    agg["last_run_id"] = run.get("callback_run_id") or run.get("id")
                    agg["status"] = run.get("callback_status")
            # trigger: definition → this session
            if run.get("run_type") in _TRIGGER_RUN_TYPES and run.get("definition_id"):
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
    ).where(run_definitions.c.id.in_(trigger_ids))
    nodes: list[dict[str, Any]] = []
    for row in conn.execute(stmt).mappings():
        data = dict(row)
        nodes.append({
            "definition_id": data["id"],
            "definition_type": data["definition_type"],
            "name": data.get("name"),
            "schedule_label": _schedule_label(data),
            "enabled": bool(data.get("enabled")),
        })
    return nodes


def _build_nodes(
    session_rows: list[dict[str, Any]],
    runs_by_session: dict[str, list[dict[str, Any]]],
    live_by_session: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for row in session_rows:
        session_id = row["id"]
        runs = runs_by_session.get(session_id, [])
        live = live_by_session.get(session_id)
        status, is_live = _node_status(live["state"] if live else None, runs)

        # Hide user-deleted chats (archived session) and sessions under an
        # archived project (avibe project scope with scope_settings.enabled=0) —
        # other session lists hide both, so they must not reappear as non-live
        # history nodes. A live row still shows so its runtime can be ended.
        is_archived_project = row.get("scope_scope_type") == "project" and row.get("scope_enabled") == 0
        if (row.get("status") == "archived" or is_archived_project) and not is_live:
            continue

        run_count_total = len(runs)
        run_count_running = sum(1 for r in runs if normalize_run_status(r["status"]) == "running")
        recent = [
            {
                "id": r["id"],
                "status": normalize_run_status(r["status"]),
                "run_type": r.get("run_type"),
                "created_at": _iso_z(r.get("created_at")),
                "started_at": _iso_z(r.get("started_at")),
                "completed_at": _iso_z(r.get("completed_at")),
            }
            for r in runs[:RUNS_PER_NODE]
        ]

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
            # Every persisted session is openable in chat EXCEPT the internal
            # private-agent-run pseudo-scope sessions (M1 retires those).
            "openable_in_chat": not is_private_run,
            "created_at": _iso_z(row.get("created_at")),
            "last_active_at": _iso_z(row.get("last_active_at")),
            "elapsed_seconds": (live or {}).get("elapsed_seconds") if is_live else None,
            "run_counts": {"total": run_count_total, "running": run_count_running},
            "runs": recent,
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
