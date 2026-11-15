"""Metadata-only NEW request collections; ordinary count semantics are retained."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import event, select

from storage import vault_service
from storage.models import vault_audit, vault_requests
from tests.test_vault_bitcoin_ecdsa_context import _case, _maximal_legacy_case
from tests.test_vault_protected_bitcoin import _create, _request
from tests.test_vault_protected_bitcoin import capable_cohort as capable_cohort
from vibe import api

NULL_FIELDS = {"secret_name", "requester", "delivery", "message_id", "card", "session"}
SUMMARY_FIELDS = NULL_FIELDS | {"id", "request_type", "status", "created_at", "decided_at", "expires_at"}


def _compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _catalog(count=104):
    request, _ = _request("p2wpkh")
    with api._vault_engine().begin() as conn:
        original = dict(conn.execute(select(vault_requests).where(vault_requests.c.id == request["id"])).mappings().one())
        # SQL metadata fixtures, not extra valid signed releases or custody claims.
        for index in range(count - 1):
            row = {**original, "id": f"vrq_{index:012x}"}
            conn.execute(vault_requests.insert().values(**row))
        expected = [row[0] for row in conn.execute(select(vault_requests.c.id).order_by(vault_requests.c.created_at.desc(), vault_requests.c.id.desc()))]
    return request, expected


@pytest.mark.parametrize("limit", [0, -1, -2, 1, 100, 101])
@pytest.mark.parametrize("session", [None, "original-test-session"])
def test_sql_limit_and_post_filter_negative_slicing_remain_distinct(capable_cohort, limit, session):
    _create()
    _, expected = _catalog()
    with api._vault_engine().begin() as conn:
        rows = vault_service.list_requests(conn, limit=limit, session=session)
    if session is not None:
        expected = expected[:limit]
    elif limit >= 0:
        expected = expected[:limit]
    assert [row["id"] for row in rows] == expected
    for row in rows:
        assert set(row) == SUMMARY_FIELDS and row["request_type"] == "sign"
        assert all(row[field] is None for field in NULL_FIELDS)
        assert len(_compact(row)) <= 1024
    comparison = _compact({"ok": True, "requests": rows})
    wrapper = len(_compact({"ok": True, "requests": []})) + max(0, len(rows) - 1)
    assert len(comparison) == sum(len(_compact(row)) for row in rows) + wrapper
    if len(rows) <= 100:
        assert len(comparison) <= 131_072 and wrapper <= 4096


def test_multiple_maximum_proofs_and_expiry_never_select_decode_or_hydrate_new_bodies(capable_cohort, monkeypatch):
    _create()
    context = _maximal_legacy_case()
    engine = api._vault_engine()
    with engine.begin() as conn:
        first = vault_service.create_sign_request(conn, "PUBLIC_TEST_BTC", digest=context["digest"], scheme="ecdsa-secp256k1-der", signing_context=context)
        second = vault_service.create_sign_request(conn, "PUBLIC_TEST_BTC", digest=context["digest"], scheme="ecdsa-secp256k1-der", signing_context=context)
        conn.execute(vault_requests.update().where(vault_requests.c.id == first["id"]).values(expires_at="2020-01-01T00:00:00+00:00"))
    original_loads = vault_service._loads
    statements = []

    def forbid_new_blob(value):
        if isinstance(value, str) and '"parentTransaction"' in value:
            pytest.fail("list/expiry decoded a NEW proof body")
        return original_loads(value)

    def forbid_hydration(*args, **kwargs):
        pytest.fail("NEW summary requested full-card hydration")

    def trace(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    monkeypatch.setattr(vault_service, "_loads", forbid_new_blob)
    monkeypatch.setattr(vault_service, "_request_row_payload", forbid_hydration)
    event.listen(engine, "before_cursor_execute", trace)
    try:
        with engine.begin() as conn:
            rows = vault_service.list_requests(conn, status=None)
            assert len(rows) == 2
            by_id = {row["id"]: row for row in rows}
            assert by_id[first["id"]]["status"] == "expired" and by_id[second["id"]]["status"] == "pending"
            audit = conn.execute(select(vault_audit).where(vault_audit.c.event == "request-expired")).mappings().one()
            assert audit["request_id"] == first["id"]
            assert "parentTransaction" not in audit["delivery"]
    finally:
        event.remove(engine, "before_cursor_execute", trace)
    # No bare vault_requests full-row SELECT at any expiry/audit/list layer.
    request_selects = [sql for sql in statements if sql.lstrip().startswith("SELECT") and "FROM vault_requests" in sql]
    assert request_selects
    for sql in request_selects:
        assert "vault_requests.delivery" not in sql or "CASE WHEN" in sql
        assert "SELECT vault_requests.id, vault_requests.request_type, vault_requests.secret_name, vault_requests.requester, vault_requests.delivery" not in sql
    assert len(_compact({"ok": True, "requests": rows})) < 2048


@pytest.mark.parametrize(("requester", "delivery", "card", "expected"), [
    ("r", "d", "c", "r"), (None, "d", "c", "d"), ("", None, "c", "c"),
    (True, "d", "c", "True"), (False, "d", "c", "d"), (0, None, "c", "c"),
    ([], {}, "c", "c"), ([1], "d", "c", "[1]"), ({"scope": "é"}, "d", "c", "{'scope': 'é'}"),
])
def test_metadata_session_precedence_matches_existing_truthiness_and_stringification(capable_cohort, requester, delivery, card, expected):
    _create()
    request, _ = _request("p2wpkh")
    with api._vault_engine().begin() as conn:
        raw = dict(conn.execute(select(vault_requests).where(vault_requests.c.id == request["id"])).mappings().one())
        stored_requester, stored_delivery = json.loads(raw["requester"]), json.loads(raw["delivery"])
        stored_requester["session_id"] = requester
        stored_delivery["session_id"] = delivery
        stored_delivery["card"]["session_id"] = card
        raw.update(requester=json.dumps(stored_requester), delivery=json.dumps(stored_delivery))
        assert vault_service._request_session_id(raw) == expected
        conn.execute(vault_requests.update().where(vault_requests.c.id == request["id"]).values(requester=raw["requester"], delivery=raw["delivery"]))
        assert len(vault_service.list_requests(conn, session=expected)) == 1
        assert vault_service.list_requests(conn, session="nonmatching") == []


def test_new_summary_and_old_kind_full_contribution_are_not_a_universal_128k_cap(capable_cohort):
    _create()
    request, _ = _request("p2wpkh")
    with api._vault_engine().begin() as conn:
        old_context = {"kind": "avault-agent-operation", "digest": "11" * 32}
        old = vault_service.create_sign_request(
            conn, "PUBLIC_TEST_BTC", digest="11" * 32, scheme="ecdsa-secp256k1-der", signing_context=old_context,
            delivery={"command": "old-kind-large-metadata" * 8000},
        )
        rows = vault_service.list_requests(conn)
    by_id = {row["id"]: row for row in rows}
    assert by_id[request["id"]]["card"] is None
    assert by_id[old["id"]]["card"]["command"] == "old-kind-large-metadata" * 8000
    assert len(_compact({"ok": True, "requests": rows})) > 131_072


def test_actual_list_http_writer_subtree_and_known_id_full_get(capable_cohort):
    from vibe.ui_server import app

    _create()
    request, _ = _request("p2wpkh")
    client = app.test_client()
    response = client.get("/api/vault/requests")
    assert response.status_code == 200
    payload = response.get_json()
    row = next(row for row in payload["requests"] if row["id"] == request["id"])
    assert set(row) == SUMMARY_FIELDS
    actual = response.content
    if isinstance(actual, str):
        actual = actual.encode()
    assert b'"parentTransaction"' not in actual and b'"signing_context"' not in actual
    assert len(actual) <= 131_072
    assert _compact(row) in actual and len(_compact(row)) <= 1024
    selected = client.get(f"/api/vault/requests/{request['id']}")
    assert selected.status_code == 200
    assert selected.get_json()["request"]["id"] == request["id"]
    assert selected.get_json()["request"]["delivery"]["signing_context"] == _case("p2wpkh")


def test_expiry_checks_each_row_at_its_transition_even_when_list_limit_is_zero(capable_cohort, monkeypatch):
    from datetime import datetime, timedelta, timezone

    _create()
    first, _ = _request("p2wpkh")
    second, _ = _request("p2wpkh")
    start = datetime(2026, 11, 14, tzinfo=timezone.utc)
    with api._vault_engine().begin() as conn:
        conn.execute(vault_requests.update().where(vault_requests.c.id.in_([first["id"], second["id"]])).values(
            expires_at=(start + timedelta(seconds=1)).isoformat(),
        ))
    ticks = iter([start, start + timedelta(seconds=2)])

    class AdvancingClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(ticks, start + timedelta(seconds=2))

    monkeypatch.setattr(vault_service, "datetime", AdvancingClock)
    with api._vault_engine().begin() as conn:
        assert vault_service.list_requests(conn, limit=0) == []
        statuses = [row[0] for row in conn.execute(select(vault_requests.c.status))]
        assert sorted(statuses) == ["expired", "pending"]


def test_viewer_acl_and_session_filter_never_materialize_new_request_bodies(capable_cohort, monkeypatch):
    from storage import resource_access_service

    _create()
    context = _maximal_legacy_case()
    with api._vault_engine().begin() as conn:
        request = vault_service.create_sign_request(conn, "PUBLIC_TEST_BTC", digest=context["digest"], scheme="ecdsa-secp256k1-der",
                                                    signing_context=context, requester={"session_id": "wanted"})
        original = dict(conn.execute(select(vault_requests).where(vault_requests.c.id == request["id"])).mappings().one())
        # Newer other-session rows must not consume the original post-filter LIMIT.
        for index in range(3):
            conn.execute(vault_requests.insert().values(**{
                **original, "id": f"vrq_other_{index}", "created_at": "2099-01-01T00:00:00+00:00",
                "requester": json.dumps({"session_id": "other"}),
            }))
    original_loads = vault_service._loads

    def no_blob(value):
        assert not (isinstance(value, str) and '"parentTransaction"' in value), "ACL/session loaded a proof body"
        return original_loads(value)

    monkeypatch.setattr(vault_service, "_loads", no_blob)
    monkeypatch.setattr(vault_service, "_request_row_payload", lambda *a, **k: pytest.fail("summary hydrated a full card"))
    viewer = resource_access_service.ResourceUserContext(subject="public-test-viewer", instance_role="viewer", is_remote=True)
    with api._vault_engine().begin() as conn:
        assert [row["id"] for row in vault_service.list_requests(conn, session="wanted", limit=1)] == [request["id"]]
        assert vault_service.list_requests(conn, session="wanted", limit=-1) == []  # Existing Python slicing.
        assert vault_service.list_requests(conn, user_context=viewer) == []
        assert vault_service.list_requests(conn, session="wanted", user_context=viewer) == []


def test_B16_real_http_hundred_summaries_with_two_maximum_parents_and_outside_focus(capable_cohort, monkeypatch):
    from vibe.ui_server import app

    _create()
    native, legacy = _case("p2wpkh"), _maximal_legacy_case()
    requests = []
    with api._vault_engine().begin() as conn:
        for index in range(101):
            context = legacy if index >= 99 else native
            requests.append(vault_service.create_sign_request(
                conn, "PUBLIC_TEST_BTC", digest=context["digest"], scheme="ecdsa-secp256k1-der", signing_context=context,
                requester={"session_id": "public-http-session"},
            )["id"])
    client = app.test_client()
    original_loads = vault_service._loads

    def no_body(value):
        assert not (isinstance(value, str) and '"unsignedTransaction"' in value), "HTTP list materialized NEW body"
        return original_loads(value)

    with monkeypatch.context() as patch:
        patch.setattr(vault_service, "_loads", no_body)
        patch.setattr(vault_service, "_request_row_payload", lambda *a, **k: pytest.fail("HTTP list hydrated NEW card"))
        for path in ("/api/vault/requests", "/api/vault/requests?session=public-http-session"):
            response = client.get(path)
            assert response.status_code == 200
            rows = response.get_json()["requests"]
            assert len(rows) == 100 and requests[0] not in {row["id"] for row in rows}
            assert {requests[99], requests[100]} <= {row["id"] for row in rows}
            assert all(set(row) == SUMMARY_FIELDS and len(_compact(row)) <= 1024 for row in rows)
            assert len(response.content) <= 131_072
            assert b"parentTransaction" not in response.content and b"unsignedTransaction" not in response.content
    # Backend direct GET is not a first-page membership operation. Web's shared
    # uncached full-Q loader and stale-response controls remain its own gate.
    full = client.get(f"/api/vault/requests/{requests[0]}")
    assert full.status_code == 200 and full.get_json()["request"]["id"] == requests[0]
    assert full.get_json()["request"]["delivery"]["signing_context"] == native
