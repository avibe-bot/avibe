"""Unit tests for ``core.services.agent_graph`` (the run-graph assembly).

Seeds a real SQLite state DB with a spawn chain, a callback, task/watch
triggers, a standalone session, an ended session, and an out-of-window
session, then asserts the frozen contract §3 payload
(``docs/plans/agents-run-graph-contract.md``): node status/liveness, scope vs
标准 standalone bucketing, spawn/callback/trigger edge aggregation, window
filter, project filter, live-only mode, node cap + truncation, and the
visibility-absent graceful degradation.
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
from storage.models import agent_runs, agent_sessions, run_definitions, scopes

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
                    created=None, last_active=None, status="active") -> None:
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
            metadata_json="{}",
            created_at=_z(created),
            updated_at=_z(last_active or created),
            last_active_at=_z(last_active or created),
        )
    )


def _insert_run(conn, run_id, *, session_id, status="succeeded", run_type="agent",
                created=None, source_kind=None, source_actor=None, definition_id=None,
                callback_session_id=None, callback_status=None, started=None, completed=None) -> None:
    created = created or (NOW - timedelta(hours=1))
    conn.execute(
        agent_runs.insert().values(
            id=run_id,
            definition_id=definition_id,
            run_type=run_type,
            status=status,
            source_kind=source_kind,
            source_actor=source_actor,
            parent_run_id=None,
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
            updated_at=_run_iso(created),
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
        _insert_session(conn, "ses_old", scope_id=PROJECT_SCOPE, backend="claude", title="Old",
                        created=NOW - timedelta(days=3), last_active=NOW - timedelta(days=3))

        _insert_definition(conn, "def_daily", name="Daily draft", cron="17 10 * * *")

        # spawn ses_root → ses_child_a (2 runs); the latest also callbacks to root
        _insert_run(conn, "run_a1", session_id="ses_child_a", source_kind="agent",
                    source_actor="ses_root", created=NOW - timedelta(minutes=40))
        _insert_run(conn, "run_a2", session_id="ses_child_a", source_kind="agent",
                    source_actor="ses_root", callback_session_id="ses_root",
                    callback_status="pending", created=NOW - timedelta(minutes=20))
        # spawn ses_root → ses_child_b (1 run)
        _insert_run(conn, "run_b1", session_id="ses_child_b", source_kind="agent",
                    source_actor="ses_root", status="succeeded", created=NOW - timedelta(minutes=30),
                    completed=NOW - timedelta(minutes=18))
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
    assert set(nodes) == {"ses_root", "ses_child_a", "ses_child_b", "ses_standalone", "ses_triggered"}


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
    assert counts["ended"] == 3
    assert counts["total"] == 5
    # visibility column absent (pre-M1) ⇒ everything reads foreground
    assert counts["foreground"] == 5
    assert counts["background"] == 0


# ── filters ──────────────────────────────────────────────────────────────────


def test_live_only_mode(seeded):
    payload = agent_graph.build_graph(live_agents=LIVE, now=NOW, engine=seeded, include_ended=False)
    nodes = _nodes_by_id(payload)
    assert set(nodes) == {"ses_root", "ses_child_a"}
    # edges to dropped nodes are gone; the callback within the live pair stays
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


def test_visibility_absent_omits_field(seeded):
    # Until M1 ships the column, nodes must not carry ``visibility`` so the
    # client hides the 移到前台/隐藏 actions instead of firing a 400 PATCH.
    nodes = agent_graph.build_graph(live_agents=LIVE, now=NOW, engine=seeded)["nodes"]
    assert all("visibility" not in n for n in nodes)


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_iso_z_normalizes_both_formats():
    assert agent_graph._iso_z("2026-07-23T02:00:00Z") == "2026-07-23T02:00:00Z"
    assert agent_graph._iso_z("2026-07-23T02:00:00.500000+00:00") == "2026-07-23T02:00:00Z"
    assert agent_graph._iso_z(None) is None


def test_node_status_resolution():
    assert agent_graph._node_status("active", None) == ("active", True)
    assert agent_graph._node_status(None, "completed") == ("succeeded", False)
    # a stale running row with no live process is surfaced as queued
    assert agent_graph._node_status(None, "running") == ("queued", False)
    assert agent_graph._node_status(None, None) == ("idle", False)


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
