"""Workbench Dock API: pin/unpin Show Pages, resident-tile order, auth parity.

Mutations are exercised at the ``vibe.api`` layer (like the sibling Show Pages
admin tests); the route layer is covered for the GET round-trip and, crucially,
for auth parity — a remote request without a session is bounced to login, so a
``/api/dock`` route can never be an unauthenticated native endpoint.
"""

import pytest

from core.dock_store import BUILTIN_DOCK_IDS, DockError
from core.show_pages import ShowPageError, ShowPageStore, ensure_show_page_dir
from tests.test_ui_remote_access_auth import _remote_peer, _save_config
from vibe import api
from vibe.ui_server import app


def _seed_session(session_id: str, *, title: str | None = None) -> None:
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions
    from storage.settings_service import upsert_scope

    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = messages_service._utc_now_iso()
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(conn, platform="slack", scope_type="channel", native_id=f"chan_{session_id}", now=now)
            conn.execute(
                agent_sessions.insert().values(
                    id=session_id,
                    scope_id=scope_id,
                    agent_backend="claude",
                    agent_variant="default",
                    session_anchor="anchor_" + session_id,
                    native_session_id="",
                    title=title,
                    status="active",
                    metadata_json="{}",
                    created_at=now,
                    updated_at=now,
                    last_active_at=now,
                )
            )
    finally:
        engine.dispose()


def _make_show_page(session_id: str) -> None:
    """Create the Show Page row (private) for a seeded session so it can be pinned."""
    ensure_show_page_dir(session_id)
    store = ShowPageStore()
    try:
        store.update_visibility(session_id, "private")
    finally:
        store.close()


def _show(session_id: str) -> str:
    return f"show:{session_id}"


