"""Unit tests for ``core.services.agent_graph`` (the run-graph assembly).

Seeds a real SQLite state DB with a spawn chain, a callback, task/watch
triggers, a standalone session, an ended session, and an out-of-window
session, then asserts the frozen contract §3 payload
(``docs/plans/agents-run-graph-contract.md``): node status/liveness, scope vs
标准 standalone bucketing, spawn/callback/trigger edge aggregation, window
filter, project filter, live-only mode, node cap + truncation, and
visibility emission + background filtering.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.services import agent_graph
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_runs, agent_sessions, run_definitions, scope_settings, scopes

NOW = datetime(2026, 7, 23, 2, 0, 0, tzinfo=timezone.utc)
PROJECT_ID = "proj_x"
PROJECT_SCOPE = f"avibe::project::{PROJECT_ID}"


def _z(dt: datetime) -> str:
    """Session-style timestamp: ``…Z`` second granularity."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_iso(dt: datetime) -> str:
    """Run-style timestamp: ``.isoformat()`` with ``+00:00`` (mixed-format DB)."""
    return dt.isoformat()


@pytest.fixture()
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    yield tmp_path


def _insert_scope(conn) -> None:
    conn.execute(
        scopes.insert().values(
            id=PROJECT_SCOPE,
            platform="avibe",
            scope_type="project",
            native_id=PROJECT_ID,
            parent_scope_id=None,
            display_name="vibe-remote",
            native_type="project",
            is_private=0,
            supports_threads=1,
            metadata_json="{}",
            first_seen_at=_z(NOW - timedelta(days=10)),
            last_seen_at=_z(NOW),
            updated_at=_z(NOW),
        )
    )


def _insert_session(conn, session_id, *, scope_id, backend="claude", title=None,
                    created=None, last_active=None, status="active",
                    visibility="foreground") -> None:
    created = created or (NOW - timedelta(hours=2))
    conn.execute(
        agent_sessions.insert().values(
            id=session_id,
            scope_id=scope_id,
            agent_id=None,
            agent_name=backend,
            agent_backend=backend,
            agent_variant="default",
            model="claude-fable-5" if backend == "claude" else "gpt-5.3-codex",
            reasoning_effort="high",
            session_anchor=session_id,
            workdir=f"/tmp/{session_id}",
            native_session_id=session_id,
            title=title,
            status=status,
            agent_status="idle",
            visibility=visibility,
            metadata_json="{}",
            created_at=_z(created),
            updated_at=_z(last_active or created),
            last_active_at=_z(last_active or created),
        )
    )


def _insert_run(conn, run_id, *, session_id, status="succeeded", run_type="agent",
                created=None, source_kind=None, source_actor=None, definition_id=None,
                callback_session_id=None, callback_status=None, started=None, completed=None,
                parent_run_id=None) -> None:
    created = created or (NOW - timedelta(hours=1))
    conn.execute(
        agent_runs.insert().values(
            id=run_id,
            definition_id=definition_id,
            run_type=run_type,
            status=status,
            source_kind=source_kind,
            source_actor=source_actor,
            parent_run_id=parent_run_id,
            agent_name=None,
            agent_id=None,
            agent_backend=None,
            model=None,
            reasoning_effort=None,
            session_policy=None,
            session_id=session_id,
            legacy_session_key=None,
            post_to=None,
            deliver_key=None,
            prompt=None,
            message=None,
            message_payload_json=None,
            result_text=None,
            result_payload_json=None,
            message_ids_json=None,
            callback_session_id=callback_session_id,
            callback_status=callback_status,
            callback_error=None,
            callback_run_id=None,
            callback_completed_at=None,
            cancel_requested=0,
            cancel_requested_at=None,
            pid=None,
            exit_code=None,
            error=None,
            stdout=None,
            stderr=None,
            created_at=_run_iso(created),
            started_at=_run_iso(started or created),
            completed_at=_run_iso(completed) if completed else None,
            # Mirror production: updated_at is bumped to the latest transition
            # time (start, then completion), never left at creation time.
            updated_at=_run_iso(completed or started or created),
            metadata_json="{}",
        )
    )


