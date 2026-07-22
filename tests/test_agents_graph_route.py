"""Route tests for ``GET /api/agents/graph``.

Locks three things the service unit tests can't: (1) the static ``/graph`` path
resolves to the graph handler and is NOT swallowed by the dynamic
``/api/agents/<name>`` rule; (2) query params flow through; (3) when the
controller is unreachable the route still returns a DB-only graph flagged
``live_unreachable`` instead of erroring.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_runs, agent_sessions
from storage.settings_service import upsert_scope
from vibe import internal_client
from vibe.ui_server import app


def _seed(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    # The route computes its 24h cutoff from the real wall clock (it does not
    # accept an injected ``now``), so anchor the fixture to now — a fixed
    # calendar date would fall out of the default window once the suite runs
    # more than 24h later.
    now = datetime.now(timezone.utc)
    now_z = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_iso = (now - timedelta(minutes=30)).isoformat()
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(conn, platform="avibe", scope_type="project",
                                    native_id="proj_route", now=now_z)
            for sid in ("ses_live", "ses_ended"):
                conn.execute(
                    agent_sessions.insert().values(
                        id=sid, scope_id=scope_id, agent_backend="claude", agent_variant="default",
                        session_anchor=sid, native_session_id=sid, title=sid.upper(),
                        status="active", agent_status="idle", metadata_json="{}",
                        created_at=now_z, updated_at=now_z, last_active_at=now_z,
                    )
                )
            conn.execute(
                agent_runs.insert().values(
                    id="run_e1", run_type="agent", status="succeeded", session_id="ses_ended",
                    cancel_requested=0, created_at=recent_iso,
                    started_at=recent_iso, updated_at=recent_iso,
                    metadata_json="{}",
                )
            )
    finally:
        engine.dispose()


def _mock_live(monkeypatch, agents):
    async def fake_list(**_kwargs):
        return {"status_code": 200, "body": {"agents": agents}}

    monkeypatch.setattr(internal_client, "list_running_agents", fake_list)


def _get(path):
    resp = app.test_client().get(path)
    return resp.status_code, json.loads(resp.content)


def test_graph_route_resolves_and_merges_liveness(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    _mock_live(monkeypatch, [{"session_id": "ses_live", "state": "active", "elapsed_seconds": 9.0}])

    status, body = _get("/api/agents/graph?window=24h")
    assert status == 200
    # Resolved to the graph handler (not vibe_agent_get(name="graph")).
    assert body["ok"] is True
    assert {"nodes", "edges", "trigger_nodes", "counts", "generated_at"} <= set(body)
    nodes = {n["session_id"]: n for n in body["nodes"]}
    assert nodes["ses_live"]["live"] is True and nodes["ses_live"]["status"] == "active"
    assert nodes["ses_ended"]["live"] is False and nodes["ses_ended"]["status"] == "succeeded"


def test_include_ended_flag(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    _mock_live(monkeypatch, [{"session_id": "ses_live", "state": "active"}])

    _, body = _get("/api/agents/graph?include_ended=0")
    assert {n["session_id"] for n in body["nodes"]} == {"ses_live"}


def test_controller_unreachable_degrades(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)

    async def fake_raise(**_kwargs):
        raise internal_client.InternalServerUnavailable("down")

    monkeypatch.setattr(internal_client, "list_running_agents", fake_raise)

    status, body = _get("/api/agents/graph")
    assert status == 200
    assert body["ok"] is True
    assert body.get("live_unreachable") is True
    # DB-only ⇒ nothing is live, but history still renders.
    assert all(n["live"] is False for n in body["nodes"])
    assert body["counts"]["live"] == 0


def test_graph_path_not_swallowed_by_name_route(monkeypatch, tmp_path):
    """The dynamic ``/api/agents/<name>`` rule must not capture ``graph``."""
    _seed(monkeypatch, tmp_path)
    _mock_live(monkeypatch, [])

    _, graph_body = _get("/api/agents/graph")
    assert "nodes" in graph_body  # graph handler, not the agent-detail handler

    # A genuine unknown agent name still routes to the agent-detail handler,
    # which returns an agent-shaped error (no "nodes" key) — proving the two
    # coexist.
    status, detail_body = _get("/api/agents/definitely-not-an-agent")
    assert "nodes" not in detail_body