def test_dock_default_is_builtins_only(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    result = api.get_dock()

    assert result["ok"] is True
    assert result["dock"]["order"] == list(BUILTIN_DOCK_IDS)
    assert result["dock"]["pins"] == []


def test_pin_appends_and_captures_title(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _seed_session("ses_pin", title="Sales Dashboard")
    _make_show_page("ses_pin")

    dock = api.pin_dock_show_page("ses_pin")["dock"]

    assert dock["order"] == [*BUILTIN_DOCK_IDS, _show("ses_pin")]
    assert len(dock["pins"]) == 1
    pin = dock["pins"][0]
    assert pin["session_id"] == "ses_pin"
    assert pin["title_snapshot"] == "Sales Dashboard"
    assert pin["pinned_at"]

    # Survives a reload (persisted to state_meta).
    assert api.get_dock()["dock"] == dock


def test_pin_without_session_title_stores_empty_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _seed_session("ses_plain")  # IM-dispatch sessions persist title=None
    _make_show_page("ses_plain")

    dock = api.pin_dock_show_page("ses_plain")["dock"]

    assert dock["pins"][0]["title_snapshot"] == ""


def test_pin_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _seed_session("ses_dup", title="Once")
    _make_show_page("ses_dup")

    first = api.pin_dock_show_page("ses_dup")["dock"]
    second = api.pin_dock_show_page("ses_dup")["dock"]

    assert first == second
    assert second["order"].count(_show("ses_dup")) == 1
    assert len(second["pins"]) == 1


def test_pin_multiple_sessions_all_survive(monkeypatch, tmp_path):
    # Pinning distinct sessions accumulates — a later pin must never drop an
    # earlier one (the read-modify-write is serialized under a lock so concurrent
    # pins can't lost-update).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    for sid in ("ses_a", "ses_b", "ses_c"):
        _seed_session(sid, title=sid.upper())
        _make_show_page(sid)
        api.pin_dock_show_page(sid)

    dock = api.get_dock()["dock"]
    assert dock["order"] == [*BUILTIN_DOCK_IDS, _show("ses_a"), _show("ses_b"), _show("ses_c")]
    assert [pin["session_id"] for pin in dock["pins"]] == ["ses_a", "ses_b", "ses_c"]


def test_pin_rejects_when_install_budget_is_full(monkeypatch, tmp_path):
    # The install (pins) budget the reconcile clamp enforces must also gate new
    # pins, else a pin can grow ``pins`` past what a read would keep and the
    # just-added page is silently dropped on the next load.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    monkeypatch.setattr("core.dock_store.MAX_PINNED_PAGES", 1)  # room for exactly one pin
    for sid in ("ses_full1", "ses_full2"):
        _seed_session(sid)
        _make_show_page(sid)

    api.pin_dock_show_page("ses_full1")  # fills the install budget (1 pin)
    with pytest.raises(DockError) as excinfo:
        api.pin_dock_show_page("ses_full2")
    assert excinfo.value.code == "dock_full"


def test_load_dock_clamps_oversized_corrupt_pins(monkeypatch, tmp_path):
    # A corrupt / hand-edited stored doc with more pins than the budget must be
    # bounded on read, not rendered as thousands of tiles. The order is honored as
    # stored (empty here) — reconcile no longer force-appends built-ins.
    from core.chat_discovery import set_state_meta
    from core.dock_store import DOCK_STATE_KEY

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    monkeypatch.setattr("core.dock_store.MAX_PINNED_PAGES", 1)  # room for exactly one pin
    set_state_meta(
        DOCK_STATE_KEY,
        {"order": [], "pins": [{"session_id": f"ses_{i}", "title_snapshot": "", "pinned_at": ""} for i in range(10)]},
    )

    dock = api.get_dock()["dock"]
    assert len(dock["pins"]) == 1  # clamped to the install budget
    assert dock["order"] == []  # stored empty order is honored; nothing force-added


def test_pin_budget_survives_new_builtin():
    # Adding library (the 4th built-in) must not shrink the install budget: the
    # budget is a fixed constant, so a valid pre-Phase-2 dock (up to 197 pins)
    # never loses a pin on reconcile when a built-in is added.
    from core.dock_store import BUILTIN_DOCK_IDS, MAX_DOCK_ITEMS, MAX_PINNED_PAGES

    assert MAX_PINNED_PAGES == 197
    assert MAX_DOCK_ITEMS - len(BUILTIN_DOCK_IDS) == MAX_PINNED_PAGES


def test_reconcile_does_not_readd_undocked_builtin(monkeypatch, tmp_path):
    # Two-layer model (§7.1c): built-ins are undockable, so reconcile must NOT
    # force-append a built-in that a stored doc omits from ``order`` (reverses the
    # pre-Phase-2.1 append rule). Existing tiles keep their order and the pin
    # survives; ``library`` stays undocked (installed, but not in the Dock).
    from core.chat_discovery import set_state_meta
    from core.dock_store import DOCK_STATE_KEY

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    set_state_meta(
        DOCK_STATE_KEY,
        {
            "order": ["files", "terminal", "editor", _show("ses_legacy")],
            "pins": [{"session_id": "ses_legacy", "title_snapshot": "Legacy", "pinned_at": "2026-01-01T00:00:00+00:00"}],
        },
    )

    dock = api.get_dock()["dock"]

    assert dock["order"] == ["files", "terminal", "editor", _show("ses_legacy")]
    assert "library" not in dock["order"]  # NOT force-appended
    assert [pin["session_id"] for pin in dock["pins"]] == ["ses_legacy"]


def test_legacy_full_order_doc_still_valid(monkeypatch, tmp_path):
    # A doc persisted with every built-in already in ``order`` (the common
    # post-#899 shape) remains valid and unchanged on read.
    from core.chat_discovery import set_state_meta
    from core.dock_store import DOCK_STATE_KEY

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    order = ["files", "terminal", "editor", "library", _show("ses_keep")]
    set_state_meta(
        DOCK_STATE_KEY,
        {
            "order": order,
            "pins": [{"session_id": "ses_keep", "title_snapshot": "Keep", "pinned_at": "2026-01-01T00:00:00+00:00"}],
        },
    )

    dock = api.get_dock()["dock"]
    assert dock["order"] == order


def test_pin_unknown_show_page_is_404(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _seed_session("ses_nopage")  # session exists but has no Show Page

    with pytest.raises(DockError) as excinfo:
        api.pin_dock_show_page("ses_nopage")
    assert excinfo.value.code == "show_page_not_found"


def test_pin_malformed_session_id_raises_show_page_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    with pytest.raises(ShowPageError) as excinfo:
        api.pin_dock_show_page("bad id!")
    assert excinfo.value.code == "invalid_session_id"


def test_unpin_removes_and_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _seed_session("ses_u", title="Bye")
    _make_show_page("ses_u")
    api.pin_dock_show_page("ses_u")

    dock = api.unpin_dock_show_page("ses_u")["dock"]
    assert dock["order"] == list(BUILTIN_DOCK_IDS)
    assert dock["pins"] == []

    # Unpinning again is a harmless no-op (never 404).
    again = api.unpin_dock_show_page("ses_u")["dock"]
    assert again == dock

    # Unpinning something never pinned is also a no-op.
    assert api.unpin_dock_show_page("ses_never")["dock"]["pins"] == []


def test_set_order_persists_valid_permutation(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _seed_session("ses_o", title="Ordered")
    _make_show_page("ses_o")
    api.pin_dock_show_page("ses_o")

    new_order = [_show("ses_o"), "library", "editor", "files", "terminal"]
    dock = api.set_dock_order(new_order)["dock"]

    assert dock["order"] == new_order
    assert api.get_dock()["dock"]["order"] == new_order


def test_set_order_accepts_library_builtin(monkeypatch, tmp_path):
    # ``library`` is a built-in Dock tile (Phase 2), so a full-set reorder that
    # includes it is accepted and persists — the same reorder path #892 shipped,
    # now covering the 4th built-in.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _seed_session("ses_lib", title="Ordered")
    _make_show_page("ses_lib")
    api.pin_dock_show_page("ses_lib")

    new_order = [_show("ses_lib"), "library", "editor", "terminal", "files"]
    dock = api.set_dock_order(new_order)["dock"]

    assert dock["order"] == new_order
    assert api.get_dock()["dock"]["order"] == new_order


def test_set_order_rejects_unknown_id(monkeypatch, tmp_path):
    # Subset validation (§7.1c) still rejects an id that is not a real dock item,
    # so a stale client can't resurrect a pin removed by another tab.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    with pytest.raises(DockError) as excinfo:
        api.set_dock_order(["files", "terminal", "editor", "show:ghost"])
    assert excinfo.value.code == "invalid_order"


def test_set_order_accepts_proper_subset(monkeypatch, tmp_path):
    # Two-layer model: the order is a SUBSET of the known ids, so omitting a
    # built-in (undocking it) is accepted and persists — no longer rejected as a
    # non-set-equal order.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    dock = api.set_dock_order(["files", "terminal"])["dock"]  # editor + library undocked
    assert dock["order"] == ["files", "terminal"]
    assert api.get_dock()["dock"]["order"] == ["files", "terminal"]


def test_set_order_accepts_empty_dock(monkeypatch, tmp_path):
    # Undocking everything (including all built-ins) is legal — the empty Dock is
    # a valid saved state (the popover shows the App Library hint client-side).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    dock = api.set_dock_order([])["dock"]
    assert dock["order"] == []
    assert api.get_dock()["dock"]["order"] == []


def test_undocked_builtin_persists_across_reload(monkeypatch, tmp_path):
    # An undocked built-in stays undocked across a reload — reconcile never
    # re-adds it (the whole point of built-ins being undockable now).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    api.set_dock_order(["files", "terminal", "editor"])  # library undocked
    assert "library" not in api.get_dock()["dock"]["order"]


def test_installed_page_can_be_undocked_but_stays_installed(monkeypatch, tmp_path):
    # The core two-layer invariant: a Show Page can be undocked (removed from
    # ``order``) while REMAINING installed (kept in ``pins``) — install and dock
    # are separate. Its ``show:`` id is absent from the order but the pin persists.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _seed_session("ses_keep", title="Keep")
    _make_show_page("ses_keep")
    api.pin_dock_show_page("ses_keep")  # installs + docks

    # Undock it (drop only its show id) while keeping every built-in docked.
    api.set_dock_order(["files", "terminal", "editor", "library"])

    dock = api.get_dock()["dock"]
    assert _show("ses_keep") not in dock["order"]  # undocked
    assert [pin["session_id"] for pin in dock["pins"]] == ["ses_keep"]  # still installed


def test_delete_pin_cascades_out_of_order(monkeypatch, tmp_path):
    # DELETE pins (uninstall) removes the page from BOTH pins and order, leaving
    # the other docked tiles intact.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _seed_session("ses_del", title="Del")
    _make_show_page("ses_del")
    api.pin_dock_show_page("ses_del")
    assert _show("ses_del") in api.get_dock()["dock"]["order"]

    dock = api.unpin_dock_show_page("ses_del")["dock"]
    assert _show("ses_del") not in dock["order"]
    assert dock["pins"] == []
    assert dock["order"] == list(BUILTIN_DOCK_IDS)  # built-ins untouched


def test_set_order_rejects_duplicates(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    with pytest.raises(DockError) as excinfo:
        api.set_dock_order(["files", "files", "terminal", "editor"])
    assert excinfo.value.code == "invalid_order"


def test_set_order_rejects_non_list(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    with pytest.raises(DockError) as excinfo:
        api.set_dock_order("files,terminal,editor")
    assert excinfo.value.code == "invalid_order"


def test_dock_route_round_trip_via_client(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().get("/api/dock", base_url="http://127.0.0.1:5123")

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["dock"]["order"] == list(BUILTIN_DOCK_IDS)


def test_dock_route_blocked_for_remote_without_session(monkeypatch, tmp_path):
    """Auth parity: the Dock routes inherit ``enforce_remote_access_cookie`` — a
    remote request without a session is bounced to the OAuth login, never served
    as an unauthenticated native endpoint."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    get_resp = app.test_client().get(
        "/api/dock",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )
    assert get_resp.status_code == 302
    assert get_resp.headers["Location"].startswith("https://backend.test/oauth/authorize?")

    # The mutating routes are gated by the same before-request hook.
    post_resp = app.test_client().post(
        "/api/dock/pins",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        json={"session_id": "ses_x"},
        follow_redirects=False,
    )
    assert post_resp.status_code != 200