def _insert_definition(conn, definition_id, *, definition_type="scheduled",
                       name=None, cron=None, run_at=None, enabled=1) -> None:
    conn.execute(
        run_definitions.insert().values(
            id=definition_id,
            definition_type=definition_type,
            name=name,
            agent_name=None,
            session_policy=None,
            session_id=None,
            legacy_session_key=None,
            prompt=None,
            message=None,
            message_payload_json=None,
            schedule_type="cron" if cron else ("at" if run_at else None),
            cron=cron,
            run_at=run_at,
            timezone="UTC",
            command_json=None,
            shell_command=None,
            prefix=None,
            cwd=None,
            mode=None,
            timeout_seconds=None,
            lifetime_timeout_seconds=None,
            retry_exit_codes_json=None,
            retry_delay_seconds=None,
            post_to=None,
            deliver_key=None,
            enabled=enabled,
            deleted_at=None,
            created_at=_run_iso(NOW - timedelta(days=1)),
            updated_at=_run_iso(NOW - timedelta(days=1)),
            last_started_at=None,
            last_finished_at=None,
            last_event_at=None,
            last_run_at=None,
            last_error=None,
            last_exit_code=None,
            last_run_id=None,
            metadata_json="{}",
        )
    )


@pytest.fixture()
def seeded(isolated_state):
    """A representative graph:

    - ses_root (project, live active) spawns ses_child_a (live idle, 2 runs)
      and ses_child_b (ended, succeeded).
    - ses_child_a reports back to ses_root (callback pending).
    - ses_standalone (scope NULL, ended failed) — a root of its own.
    - ses_triggered (project, ended) fired by scheduled task def_daily (2 runs).
    - ses_old (project) has a run 3 days ago — outside the 24h window.
    """
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        _insert_scope(conn)
        _insert_session(conn, "ses_root", scope_id=PROJECT_SCOPE, backend="claude", title="Root PM")
        _insert_session(conn, "ses_child_a", scope_id=PROJECT_SCOPE, backend="codex", title="Backend lane")
        _insert_session(conn, "ses_child_b", scope_id=PROJECT_SCOPE, backend="claude", title="Frontend lane")
        _insert_session(conn, "ses_standalone", scope_id=None, backend="codex", title="Standalone")
        _insert_session(conn, "ses_triggered", scope_id=PROJECT_SCOPE, backend="claude", title="Daily draft")
        _insert_session(conn, "ses_queued", scope_id=PROJECT_SCOPE, backend="claude", title="Queued lane")
        _insert_session(conn, "ses_old", scope_id=PROJECT_SCOPE, backend="claude", title="Old",
                        created=NOW - timedelta(days=3), last_active=NOW - timedelta(days=3))

        _insert_definition(conn, "def_daily", name="Daily draft", cron="17 10 * * *")

        # spawn ses_root → ses_child_a (2 runs); the latest also callbacks to root
        _insert_run(conn, "run_a1", session_id="ses_child_a", source_kind="agent",
                    source_actor="ses_root", created=NOW - timedelta(minutes=40))
        _insert_run(conn, "run_a2", session_id="ses_child_a", source_kind="agent",
                    source_actor="ses_root", callback_session_id="ses_root",
                    callback_status="pending", created=NOW - timedelta(minutes=20))
        # spawn ses_root → ses_child_b (1 run); it also *routes* a callback to root
        # but with NULL callback_status (sync-delegated) — A4 ⇒ no callback edge.
        _insert_run(conn, "run_b1", session_id="ses_child_b", source_kind="agent",
                    source_actor="ses_root", status="succeeded", callback_session_id="ses_root",
                    callback_status=None, created=NOW - timedelta(minutes=30),
                    completed=NOW - timedelta(minutes=18))
        # queued (accepted-but-not-started) non-live run — A5 ⇒ shows in Active.
        _insert_run(conn, "run_q1", session_id="ses_queued", status="queued",
                    created=NOW - timedelta(minutes=5))
        # standalone root, its own failed run
        _insert_run(conn, "run_s1", session_id="ses_standalone", status="failed",
                    created=NOW - timedelta(minutes=50))
        # scheduled trigger def_daily → ses_triggered (2 runs)
        _insert_run(conn, "run_t1", session_id="ses_triggered", run_type="scheduled",
                    definition_id="def_daily", created=NOW - timedelta(hours=6))
        _insert_run(conn, "run_t2", session_id="ses_triggered", run_type="scheduled",
                    definition_id="def_daily", created=NOW - timedelta(hours=2))
        # out-of-window run
        _insert_run(conn, "run_old", session_id="ses_old", created=NOW - timedelta(days=3))
    return engine


