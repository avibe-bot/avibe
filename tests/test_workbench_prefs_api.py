"""Workbench prefs + Harness session-filter — route/api coverage.

The background-work banner's global toggle (spec req 2) is a state_meta pref
exposed at ``/api/workbench/prefs``; banner rows deep-link into a session-scoped
Harness tab (spec req 4) via ``?session_id=`` on the harness bootstrap route.
Mutations run at the ``vibe.api`` layer (like the Dock tests); the GET routes and
the session filter are exercised through the Flask-compat test client.
"""

from tests.test_ui_remote_access_auth import _save_config
from vibe import api
from vibe.ui_server import app

_BASE = "http://127.0.0.1:5123"
_NOW = "2026-07-16T00:00:00Z"


def test_banner_pref_api_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()

    assert api.get_workbench_prefs()["background_work_banner_enabled"] is True  # default ON
    assert api.set_workbench_prefs(background_work_banner_enabled=False)[
        "background_work_banner_enabled"
    ] is False
    assert api.get_workbench_prefs()["background_work_banner_enabled"] is False
    # An omitted field leaves the stored value untouched.
    assert api.set_workbench_prefs()["background_work_banner_enabled"] is False
    assert api.set_workbench_prefs(background_work_banner_enabled=True)[
        "background_work_banner_enabled"
    ] is True


def test_banner_pref_get_route_defaults_on(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()

    response = app.test_client().get("/api/workbench/prefs", base_url=_BASE)
    assert response.status_code == 200
    assert response.get_json()["background_work_banner_enabled"] is True


def _seed_watch(engine, watch_id: str, session_id: str) -> None:
    from storage.models import run_definitions

    with engine.begin() as conn:
        conn.execute(
            run_definitions.insert().values(
                id=watch_id,
                definition_type="watch",
                name=watch_id,
                session_id=session_id,
                enabled=1,
                deleted_at=None,
                created_at=_NOW,
                updated_at=_NOW,
                metadata_json="{}",
            )
        )


def test_harness_bootstrap_scopes_watches_by_session(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()
    engine = create_sqlite_engine()
    try:
        _seed_watch(engine, "w-a", "ses-A")
        _seed_watch(engine, "w-b", "ses-B")
    finally:
        engine.dispose()

    client = app.test_client()
    scoped = client.get("/api/harness/bootstrap?tab=watches&session_id=ses-A", base_url=_BASE)
    assert scoped.status_code == 200
    assert [w["id"] for w in scoped.get_json()["page"]["watches"]] == ["w-a"]

    unscoped = client.get("/api/harness/bootstrap?tab=watches", base_url=_BASE)
    assert {w["id"] for w in unscoped.get_json()["page"]["watches"]} == {"w-a", "w-b"}