LIVE = [
    {"session_id": "ses_root", "state": "active", "elapsed_seconds": 1560.0, "backend": "claude"},
    {"session_id": "ses_child_a", "state": "idle", "elapsed_seconds": 12.0, "backend": "codex"},
]


def _nodes_by_id(payload):
    return {n["session_id"]: n for n in payload["nodes"]}


def _edge(payload, kind, src, dst):
    for e in payload["edges"]:
        if e["kind"] == kind and e["from"] == src and e["to"] == dst:
            return e
    return None


# ── happy-path assembly ──────────────────────────────────────────────────────


def test_history_payload_shape(seeded):
    payload = agent_graph.build_graph(live_agents=LIVE, window="24h", now=NOW, engine=seeded)
    assert payload["ok"] is True
    assert payload["window"] == "24h"
    assert payload["generated_at"].endswith("Z")
    assert payload["truncated"] is False

    nodes = _nodes_by_id(payload)
    # ses_old is outside the 24h window and not live → excluded.
    assert set(nodes) == {
        "ses_root", "ses_child_a", "ses_child_b", "ses_standalone", "ses_triggered", "ses_queued",
    }


def test_live_and_ended_status(seeded):
    nodes = _nodes_by_id(agent_graph.build_graph(live_agents=LIVE, now=NOW, engine=seeded))
    assert nodes["ses_root"]["live"] is True
    assert nodes["ses_root"]["status"] == "active"
    assert nodes["ses_root"]["elapsed_seconds"] == 1560.0
    assert nodes["ses_child_a"]["live"] is True
    assert nodes["ses_child_a"]["status"] == "idle"
    # ended nodes take the latest run outcome and are not live
    assert nodes["ses_child_b"]["live"] is False
    assert nodes["ses_child_b"]["status"] == "succeeded"
    assert nodes["ses_standalone"]["status"] == "failed"
    assert nodes["ses_child_b"]["elapsed_seconds"] is None


def test_scope_vs_standalone(seeded):
    nodes = _nodes_by_id(agent_graph.build_graph(live_agents=LIVE, now=NOW, engine=seeded))
    assert nodes["ses_triggered"]["project_id"] == PROJECT_ID
    assert nodes["ses_triggered"]["scope_label"] == "vibe-remote"
    assert nodes["ses_triggered"]["platform"] == "avibe"
    # standalone: NULL scope ⇒ every scope field null (独立 bucket)
    assert nodes["ses_standalone"]["scope_id"] is None
    assert nodes["ses_standalone"]["project_id"] is None
    assert nodes["ses_standalone"]["scope_label"] is None
    assert nodes["ses_standalone"]["platform"] is None


def test_every_node_openable_in_chat(seeded):
    nodes = _nodes_by_id(agent_graph.build_graph(live_agents=LIVE, now=NOW, engine=seeded))
    assert all(n["openable_in_chat"] for n in nodes.values())


def test_private_agent_run_scope_is_internal(isolated_state):
    # A private ``vibe agent run`` session on the legacy private_agent_run
    # pseudo-scope must render as an internal run: no platform/scope label/
    # project, and not openable in chat (mirrors running-agents enrichment).
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            conn.execute(
                scopes.insert().values(
                    id="slack::private::pv1", platform="slack", scope_type="private",
                    native_id="pv1", parent_scope_id=None, display_name="internal-run",
                    native_type="private_agent_run", is_private=1, supports_threads=0,
                    metadata_json="{}", first_seen_at=_z(NOW), last_seen_at=_z(NOW), updated_at=_z(NOW),
                )
            )
            _insert_session(conn, "ses_priv", scope_id="slack::private::pv1", backend="codex",
                            title="Internal run")
            _insert_run(conn, "run_p1", session_id="ses_priv", status="succeeded",
                        created=NOW - timedelta(minutes=10))
        node = _nodes_by_id(agent_graph.build_graph(live_agents=[], now=NOW, engine=engine))["ses_priv"]
        assert node["platform"] is None
        assert node["scope_label"] is None
        assert node["project_id"] is None
        assert node["openable_in_chat"] is False
    finally:
        engine.dispose()


def test_window_includes_recently_completed_run(isolated_state):
    # A run created before the 24h cutoff but completed inside it must keep its
    # session in the window (recent activity, not just creation time).
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            _insert_session(conn, "ses_lr", scope_id=None, backend="claude", title="Long run")
            _insert_run(conn, "run_lr", session_id="ses_lr", status="succeeded",
                        created=NOW - timedelta(days=3), completed=NOW - timedelta(minutes=10))
        nodes = _nodes_by_id(agent_graph.build_graph(live_agents=[], now=NOW, engine=engine, window="24h"))
        assert "ses_lr" in nodes
    finally:
        engine.dispose()


def test_callback_delivery_is_not_a_spawn(isolated_state):
    # A delegated run spawns callee and routes a callback to caller; the callee's
    # explicit callback-delivery run reports INTO the caller's session with
    # parent_run_id = the delegated run. That report must NOT create a backwards
    # spawn edge callee→caller (only the real spawn caller→callee + the callback).
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            _insert_session(conn, "ses_caller", scope_id=None, backend="claude", title="Caller")
            _insert_session(conn, "ses_callee", scope_id=None, backend="codex", title="Callee")
            _insert_run(conn, "run_deleg", session_id="ses_callee", source_kind="agent",
                        source_actor="ses_caller", callback_session_id="ses_caller",
                        callback_status="sent", created=NOW - timedelta(minutes=30))
            _insert_run(conn, "run_report", session_id="ses_caller", source_kind="agent",
                        source_actor="ses_callee", parent_run_id="run_deleg",
                        created=NOW - timedelta(minutes=10))
        payload = agent_graph.build_graph(live_agents=[], now=NOW, engine=engine)
        assert _edge(payload, "spawn", "ses_caller", "ses_callee") is not None
        assert _edge(payload, "spawn", "ses_callee", "ses_caller") is None
        assert _edge(payload, "callback", "ses_callee", "ses_caller") is not None
    finally:
        engine.dispose()


def test_live_node_lineage_survives_old_window(isolated_state):
    # A live session spawned by a run OLDER than the window: the caller has no
    # in-window run, but its lineage must still be loaded so the spawn edge and
    # the caller node survive (not a human-started root).
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            _insert_session(conn, "ses_caller", scope_id=None, backend="claude", title="Caller")
            _insert_session(conn, "ses_livekid", scope_id=None, backend="codex", title="Live kid")
            _insert_run(conn, "run_oldspawn", session_id="ses_livekid", source_kind="agent",
                        source_actor="ses_caller", created=NOW - timedelta(days=3))
        live = [{"session_id": "ses_livekid", "state": "active", "elapsed_seconds": 5.0}]
        payload = agent_graph.build_graph(live_agents=live, now=NOW, engine=engine, window="24h")
        nodes = _nodes_by_id(payload)
        assert "ses_livekid" in nodes and nodes["ses_livekid"]["live"] is True
        assert "ses_caller" in nodes  # pulled in as lineage despite the old run
        assert _edge(payload, "spawn", "ses_caller", "ses_livekid") is not None
    finally:
        engine.dispose()


def test_lineage_expands_to_fixed_point(isolated_state):
    # A→B→C where A and B's spawn runs are older than the window and only C is
    # live: fixed-point expansion must pull in BOTH B (1 hop) and A (2 hops).
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            for sid in ("ses_a", "ses_b", "ses_c"):
                _insert_session(conn, sid, scope_id=None, backend="claude", title=sid)
            _insert_run(conn, "run_b", session_id="ses_b", source_kind="agent",
                        source_actor="ses_a", created=NOW - timedelta(days=3))
            _insert_run(conn, "run_c", session_id="ses_c", source_kind="agent",
                        source_actor="ses_b", created=NOW - timedelta(days=3))
        live = [{"session_id": "ses_c", "state": "active", "elapsed_seconds": 5.0}]
        payload = agent_graph.build_graph(live_agents=live, now=NOW, engine=engine, window="24h")
        nodes = _nodes_by_id(payload)
        assert {"ses_a", "ses_b", "ses_c"} <= set(nodes)
        assert _edge(payload, "spawn", "ses_a", "ses_b") is not None
        assert _edge(payload, "spawn", "ses_b", "ses_c") is not None
    finally:
        engine.dispose()


def test_archived_project_sessions_hidden(isolated_state):
    # A session under an archived project (avibe project scope with
    # scope_settings.enabled=0) is hidden like an archived session.
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            conn.execute(
                scopes.insert().values(
                    id="avibe::project::proj_arch", platform="avibe", scope_type="project",
                    native_id="proj_arch", parent_scope_id=None, display_name="Archived proj",
                    native_type="project", is_private=0, supports_threads=1, metadata_json="{}",
                    first_seen_at=_z(NOW), last_seen_at=_z(NOW), updated_at=_z(NOW),
                )
            )
            conn.execute(
                scope_settings.insert().values(
                    scope_id="avibe::project::proj_arch", enabled=0, role=None,
                    workdir="/tmp/arch", agent_name=None, agent_backend=None, agent_variant=None,
                    model=None, reasoning_effort=None, require_mention=None, settings_version=1,
                    settings_json="{}", created_at=_z(NOW), updated_at=_z(NOW),
                )
            )
            _insert_session(conn, "ses_ap", scope_id="avibe::project::proj_arch", backend="claude",
                            title="Under archived project")
            _insert_run(conn, "run_ap", session_id="ses_ap", status="succeeded",
                        created=NOW - timedelta(minutes=10))
        nodes = _nodes_by_id(agent_graph.build_graph(live_agents=[], now=NOW, engine=engine))
        assert "ses_ap" not in nodes
    finally:
        engine.dispose()


def test_archived_non_live_session_excluded(isolated_state):
    # An archived (user-deleted) chat with a recent run must not reappear as a
    # history node.
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            _insert_session(conn, "ses_arch", scope_id=None, backend="claude", title="Archived",
                            status="archived")
            _insert_run(conn, "run_arch", session_id="ses_arch", status="canceled",
                        created=NOW - timedelta(minutes=10))
        nodes = _nodes_by_id(agent_graph.build_graph(live_agents=[], now=NOW, engine=engine))
        assert "ses_arch" not in nodes
    finally:
        engine.dispose()


def test_spawn_edges_aggregate(seeded):
    payload = agent_graph.build_graph(live_agents=LIVE, now=NOW, engine=seeded)
    spawn_a = _edge(payload, "spawn", "ses_root", "ses_child_a")
    assert spawn_a is not None and spawn_a["run_count"] == 2
    assert spawn_a["last_run_id"] == "run_a2"  # newest of the pair
    spawn_b = _edge(payload, "spawn", "ses_root", "ses_child_b")
    assert spawn_b is not None and spawn_b["run_count"] == 1


def test_callback_edge(seeded):
    payload = agent_graph.build_graph(live_agents=LIVE, now=NOW, engine=seeded)
    cb = _edge(payload, "callback", "ses_child_a", "ses_root")
    assert cb is not None
    assert cb["status"] == "pending"


def test_a4_no_callback_edge_without_status(seeded):
    # run_b1 routes a callback to ses_root but with NULL callback_status
    # (sync-delegated); A4 ⇒ no edge emitted.
    payload = agent_graph.build_graph(live_agents=LIVE, now=NOW, engine=seeded)
    assert _edge(payload, "callback", "ses_child_b", "ses_root") is None


def test_trigger_edge_and_node(seeded):
    payload = agent_graph.build_graph(live_agents=LIVE, now=NOW, engine=seeded)
    tr = _edge(payload, "trigger", "def:def_daily", "ses_triggered")
    assert tr is not None and tr["run_count"] == 2
    triggers = {t["definition_id"]: t for t in payload["trigger_nodes"]}
    assert "def_daily" in triggers
    assert triggers["def_daily"]["definition_type"] == "scheduled"
    assert triggers["def_daily"]["schedule_label"] == "cron 17 10 * * *"
    assert triggers["def_daily"]["enabled"] is True


def test_node_runs_timeline(seeded):
    nodes = _nodes_by_id(agent_graph.build_graph(live_agents=LIVE, now=NOW, engine=seeded))
    child_a = nodes["ses_child_a"]
    assert child_a["run_counts"]["total"] == 2
    # A1 run-row shape: newest first, id + status + run_type + created/started/
    # completed (Z-normalized), capped at 10.
    assert [r["id"] for r in child_a["runs"]] == ["run_a2", "run_a1"]
    assert len(child_a["runs"]) <= agent_graph.RUNS_PER_NODE == 10
    row = child_a["runs"][0]
    assert set(row) == {"id", "status", "run_type", "created_at", "started_at", "completed_at"}
    assert row["created_at"].endswith("Z") and row["started_at"].endswith("Z")


def test_live_unreachable_flag(seeded):
    # A2: always present, default False; the route flips it True when the
    # controller is down.
    default = agent_graph.build_graph(live_agents=LIVE, now=NOW, engine=seeded)
    assert default["live_unreachable"] is False
    degraded = agent_graph.build_graph(live_agents=[], now=NOW, engine=seeded, live_unreachable=True)
    assert degraded["live_unreachable"] is True


def test_counts(seeded):
    counts = agent_graph.build_graph(live_agents=LIVE, now=NOW, engine=seeded)["counts"]
    assert counts["live"] == 2
    assert counts["active"] == 1
    assert counts["idle"] == 1
    assert counts["queued"] == 1
    assert counts["ended"] == 4
    assert counts["total"] == 6
    # seeded sessions default to foreground (no explicit visibility set)
    assert counts["foreground"] == 6
    assert counts["background"] == 0


# ── filters ──────────────────────────────────────────────────────────────────


def test_active_mode_keeps_live_and_queued(seeded):
    # A5: Active view = non-terminal (live + queued). Live ses_root/ses_child_a
    # AND the queued ses_queued stay; terminal non-live nodes drop.
    payload = agent_graph.build_graph(live_agents=LIVE, now=NOW, engine=seeded, include_ended=False)
    nodes = _nodes_by_id(payload)
    assert set(nodes) == {"ses_root", "ses_child_a", "ses_queued"}
    assert nodes["ses_queued"]["status"] == "queued"
    # edges to dropped (terminal) nodes are gone; the live-pair callback stays
    assert _edge(payload, "callback", "ses_child_a", "ses_root") is not None
    assert _edge(payload, "spawn", "ses_root", "ses_child_b") is None
    assert _edge(payload, "trigger", "def:def_daily", "ses_triggered") is None
    assert payload["trigger_nodes"] == []


def test_project_filter_standalone(seeded):
    nodes = _nodes_by_id(
        agent_graph.build_graph(live_agents=LIVE, now=NOW, engine=seeded, project="standalone")
    )
    assert set(nodes) == {"ses_standalone"}


def test_project_filter_concrete(seeded):
    nodes = _nodes_by_id(
        agent_graph.build_graph(live_agents=LIVE, now=NOW, engine=seeded, project=PROJECT_ID)
    )
    assert "ses_standalone" not in nodes
    assert "ses_root" in nodes


def test_window_widening_includes_old(seeded):
    nodes24 = _nodes_by_id(agent_graph.build_graph(live_agents=LIVE, now=NOW, engine=seeded, window="24h"))
    assert "ses_old" not in nodes24
    nodes7d = _nodes_by_id(agent_graph.build_graph(live_agents=LIVE, now=NOW, engine=seeded, window="7d"))
    assert "ses_old" in nodes7d


def test_no_live_agents_degrades_to_db_only(seeded):
    # Controller unreachable ⇒ no liveness; every node is non-live/ended.
    payload = agent_graph.build_graph(live_agents=[], now=NOW, engine=seeded)
    assert all(n["live"] is False for n in payload["nodes"])
    assert payload["counts"]["live"] == 0


def test_node_cap_truncates(seeded):
    payload = agent_graph.build_graph(live_agents=LIVE, now=NOW, engine=seeded, node_cap=2)
    assert payload["truncated"] is True
    assert len(payload["nodes"]) == 2
    # live sessions are the most significant → survive the cap
    assert {n["session_id"] for n in payload["nodes"]} == {"ses_root", "ses_child_a"}


def test_cap_applies_project_filter_before_capping(isolated_state):
    # With more candidates than the cap, a project-filtered view must still
    # return that project's session even when it is older than sessions in other
    # scopes — the pre-run candidate cap applies the project/background filter
    # first, so a busy install can't crowd the selected project out of the cap
    # (and its runs are the only ones loaded).
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        _insert_scope(conn)
        _insert_session(conn, "ses_proj", scope_id=PROJECT_SCOPE, title="Proj",
                        last_active=NOW - timedelta(hours=3))
        _insert_run(conn, "run_proj", session_id="ses_proj", created=NOW - timedelta(hours=3))
        # Two newer standalone sessions that would win a global recency cap.
        for idx in range(2):
            sid = f"ses_free{idx}"
            _insert_session(conn, sid, scope_id=None, title=f"Free{idx}",
                            last_active=NOW - timedelta(minutes=idx + 1))
            _insert_run(conn, f"run_free{idx}", session_id=sid,
                        created=NOW - timedelta(minutes=idx + 1))

    payload = agent_graph.build_graph(
        live_agents=[], now=NOW, engine=engine, node_cap=1, project=PROJECT_ID
    )
    assert [n["session_id"] for n in payload["nodes"]] == ["ses_proj"]


def test_active_view_keeps_queued_past_recency_cap(isolated_state):
    # A queued session with old last_active must still appear in the active view
    # even when newer *ended* sessions would fill a small cap — the active view
    # loads live/queued candidates directly, not a recency slice of raw sessions.
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        _insert_session(conn, "ses_q", scope_id=None, title="Queued",
                        last_active=NOW - timedelta(hours=5))
        _insert_run(conn, "run_q", session_id="ses_q", status="queued",
                    created=NOW - timedelta(hours=5))
        for idx in range(2):
            sid = f"ses_done{idx}"
            _insert_session(conn, sid, scope_id=None, title=f"Done{idx}",
                            last_active=NOW - timedelta(minutes=idx + 1))
            _insert_run(conn, f"run_done{idx}", session_id=sid, status="succeeded",
                        created=NOW - timedelta(minutes=idx + 1))

    payload = agent_graph.build_graph(
        live_agents=[], now=NOW, engine=engine, node_cap=1, include_ended=False
    )
    assert [n["session_id"] for n in payload["nodes"]] == ["ses_q"]


def test_missing_lineage_reference_terminates(isolated_state):
    # A retained run pointing at a session id with no agent_sessions row (a stale
    # source_actor/callback from imported or repaired state) must not spin the
    # lineage fixed-point loop forever: it terminates and returns a graph without
    # a node for the missing session.
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        _insert_session(conn, "ses_live", scope_id=None, title="Live")
        _insert_run(conn, "run_live", session_id="ses_live", source_kind="agent",
                    source_actor="ses_ghost")

    payload = agent_graph.build_graph(
        live_agents=[{"session_id": "ses_live", "state": "active"}], now=NOW, engine=engine
    )
    ids = {n["session_id"] for n in payload["nodes"]}
    assert "ses_live" in ids
    assert "ses_ghost" not in ids


def test_active_view_captures_nonterminal_past_window_and_aliases(isolated_state):
    # Active view (A5) surfaces any session with a non-terminal run regardless of
    # the history window or the raw status alias: a stuck queued job older than
    # the cutoff and a controller-down `running` row both count, with live_agents
    # empty (controller unreachable).
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        _insert_session(conn, "ses_oldq", scope_id=None, title="Old queued",
                        last_active=NOW - timedelta(days=3))
        _insert_run(conn, "run_oldq", session_id="ses_oldq", status="queued",
                    created=NOW - timedelta(days=3))
        _insert_session(conn, "ses_run", scope_id=None, title="Running",
                        last_active=NOW - timedelta(minutes=5))
        _insert_run(conn, "run_run", session_id="ses_run", status="running",
                    created=NOW - timedelta(minutes=5))

    payload = agent_graph.build_graph(
        live_agents=[], now=NOW, engine=engine, include_ended=False, window="1h"
    )
    # running (no live process) normalizes to queued; the 3-day-old queued job is
    # kept even though it is far outside the 1h window.
    assert {n["session_id"]: n["status"] for n in payload["nodes"]} == {
        "ses_oldq": "queued",
        "ses_run": "queued",
    }


def test_null_status_callback_target_not_promoted(isolated_state):
    # A run that records a callback route but no callback_status (A4: no edge)
    # must not promote the (otherwise inactive) target into an unconnected node.
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        _insert_session(conn, "ses_caller", scope_id=None, title="Caller")
        _insert_session(conn, "ses_target", scope_id=None, title="Target")
        _insert_run(conn, "run_c", session_id="ses_caller",
                    callback_session_id="ses_target", callback_status=None)

    payload = agent_graph.build_graph(
        live_agents=[{"session_id": "ses_caller", "state": "active"}], now=NOW, engine=engine
    )
    assert {n["session_id"] for n in payload["nodes"]} == {"ses_caller"}
    assert not any(e["kind"] == "callback" for e in payload["edges"])


def test_archived_sessions_excluded_before_cap(isolated_state):
    # Archived sessions must be dropped before the node cap so a burst of recent
    # archived chats can't consume the cap and starve an older visible session.
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        for idx in range(2):
            sid = f"ses_arch{idx}"
            _insert_session(conn, sid, scope_id=None, title=f"Arch{idx}",
                            status="archived", last_active=NOW - timedelta(minutes=idx + 1))
            _insert_run(conn, f"run_arch{idx}", session_id=sid,
                        created=NOW - timedelta(minutes=idx + 1))
        _insert_session(conn, "ses_vis", scope_id=None, title="Visible",
                        last_active=NOW - timedelta(hours=2))
        _insert_run(conn, "run_vis", session_id="ses_vis", created=NOW - timedelta(hours=2))

    payload = agent_graph.build_graph(live_agents=[], now=NOW, engine=engine, node_cap=1)
    assert [n["session_id"] for n in payload["nodes"]] == ["ses_vis"]


def test_visibility_emitted_and_filtered(isolated_state):
    # M1's ``agent_sessions.visibility`` is now a hard column: every node
    # carries it (legacy rows backfill to foreground), a background session is
    # reflected in the counts, and ``include_background=0`` drops it.
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        _insert_session(conn, "ses_fg", scope_id=None, title="Foreground")
        _insert_session(conn, "ses_bg", scope_id=None, title="Background",
                        visibility="background")
        # A window run makes each session a graph candidate (bare sessions with
        # no run and no liveness are not surfaced).
        _insert_run(conn, "run_fg", session_id="ses_fg")
        _insert_run(conn, "run_bg", session_id="ses_bg")

    full = agent_graph.build_graph(live_agents=[], now=NOW, engine=engine)
    by_id = _nodes_by_id(full)
    assert by_id["ses_fg"]["visibility"] == "foreground"
    assert by_id["ses_bg"]["visibility"] == "background"
    assert full["counts"]["foreground"] == 1
    assert full["counts"]["background"] == 1

    hidden = agent_graph.build_graph(
        live_agents=[], now=NOW, engine=engine, include_background=False
    )
    assert set(_nodes_by_id(hidden)) == {"ses_fg"}


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_iso_z_normalizes_both_formats():
    assert agent_graph._iso_z("2026-07-23T02:00:00Z") == "2026-07-23T02:00:00Z"
    assert agent_graph._iso_z("2026-07-23T02:00:00.500000+00:00") == "2026-07-23T02:00:00Z"
    assert agent_graph._iso_z(None) is None


def test_node_status_resolution():
    assert agent_graph._node_status("active", []) == ("active", True)
    assert agent_graph._node_status(None, [{"status": "completed"}]) == ("succeeded", False)
    # a stale running row with no live process is surfaced as queued
    assert agent_graph._node_status(None, [{"status": "running"}]) == ("queued", False)
    assert agent_graph._node_status(None, []) == ("idle", False)
    # any non-terminal run keeps the node active, even behind a newer finished
    # run on a reused/stuck session (runs are newest-first)
    assert agent_graph._node_status(
        None, [{"status": "succeeded"}, {"status": "queued"}]
    ) == ("queued", False)


def test_merge_live_state_prefers_active():
    assert agent_graph._merge_live_state(["idle", "active", "orphan"]) == "active"
    assert agent_graph._merge_live_state(["idle", "orphan"]) == "orphan"
    assert agent_graph._merge_live_state([]) == "idle"


def test_liveness_elapsed_from_winning_state_row():
    # Two backend rows for one session: the idle row has a larger elapsed, but
    # the node shows the ACTIVE state, so it must use the active row's elapsed.
    indexed = agent_graph._index_live_agents([
        {"session_id": "s", "state": "idle", "elapsed_seconds": 9999.0},
        {"session_id": "s", "state": "active", "elapsed_seconds": 42.0},
    ])
    assert indexed["s"]["state"] == "active"
    assert indexed["s"]["elapsed_seconds"] == 42.0
